from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import aiosqlite
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
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.retrieval.recall_pipeline import (
    RecallPipeline,
    RecallQueryBranch,
)
from astrbot_plugin_livingmemory.core.retrieval.temporal_constraint import (
    TemporalConstraint,
    matching_sources,
)
from astrbot_plugin_livingmemory.core.retrieval.topic_recall_pipeline import (
    TopicRecallPipeline,
)
from astrbot_plugin_livingmemory.core.retrieval.topic_retriever import (
    TopicRecallResult,
    TopicRetriever,
)
from astrbot_plugin_livingmemory.storage.memory_identity_store import (
    MemoryIdentityStore,
)


def _ts(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def test_temporal_constraint_parses_offsets_and_closed_boundaries():
    utc = TemporalConstraint.from_payload(
        {
            "mode": "range",
            "start": "2026-07-01T00:00:00Z",
            "end": "2026-07-02T00:00:00Z",
        }
    )
    offset = TemporalConstraint.from_payload(
        {
            "mode": "range",
            "start": "2026-07-01T08:00:00+08:00",
            "end": "2026-07-02T08:00:00+08:00",
        }
    )
    assert utc is not None and offset is not None
    assert utc.start_at == offset.start_at
    assert utc.end_at == offset.end_at
    assert utc.overlaps(_ts("2026-06-30T23:00:00"), utc.start_at)
    assert utc.overlaps(utc.end_at, _ts("2026-07-03T00:00:00"))
    assert not utc.overlaps(_ts("2026-06-01T00:00:00"), _ts("2026-06-30T23:59:59"))


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "unknown", "start": "2026-07-01T00:00:00Z"},
        {"mode": "range"},
        {"mode": "range", "start": "2026-07-01T00:00:00"},
        {
            "mode": "range",
            "start": "2026-07-02T00:00:00Z",
            "end": "2026-07-01T00:00:00Z",
        },
        {"mode": "earliest", "order": "sideways"},
    ],
)
def test_temporal_constraint_rejects_invalid_payloads(payload):
    with pytest.raises(ValueError):
        TemporalConstraint.from_payload(payload)


def test_topic_range_matches_discrete_sources_instead_of_wide_envelope():
    constraint = TemporalConstraint.from_payload(
        {
            "mode": "range",
            "start": "2026-07-05T00:00:00Z",
            "end": "2026-07-05T23:59:59Z",
        }
    )
    sources = [
        {"timeline_uid": "t-1", "started_at": _ts("2026-07-01T10:00:00"), "ended_at": _ts("2026-07-01T11:00:00")},
        {"timeline_uid": "t-2", "started_at": _ts("2026-07-10T10:00:00"), "ended_at": _ts("2026-07-10T11:00:00")},
    ]
    assert constraint is not None
    assert matching_sources(constraint, sources) == []


@pytest.mark.asyncio
async def test_identity_store_distinguishes_real_source_time_from_fallback(tmp_path):
    db_path = tmp_path / "memory.db"
    store = MemoryIdentityStore(str(db_path))
    await store.initialize()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, metadata TEXT)"
        )
        await db.executemany(
            "INSERT INTO documents (id, metadata) VALUES (?, ?)",
            [(1, "{}"), (2, "{}")],
        )
        await db.commit()
    for document_id in (1, 2):
        await store.upsert_memory(
            memory_uid=f"timeline-{document_id}",
            document_id=document_id,
            memory_layer="timeline",
            memory_space_id="space-1",
            revision=1,
            created_at=100.0 + document_id,
        )
    await store.upsert_source_span(
        "timeline-1",
        {"session_id": "s-1", "start_index": 0, "end_index": 3},
        fallback_time=101.0,
    )
    await store.upsert_source_span(
        "timeline-2",
        {"session_id": "s-1", "started_at": 200.0},
        fallback_time=999.0,
    )
    anchors = await store.get_time_anchors_by_document_ids([1, 2])
    assert anchors[1]["time_basis"] == "timeline_created_at"
    assert anchors[1]["time_fallback"] is True
    assert anchors[1]["started_at"] == 101.0
    assert anchors[2]["time_basis"] == "timeline_source_span"
    assert anchors[2]["time_fallback"] is False
    assert anchors[2]["started_at"] == anchors[2]["ended_at"] == 200.0


