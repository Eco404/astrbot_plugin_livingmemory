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


class _EmbeddingProvider:
    provider_config = {"id": "test-embedding", "model": "semantic-v1"}

    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors
        self.calls: list[list[str]] = []

    def get_dim(self) -> int:
        return 2

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vectors[text] for text in texts]


async def _add_round(
    store: ConversationStore,
    user_text: str,
    assistant_text: str,
    *,
    session_id: str = "test:FriendMessage:user",
) -> None:
    for role, content in (("user", user_text), ("assistant", assistant_text)):
        await store.add_message(
            Message(
                id=0,
                session_id=session_id,
                role=role,
                content=content,
                sender_id="user" if role == "user" else "bot",
                sender_name="User" if role == "user" else "Bot",
                platform="test",
            )
        )


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
    config = ConfigManager({})
    config.apply_runtime_overrides(
        {"reflection_engine.source_retention_importance_threshold": 0.7}
    )
    service = TimelineSummaryService(
        config_manager=config,
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
    assert kwargs["source_messages"] == await manager.get_messages_range(
        "test:FriendMessage:user", 0, 4
    )
    assert kwargs["source_retention_reason"] == "importance_threshold"
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
async def test_summary_service_records_new_boundaries_while_blocked(tmp_path: Path):
    store, manager = await _conversation(tmp_path)
    session_id = "test:FriendMessage:user"
    await manager.update_session_metadata(
        session_id,
        "pending_summary",
        {
            "start_index": 0,
            "end_index": 2,
            "retry_count": 3,
            "next_retry_at": 9_999_999_999.0,
            "blocked": True,
            "error": "provider unavailable",
            "trigger_type": "round_limit",
            "persona_id": "persona",
        },
    )
    processor = AsyncMock()
    processor.process_conversation.side_effect = RuntimeError("still unavailable")
    service = TimelineSummaryService(
        config_manager=ConfigManager({}),
        conversation_manager=manager,
        memory_engine=AsyncMock(),
        memory_processor=processor,
    )

    result = await service.summarize_if_needed(
        session_id,
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=1,
    )

    assert result.status == "failed"
    session = await store.get_session(session_id)
    assert session is not None
    pending = session.metadata["pending_summary"]
    assert pending["start_index"] == 0
    assert pending["end_index"] == 2
    assert pending["retry_count"] == 4
    assert pending["blocked"] is True
    assert pending["next_retry_at"] is not None
    assert [
        (item["start_index"], item["end_index"]) for item in pending["queued_windows"]
    ] == [(2, 4)]
    processor.process_conversation.assert_awaited_once_with(
        messages=await manager.get_messages_range(session_id, 0, 2),
        is_group_chat=False,
        persona_id="persona",
        allow_no_memory=True,
    )
    await store.close()


@pytest.mark.asyncio
async def test_summary_service_provider_change_bypasses_blocked_cooldown(
    tmp_path: Path,
):
    store, manager = await _conversation(tmp_path)
    session_id = "test:FriendMessage:user"
    await manager.update_session_metadata(
        session_id,
        "pending_summary",
        {
            "start_index": 0,
            "end_index": 4,
            "retry_count": 3,
            "next_retry_at": 9_999_999_999.0,
            "blocked": True,
            "error": "old provider unavailable",
            "trigger_type": "round_limit",
            "persona_id": "persona",
            "provider_signature": {
                "configured_provider_id": "provider-old",
                "provider_id": "",
                "model_id": "",
                "runtime_class": "",
            },
        },
    )
    processor = AsyncMock()
    processor.process_conversation.return_value = (
        "summary",
        {"topics": ["topic"], "facts": ["fact"]},
        7.0,
    )
    processor.classify_atoms_from_metadata = MagicMock(return_value=[])
    config = ConfigManager({})
    config.apply_runtime_overrides(
        {"provider_settings.llm_provider_id": "provider-new"}
    )
    service = TimelineSummaryService(
        config_manager=config,
        conversation_manager=manager,
        memory_engine=AsyncMock(),
        memory_processor=processor,
    )

    result = await service.summarize_if_needed(
        session_id,
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=2,
    )

    assert result.status == "created"
    processor.process_conversation.assert_awaited_once()
    session = await store.get_session(session_id)
    assert session is not None
    assert session.metadata["last_summarized_index"] == 4
    assert "pending_summary" not in session.metadata
    await store.close()


@pytest.mark.asyncio
async def test_summary_service_recovers_blocked_windows_in_original_order(
    tmp_path: Path,
):
    store, manager = await _conversation(tmp_path)
    session_id = "test:FriendMessage:user"
    await manager.update_session_metadata(
        session_id,
        "pending_summary",
        {
            "start_index": 0,
            "end_index": 2,
            "retry_count": 3,
            "next_retry_at": 9_999_999_999.0,
            "blocked": True,
            "error": "provider unavailable",
            "trigger_type": "round_limit",
            "persona_id": "persona",
        },
    )
    processor = AsyncMock()
    processor.process_conversation.side_effect = [
        ("summary-1", {"topics": ["topic-1"], "facts": ["fact-1"]}, 7.0),
        ("summary-2", {"topics": ["topic-2"], "facts": ["fact-2"]}, 6.0),
    ]
    processor.classify_atoms_from_metadata = MagicMock(return_value=[])
    engine = AsyncMock()
    service = TimelineSummaryService(
        config_manager=ConfigManager({}),
        conversation_manager=manager,
        memory_engine=engine,
        memory_processor=processor,
    )

    result = await service.summarize_if_needed(
        session_id,
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=1,
    )

    assert result.status == "created"
    assert result.start_index == 0
    assert result.end_index == 4
    assert result.message_count == 4
    assert result.topics == ["topic-1", "topic-2"]
    assert engine.add_memory.await_count == 2
    windows = [
        call.kwargs["metadata"]["source_window"]
        for call in engine.add_memory.await_args_list
    ]
    assert [(item["start_index"], item["end_index"]) for item in windows] == [
        (0, 2),
        (2, 4),
    ]
    session = await store.get_session(session_id)
    assert session is not None
    assert session.metadata["last_summarized_index"] == 4
    assert "pending_summary" not in session.metadata
    await store.close()


@pytest.mark.asyncio
async def test_summary_service_reconstructs_multiple_missed_boundaries(tmp_path: Path):
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    manager = ConversationManager(store)
    session_id = "test:FriendMessage:user"
    for index in range(40):
        await _add_round(store, f"user-{index}", f"assistant-{index}")
    await manager.update_session_metadata_values(
        session_id,
        {
            "last_summarized_index": 16,
            "pending_summary": {
                "start_index": 16,
                "end_index": 32,
                "retry_count": 3,
                "next_retry_at": None,
                "blocked": True,
                "error": "provider unavailable",
                "trigger_type": "round_limit",
                "persona_id": "persona",
            },
        },
    )
    processor = AsyncMock()
    processor.process_conversation.side_effect = [
        (
            "",
            {
                "memory_decision": "no_memory",
                "no_memory_reason": "ack_only",
                "summary_quality": "normal",
                "message_coverage": [],
            },
            0.1,
        ),
        *[
            (f"summary-{index}", {"topics": [f"topic-{index}"], "facts": []}, 6.0)
            for index in range(2, 6)
        ],
    ]
    processor.classify_atoms_from_metadata = MagicMock(return_value=[])
    engine = AsyncMock()
    config = ConfigManager({})
    config.apply_runtime_overrides(
        {"reflection_engine.topic_continuation_enabled": False}
    )
    service = TimelineSummaryService(
        config_manager=config,
        conversation_manager=manager,
        memory_engine=engine,
        memory_processor=processor,
    )

    result = await service.summarize_if_needed(
        session_id,
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=8,
    )

    assert result.status == "created"
    assert result.start_index == 16
    assert result.end_index == 80
    assert result.message_count == 64
    assert processor.process_conversation.await_count == 4
    windows = [
        call.kwargs["messages"]
        for call in processor.process_conversation.await_args_list
    ]
    assert [len(messages) for messages in windows] == [16, 16, 16, 16]
    assert [messages[0].content for messages in windows] == [
        "user-8",
        "user-16",
        "user-24",
        "user-32",
    ]
    assert engine.add_memory.await_count == 3
    session = await store.get_session(session_id)
    assert session is not None
    assert session.metadata["last_summarized_index"] == 80
    assert "pending_summary" not in session.metadata
    await store.close()


@pytest.mark.asyncio
async def test_summary_service_audits_no_memory_and_advances_checkpoint(tmp_path: Path):
    store, manager = await _conversation(tmp_path)
    processor = AsyncMock()
    processor.process_conversation.return_value = (
        "",
        {
            "memory_decision": "no_memory",
            "no_memory_reason": "ack_only",
            "summary_quality": "normal",
            "message_coverage": [
                {
                    "message_ref": f"M{index}",
                    "disposition": "context",
                    "fact_indexes": [],
                    "reason": "无持久信息",
                }
                for index in range(1, 5)
            ],
        },
        0.1,
    )
    processor.classify_atoms_from_metadata = MagicMock()
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

    assert result.status == "no_memory"
    assert result.decision_reason == "ack_only"
    engine.add_memory.assert_not_awaited()
    processor.classify_atoms_from_metadata.assert_not_called()
    processor.process_conversation.assert_awaited_once_with(
        messages=await manager.get_messages_range("test:FriendMessage:user", 0, 4),
        is_group_chat=False,
        persona_id="persona",
        allow_no_memory=True,
    )
    session = await store.get_session("test:FriendMessage:user")
    assert session is not None
    assert session.metadata["last_summarized_index"] == 4
    assert session.metadata["last_summary_decision"] == "no_memory"
    assert "pending_summary" not in session.metadata
    assert await store.get_message_count("test:FriendMessage:user") == 4
    decisions = await store.list_summary_decisions(session_id="test:FriendMessage:user")
    assert len(decisions) == 1
    assert decisions[0]["reason"] == "ack_only"
    assert len(decisions[0]["message_coverage"]) == 4
    job = await store.get_summary_job("test:FriendMessage:user")
    assert job is not None and job["status"] == "completed"
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


@pytest.mark.asyncio
async def test_summary_service_skips_unchanged_insufficient_window(tmp_path: Path):
    store, manager = await _conversation(tmp_path)
    original_get_messages_range = manager.get_messages_range
    manager.get_messages_range = AsyncMock(wraps=original_get_messages_range)
    processor = AsyncMock()
    processor.process_conversation.return_value = (
        "summary",
        {"topics": ["topic"], "facts": ["fact"]},
        7.0,
    )
    processor.classify_atoms_from_metadata = MagicMock(return_value=[])
    service = TimelineSummaryService(
        config_manager=ConfigManager({}),
        conversation_manager=manager,
        memory_engine=AsyncMock(),
        memory_processor=processor,
    )

    first = await service.summarize_if_needed(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="idle",
        min_rounds=3,
    )
    second = await service.summarize_if_needed(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="idle",
        min_rounds=3,
    )

    assert first.status == second.status == "insufficient"
    assert manager.get_messages_range.await_count == 1
    processor.process_conversation.assert_not_awaited()

    changed_setting = await service.summarize_if_needed(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="idle",
        min_rounds=2,
    )

    assert changed_setting.status == "created"
    assert manager.get_messages_range.await_count == 2
    processor.process_conversation.assert_awaited_once()
    await store.close()


@pytest.mark.asyncio
async def test_summary_service_rechecks_insufficient_window_after_new_messages(
    tmp_path: Path,
):
    store, manager = await _conversation(tmp_path)
    original_get_messages_range = manager.get_messages_range
    manager.get_messages_range = AsyncMock(wraps=original_get_messages_range)
    processor = AsyncMock()
    processor.process_conversation.return_value = (
        "summary",
        {"topics": ["topic"], "facts": ["fact"]},
        7.0,
    )
    processor.classify_atoms_from_metadata = MagicMock(return_value=[])
    service = TimelineSummaryService(
        config_manager=ConfigManager({}),
        conversation_manager=manager,
        memory_engine=AsyncMock(),
        memory_processor=processor,
    )

    insufficient = await service.summarize_if_needed(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="idle",
        min_rounds=3,
    )
    await _add_round(store, "new message", "new answer")
    after_new_messages = await service.summarize_if_needed(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="idle",
        min_rounds=3,
    )

    assert insufficient.status == "insufficient"
    assert after_new_messages.status == "created"
    assert manager.get_messages_range.await_count == 2
    processor.process_conversation.assert_awaited_once()
    await store.close()


@pytest.mark.asyncio
async def test_topic_continuation_reuses_multiple_base_centers_and_splits_new_topic(
    tmp_path: Path,
):
    session_id = "test:FriendMessage:user"
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    manager = ConversationManager(store)
    await _add_round(store, "alpha", "answer alpha")
    await _add_round(store, "beta", "answer beta")
    provider = _EmbeddingProvider(
        {
            "alpha": [1.0, 0.0],
            "beta": [0.0, 1.0],
            "alpha followup": [0.99, 0.01],
            "new gamma": [-1.0, 0.0],
        }
    )
    processor = AsyncMock()
    processor.process_conversation.return_value = (
        "summary",
        {"topics": ["topic"], "facts": ["fact"]},
        7.0,
    )
    processor.classify_atoms_from_metadata = MagicMock(return_value=[])
    engine = AsyncMock()
    config = ConfigManager({})
    config.apply_runtime_overrides(
        {
            "reflection_engine.topic_continuation_enabled": True,
            "reflection_engine.topic_continuation_force_summary_rounds": 6,
        }
    )
    service = TimelineSummaryService(
        config_manager=config,
        conversation_manager=manager,
        memory_engine=engine,
        memory_processor=processor,
        embedding_provider_resolver=lambda: provider,
    )

    first = await service.summarize_if_needed(
        session_id,
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=2,
    )
    assert first.status == "continuing"
    assert provider.calls == [["alpha", "beta"]]
    assert len(await store.get_pending_message_features([1, 3])) == 2

    await _add_round(store, "alpha followup", "continued answer")
    second = await service.summarize_if_needed(
        session_id,
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=2,
    )
    assert second.status == "continuing"
    assert provider.calls[-1] == ["alpha followup"]
    assert sum(len(call) for call in provider.calls) == 3

    await _add_round(store, "new gamma", "gamma answer")
    third = await service.summarize_if_needed(
        session_id,
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=2,
    )
    assert third.status == "created"
    assert third.end_index == 6
    assert third.message_count == 6
    summarized = processor.process_conversation.await_args.kwargs["messages"]
    assert [item.content for item in summarized][-2:] == [
        "alpha followup",
        "continued answer",
    ]
    session = await store.get_session(session_id)
    assert session is not None
    assert session.metadata["last_summarized_index"] == 6
    remaining_features = await store.get_pending_message_features([1, 3, 5, 7])
    assert set(remaining_features) == {7}
    await store.close()


@pytest.mark.asyncio
async def test_topic_continuation_force_limit_summarizes_all(tmp_path: Path):
    session_id = "test:FriendMessage:user"
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    manager = ConversationManager(store)
    provider = _EmbeddingProvider({f"alpha {index}": [1.0, 0.0] for index in range(4)})
    for index in range(4):
        await _add_round(store, f"alpha {index}", f"answer {index}")
    processor = AsyncMock()
    processor.process_conversation.return_value = (
        "summary",
        {"topics": ["topic"], "facts": ["fact"]},
        7.0,
    )
    processor.classify_atoms_from_metadata = MagicMock(return_value=[])
    engine = AsyncMock()
    config = ConfigManager({})
    config.apply_runtime_overrides(
        {
            "reflection_engine.topic_continuation_enabled": True,
            "reflection_engine.topic_continuation_force_summary_rounds": 4,
        }
    )
    service = TimelineSummaryService(
        config_manager=config,
        conversation_manager=manager,
        memory_engine=engine,
        memory_processor=processor,
        embedding_provider_resolver=lambda: provider,
    )
    result = await service.summarize_if_needed(
        session_id,
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=2,
    )
    assert result.status == "created"
    assert result.end_index == 8
    assert (
        engine.add_memory.await_args.kwargs["metadata"]["source_window"][
            "boundary_reason"
        ]
        == "force_summary_limit"
    )
    await store.close()


@pytest.mark.asyncio
async def test_idle_summary_bypasses_topic_continuation(tmp_path: Path):
    session_id = "test:FriendMessage:user"
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    manager = ConversationManager(store)
    await _add_round(store, "alpha", "answer alpha")
    await _add_round(store, "alpha followup", "continued answer")
    provider = _EmbeddingProvider({"alpha": [1.0, 0.0], "alpha followup": [0.99, 0.01]})
    processor = AsyncMock()
    processor.process_conversation.return_value = (
        "summary",
        {"topics": ["topic"], "facts": ["fact"]},
        7.0,
    )
    processor.classify_atoms_from_metadata = MagicMock(return_value=[])
    service = TimelineSummaryService(
        config_manager=ConfigManager({}),
        conversation_manager=manager,
        memory_engine=AsyncMock(),
        memory_processor=processor,
        embedding_provider_resolver=lambda: provider,
    )

    result = await service.summarize_if_needed(
        session_id,
        persona_id="persona",
        trigger_type="idle",
        min_rounds=2,
    )

    assert result.status == "created"
    assert provider.calls == []
    await store.close()


@pytest.mark.asyncio
async def test_failed_summary_retry_keeps_original_end_index(tmp_path: Path):
    store, manager = await _conversation(tmp_path)
    processor = AsyncMock()
    processor.process_conversation.side_effect = [
        RuntimeError("provider unavailable"),
        ("summary", {"topics": ["topic"], "facts": ["fact"]}, 7.0),
        ("later summary", {"topics": ["later"], "facts": ["fact"]}, 6.0),
    ]
    processor.classify_atoms_from_metadata = MagicMock(return_value=[])
    engine = AsyncMock()
    service = TimelineSummaryService(
        config_manager=ConfigManager({}),
        conversation_manager=manager,
        memory_engine=engine,
        memory_processor=processor,
    )
    first = await service.summarize_if_needed(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="round_limit",
        min_rounds=2,
    )
    assert first.status == "failed"
    await _add_round(store, "later", "later answer")
    retried = await service.summarize_if_needed(
        "test:FriendMessage:user",
        persona_id="persona",
        trigger_type="manual",
        min_rounds=1,
        force=True,
    )
    assert retried.status == "created"
    assert retried.start_index == 0
    assert retried.end_index == 6
    assert retried.message_count == 6
    successful_calls = processor.process_conversation.await_args_list[1:]
    assert [len(call.kwargs["messages"]) for call in successful_calls] == [4, 2]
    assert await store.get_message_count("test:FriendMessage:user") == 6
    await store.close()
