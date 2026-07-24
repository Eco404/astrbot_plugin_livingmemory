from __future__ import annotations

import pytest

from astrbot_plugin_livingmemory.core.embedding_signature import (
    TOPIC_CENTROID_EMBEDDING_FORMAT,
    TOPIC_FRAGMENT_EMBEDDING_FORMAT,
    make_embedding_signature,
)
from astrbot_plugin_livingmemory.core.models.topic_memory import (
    TopicFragmentDraft,
    TopicMemory,
)
from astrbot_plugin_livingmemory.core.retrieval.recall_pipeline import RecallQueryBranch
from astrbot_plugin_livingmemory.core.retrieval.topic_recall_pipeline import (
    TopicRecallPipeline,
)
from astrbot_plugin_livingmemory.core.retrieval.topic_retriever import (
    TopicEmbeddingCompatibilityError,
    TopicRecallResult,
    TopicRetriever,
)


class _Embedding:
    def __init__(self):
        self.calls = 0

    async def get_embeddings(self, texts: list[str]):
        self.calls += 1
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

    async def timeline_document_ids(self, timeline_uids):
        return [int(uid.removeprefix("timeline-")) for uid in timeline_uids]


class _FixedScoreRetriever(TopicRetriever):
    def __init__(self, store, candidates, *, rerank_provider=None):
        self.store = store
        self.candidates = candidates
        self.rerank_provider = rerank_provider
        self.embedding_provider = _Embedding()
        self.provider_resolver = None
        self.config = {"recall_rerank_weight": 0.35}

    async def _get_embeddings(self, texts):
        return [[1.0] for _ in texts]

    async def search(self, *args, **kwargs):
        return list(self.candidates)

    @staticmethod
    def _rank_score(topic, relevance):
        return relevance


class _MappedScoreRetriever(_FixedScoreRetriever):
    def __init__(self, store, results_by_query, *, rerank_provider=None):
        super().__init__(store, [], rerank_provider=rerank_provider)
        self.results_by_query = results_by_query

    async def search(self, query, *args, **kwargs):
        return list(self.results_by_query.get(query, []))


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


def _payload(uid, title, embedding, *, sources=None, actors=None, importance=0.6):
    return {
        "topic": TopicMemory(
            topic_uid=uid,
            memory_space_id="space-1",
            title=title,
            summary=f"{title}的详细总结",
            importance=importance,
            confidence=0.8,
            embedding_signature=make_embedding_signature(
                _Embedding(),
                dimension=len(embedding),
                input_format_version=TOPIC_CENTROID_EMBEDDING_FORMAT,
                generated_at=1.0,
            ),
            metadata={"embedding": embedding, "keywords": [title]},
        ),
        "atoms": [{"content": f"{title}事实", "importance": 0.8}],
        "sources": sources or [],
        "actors": actors or [],
    }


def _fragment_signature(embedding: list[float]) -> dict:
    return make_embedding_signature(
        _Embedding(),
        dimension=len(embedding),
        input_format_version=TOPIC_FRAGMENT_EMBEDDING_FORMAT,
        generated_at=1.0,
    )


@pytest.mark.asyncio
async def test_topic_retriever_rejects_unsigned_legacy_vectors():
    payload = _payload("weather", "上海雷雨", [1.0, 0.0])
    payload["topic"].embedding_signature = {}
    retriever = TopicRetriever(
        _Store([payload]),
        embedding_provider=_Embedding(),
        config={"recall_use_rerank": False},
    )

    with pytest.raises(TopicEmbeddingCompatibilityError, match="重新向量化"):
        await retriever.search("上海天气", memory_space_id="space-1", k=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signature_update", "expected_reason"),
    [
        ({"model_id": "retired-model"}, "模型已变更"),
        ({"dimension": 3}, "向量维度已变更"),
    ],
)
async def test_topic_retriever_rejects_model_or_dimension_changes(
    signature_update,
    expected_reason,
):
    payload = _payload("weather", "上海雷雨", [1.0, 0.0])
    payload["topic"].embedding_signature.update(signature_update)
    retriever = TopicRetriever(
        _Store([payload]),
        embedding_provider=_Embedding(),
        config={"recall_use_rerank": False},
    )

    with pytest.raises(TopicEmbeddingCompatibilityError, match=expected_reason):
        await retriever.search("上海天气", memory_space_id="space-1", k=1)


