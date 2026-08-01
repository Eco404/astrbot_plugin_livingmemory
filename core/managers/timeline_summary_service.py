"""Single production path for creating Timeline memories from conversations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

from ..utils import OperationContext
from .timeline_topic_continuation import (
    TimelineTopicContinuationEvaluator,
    build_dialogue_units,
)

if TYPE_CHECKING:
    from ..base.config_manager import ConfigManager
    from ..processors.memory_processor import MemoryProcessor
    from .conversation_manager import ConversationManager
    from .memory_engine import MemoryEngine


@dataclass(slots=True)
class TimelineSummaryResult:
    status: str
    session_id: str
    start_index: int = 0
    end_index: int = 0
    message_count: int = 0
    importance: float | None = None
    topics: list[str] | None = None
    decision_reason: str | None = None
    error: str | None = None


class TimelineSummaryService:
    """Coordinate threshold, idle, and manual summaries with one session lock."""

    MAX_AUTOMATIC_RETRIES = 3

    def __init__(
        self,
        *,
        config_manager: "ConfigManager",
        conversation_manager: "ConversationManager",
        memory_engine: "MemoryEngine",
        memory_processor: "MemoryProcessor",
        embedding_provider_resolver: Callable[[], Any] | None = None,
    ) -> None:
        self.config_manager = config_manager
        self.conversation_manager = conversation_manager
        self.memory_engine = memory_engine
        self.memory_processor = memory_processor
        self._continuation_evaluator = TimelineTopicContinuationEvaluator(
            conversation_manager.store,
            embedding_provider_resolver or (lambda: None),
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._closing = False

    def schedule_if_needed(
        self,
        session_id: str,
        *,
        persona_id: str | None,
        trigger_type: str,
        min_rounds: int,
    ) -> bool:
        """Schedule without creating duplicate work for the same session."""
        if self._closing:
            return False
        existing = self._tasks.get(session_id)
        if existing and not existing.done():
            return False
        task = asyncio.create_task(
            self.summarize_if_needed(
                session_id,
                persona_id=persona_id,
                trigger_type=trigger_type,
                min_rounds=min_rounds,
            ),
            name=f"livingmemory-timeline-summary-{session_id[-24:]}",
        )
        self._tasks[session_id] = task
        task.add_done_callback(
            lambda finished, sid=session_id: self._on_task_done(sid, finished)
        )
        return True

    async def summarize_if_needed(
        self,
        session_id: str,
        *,
        persona_id: str | None,
        trigger_type: str,
        min_rounds: int,
        force: bool = False,
    ) -> TimelineSummaryResult:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked() and not force:
            return TimelineSummaryResult("busy", session_id)
        async with lock:
            return await self._summarize_locked(
                session_id,
                persona_id=persona_id,
                trigger_type=trigger_type,
                min_rounds=max(1, int(min_rounds)),
                force=force,
            )

    async def _summarize_locked(
        self,
        session_id: str,
        *,
        persona_id: str | None,
        trigger_type: str,
        min_rounds: int,
        force: bool,
    ) -> TimelineSummaryResult:
        store = self.conversation_manager.store
        total_messages = await store.get_message_count(session_id)
        raw_last = await self.conversation_manager.get_session_metadata(
            session_id, "last_summarized_index", 0
        )
        try:
            last_summarized = max(0, int(raw_last))
        except (TypeError, ValueError):
            last_summarized = 0
        if last_summarized > total_messages:
            last_summarized = total_messages
            await self.conversation_manager.update_session_metadata(
                session_id, "last_summarized_index", total_messages
            )

        pending = await self.conversation_manager.get_session_metadata(
            session_id, "pending_summary", None
        )
        pending = pending if isinstance(pending, dict) else {}
        start_index = max(0, int(pending.get("start_index", last_summarized)))
        pending_end_value = pending.get("end_index")
        has_pending_range = pending_end_value is not None and bool(pending)
        pending_end = int(pending_end_value or total_messages)
        end_index = (
            min(total_messages, max(start_index, pending_end))
            if has_pending_range
            else total_messages
        )
        retry_count = max(0, int(pending.get("retry_count", 0)))
        next_retry_at = float(pending.get("next_retry_at", 0.0) or 0.0)
        blocked = bool(pending.get("blocked", False))
        if not force and (blocked or next_retry_at > time.time()):
            return TimelineSummaryResult(
                "deferred",
                session_id,
                start_index=start_index,
                end_index=end_index,
                message_count=max(0, end_index - start_index),
                error=str(pending.get("error") or ""),
            )

        unsummarized = max(0, end_index - start_index)
        if unsummarized < 2:
            return TimelineSummaryResult(
                "insufficient",
                session_id,
                start_index=start_index,
                end_index=end_index,
                message_count=unsummarized,
            )

        history = await self.conversation_manager.get_messages_range(
            session_id=session_id,
            start_index=start_index,
            end_index=end_index,
        )
        if len(history) < 2:
            return TimelineSummaryResult(
                "insufficient",
                session_id,
                start_index=start_index,
                end_index=end_index,
                message_count=len(history),
            )

        units = build_dialogue_units(history)
        if not force and len(units) < min_rounds:
            return TimelineSummaryResult(
                "insufficient",
                session_id,
                start_index=start_index,
                end_index=end_index,
                message_count=len(history),
            )

        boundary_diagnostics: dict[str, Any] = {}
        continuation_enabled = bool(
            self.config_manager.get(
                "reflection_engine.topic_continuation_enabled", True
            )
        )
        if (
            not force
            and not has_pending_range
            and trigger_type == "round_limit"
            and continuation_enabled
        ):
            base_rounds = max(1, int(min_rounds))
            force_rounds = max(
                base_rounds + 1,
                int(
                    self.config_manager.get(
                        "reflection_engine.topic_continuation_force_summary_rounds",
                        base_rounds * 2,
                    )
                ),
            )
            decision = await self._continuation_evaluator.evaluate(
                history,
                base_rounds=base_rounds,
                force_rounds=force_rounds,
            )
            boundary_diagnostics = {
                "boundary_reason": decision.reason,
                "dialogue_unit_count": decision.unit_count,
                "base_rounds": base_rounds,
                "force_summary_rounds": force_rounds,
                "provisional_topic_count": decision.provisional_topic_count,
                "max_topic_similarity": decision.max_similarity,
            }
            if decision.action == "continue":
                await self.conversation_manager.update_session_metadata(
                    session_id,
                    "topic_continuation_state",
                    {
                        "status": "continuing",
                        **boundary_diagnostics,
                        "updated_at": time.time(),
                    },
                )
                return TimelineSummaryResult(
                    "continuing",
                    session_id,
                    start_index=start_index,
                    end_index=end_index,
                    message_count=len(history),
                    decision_reason=decision.reason,
                )
            if 0 < decision.summary_end_offset < len(history):
                end_index = start_index + decision.summary_end_offset
                history = history[: decision.summary_end_offset]
                units = build_dialogue_units(history)

        effective_persona = str(
            persona_id
            or await self.conversation_manager.get_session_metadata(
                session_id, "last_persona_id", ""
            )
            or "default"
        )
        job = await store.start_summary_job(
            session_id,
            trigger_type=trigger_type,
            start_index=start_index,
            end_index=end_index,
            persona_id=effective_persona,
            retry_count=retry_count,
        )
        is_group = bool(history[0].group_id) or "GroupMessage" in session_id
        try:
            async with OperationContext("Timeline 记忆总结", session_id):
                content, metadata, importance = (
                    await self.memory_processor.process_conversation(
                        messages=history,
                        is_group_chat=is_group,
                        persona_id=effective_persona,
                        allow_no_memory=True,
                    )
                )
                metadata["source_window"] = {
                    "session_id": session_id,
                    "start_index": start_index,
                    "end_index": end_index,
                    "message_count": end_index - start_index,
                    "first_message_id": history[0].id,
                    "last_message_id": history[-1].id,
                    "started_at": history[0].timestamp,
                    "ended_at": history[-1].timestamp,
                    "triggered_by": trigger_type,
                    **boundary_diagnostics,
                }
                if (
                    metadata.get("memory_decision") == "no_memory"
                    and metadata.get("summary_quality") in {"normal", "repaired"}
                    and not str(content).strip()
                ):
                    reason = str(metadata.get("no_memory_reason") or "")
                    await store.complete_no_memory_summary(
                        session_id,
                        job_uid=str(job["job_uid"]),
                        trigger_type=trigger_type,
                        start_index=start_index,
                        end_index=end_index,
                        first_message_id=history[0].id,
                        last_message_id=history[-1].id,
                        persona_id=effective_persona,
                        reason=reason,
                        importance=float(importance),
                        message_coverage=[
                            dict(row)
                            for row in metadata.get("message_coverage", [])
                            if isinstance(row, dict)
                        ],
                        metadata=metadata,
                    )
                    await self.conversation_manager.update_session_metadata(
                        session_id, "topic_continuation_state", None
                    )
                    await self._clear_pending_features(history)
                    logger.info(
                        "[%s] Timeline 窗口已检查且无需写入记忆: "
                        "trigger=%s range=[%s:%s] reason=%s",
                        session_id,
                        trigger_type,
                        start_index,
                        end_index,
                        reason,
                    )
                    return TimelineSummaryResult(
                        "no_memory",
                        session_id,
                        start_index=start_index,
                        end_index=end_index,
                        message_count=len(history),
                        importance=float(importance),
                        topics=[],
                        decision_reason=reason,
                    )

                atoms = self.memory_processor.classify_atoms_from_metadata(
                    metadata=metadata,
                    parent_importance=importance,
                    session_id=session_id,
                    persona_id=effective_persona,
                )
                retention_threshold = float(
                    self.config_manager.get(
                        "reflection_engine.source_retention_importance_threshold",
                        0.8,
                    )
                )
                normalized_importance = float(importance)
                if normalized_importance > 1.0:
                    normalized_importance /= 10.0
                retain_source = normalized_importance >= retention_threshold
                await self.memory_engine.add_memory(
                    content=content,
                    session_id=session_id,
                    persona_id=effective_persona,
                    importance=importance,
                    metadata=metadata,
                    atoms=atoms,
                    source_messages=history if retain_source else None,
                    source_retention_reason="importance_threshold",
                )
                await self.conversation_manager.update_session_metadata_values(
                    session_id,
                    {
                        "last_summarized_index": end_index,
                        "pending_summary": None,
                        "last_summary_trigger": trigger_type,
                        "last_summary_at": time.time(),
                        "last_persona_id": effective_persona,
                        "last_summary_decision": "store",
                        "last_no_memory_reason": None,
                        "topic_continuation_state": None,
                    },
                )
                await store.finish_summary_job(session_id)
                await self._clear_pending_features(history)
                logger.info(
                    "[%s] Timeline 总结完成: trigger=%s range=[%s:%s]",
                    session_id,
                    trigger_type,
                    start_index,
                    end_index,
                )
                return TimelineSummaryResult(
                    "created",
                    session_id,
                    start_index=start_index,
                    end_index=end_index,
                    message_count=len(history),
                    importance=float(importance),
                    topics=list(metadata.get("topics", [])),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            new_retry_count = retry_count + 1
            blocked = new_retry_count >= self.MAX_AUTOMATIC_RETRIES
            next_retry = None if blocked else time.time() + min(
                3600.0, 60.0 * (2 ** max(0, new_retry_count - 1))
            )
            pending_value = {
                "start_index": start_index,
                "end_index": end_index,
                "retry_count": new_retry_count,
                "next_retry_at": next_retry,
                "blocked": blocked,
                "error": str(exc)[:1000],
            }
            await self.conversation_manager.update_session_metadata(
                session_id, "pending_summary", pending_value
            )
            await store.fail_summary_job(
                session_id,
                retry_count=new_retry_count,
                next_retry_at=next_retry,
                error=str(exc),
            )
            logger.error(
                "[%s] Timeline 总结失败，已保留断点（%s/%s）: %s",
                session_id,
                new_retry_count,
                self.MAX_AUTOMATIC_RETRIES,
                exc,
                exc_info=True,
            )
            return TimelineSummaryResult(
                "failed",
                session_id,
                start_index=start_index,
                end_index=end_index,
                message_count=len(history),
                error=str(exc),
            )

    async def _clear_pending_features(self, history: list[Any]) -> None:
        try:
            await self.conversation_manager.store.delete_pending_message_features(
                message_ids=[int(item.id or 0) for item in history]
            )
        except Exception as exc:
            logger.warning("清理已总结消息的短期查询特征失败: %s", exc)

    def _on_task_done(self, session_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(session_id) is task:
            self._tasks.pop(session_id, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.error("[%s] Timeline 总结后台任务异常", session_id, exc_info=True)

    async def shutdown(self) -> None:
        self._closing = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


__all__ = ["TimelineSummaryResult", "TimelineSummaryService"]
