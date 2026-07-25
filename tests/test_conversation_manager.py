"""
Tests for ConversationManager behaviors.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_livingmemory.core.managers.conversation_manager import (
    ConversationManager,
)
from astrbot_plugin_livingmemory.core.models.conversation_identity import (
    audit_message_identity,
)
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore

from astrbot.api.platform import MessageType


class _DummyEvent:
    def __init__(self, session_id: str, group: bool = False):
        self.unified_msg_origin = session_id
        self._group = group
        self.sender_id = "u1"
        self.sender_name = "Tester"

    def get_sender_id(self):
        return "u1"

    def get_sender_name(self):
        return "Tester"

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE if self._group else MessageType.FRIEND_MESSAGE

    def get_platform_name(self):
        return "test"

    def get_self_id(self):
        return "bot-1"


class _DummyTelegramEvent(_DummyEvent):
    def __init__(
        self,
        session_id: str,
        first_name: str | None = None,
        last_name: str | None = None,
        user_id: int = 12345,
    ):
        super().__init__(session_id, group=False)
        self.sender_id = str(user_id)
        raw_user = SimpleNamespace(
            id=user_id,
            username=None,
            first_name=first_name,
            last_name=last_name,
        )
        self.message_obj = SimpleNamespace(
            sender=SimpleNamespace(user_id=str(user_id), nickname="Unknown"),
            raw_message=SimpleNamespace(
                message=SimpleNamespace(from_user=raw_user),
                effective_user=raw_user,
            ),
        )

    def get_sender_id(self):
        return self.sender_id

    def get_sender_name(self):
        return "Unknown"

    def get_platform_name(self):
        return "telegram"


@pytest.mark.asyncio
async def test_conversation_manager_add_and_get_context(tmp_path: Path):
    db_path = tmp_path / "cm.db"
    store = ConversationStore(str(db_path))
    await store.initialize()
    manager = ConversationManager(store=store, max_cache_size=2, context_window_size=10)

    event = _DummyEvent("test:private:s1", group=False)
    await manager.add_message_from_event(event, role="user", content="hello")
    await manager.add_message_from_event(event, role="assistant", content="world")

    context = await manager.get_context("test:private:s1")
    assert len(context) == 2
    assert context[0]["role"] == "user"

    messages = await manager.get_messages("test:private:s1", limit=10)
    assert len(messages) == 2
    assert messages[0].metadata["actor_id"] == "test:human:u1"
    assert messages[1].sender_id == "bot-1"
    assert messages[1].sender_name == "bot-1"
    assert messages[1].metadata["actor_id"] == "test:assistant:bot-1"

    session = await manager.get_session_info("test:private:s1")
    assert session is not None
    assert session.message_count == 2

    await store.close()


@pytest.mark.asyncio
async def test_group_assistant_never_inherits_triggering_user_name(tmp_path: Path):
    store = ConversationStore(str(tmp_path / "group-identity.db"))
    await store.initialize()
    manager = ConversationManager(store=store)
    event = _DummyEvent("test:GroupMessage:g1", group=True)

    message = await manager.add_message_from_event(
        event, role="assistant", content="reply"
    )

    assert message.sender_id == "bot-1"
    assert message.sender_name == "bot-1"
    assert message.sender_name != event.get_sender_name()
    assert message.metadata["is_bot_message"] is True
    assert message.metadata["actor_type"] == "assistant"
    assert message.metadata["actor_id"] == "test:assistant:bot-1"
    assert message.metadata["identity_source"] == "event_self_id"
    assert message.metadata["event_source"] == "llm_response"
    await store.close()


@pytest.mark.asyncio
async def test_synthetic_assistant_event_uses_platform_instance_and_persona(
    tmp_path: Path,
):
    store = ConversationStore(str(tmp_path / "synthetic-identity.db"))
    await store.initialize()
    manager = ConversationManager(store=store)

    class _SyntheticEvent:
        unified_msg_origin = "QQ20000001:FriendMessage:10000001"
        message_obj = SimpleNamespace(
            self_id="10000001",
            sender=SimpleNamespace(user_id="10000001", nickname="示例甲"),
        )

        def get_sender_id(self):
            return "10000001"

        def get_sender_name(self):
            return "示例甲"

        def get_self_id(self):
            return "10000001"

        def get_message_type(self):
            return MessageType.FRIEND_MESSAGE

        def get_platform_name(self):
            return "QQ20000001"

    message = await manager.add_message_from_event(
        _SyntheticEvent(),
        role="assistant",
        content="reply",
        persona_id="persona-only",
        persona_name="测试助手",
    )

    assert message.sender_id == "20000001"
    assert message.sender_name == "测试助手"
    assert message.platform == "qq"
    assert message.metadata["actor_id"] == "qq:assistant:20000001"
    assert message.metadata["persona_id"] == "persona-only"
    assert message.metadata["platform_instance_id"] == "QQ20000001"
    assert message.metadata["identity_source"] == "platform_instance_id"
    assert "event_self_id_collides_with_human_sender" in message.metadata[
        "identity_warnings"
    ]
    assert "message_object_self_id_collides_with_human_sender" in message.metadata[
        "identity_warnings"
    ]
    await store.close()


def test_identity_audit_detects_historical_synthetic_assistant_row():
    from astrbot_plugin_livingmemory.core.models.conversation_models import Message

    message = Message(
        id=1034,
        session_id="QQ20000001:FriendMessage:10000001",
        role="assistant",
        content="legacy",
        sender_id="10000001",
        sender_name="10000001",
        platform="QQ20000001",
        metadata={"actor_type": "assistant"},
    )

    assert set(audit_message_identity(message)) >= {
        "platform_value_is_instance_id",
        "assistant_display_name_is_account_id",
        "assistant_sender_matches_session_peer",
    }


@pytest.mark.asyncio
async def test_conversation_manager_range_and_metadata(tmp_path: Path):
    db_path = tmp_path / "cm2.db"
    store = ConversationStore(str(db_path))
    await store.initialize()
    manager = ConversationManager(store=store, max_cache_size=2, context_window_size=10)

    event = _DummyEvent("test:private:s2", group=False)
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        await manager.add_message_from_event(event, role=role, content=f"m-{i}")

    rng = await manager.get_messages_range(
        "test:private:s2", start_index=2, end_index=5
    )
    assert [m.content for m in rng] == ["m-2", "m-3", "m-4"]

    await manager.update_session_metadata("test:private:s2", "last_summarized_index", 3)
    assert (
        await manager.get_session_metadata(
            "test:private:s2", "last_summarized_index", default=0
        )
        == 3
    )

    await manager.clear_session("test:private:s2")
    assert await store.get_message_count("test:private:s2") == 0

    await store.close()


@pytest.mark.asyncio
async def test_conversation_manager_resolves_telegram_name_without_username(
    tmp_path: Path,
):
    db_path = tmp_path / "telegram.db"
    store = ConversationStore(str(db_path))
    await store.initialize()
    manager = ConversationManager(store=store, max_cache_size=2, context_window_size=10)

    event = _DummyTelegramEvent(
        "telegram:private:s3",
        first_name="Alice",
        last_name="Lee",
        user_id=67890,
    )
    message = await manager.add_message_from_event(event, role="user", content="hello")

    assert message.sender_id == "67890"
    assert message.sender_name == "Alice Lee"

    await store.close()


@pytest.mark.asyncio
async def test_conversation_manager_falls_back_to_sender_id_for_unknown_name(
    tmp_path: Path,
):
    db_path = tmp_path / "telegram-id.db"
    store = ConversationStore(str(db_path))
    await store.initialize()
    manager = ConversationManager(store=store, max_cache_size=2, context_window_size=10)

    event = _DummyTelegramEvent("telegram:private:s4", user_id=24680)
    message = await manager.add_message_from_event(event, role="user", content="hello")

    assert message.sender_id == "24680"
    assert message.sender_name == "24680"

    await store.close()


@pytest.mark.asyncio
async def test_session_alias_routes_new_messages_to_canonical_session(tmp_path: Path):
    store = ConversationStore(str(tmp_path / "aliases.db"))
    await store.initialize()
    manager = ConversationManager(store=store, max_cache_size=2)

    canonical = "test:FriendMessage:current"
    legacy = "test:private:legacy"
    await manager.add_message(canonical, "user", "current message")
    await manager.add_message(legacy, "user", "legacy message")
    await store.set_session_aliases(canonical, [legacy])

    assert await manager.resolve_session_id(legacy) == canonical
    assert await manager.get_session_scope(legacy) == [canonical, legacy]

    await manager.add_message(legacy, "user", "new message")
    canonical_messages = await store.get_messages(canonical, limit=10)
    legacy_messages = await store.get_messages(legacy, limit=10)

    assert [item.content for item in canonical_messages] == [
        "current message",
        "new message",
    ]
    assert [item.content for item in legacy_messages] == ["legacy message"]
    await store.close()


@pytest.mark.asyncio
async def test_cleanup_expired_sessions_only_evicts_memory_cache(tmp_path: Path):
    store = ConversationStore(str(tmp_path / "cache-expiry.db"))
    await store.initialize()
    manager = ConversationManager(store=store, session_ttl=60)
    session_id = "test:private:cache"
    await manager.add_message(session_id, "user", "persist me")
    await manager.get_messages(session_id, use_cache=True)

    messages, _last_access = manager._cache[session_id]
    manager._cache[session_id] = (messages, 0.0)
    assert await manager.cleanup_expired_sessions() == 1
    assert session_id not in manager._cache
    assert await store.get_message_count(session_id) == 1
    assert await store.get_session(session_id) is not None
    await store.close()