@pytest.mark.asyncio
async def test_topic_retriever_refreshes_reloaded_embedding_provider():
    payload = _payload("weather", "上海雷雨", [1.0, 0.0])
    initial = _Embedding()

    class ReloadedEmbedding(_Embedding):
        provider_config = {"id": "reloaded", "model": "next-model"}

    reloaded = ReloadedEmbedding()
    retriever = TopicRetriever(
        _Store([payload]),
        embedding_provider=initial,
        provider_resolver=lambda: {"embedding_provider": reloaded},
        config={"recall_use_rerank": False},
    )

    with pytest.raises(TopicEmbeddingCompatibilityError, match="Provider 已变更"):
        await retriever.search("上海天气", memory_space_id="space-1", k=1)
    assert retriever.embedding_provider is reloaded


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
async def test_topic_retriever_keeps_relevance_when_rerank_has_one_candidate():
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
    assert result.relevance_score == pytest.approx(result.base_relevance_score)
    assert result.rerank_rank == 1
    assert result.rerank_percentile == pytest.approx(0.0)
    assert result.rerank_rank_boost == pytest.approx(0.0)


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
async def test_topic_pipeline_suppresses_high_context_coverage_without_tracking_early():
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
    assert store.accessed == []
    await pipeline.record_topic_access(outcome.results)
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
    )

    assert outcome.applied_threshold == pytest.approx(0.32)
    assert [item.topic_uid for item in outcome.results] == ["moderate"]


@pytest.mark.asyncio
async def test_recent_context_cannot_qualify_a_topic_missing_from_current_query():
    store = _Store([_payload("placeholder", "占位", [1.0])])
    current_topic = _payload("current", "工资核对", [1.0])["topic"]
    context_only_topic = _payload("context-only", "酒店住宿", [1.0])["topic"]
    retriever = _MappedScoreRetriever(
        store,
        {
            "这次工资对吗": [
                TopicRecallResult(current_topic, 0.40, 0.40, 0.40, 0.0)
            ],
            "上次的酒店": [
                TopicRecallResult(
                    context_only_topic,
                    0.95,
                    0.95,
                    0.95,
                    0.0,
                )
            ],
        },
    )
    pipeline = TopicRecallPipeline(
        retriever,
        {"recall_min_relevance": 0.32, "recall_relative_floor": 0.0},
    )

    outcome = await pipeline.search(
        branches=[
            RecallQueryBranch("current", "这次工资对吗", 1.0, "user"),
            RecallQueryBranch("recent_user", "上次的酒店", 0.45, "user"),
        ],
        memory_space_id="space-1",
        final_k=3,
    )

    assert [item.topic_uid for item in outcome.results] == ["current"]
    context_only = next(
        item for item in outcome.candidates if item.topic_uid == "context-only"
    )
    assert context_only.current_relevance == pytest.approx(0.0)
    assert context_only.filter_reason == "missing_current_query_match"