def _timeline_result(memory_id: int, relevance: float) -> HybridResult:
    return HybridResult(
        doc_id=memory_id,
        final_score=relevance,
        rrf_score=relevance,
        bm25_score=0.0,
        vector_score=relevance,
        content=f"memory {memory_id}",
        metadata={},
        score_breakdown={"document_vector_score": relevance},
    )


class _TimelineIdentity:
    def __init__(self, anchors):
        self.anchors = anchors
        self.list_calls = 0

    async def list_timeline_document_ids(self, **kwargs):
        self.list_calls += 1
        return list(self.anchors)

    async def get_time_anchors_by_document_ids(self, document_ids):
        return {item: self.anchors[item] for item in document_ids}


class _TimelineEngine:
    def __init__(self, results, anchors):
        self.results = results
        self.memory_identity_store = _TimelineIdentity(anchors)
        self.requested_k = 0

    async def search_memories(self, **kwargs):
        self.requested_k = kwargs["k"]
        return [replace(item, metadata=dict(item.metadata)) for item in self.results[: kwargs["k"]]]


@pytest.mark.asyncio
async def test_timeline_range_expands_before_semantic_top_k_and_filters():
    target_time = _ts("2026-07-20T12:00:00")
    results = [_timeline_result(index, 0.99 - index / 1000) for index in range(1, 31)]
    results.append(_timeline_result(31, 0.61))
    anchors = {
        index: {
            "memory_uid": f"t-{index}",
            "started_at": _ts("2026-06-01T12:00:00") if index < 31 else target_time,
            "ended_at": _ts("2026-06-01T13:00:00") if index < 31 else target_time,
            "time_basis": "timeline_source_span",
            "time_fallback": False,
        }
        for index in range(1, 32)
    }
    engine = _TimelineEngine(results, anchors)
    constraint = TemporalConstraint.from_payload(
        {
            "mode": "range",
            "start": "2026-07-20T00:00:00Z",
            "end": "2026-07-20T23:59:59Z",
        }
    )
    outcome = await RecallPipeline(engine).search(
        current_query="memory",
        final_k=1,
        temporal=constraint,
        track_access=False,
    )
    assert engine.requested_k == 31
    assert [item.doc_id for item in outcome.results] == [31]
    assert outcome.temporal_suppressed == 30
    assert outcome.results[0].metadata["matched_source_uids"] == ["t-31"]


@pytest.mark.asyncio
async def test_timeline_without_temporal_constraint_does_not_load_time_index():
    engine = _TimelineEngine([_timeline_result(1, 0.8)], {})
    await RecallPipeline(engine).search(
        current_query="memory",
        final_k=1,
        track_access=False,
    )
    assert engine.memory_identity_store.list_calls == 0


class _Embedding:
    async def get_embeddings(self, texts):
        return [[1.0, 0.0] for _ in texts]


class _TopicStore:
    def __init__(self, payloads, fragments=None):
        self.payloads = payloads
        self.fragments = fragments or []

    async def list_topic_recall_payloads(self, memory_space_id, limit=1000, topic_uids=None):
        rows = self.payloads
        if topic_uids:
            rows = [item for item in rows if item["topic"].topic_uid in topic_uids]
        return rows[:limit]

    async def list_active_fragments_for_topics(self, topic_uids):
        return [
            item for item in self.fragments if item["topic_uid"] in topic_uids
        ]


def _topic_payload(uid: str, source_times: list[tuple[str, float]]) -> dict:
    embedding = [1.0, 0.0]
    return {
        "topic": TopicMemory(
            topic_uid=uid,
            memory_space_id="space-1",
            title=uid,
            summary=f"{uid} summary",
            importance=0.7,
            confidence=0.9,
            embedding_signature=make_embedding_signature(
                _Embedding(),
                dimension=2,
                input_format_version=TOPIC_CENTROID_EMBEDDING_FORMAT,
                generated_at=1.0,
            ),
            metadata={"embedding": embedding, "keywords": [uid]},
        ),
        "atoms": [],
        "actors": [],
        "sources": [
            {
                "timeline_uid": source_uid,
                "started_at": source_time,
                "ended_at": source_time,
                "time_basis": "timeline_source_span",
                "time_fallback": False,
            }
            for source_uid, source_time in source_times
        ],
    }


