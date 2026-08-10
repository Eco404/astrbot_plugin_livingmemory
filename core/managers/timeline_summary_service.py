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
    MAX_BACKLOG_WINDOWS_PER_RUN = 64
    MAX_INSUFFICIENT_SIGNATURES = 2048

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
        self._insufficient_signatures: dict[str, tuple[int, int, int, int]] = {}
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
            processed_messages = 0
            created_memory = False
            combined_topics: list[str] = []
            first_start: int | None = None
            result = TimelineSummaryResult("insufficient", session_id)
            for _ in range(self.MAX_BACKLOG_WINDOWS_PER_RUN):
                result = await self._summarize_locked(
                    session_id,
                    persona_id=persona_id,
                    trigger_type=trigger_type,
                    min_rounds=max(1, int(min_rounds)),
                    force=force,
                )
                if result.status not in {"created", "no_memory"}:
                    break
                created_memory = created_memory or result.status == "created"
                if first_start is None:
                    first_start = result.start_index
                processed_messages += result.message_count
                for topic in result.topics or []:
                    if topic not in combined_topics:
                        combined_topics.append(topic)
                pending = await self.conversation_manager.get_session_metadata(
                    session_id, "pending_summary", None
                )
                if not isinstance(pending, dict) or not pending:
                    break
                # Once the oldest window succeeds, drain every boundary that was
                # durably recorded while the provider was unavailable. The next
                # window no longer needs force: it is already an exact pending
                # range, and normal mode lets us reconstruct additional missed
                # boundaries instead of folding the entire tail into one window.
                force = False
            if processed_messages:
                if created_memory:
                    result.status = "created"
                result.start_index = (
                    first_start if first_start is not None else result.start_index
                )
                result.message_count = processed_messages
                result.topics = combined_topics
            return result

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
        queued_windows = self._normalize_queued_windows(pending, total_messages)
        start_index = max(0, int(pending.get("start_index", last_summarized)))
        pending_end_value = pending.get("end_index")
        has_pending_range = pending_end_value is not None and bool(pending)
        pending_end = int(pending_end_value or total_messages)
        end_index = (
            min(total_messages, max(start_index, pending_end))
            if has_pending_range
            else total_messages
        )
        new_boundary_recorded = False
        if has_pending_range:
            (
                pending,
                queued_windows,
                new_boundary_recorded,
            ) = await self._record_due_backlog_window(
                session_id=session_id,
                pending=pending,
                queued_windows=queued_windows,
                total_messages=total_messages,
                persona_id=persona_id,
                trigger_type=trigger_type,
                min_rounds=min_rounds,
                force=force,
            )
        retry_count = max(0, int(pending.get("retry_count", 0)))
        next_retry_at = float(pending.get("next_retry_at", 0.0) or 0.0)
        provider_signature = self._summary_provider_signature()
        previous_provider_signature = pending.get("provider_signature")
        provider_changed = (
            isinstance(previous_provider_signature, dict)
            and previous_provider_signature != provider_signature
        )
        if provider_changed:
            logger.info(
                "[%s] Timeline 总结 Provider 或模型已变化，立即重试最早失败窗口",
                session_id,
            )
        if (
            not force
            and not new_boundary_recorded
            and not provider_changed
            and next_retry_at > time.time()
        ):
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

        insufficient_signature = (
            total_messages,
            start_index,
            end_index,
            min_rounds,
        )
        if (
            not force
            and self._insufficient_signatures.get(session_id) == insufficient_signature
        ):
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
            self._remember_insufficient(session_id, insufficient_signature)
            return TimelineSummaryResult(
                "insufficient",
                session_id,
                start_index=start_index,
                end_index=end_index,
                message_count=len(history),
            )
        self._insufficient_signatures.pop(session_id, None)

        boundary_diagnostics = dict(pending.get("boundary_diagnostics") or {})
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
            pending.get("persona_id")
            or persona_id
            or await self.conversation_manager.get_session_metadata(
                session_id, "last_persona_id", ""
            )
            or "default"
        )
        effective_trigger = str(pending.get("trigger_type") or trigger_type)
        job = await store.start_summary_job(
            session_id,
            trigger_type=effective_trigger,
            start_index=start_index,
            end_index=end_index,
            persona_id=effective_persona,
            retry_count=retry_count,
        )
        is_group = bool(history[0].group_id) or "GroupMessage" in session_id
        try:
            async with OperationContext("Timeline 记忆总结", session_id):
                (
                    content,
                    metadata,
                    importance,
                ) = await self.memory_processor.process_conversation(
                    messages=history,
                    is_group_chat=is_group,
                    persona_id=effective_persona,
                    allow_no_memory=True,
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
                    "triggered_by": effective_trigger,
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
                        trigger_type=effective_trigger,
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
                    next_pending = self._next_pending_window(
                        queued_windows,
                        completed_end=end_index,
                    )
                    if next_pending is not None:
                        await self.conversation_manager.update_session_metadata(
                            session_id, "pending_summary", next_pending
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
                        "pending_summary": self._next_pending_window(
                            queued_windows,
                            completed_end=end_index,
                        ),
                        "last_summary_trigger": effective_trigger,
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
                    effective_trigger,
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
            # Reaching the per-cycle retry limit marks the window as degraded for
            # diagnostics, but never makes it permanently unreachable. A later
            # due boundary retries it immediately; otherwise the hourly cooldown
            # eventually retries it after the provider recovers.
            next_retry = time.time() + (
                3600.0
                if blocked
                else min(3600.0, 60.0 * (2 ** max(0, new_retry_count - 1)))
            )
            pending_value = {
                "start_index": start_index,
                "end_index": end_index,
                "retry_count": new_retry_count,
                "next_retry_at": next_retry,
                "blocked": blocked,
                "error": str(exc)[:1000],
                "trigger_type": effective_trigger,
                "persona_id": effective_persona,
                "boundary_diagnostics": boundary_diagnostics,
                "queued_windows": queued_windows,
                "provider_signature": provider_signature,
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

    def _summary_provider_signature(self) -> dict[str, str]:
        """Return retry-relevant LLM identity without persisting credentials."""
        configured_id = str(
            self.config_manager.get("provider_settings.llm_provider_id", "") or ""
        )
        provider = None
        resolver = getattr(
            type(self.memory_processor), "_get_current_llm_provider", None
        )
        if callable(resolver):
            try:
                provider = resolver(self.memory_processor)
            except Exception:
                provider = None
        raw_config = getattr(provider, "provider_config", {}) or {}
        if hasattr(raw_config, "get"):
            provider_id = str(
                raw_config.get("id") or raw_config.get("provider_id") or ""
            )
            model_id = str(
                raw_config.get("model") or raw_config.get("model_name") or ""
            )
        else:
            provider_id = ""
            model_id = ""
        if provider is not None and not model_id:
            get_model = getattr(provider, "get_model", None)
            if callable(get_model):
                try:
                    model_id = str(get_model() or "")
                except Exception:
                    model_id = ""
        return {
            "configured_provider_id": configured_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "runtime_class": (
                f"{type(provider).__module__}.{type(provider).__qualname__}"
                if provider is not None
                else ""
            ),
        }

    @staticmethod
    def _normalize_queued_windows(
        pending: dict[str, Any], total_messages: int
    ) -> list[dict[str, Any]]:
        active_end = max(0, int(pending.get("end_index", 0) or 0))
        cursor = active_end
        normalized: list[dict[str, Any]] = []
        for value in pending.get("queued_windows", []):
            if not isinstance(value, dict):
                continue
            start_index = max(cursor, int(value.get("start_index", cursor) or cursor))
            end_index = min(
                max(0, int(total_messages)),
                max(
                    start_index,
                    int(value.get("end_index", start_index) or start_index),
                ),
            )
            if end_index <= cursor:
                continue
            normalized.append(
                {
                    "start_index": cursor,
                    "end_index": end_index,
                    "trigger_type": str(value.get("trigger_type") or "round_limit"),
                    "persona_id": str(value.get("persona_id") or "default"),
                    "queued_at": float(
                        value.get("queued_at", time.time()) or time.time()
                    ),
                    "boundary_diagnostics": dict(
                        value.get("boundary_diagnostics") or {}
                    ),
                }
            )
            cursor = end_index
        return normalized

    async def _record_due_backlog_window(
        self,
        *,
        session_id: str,
        pending: dict[str, Any],
        queued_windows: list[dict[str, Any]],
        total_messages: int,
        persona_id: str | None,
        trigger_type: str,
        min_rounds: int,
        force: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        tail_index = max(
            [
                int(pending.get("end_index", 0) or 0),
                *(int(item["end_index"]) for item in queued_windows),
            ]
        )
        if total_messages <= tail_index:
            return pending, queued_windows, False
        history = await self.conversation_manager.get_messages_range(
            session_id=session_id,
            start_index=tail_index,
            end_index=total_messages,
        )
        if len(history) < 2:
            return pending, queued_windows, False
        boundary = await self._next_backlog_boundary(
            history,
            trigger_type=trigger_type,
            min_rounds=min_rounds,
            force=force,
        )
        if boundary is None:
            return pending, queued_windows, False
        end_offset, boundary_diagnostics = boundary
        window = {
            "start_index": tail_index,
            "end_index": tail_index + end_offset,
            "trigger_type": str(trigger_type),
            "persona_id": str(persona_id or "default"),
            "queued_at": time.time(),
            "boundary_diagnostics": boundary_diagnostics,
        }
        queued_windows = [*queued_windows, window]
        pending = {**pending, "queued_windows": queued_windows}
        await self.conversation_manager.update_session_metadata(
            session_id, "pending_summary", pending
        )
        logger.info(
            "[%s] Timeline 总结失败期间记录后续边界: range=[%s:%s] backlog=%s",
            session_id,
            tail_index,
            window["end_index"],
            len(queued_windows),
        )
        return pending, queued_windows, True

    async def _next_backlog_boundary(
        self,
        history: list[Any],
        *,
        trigger_type: str,
        min_rounds: int,
        force: bool,
    ) -> tuple[int, dict[str, Any]] | None:
        """Recover the first boundary that would have fired in this tail."""
        units = build_dialogue_units(history)
        if not units:
            return None
        if force:
            return len(history), {"boundary_reason": "forced_backlog_capture"}
        if len(units) < min_rounds:
            return None
        if trigger_type != "round_limit":
            return len(history), {"boundary_reason": f"{trigger_type}_backlog"}

        continuation_enabled = bool(
            self.config_manager.get(
                "reflection_engine.topic_continuation_enabled", True
            )
        )
        if not continuation_enabled:
            return units[min_rounds - 1].end_offset, {
                "boundary_reason": "round_limit",
                "dialogue_unit_count": min_rounds,
                "base_rounds": min_rounds,
            }

        force_rounds = max(
            min_rounds + 1,
            int(
                self.config_manager.get(
                    "reflection_engine.topic_continuation_force_summary_rounds",
                    min_rounds * 2,
                )
            ),
        )
        maximum = min(len(units), force_rounds)
        for unit_count in range(min_rounds, maximum + 1):
            prefix_end = units[unit_count - 1].end_offset
            decision = await self._continuation_evaluator.evaluate(
                history[:prefix_end],
                base_rounds=min_rounds,
                force_rounds=force_rounds,
            )
            diagnostics = {
                "boundary_reason": decision.reason,
                "dialogue_unit_count": decision.unit_count,
                "base_rounds": min_rounds,
                "force_summary_rounds": force_rounds,
                "provisional_topic_count": decision.provisional_topic_count,
                "max_topic_similarity": decision.max_similarity,
            }
            if decision.action != "summarize":
                continue
            if 0 < decision.summary_end_offset < prefix_end:
                return decision.summary_end_offset, diagnostics
            return prefix_end, diagnostics
        return None

    @staticmethod
    def _next_pending_window(
        queued_windows: list[dict[str, Any]], *, completed_end: int
    ) -> dict[str, Any] | None:
        remaining = [
            dict(item)
            for item in queued_windows
            if int(item.get("end_index", 0) or 0) > int(completed_end)
        ]
        if not remaining:
            return None
        next_window = remaining.pop(0)
        return {
            "start_index": int(completed_end),
            "end_index": int(next_window["end_index"]),
            "retry_count": 0,
            "next_retry_at": None,
            "blocked": False,
            "error": None,
            "trigger_type": str(next_window.get("trigger_type") or "round_limit"),
            "persona_id": str(next_window.get("persona_id") or "default"),
            "boundary_diagnostics": dict(
                next_window.get("boundary_diagnostics") or {}
            ),
            "queued_windows": remaining,
        }

    async def _clear_pending_features(self, history: list[Any]) -> None:
        try:
            await self.conversation_manager.store.delete_pending_message_features(
                message_ids=[int(item.id or 0) for item in history]
            )
        except Exception as exc:
            logger.warning("清理已总结消息的短期查询特征失败: %s", exc)

    def _remember_insufficient(
        self, session_id: str, signature: tuple[int, int, int, int]
    ) -> None:
        if (
            session_id not in self._insufficient_signatures
            and len(self._insufficient_signatures) >= self.MAX_INSUFFICIENT_SIGNATURES
        ):
            self._insufficient_signatures.pop(next(iter(self._insufficient_signatures)))
        self._insufficient_signatures[session_id] = signature

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
        self._insufficient_signatures.clear()


__all__ = ["TimelineSummaryResult", "TimelineSummaryService"]