@pytest.mark.asyncio
async def test_recent_context_is_a_bounded_ranking_bonus_only():
    store = _Store([_payload("placeholder", "占位", [1.0])])
    supported = _payload("supported", "加班安排", [1.0])["topic"]
    other = _payload("other", "下班安排", [1.0])["topic"]
    retriever = _MappedScoreRetriever(
        store,
        {
            "今晚怎么安排": [
                TopicRecallResult(supported, 0.40, 0.40, 0.40, 0.0),
                TopicRecallResult(other, 0.42, 0.42, 0.42, 0.0),
            ],
            "之前在加班": [
                TopicRecallResult(supported, 0.90, 0.90, 0.90, 0.0)
            ],
        },
    )
    pipeline = TopicRecallPipeline(
        retriever,
        {
            "recall_min_relevance": 0.0,
            "recall_relative_floor": 0.0,
            "recall_context_support_cap": 0.08,
            "recall_use_rerank": False,
        },
    )

    outcome = await pipeline.search(
        branches=[
            RecallQueryBranch("current", "今晚怎么安排", 1.0, "user"),
            RecallQueryBranch("recent_user", "之前在加班", 0.45, "user"),
        ],
        memory_space_id="space-1",
        final_k=2,
    )

    supported_result = next(
        item for item in outcome.results if item.topic_uid == "supported"
    )
    assert supported_result.current_relevance == pytest.approx(0.40)
    assert supported_result.relevance_score == pytest.approx(0.40)
    assert 0.0 < supported_result.context_support <= 0.08
    assert outcome.results[0].topic_uid == "supported"


@pytest.mark.asyncio
async def test_topic_pipeline_reranks_candidate_union_once_with_current_query():
    store = _Store([_payload("placeholder", "占位", [1.0])])
    first = _payload("first", "工资计算", [1.0])["topic"]
    second = _payload("second", "工资补发", [1.0])["topic"]
    reranker = _CountingReranker([0.1, 0.9])
    retriever = _MappedScoreRetriever(
        store,
        {
            "工资": [
                TopicRecallResult(first, 0.41, 0.41, 0.41, 0.0),
                TopicRecallResult(second, 0.40, 0.40, 0.40, 0.0),
            ],
            "上次补发": [
                TopicRecallResult(second, 0.90, 0.90, 0.90, 0.0)
            ],
        },
        rerank_provider=reranker,
    )
    pipeline = TopicRecallPipeline(
        retriever,
        {
            "recall_min_relevance": 0.0,
            "recall_relative_floor": 0.0,
            "recall_use_rerank": True,
            "recall_rerank_weight": 0.35,
        },
    )

    outcome = await pipeline.search(
        branches=[
            RecallQueryBranch("current", "工资", 1.0, "user"),
            RecallQueryBranch("recent_user", "上次补发", 0.45, "user"),
        ],
        memory_space_id="space-1",
        final_k=2,
    )

    assert reranker.calls == 1
    assert {item.current_relevance for item in outcome.results} == {0.40, 0.41}
    assert sorted(item.rerank_rank for item in outcome.results) == [1, 2]


@pytest.mark.asyncio
async def test_low_confidence_rerank_cannot_overturn_base_order():
    store = _Store([_payload("placeholder", "占位", [1.0])])
    first = _payload("first", "工资补发", [1.0])["topic"]
    second = _payload("second", "持续陪伴", [1.0])["topic"]
    reranker = _CountingReranker([0.005, 0.006])
    retriever = _FixedScoreRetriever(
        store,
        [
            TopicRecallResult(first, 0.50, 0.50, 0.50, 0.0),
            TopicRecallResult(second, 0.495, 0.495, 0.495, 0.0),
        ],
        rerank_provider=reranker,
    )
    pipeline = TopicRecallPipeline(
        retriever,
        {
            "recall_min_relevance": 0.0,
            "recall_relative_floor": 0.0,
            "recall_selection_relative_floor": 0.5,
            "recall_use_rerank": True,
            "recall_rerank_weight": 0.35,
            "recall_mmr_lambda": 1.0,
        },
    )

    outcome = await pipeline.search(
        branches=[RecallQueryBranch("current", "之前工资怎么样", 1.0, "user")],
        memory_space_id="space-1",
        final_k=2,
    )

    assert [item.topic_uid for item in outcome.results] == ["first", "second"]
    assert outcome.results[0].rerank_confidence < 0.02
    assert max(item.rerank_rank_boost for item in outcome.results) < 0.001


