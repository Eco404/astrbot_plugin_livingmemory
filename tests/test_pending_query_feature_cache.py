from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import (
    MemoryRecall,
)
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore


@pytest.mark.asyncio
async def test_recall_current_query_vector_is_cached_for_pending_summary(
    tmp_path: Path,
):
    session_id = "test:FriendMessage:user"
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    message_id = await store.add_message(
        Message(
            id=0,
            session_id=session_id,
            role="user",
            content="continued topic",
            sender_id="user",
            sender_name="User",
        )
    )
    message = Message(
        id=message_id,
        session_id=session_id,
        role="user",
        content="continued topic",
        sender_id="user",
        sender_name="User",
    )
    provider = SimpleNamespace(
        provider_config={"id": "embedding", "model": "semantic-v1"}
    )
    retriever = SimpleNamespace(
        embedding_provider=provider,
        refresh_providers=lambda: None,
    )
    recall = object.__new__(MemoryRecall)
    recall.config_manager = SimpleNamespace(get=lambda _key, default=None: default)
    recall.conversation_manager = SimpleNamespace(store=store)
    recall.memory_engine = SimpleNamespace(
        topic_recall_pipeline=SimpleNamespace(retriever=retriever)
    )
    timeline_outcome = SimpleNamespace(
        branches=[SimpleNamespace(name="current", text="continued topic")]
    )
    topic_outcome = SimpleNamespace(query_vectors=[[0.25, 0.75]])

    await recall._cache_current_query_vector(
        message,
        timeline_outcome,
        topic_outcome,
    )

    cached = await store.get_pending_message_features([message_id])
    assert cached[message_id]["embedding"] == [0.25, 0.75]
    assert cached[message_id]["provider_id"] == "embedding"
    assert cached[message_id]["model_id"] == "semantic-v1"
    await store.close()
