"""
Tests for core data models.
"""

from astrbot_plugin_livingmemory.core.models.conversation_models import (
    MemoryEvent,
    Message,
    Session,
    deserialize_from_json,
    serialize_to_json,
)
from astrbot_plugin_livingmemory.core.models.memory_identity import (
    resolve_memory_space,
)


def test_message_roundtrip_and_format():
    msg = Message(
        id=1,
        session_id="s1",
        role="assistant",
        content="hello",
        sender_id="bot",
        sender_name="Bot",
        group_id="g1",
        platform="test",
        metadata={"is_bot_message": True},
    )
    d = msg.to_dict()
    msg2 = Message.from_dict(d)
    assert msg2.content == "hello"

    llm = msg.format_for_llm(include_sender_name=True)
    assert llm["role"] == "assistant"
    assert "[Bot:" in llm["content"]


def test_message_multimodal_content_is_normalized_for_llm():
    msg = Message(
        id=1,
        session_id="s1",
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            {"type": "text", "text": "图片里的日程是下午三点"},
        ],
        sender_id="u1",
        sender_name="Alice",
        group_id="g1",
        platform="test",
        metadata={},
    )

    llm = msg.format_for_llm(include_sender_name=True)

    assert "图片里的日程是下午三点" in llm["content"]
    assert "image_url" not in llm["content"]
    assert "example.test" not in llm["content"]


def test_message_image_only_content_uses_placeholder():
    msg = Message(
        id=1,
        session_id="s1",
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
        ],
        sender_id="u1",
        sender_name="Alice",
        group_id=None,
        platform="test",
        metadata={},
    )

    assert msg.format_for_llm(include_sender_name=True)["content"] == "[图片消息]"
    assert msg.to_dict()["content"] == "[图片消息]"


def test_session_and_memory_event_helpers():
    session = Session(
        id=1,
        session_id="s1",
        platform="test",
        created_at=1.0,
        last_active_at=1.0,
    )
    session.add_participant("u1")
    session.add_participant("u1")
    assert session.participants == ["u1"]

    event = MemoryEvent(memory_content="x", importance_score=0.8, session_id="s1")
    assert event.is_important(0.5) is True
    assert MemoryEvent.from_dict(event.to_dict()).session_id == "s1"


def test_json_helpers():
    payload = {"a": 1}
    raw = serialize_to_json(payload)
    assert isinstance(raw, str)
    assert deserialize_from_json(raw)["a"] == 1
    assert deserialize_from_json(None, default={}) == {}


def test_memory_space_is_deterministic_and_strictly_isolated():
    first = resolve_memory_space("bot-a:FriendMessage:user-1", "persona-a")
    same = resolve_memory_space("bot-a:FriendMessage:user-1", "persona-a")
    other_user = resolve_memory_space("bot-a:FriendMessage:user-2", "persona-a")
    other_persona = resolve_memory_space("bot-a:FriendMessage:user-1", "persona-b")

    assert first == same
    assert first.chat_type == "private"
    assert first.bot_account == "bot-a"
    assert first.target_id == "user-1"
    assert first.memory_space_id != other_user.memory_space_id
    assert first.memory_space_id != other_persona.memory_space_id