@pytest.mark.asyncio
async def test_dynamic_selection_floor_returns_fewer_than_requested():
    store = _Store([_payload("placeholder", "占位", [1.0])])
    candidates = [
        TopicRecallResult(
            _payload(uid, uid, [1.0])["topic"],
            score,
            score,
            score,
            0.0,
        )
        for uid, score in (("strong", 0.50), ("near", 0.46), ("weak", 0.44))
    ]
    pipeline = TopicRecallPipeline(
        _FixedScoreRetriever(store, candidates),
        {
            "recall_min_relevance": 0.0,
            "recall_relative_floor": 0.0,
            "recall_selection_relative_floor": 0.90,
            "recall_use_rerank": False,
            "recall_mmr_lambda": 1.0,
        },
    )

    outcome = await pipeline.search(
        branches=[RecallQueryBranch("current", "模糊查询", 1.0, "user")],
        memory_space_id="space-1",
        final_k=5,
    )

    assert outcome.selection_threshold == pytest.approx(0.45)
    assert [item.topic_uid for item in outcome.results] == ["strong", "near"]
    weak = next(item for item in outcome.candidates if item.topic_uid == "weak")
    assert weak.filter_reason == "below_selection_floor"


@pytest.mark.asyncio
async def test_topic_pipeline_actor_match_only_adds_configured_boost():
    actor_id = "qq:human:u1"
    store = _Store([_payload("placeholder", "占位", [1.0])])
    matched_topic = _payload("matched", "项目进展", [1.0])["topic"]
    other_topic = _payload("other", "项目进展", [1.0])["topic"]
    retriever = _FixedScoreRetriever(
        store,
        [
            TopicRecallResult(
                matched_topic,
                0.40,
                0.40,
                0.40,
                0.0,
                actors=[
                    {
                        "actor_id": actor_id,
                        "relation_type": "subject",
                        "resolution_status": "resolved",
                    }
                ],
            ),
            TopicRecallResult(other_topic, 0.42, 0.42, 0.42, 0.0),
        ],
    )
    pipeline = TopicRecallPipeline(
        retriever,
        {
            "recall_actor_match_boost": 0.04,
            "recall_min_relevance": 0.0,
            "recall_relative_floor": 0.0,
        },
    )

    outcome = await pipeline.search(
        branches=[RecallQueryBranch("current", "项目", 1.0, "user")],
        memory_space_id="space-1",
        final_k=2,
        current_actor_ids={actor_id},
    )

    assert [item.topic_uid for item in outcome.results] == ["matched", "other"]
    assert outcome.results[0].base_relevance_score == pytest.approx(0.40)
    assert outcome.results[0].relevance_score == pytest.approx(0.40)
    assert outcome.results[0].current_relevance == pytest.approx(0.40)
    assert outcome.results[0].actor_match_boost == pytest.approx(0.04)
    assert outcome.results[0].matched_actor_ids == [actor_id]


