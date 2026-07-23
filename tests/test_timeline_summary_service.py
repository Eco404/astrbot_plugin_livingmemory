from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.managers.conversation_manager import (
    ConversationManager,
)
from astrbot_plugin_livingmemory.core.managers.timeline_summary_service import (
    TimelineSummaryService,
)
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.schedulers.idle_summary_scheduler import (
    IdleSummaryScheduler,
)
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore


async def _conversation(
    tmp_path: Path, *, session_id: str = "test:FriendMessage:user"
) -> tuple[ConversationStore, ConversationManager]:
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    manager = ConversationManager(store)
    for index, role in enumerate(("user", "assistant", "user", "assistant")):
        await store.add_message(
            Message(
                id=0,
                session_id=session_id,
                role=role,
                content=f"message-{index}",
                sender_id="user" if role == "user" else "bot",
                sender_name="User" if role == "user" else "Bot",
                platform="test",
            )
        )
    return store, manager


@pytest.mark.asyncio
async def test_summary_service_creates_memory_and_advances_checkpoint(tmp_path: Path):
    store, manager = await _conversation(tmp_path)
    processor = AsyncMock()
    processor.process_conversation.return_value = (
        "summary",
        {"topics": ["topic"], "facts": ["fact"]},
        7.5,
    )
    processor.classify_atoms_from_metadata = MagicMock(return_value=["atom"])
    engine = AsyncMock()
    service = TimelineSummaryService(
        config_manager=ConfigManager({}),
        conversation_manager=manager,
        memory_engine=engine,
        memory_processor=processor,
    )

    result = await service.summarize_if_needed(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="idle",
        min_rounds=2,
    )

    assert result.status == "created"
    assert result.message_count == 4
    engine.add_memory.assert_awaited_once()
    kwargs = engine.add_memory.await_args.kwargs
    assert kwargs["metadata"]["source_window"]["triggered_by"] == "idle"
    assert kwargs["metadata"]["source_window"]["start_index"] == 0
    assert kwargs["metadata"]["source_window"]["end_index"] == 4
    session = await store.get_session("test:FriendMessage:user")
    assert session is not None
    assert session.metadata["last_summarized_index"] == 4
    assert "pending_summary" not in session.metadata
    job = await store.get_summary_job("test:FriendMessage:user")
    assert job is not None and job["status"] == "completed"
    await store.close()


@pytest.mark.asyncio
async def test_summary_service_failure_preserves_source_range(tmp_path: Path):
    store, manager = await _conversation(tmp_path)
    processor = AsyncMock()
    processor.process_conversation.side_effect = RuntimeError("provider unavailable")
    service = TimelineSummaryService(
        config_manager=ConfigManager({}),
        conversation_manager=manager,
        memory_engine=AsyncMock(),
        memory_processor=processor,
    )

    result = await service.summarize_if_needed(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=2,
    )

    assert result.status == "failed"
    session = await store.get_session("test:FriendMessage:user")
    assert session is not None
    assert session.metadata.get("last_summarized_index", 0) == 0
    pending = session.metadata["pending_summary"]
    assert pending["start_index"] == 0
    assert pending["end_index"] == 4
    assert pending["retry_count"] == 1
    job = await store.get_summary_job("test:FriendMessage:user")
    assert job is not None and job["status"] == "failed"
    await store.close()


@pytest.mark.asyncio
async def test_idle_scheduler_only_schedules_resolved_persona(tmp_path: Path):
    store, manager = await _conversation(tmp_path)
    await manager.update_session_metadata_values(
        "test:FriendMessage:user", {"last_persona_id": "persona"}
    )
    assert store.connection is not None
    await store.connection.execute(
        "UPDATE sessions SET last_active_at = 0 WHERE session_id = ?",
        ("test:FriendMessage:user",),
    )
    await store.connection.commit()
    summary_service = MagicMock()
    summary_service.schedule_if_needed.return_value = True
    config = ConfigManager({})
    config.apply_runtime_overrides(
        {
            "reflection_engine.idle_summary_enabled": True,
            "reflection_engine.idle_summary_delay_minutes": 1,
            "reflection_engine.idle_summary_min_rounds": 2,
        }
    )
    scheduler = IdleSummaryScheduler(
        config_manager=config,
        conversation_manager=manager,
        summary_service=summary_service,
    )

    assert await scheduler.scan_once() == 1
    summary_service.schedule_if_needed.assert_called_once_with(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="idle",
        min_rounds=2,
    )
    await store.close()
