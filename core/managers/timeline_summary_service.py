"""Single production path for creating Timeline memories from conversations."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

from ..utils import OperationContext

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
    ) -> None:
        self.config_manager = config_manager
        self.conversation_manager = conversation_manager
        self.memory_engine = memory_engine
        self.memory_processor = memory_processor
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
        end_index = total_messages
        retry_count = max(0, int(pending.get("retry_count", 0)))
        pending_end = int(pending.get("end_index", end_index) or end_index)
        if end_index > pending_end:
            # New messages make this a new larger work unit rather than silently
            # abandoning the failed source range.
            retry_count = 0
        next_retry_at = float(pending.get("next_retry_at", 0.0) or 0.0)
        blocked = bool(pending.get("blocked", False)) and end_index <= pending_end
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
        if unsummarized < 2 or (not force and unsummarized // 2 < min_rounds):
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

        effective_persona = str(
            persona_id
            or await self.conversation_manager.get_session_metadata(
                session_id, "last_persona_id", ""
            )
            or "default"
        )
        await store.start_summary_job(
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
                    )
                )
                atoms = self.memory_processor.classify_atoms_from_metadata(
                    metadata=metadata,
                    parent_importance=importance,
                    session_id=session_id,
                    persona_id=effective_persona,
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
                }
                await self.memory_engine.add_memory(
                    content=content,
                    session_id=session_id,
                    persona_id=effective_persona,
                    importance=importance,
                    metadata=metadata,
                    atoms=atoms,
                )
                await self.conversation_manager.update_session_metadata_values(
                    session_id,
                    {
                        "last_summarized_index": end_index,
                        "pending_summary": None,
                        "last_summary_trigger": trigger_type,
                        "last_summary_at": time.time(),
                        "last_persona_id": effective_persona,
                    },
                )
                await store.finish_summary_job(session_id)
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
