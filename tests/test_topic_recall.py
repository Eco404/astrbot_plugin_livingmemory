from __future__ import annotations

import pytest

from astrbot_plugin_livingmemory.core.models.topic_memory import (
    TopicFragmentDraft,
    TopicMemory,
)
from astrbot_plugin_livingmemory.core.retrieval.recall_pipeline import RecallQueryBranch
from astrbot_plugin_livingmemory.core.retrieval.topic_recall_pipeline import (
    TopicRecallPipeline,
)
from astrbot_plugin_livingmemory.core.retrieval.topic_retriever import TopicRetriever
from astrbot_plugin_livingmemory.core.retrieval.topic_retriever import (
    TopicRecallResult,
)


class _Embedding:
    async def get_embeddings(self, texts: list[str]):
        vectors = {
            "上海天气": [1.0, 0.0],
            "之前的旅行": [0.8, 0.2],
        }
        return [vectors.get(text, [0.0, 1.0]) for text in texts]


class _Store:
    def __init__(self, payloads, fragments=None):
        self.payloads = payloads
        self.fragments = fragments or []
        self.accessed = []

    async def list_topic_recall_payloads(self, memory_space_id, limit=1000):
        assert memory_space_id == "space-1"
        return self.payloads[:limit]

    async def record_topic_access(self, topic_uids):
        self.accessed.extend(topic_uids)
        return len(topic_uids)

    async def list_active_fragments_for_topics(self, topic_uids):
        return [
            item for item in self.fragments if item["topic_uid"] in topic_uids
        ]


class _FixedScoreRetriever:
    def __init__(self, store, candidates):
        self.store = store
        self.candidates = candidates

    async def _get_embeddings(self, texts):
        return [[1.0] for _ in texts]

    async def search(self, *args, **kwargs):
        return list(self.candidates)

    @staticmethod
    def _rank_score(topic, relevance):
        return relevance


class _RerankRow:
    def __init__(self, index, relevance_score):
        self.index = index
        self.relevance_score = relevance_score


class _CountingReranker:
    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = 0

    async def rerank(self, query, documents, top_n=None):
        self.calls += 1
        return [
            _RerankRow(index, score)
            for index, score in enumerate(self.scores[: len(documents)])
        ]


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
async def test_topic_retriever_uses_configurable_rerank_weight():
    store = _Store([_payload("weather", "上海雷雨", [1.0, 0.0])])
    reranker = _CountingReranker([0.0])
    retriever = TopicRetriever(
        store,
        embedding_provider=_Embedding(),
        rerank_provider=reranker,
        config={"recall_use_rerank": True, "recall_rerank_weight": 0.35},
    )

    result = (
        await retriever.search("上海天气", memory_space_id="space-1", k=1)
    )[0]

    assert reranker.calls == 1
    assert result.base_relevance_score is not None
    assert result.rerank_score == pytest.approx(0.0)
    assert result.relevance_score == pytest.approx(
        result.base_relevance_score * 0.65
    )


@pytest.mark.asyncio
async def test_topic_retriever_skips_rerank_when_weight_is_zero():
    store = _Store([_payload("weather", "上海雷雨", [1.0, 0.0])])
    reranker = _CountingReranker([1.0])
    retriever = TopicRetriever(
        store,
        embedding_provider=_Embedding(),
        rerank_provider=reranker,
        config={"recall_use_rerank": True, "recall_rerank_weight": 0.0},
    )

    result = (
        await retriever.search("上海天气", memory_space_id="space-1", k=1)
    )[0]

    assert reranker.calls == 0
    assert result.rerank_score is None
    assert result.relevance_score == pytest.approx(result.base_relevance_score)


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


@pytest.mark.asyncio
async def test_topic_pipeline_default_floor_accepts_moderate_match_but_rejects_weak_one():
    store = _Store([_payload("placeholder", "占位", [1.0])])
    moderate_topic = _payload("moderate", "临时加班", [1.0])["topic"]
    weak_topic = _payload("weak", "无关闲聊", [1.0])["topic"]
    retriever = _FixedScoreRetriever(
        store,
        [
            TopicRecallResult(moderate_topic, 0.36, 0.36, 0.36, 0.0),
            TopicRecallResult(weak_topic, 0.29, 0.29, 0.29, 0.0),
        ],
    )
    pipeline = TopicRecallPipeline(retriever, {})

    outcome = await pipeline.search(
        branches=[RecallQueryBranch("current", "今晚是否加班", 1.0, "user")],
        memory_space_id="space-1",
        final_k=3,
        track_access=False,
    )

    assert outcome.applied_threshold == pytest.approx(0.32)
    assert [item.topic_uid for item in outcome.results] == ["moderate"]


@pytest.mark.asyncio
async def test_topic_fragment_supplements_only_serve_formal_third_person_rows():
    topic = _payload("weather", "上海雷雨", [1.0, 0.0])["topic"]
    safe = TopicFragmentDraft(
        fragment_uid="safe-fragment",
        run_uid="run-1",
        candidate_group_uid="group-1",
        memory_space_id="space-1",
        label="上海雷雨出行",
        summary="唯提醒空雨携带雨具",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[{"content": "空雨决定携带雨具"}],
        embedding=[1.0, 0.0],
        metadata={"narrative_schema_version": "third_person_roles_v1"},
    )
    legacy = TopicFragmentDraft(
        fragment_uid="legacy-fragment",
        run_uid="run-1",
        candidate_group_uid="group-1",
        memory_space_id="space-1",
        label="旧片段",
        summary="我提醒用户带伞",
        timeline_uids=["timeline-2"],
        source_revisions={"timeline-2": 1},
        facts=[],
        embedding=[1.0, 0.0],
        metadata={},
    )
    store = _Store(
        [_payload("weather", "上海雷雨", [1.0, 0.0])],
        fragments=[
            {"topic_uid": "weather", "fragment": safe, "sources": []},
            {"topic_uid": "weather", "fragment": legacy, "sources": []},
        ],
    )
    retriever = TopicRetriever(
        store,
        embedding_provider=_Embedding(),
        config={"recall_use_rerank": False},
    )
    pipeline = TopicRecallPipeline(
        retriever,
        {
            "recall_use_rerank": False,
            "fragment_min_relevance": 0.0,
            "fragment_relative_floor": 0.0,
        },
    )
    parent = TopicRecallResult(topic, 0.9, 0.9, 0.9, 0.0)

    outcome = await pipeline.search_fragment_supplements(
        branches=[RecallQueryBranch("current", "上海天气", 1.0, "user")],
        topic_results=[parent],
        limit=2,
    )

    assert outcome.available_count == 1
    assert [item.fragment_uid for item in outcome.results] == ["safe-fragment"]
    assert "唯提醒空雨" in outcome.results[0].content
