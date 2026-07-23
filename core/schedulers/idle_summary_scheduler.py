"""Background scanner that closes short conversations after they become idle."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from ..base.config_manager import ConfigManager
    from ..managers.conversation_manager import ConversationManager
    from ..managers.timeline_summary_service import TimelineSummaryService


class IdleSummaryScheduler:
    def __init__(
        self,
        *,
        config_manager: "ConfigManager",
        conversation_manager: "ConversationManager",
        summary_service: "TimelineSummaryService",
    ) -> None:
        self.config_manager = config_manager
        self.conversation_manager = conversation_manager
        self.summary_service = summary_service
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._running = False

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run(), name="livingmemory-idle-summary-scheduler"
        )
        logger.info("IdleSummaryScheduler 已启动")

    def notify_settings_changed(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def scan_once(self) -> int:
        if not self.config_manager.get(
            "reflection_engine.idle_summary_enabled", True
        ):
            return 0
        delay_minutes = float(
            self.config_manager.get(
                "reflection_engine.idle_summary_delay_minutes", 30.0
            )
        )
        min_rounds = int(
            self.config_manager.get(
                "reflection_engine.idle_summary_min_rounds", 3
            )
        )
        sessions = await self.conversation_manager.store.get_idle_sessions(
            last_active_before=time.time() - max(60.0, delay_minutes * 60.0),
            limit=50,
        )
        scheduled = 0
        for session in sessions:
            persona_id = str(session.metadata.get("last_persona_id") or "").strip()
            if not persona_id:
                # A persisted persona is required so an idle task cannot write a
                # Timeline into the wrong memory space after a plugin restart.
                continue
            if self.summary_service.schedule_if_needed(
                session.session_id,
                persona_id=persona_id,
                trigger_type="idle",
                min_rounds=min_rounds,
            ):
                scheduled += 1
        return scheduled

    async def _run(self) -> None:
        # Let plugin startup and provider initialization settle before the first scan.
        await asyncio.sleep(5)
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("空闲 Timeline 总结扫描失败", exc_info=True)
            interval = int(
                self.config_manager.get(
                    "reflection_engine.idle_summary_scan_interval_seconds", 60
                )
            )
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=max(30, min(interval, 3600))
                )
            except TimeoutError:
                pass


__all__ = ["IdleSummaryScheduler"]