@pytest.mark.asyncio
async def test_topic_fragment_supplements_only_serve_role_anchored_rows():
    topic = _payload("weather", "上海雷雨", [1.0, 0.0])["topic"]
    safe = TopicFragmentDraft(
        fragment_uid="safe-fragment",
        run_uid="run-1",
        candidate_group_uid="group-1",
        memory_space_id="space-1",
        label="上海雷雨出行",
        summary="我提醒空雨携带雨具",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[{"content": "空雨决定携带雨具"}],
        embedding=[1.0, 0.0],
        embedding_signature=_fragment_signature([1.0, 0.0]),
        metadata={"narrative_schema_version": "first_person_assistant_roles_v2"},
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
        embedding_signature=_fragment_signature([1.0, 0.0]),
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
    assert "我提醒空雨" in outcome.results[0].content


@pytest.mark.asyncio
async def test_fragment_candidates_rerank_once_without_changing_current_relevance():
    topic = _payload("weather", "上海雷雨", [1.0, 0.0])["topic"]
    fragments = []
    for index, embedding in enumerate(([1.0, 0.0], [0.9, 0.1]), 1):
        fragment = TopicFragmentDraft(
            fragment_uid=f"fragment-{index}",
            run_uid="run-1",
            candidate_group_uid="group-1",
            memory_space_id="space-1",
            label=f"雷雨片段 {index}",
            summary=f"上海雷雨出行提醒 {index}",
            timeline_uids=[f"timeline-{index}"],
            source_revisions={f"timeline-{index}": 1},
            facts=[{"content": "携带雨具"}],
            embedding=embedding,
            embedding_signature=_fragment_signature(embedding),
            metadata={"narrative_schema_version": "first_person_assistant_roles_v3"},
        )
        fragments.append(
            {"topic_uid": "weather", "fragment": fragment, "sources": []}
        )
    reranker = _CountingReranker([0.0, 1.0])
    store = _Store([_payload("weather", "上海雷雨", [1.0, 0.0])], fragments)
    retriever = TopicRetriever(
        store,
        embedding_provider=_Embedding(),
        rerank_provider=reranker,
        config={"recall_use_rerank": True, "recall_rerank_weight": 0.35},
    )
    pipeline = TopicRecallPipeline(
        retriever,
        {
            "recall_use_rerank": True,
            "recall_rerank_weight": 0.35,
            "fragment_min_relevance": 0.0,
            "fragment_relative_floor": 0.0,
        },
    )
    parent = TopicRecallResult(
        topic,
        0.9,
        0.9,
        0.9,
        0.0,
        current_relevance=0.9,
    )

    outcome = await pipeline.search_fragment_supplements(
        branches=[
            RecallQueryBranch("current", "上海天气", 1.0, "user"),
            RecallQueryBranch("recent_user", "之前的旅行", 0.45, "user"),
        ],
        topic_results=[parent],
        limit=2,
    )

    assert reranker.calls == 1
    assert sorted(item.rerank_rank for item in outcome.results) == [1, 2]
    assert all(
        item.relevance_score == pytest.approx(item.current_relevance)
        for item in outcome.results
    )


@pytest.mark.asyncio
async def test_fragment_reuses_query_vectors_and_keeps_facts_when_body_duplicates_parent():
    payload = _payload("wage", "工资补发", [1.0, 0.0])
    topic = payload["topic"]
    topic.summary = "工资少发六百元，计划下月补回。"
    fragment = TopicFragmentDraft(
        fragment_uid="wage-fragment",
        run_uid="run-1",
        candidate_group_uid="group-1",
        memory_space_id="space-1",
        label="工资补发",
        summary="工资少发六百元，计划下月补回。",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[
            {"content": "工资少发六百元"},
            {"content": "六月实际工作二十天"},
            {"content": "合同日薪为三百元"},
            {"content": "计划在八月补发"},
        ],
        embedding=[1.0, 0.0],
        embedding_signature=_fragment_signature([1.0, 0.0]),
        metadata={"narrative_schema_version": "first_person_assistant_roles_v3"},
    )
    store = _Store(
        [payload],
        [{"topic_uid": "wage", "fragment": fragment, "sources": []}],
    )
    embedding = _Embedding()
    retriever = TopicRetriever(
        store,
        embedding_provider=embedding,
        config={"recall_use_rerank": False},
    )
    pipeline = TopicRecallPipeline(
        retriever,
        {
            "recall_use_rerank": False,
            "recall_min_relevance": 0.0,
            "recall_relative_floor": 0.0,
            "fragment_min_relevance": 0.0,
            "fragment_relative_floor": 0.0,
        },
    )
    branches = [RecallQueryBranch("current", "上海天气", 1.0, "user")]
    topic_outcome = await pipeline.search(
        branches=branches,
        memory_space_id="space-1",
        final_k=1,
    )
    fragment_outcome = await pipeline.search_fragment_supplements(
        branches=branches,
        topic_results=topic_outcome.results,
        limit=1,
        query_vectors=topic_outcome.query_vectors,
    )

    assert embedding.calls == 1
    assert len(fragment_outcome.results) == 1
    assert fragment_outcome.available_count == 1
    assert fragment_outcome.duplicate_parent_count == 1
    result = fragment_outcome.results[0]
    assert result.body_suppressed is True
    assert result.fact_contents == [
        "工资少发六百元",
        "六月实际工作二十天",
        "合同日薪为三百元",
        "计划在八月补发",
    ]
    assert result.content.startswith("Topic 片段补充事实: 工资少发六百元")
    assert "六月实际工作二十天" in result.content
    assert "合同日薪为三百元" in result.content
    assert "计划在八月补发" in result.content
    assert "计划下月补回" not in result.content
    assert result.filter_reason is None
    assert result.to_dict()["fact_count"] == 4


@pytest.mark.asyncio
async def test_fragment_skips_parent_duplicate_body_only_when_it_has_no_facts():
    payload = _payload("wage", "工资补发", [1.0, 0.0])
    topic = payload["topic"]
    topic.summary = "工资少发六百元，计划下月补回。"
    fragment = TopicFragmentDraft(
        fragment_uid="wage-fragment",
        run_uid="run-1",
        candidate_group_uid="group-1",
        memory_space_id="space-1",
        label="工资补发",
        summary="工资少发六百元，计划下月补回。",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[],
        embedding=[1.0, 0.0],
        embedding_signature=_fragment_signature([1.0, 0.0]),
        metadata={"narrative_schema_version": "first_person_assistant_roles_v3"},
    )
    store = _Store(
        [payload],
        [{"topic_uid": "wage", "fragment": fragment, "sources": []}],
    )
    pipeline = TopicRecallPipeline(
        TopicRetriever(
            store,
            embedding_provider=_Embedding(),
            config={"recall_use_rerank": False},
        ),
        {
            "recall_use_rerank": False,
            "recall_min_relevance": 0.0,
            "recall_relative_floor": 0.0,
            "fragment_min_relevance": 0.0,
            "fragment_relative_floor": 0.0,
        },
    )
    branches = [RecallQueryBranch("current", "工资", 1.0, "user")]
    topic_outcome = await pipeline.search(
        branches=branches,
        memory_space_id="space-1",
        final_k=1,
    )
    fragment_outcome = await pipeline.search_fragment_supplements(
        branches=branches,
        topic_results=topic_outcome.results,
        limit=1,
        query_vectors=topic_outcome.query_vectors,
    )

    assert fragment_outcome.results == []
    assert fragment_outcome.duplicate_parent_count == 1
    assert (
        fragment_outcome.candidates[0].filter_reason
        == "duplicate_parent_without_facts"
    )


@pytest.mark.asyncio
async def test_source_timeline_access_is_bounded_per_consumed_topic_and_deduplicated():
    first = _payload(
        "first",
        "第一个话题",
        [1.0, 0.0],
        sources=[
            {
                "timeline_uid": f"timeline-{index}",
                "contribution_weight": float(10 - index),
                "semantic_similarity": 0.5,
            }
            for index in range(1, 6)
        ],
    )
    second = _payload(
        "second",
        "第二个话题",
        [0.9, 0.1],
        sources=[
            {"timeline_uid": "timeline-2", "contribution_weight": 1.0},
            {"timeline_uid": "timeline-6", "contribution_weight": 0.9},
            {"timeline_uid": "timeline-7", "contribution_weight": 0.8},
            {"timeline_uid": "timeline-8", "contribution_weight": 0.7},
        ],
    )
    store = _Store([first, second])
    pipeline = TopicRecallPipeline(
        _FixedScoreRetriever(store, []), {"recall_use_rerank": False}
    )
    results = [
        TopicRecallResult(
            topic=payload["topic"],
            relevance_score=0.8,
            final_score=0.8,
            embedding_score=0.8,
            keyword_score=0.0,
            sources=payload["sources"],
        )
        for payload in (first, second)
    ]

    document_ids = await pipeline.source_timeline_document_ids(results, [])

    # Each Topic contributes at most three sources. The repeated timeline-2 is
    # refreshed once, so one broad Topic cannot keep its entire history alive.
    assert document_ids == [1, 2, 3, 6, 7]
