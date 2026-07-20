from __future__ import annotations

import pytest

from astrbot_plugin_livingmemory.core.models.topic_memory import TopicMemory
from astrbot_plugin_livingmemory.core.retrieval.recall_pipeline import RecallQueryBranch
from astrbot_plugin_livingmemory.core.retrieval.topic_recall_pipeline import (
    TopicRecallPipeline,
)
from astrbot_plugin_livingmemory.core.retrieval.topic_retriever import TopicRetriever


class _Embedding:
    async def get_embeddings(self, texts: list[str]):
        vectors = {
            "上海天气": [1.0, 0.0],
            "之前的旅行": [0.8, 0.2],
        }
        return [vectors.get(text, [0.0, 1.0]) for text in texts]


class _Store:
    def __init__(self, payloads):
        self.payloads = payloads
        self.accessed = []

    async def list_topic_recall_payloads(self, memory_space_id, limit=1000):
        assert memory_space_id == "space-1"
        return self.payloads[:limit]

    async def record_topic_access(self, topic_uids):
        self.accessed.extend(topic_uids)
        return len(topic_uids)


def _payload(uid, title, embedding, *, sources=None, importance=0.6):
    return {
        "topic": TopicMemory(
            topic_uid=uid,
            memory_space_id="space-1",
            title=title,
            summary=f"{title}的详细总结",
            importance=importance,
            confidence=0.8,
            metadata={"embedding": embedding, "keywords": [title]},
        ),
        "atoms": [{"content": f"{title}事实", "importance": 0.8}],
        "sources": sources or [],
    }


@pytest.mark.asyncio
async def test_topic_retriever_uses_stored_vectors_without_llm():
    store = _Store(
        [
            _payload("weather", "上海雷雨", [1.0, 0.0]),
            _payload("coding", "插件开发", [0.0, 1.0]),
        ]
    )
    retriever = TopicRetriever(
        store,
        embedding_provider=_Embedding(),
        config={"recall_use_rerank": False},
    )

    results = await retriever.search(
        "上海天气", memory_space_id="space-1", k=2
    )

    assert [item.topic_uid for item in results] == ["weather", "coding"]
    assert results[0].embedding_score == pytest.approx(1.0)
    assert results[0].rerank_score is None


@pytest.mark.asyncio
async def test_topic_pipeline_suppresses_high_context_coverage_and_tracks_selected():
    covered_sources = [
        {
            "timeline_uid": f"timeline-{index}",
            "session_id": "session-1",
            "start_index": 90 + index,
            "end_index": 95 + index,
        }
        for index in range(5)
    ]
    partial_sources = [
        {
            "timeline_uid": "old",
            "session_id": "session-1",
            "start_index": 10,
            "end_index": 20,
        },
        {
            "timeline_uid": "visible",
            "session_id": "session-1",
            "start_index": 95,
            "end_index": 100,
        },
    ]
    store = _Store(
        [
            _payload("covered", "上海天气", [1.0, 0.0], sources=covered_sources),
            _payload("partial", "上海旅行", [0.9, 0.1], sources=partial_sources),
        ]
    )
    retriever = TopicRetriever(
        store,
        embedding_provider=_Embedding(),
        config={"recall_use_rerank": False},
    )
    pipeline = TopicRecallPipeline(
        retriever,
        {
            "recall_min_relevance": 0.2,
            "recall_relative_floor": 0.0,
            "recall_context_overlap_threshold": 0.8,
        },
    )

    outcome = await pipeline.search(
        branches=[RecallQueryBranch("current", "上海天气", 1.0, "user")],
        memory_space_id="space-1",
        final_k=2,
        context_session_id="session-1",
        visible_message_start_index=90,
        visible_message_end_index=105,
    )

    assert [item.topic_uid for item in outcome.results] == ["partial"]
    assert outcome.context_suppressed == 1
    assert outcome.results[0].context_coverage == pytest.approx(0.5)
    assert store.accessed == ["partial"]