@pytest.mark.asyncio
async def test_topic_pipeline_filters_each_source_span_before_scoring():
    july_1 = _ts("2026-07-01T12:00:00")
    july_5 = _ts("2026-07-05T12:00:00")
    july_10 = _ts("2026-07-10T12:00:00")
    store = _TopicStore(
        [
            _topic_payload("wide-gap", [("t-1", july_1), ("t-10", july_10)]),
            _topic_payload("actual-match", [("t-5", july_5)]),
        ]
    )
    retriever = TopicRetriever(store, embedding_provider=_Embedding(), config={})
    pipeline = TopicRecallPipeline(
        retriever,
        {
            "recall_min_relevance": 0.1,
            "recall_relative_floor": 0.1,
            "recall_selection_relative_floor": 0.1,
            "recall_use_rerank": False,
        },
    )
    constraint = TemporalConstraint.from_payload(
        {
            "mode": "range",
            "start": "2026-07-05T00:00:00Z",
            "end": "2026-07-05T23:59:59Z",
        }
    )
    outcome = await pipeline.search(
        branches=[RecallQueryBranch("current", "event", 1.0, "user")],
        memory_space_id="space-1",
        final_k=3,
        temporal=constraint,
    )
    assert [item.topic_uid for item in outcome.results] == ["actual-match"]
    assert outcome.results[0].matched_source_uids == ["t-5"]
    assert outcome.temporal_suppressed == 1


@pytest.mark.asyncio
async def test_fragment_pipeline_filters_source_spans_before_scoring():
    july_5 = _ts("2026-07-05T12:00:00")
    july_10 = _ts("2026-07-10T12:00:00")
    topic = _topic_payload("event", [("t-5", july_5)])["topic"]

    def fragment(uid: str, timeline_uid: str) -> TopicFragmentDraft:
        return TopicFragmentDraft(
            fragment_uid=uid,
            run_uid="run-1",
            candidate_group_uid="group-1",
            memory_space_id="space-1",
            label=f"event {uid}",
            summary=f"event summary {uid}",
            timeline_uids=[timeline_uid],
            source_revisions={timeline_uid: 1},
            facts=[{"content": f"event fact {uid}"}],
            embedding=[1.0, 0.0],
            embedding_signature=make_embedding_signature(
                _Embedding(),
                dimension=2,
                input_format_version=TOPIC_FRAGMENT_EMBEDDING_FORMAT,
                generated_at=1.0,
            ),
            metadata={"narrative_schema_version": "first_person_assistant_roles_v5"},
        )

    store = _TopicStore(
        [_topic_payload("event", [("t-5", july_5)])],
        [
            {
                "topic_uid": "event",
                "fragment": fragment("matched", "t-5"),
                "sources": [
                    {
                        "timeline_uid": "t-5",
                        "started_at": july_5,
                        "ended_at": july_5,
                        "time_basis": "timeline_source_span",
                        "time_fallback": False,
                    }
                ],
            },
            {
                "topic_uid": "event",
                "fragment": fragment("outside", "t-10"),
                "sources": [
                    {
                        "timeline_uid": "t-10",
                        "started_at": july_10,
                        "ended_at": july_10,
                        "time_basis": "timeline_source_span",
                        "time_fallback": False,
                    }
                ],
            },
        ],
    )
    pipeline = TopicRecallPipeline(
        TopicRetriever(store, embedding_provider=_Embedding(), config={}),
        {
            "recall_use_rerank": False,
            "fragment_min_relevance": 0.0,
            "fragment_relative_floor": 0.0,
        },
    )
    constraint = TemporalConstraint.from_payload(
        {
            "mode": "range",
            "start": "2026-07-05T00:00:00Z",
            "end": "2026-07-05T23:59:59Z",
        }
    )
    outcome = await pipeline.search_fragment_supplements(
        branches=[RecallQueryBranch("current", "event", 1.0, "user")],
        topic_results=[TopicRecallResult(topic, 0.9, 0.9, 0.9, 0.0)],
        limit=2,
        temporal=constraint,
    )
    assert outcome.available_count == 1
    assert [item.fragment_uid for item in outcome.results] == ["matched"]
    assert outcome.results[0].matched_source_uids == ["t-5"]
    assert outcome.temporal_suppressed == 1
