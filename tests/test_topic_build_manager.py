from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.managers.topic_build_manager import (
    TopicBuildManager,
    TopicBuildValidationError,
)
from astrbot_plugin_livingmemory.core.models.identity_profile import (
    AuthoritativeIdentityStore,
)
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.managers.topic_maintenance_manager import (
    TopicMaintenanceManager,
)
from astrbot_plugin_livingmemory.storage.topic_memory_store import TopicMemoryStore
from astrbot_plugin_livingmemory.storage.memory_identity_store import MemoryIdentityStore
from astrbot_plugin_livingmemory.core.models.topic_memory import (
    TimelineTopicCandidate,
    TopicCandidateGroup,
    TopicFragmentDraft,
    TopicMaintenanceMode,
    TopicMemory,
)
from tests.test_topic_maintenance_manager import _create_timeline_db


class _Response:
    def __init__(self, payload):
        self.completion_text = json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_selected_unindexed_build_does_not_expand_to_full_without_topics():
    store = SimpleNamespace(list_topics=AsyncMock(return_value=[]))
    candidate_manager = SimpleNamespace(
        start_scan=AsyncMock(return_value={"run_uid": "run-selected"})
    )
    manager = TopicBuildManager(
        ":memory:",
        store,
        candidate_manager,
    )
    manager.build_from_scan = AsyncMock(
        return_value={"status": "completed", "run_uid": "run-selected"}
    )

    result = await manager.build_space(
        "space-1",
        mode=TopicMaintenanceMode.INCREMENTAL,
        timeline_uids=["timeline-1", "timeline-2"],
    )

    assert result["status"] == "completed"
    kwargs = candidate_manager.start_scan.await_args.kwargs
    assert kwargs["mode"] is TopicMaintenanceMode.INCREMENTAL
    assert kwargs["since"] is None
    assert kwargs["timeline_uids"] == ["timeline-1", "timeline-2"]
    assert kwargs["only_unindexed"] is True
    store.list_topics.assert_awaited_once()


@pytest.mark.asyncio
async def test_incremental_build_uses_expanded_scope_and_shared_scan_pipeline():
    store = SimpleNamespace(list_topics=AsyncMock(return_value=[SimpleNamespace()]))
    seeds = [SimpleNamespace(memory_uid="timeline-new")]
    scope = {
        "seed_timeline_uids": ["timeline-new"],
        "timeline_uids": ["timeline-old", "timeline-new"],
        "affected_topic_uids": ["topic-existing"],
        "time_cluster_keys": {
            "timeline-old": "cluster-1",
            "timeline-new": "cluster-1",
        },
        "scope_limited": False,
    }
    candidate_manager = SimpleNamespace(
        load_candidates=AsyncMock(return_value=seeds),
        prepare_incremental_scope=AsyncMock(return_value=scope),
        start_scan=AsyncMock(return_value={"run_uid": "run-local"}),
    )
    manager = TopicBuildManager(
        ":memory:",
        store,
        candidate_manager,
        config={"time_gap_hours": 6.0, "candidate_similarity_threshold": 0.52},
    )
    manager.build_from_scan = AsyncMock(
        return_value={"status": "completed", "run_uid": "run-local"}
    )

    result = await manager.build_space(
        "space-1",
        mode=TopicMaintenanceMode.INCREMENTAL,
        timeline_uids=["timeline-new"],
    )

    assert result["status"] == "completed"
    candidate_manager.load_candidates.assert_awaited_once_with(
        "space-1",
        since=None,
        timeline_uids=["timeline-new"],
        only_unindexed=True,
    )
    kwargs = candidate_manager.start_scan.await_args.kwargs
    assert kwargs["mode"] is TopicMaintenanceMode.INCREMENTAL
    assert kwargs["timeline_uids"] == ["timeline-old", "timeline-new"]
    assert kwargs["only_unindexed"] is False
    assert kwargs["run_config"]["time_cluster_keys"] == scope["time_cluster_keys"]
    assert kwargs["run_config"]["topic_settings"] == manager.config
    assert kwargs["run_metadata"]["incremental_scope"] == scope
    assert kwargs["run_metadata"]["pipeline"] == "shared_full_pipeline"


class _GroundedLLM:
    def __init__(
        self, *, hallucinate: bool = False, unknown_atom_fingerprint: bool = False
    ):
        self.hallucinate = hallucinate
        self.unknown_atom_fingerprint = unknown_atom_fingerprint
        self.provider_config = {"id": "fake-llm", "model": "test"}

    async def text_chat(self, *, prompt: str, system_prompt: str):
        input_text = prompt.split("INPUT:\n", 1)[1]
        input_text = input_text.split("\n\nCORRECTION REQUIRED:", 1)[0]
        inputs = json.loads(input_text)
        if prompt.startswith("Split"):
            fragments = []
            for item in inputs["timelines"]:
                timeline_ref = (
                    "unknown-timeline" if self.hallucinate else item["ref"]
                )
                travel = "京都" in item["summary"]
                source_fact = item["source_facts"][0]
                fragments.append(
                    {
                        "label": "京都旅行" if travel else "Rust 学习",
                        "summary": item["summary"],
                        "importance": 0.8 if travel else 0.6,
                        "confidence": 0.9,
                        "timeline_refs": [timeline_ref],
                        "keywords": ["京都" if travel else "Rust"],
                        "facts": [
                            {
                                "type": "planned" if travel else "factual",
                                "content": source_fact["content"],
                                "importance": 0.8,
                                "confidence": 0.9,
                                "source_refs": [source_fact["ref"]],
                            }
                        ],
                    }
                )
            return _Response({"fragments": fragments})

        atoms = []
        for item in inputs["fragments"]:
            for fact in item["facts"]:
                atoms.append(
                    {
                        "type": fact["type"],
                        "content": fact["content"],
                        "importance": fact["importance"],
                        "confidence": fact["confidence"],
                        "source_fact_refs": [fact["ref"]],
                    }
                )
        return _Response(
            {
                "title": inputs["fragments"][0]["label"],
                "summary": "；".join(
                    item["summary"] for item in inputs["fragments"]
                ),
                "importance": 0.8,
                "confidence": 0.9,
                "atoms": atoms,
            }
        )


class _ConcurrentGroundedLLM(_GroundedLLM):
    def __init__(self):
        super().__init__()
        self.active_calls = 0
        self.max_active_calls = 0
        self.active_fragment_calls = 0
        self.max_active_fragment_calls = 0

    async def text_chat(self, *, prompt: str, system_prompt: str):
        is_fragment = prompt.startswith("Split")
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        if is_fragment:
            self.active_fragment_calls += 1
            self.max_active_fragment_calls = max(
                self.max_active_fragment_calls,
                self.active_fragment_calls,
            )
        try:
            await asyncio.sleep(0.02)
            return await super().text_chat(prompt=prompt, system_prompt=system_prompt)
        finally:
            self.active_calls -= 1
            if is_fragment:
                self.active_fragment_calls -= 1


class _ToggleFailingSynthesisLLM(_GroundedLLM):
    def __init__(self):
        super().__init__()
        self.fail_synthesis = True
        self.fragment_calls = 0

    async def text_chat(self, *, prompt: str, system_prompt: str):
        if prompt.startswith("Split"):
            self.fragment_calls += 1
        elif self.fail_synthesis:
            raise TimeoutError("simulated provider timeout")
        return await super().text_chat(prompt=prompt, system_prompt=system_prompt)


class _CorrectingSynthesisLLM(_GroundedLLM):
    def __init__(self):
        super().__init__()
        self.synthesis_calls = 0

    async def text_chat(self, *, prompt: str, system_prompt: str):
        if not prompt.startswith("Synthesize"):
            return await super().text_chat(prompt=prompt, system_prompt=system_prompt)
        self.synthesis_calls += 1
        if "CORRECTION REQUIRED:" not in prompt:
            return _Response(
                {
                    "title": "错误输出",
                    "summary": "引用了不存在的事实",
                    "importance": 0.5,
                    "confidence": 0.5,
                    "atoms": [
                        {
                            "type": "factual",
                            "content": "错误事实",
                            "importance": 0.5,
                            "confidence": 0.5,
                            "source_fact_refs": ["F999"],
                        }
                    ],
                }
            )
        return await super().text_chat(prompt=prompt, system_prompt=system_prompt)


class _ComponentReviewLLM:
    provider_config = {"id": "review-llm", "model": "test"}

    def __init__(self, *, invalid_first: bool = False, fail: bool = False):
        self.invalid_first = invalid_first
        self.fail = fail
        self.calls = 0

    async def text_chat(self, *, prompt: str, system_prompt: str):
        self.calls += 1
        if self.fail:
            raise TimeoutError("component review unavailable")
        input_text = prompt.split("INPUT:\n", 1)[1]
        input_text = input_text.split("\n\nCORRECTION REQUIRED:", 1)[0]
        payload = json.loads(input_text)
        refs = [item["ref"] for item in payload["fragments"]]
        if self.invalid_first and self.calls == 1:
            refs = refs[:-1]
        midpoint = max(1, len(refs) // 2)
        groups = [
            {"label": "前半", "reason": "测试拆分", "fragment_refs": refs[:midpoint]},
            {"label": "后半", "reason": "测试拆分", "fragment_refs": refs[midpoint:]},
        ]
        return _Response(
            {"groups": [group for group in groups if group["fragment_refs"]]}
        )


class _CheckpointStore:
    def __init__(self):
        self.checkpoints = {}

    async def get_build_checkpoint(self, run_uid, checkpoint_key):
        return self.checkpoints.get((run_uid, checkpoint_key))

    async def save_build_checkpoint(
        self,
        *,
        run_uid,
        checkpoint_key,
        stage,
        input_hash,
        payload,
        metadata=None,
    ):
        self.checkpoints[(run_uid, checkpoint_key)] = {
            "stage": stage,
            "input_hash": input_hash,
            "payload": payload,
            "metadata": metadata or {},
        }


class _Embedding:
    async def get_embeddings(self, texts: list[str]):
        return [[1.0, 0.0] if "京都" in text else [0.0, 1.0] for text in texts]


class _RerankResult:
    def __init__(self, index: int, relevance_score: float):
        self.index = index
        self.relevance_score = relevance_score


class _AllPassReranker:
    provider_config = {"id": "fake-rerank", "model": "test"}

    async def rerank(self, _query: str, documents: list[str], top_n: int | None = None):
        limit = len(documents) if top_n is None else min(len(documents), top_n)
        return [_RerankResult(index, 0.9) for index in range(limit)]


class _ConcurrentReranker(_AllPassReranker):
    def __init__(self):
        self.active_calls = 0
        self.max_active_calls = 0
        self.calls = 0

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None):
        self.calls += 1
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0.01)
            return await super().rerank(query, documents, top_n)
        finally:
            self.active_calls -= 1


class _PartiallyFailingReranker(_AllPassReranker):
    async def rerank(self, query: str, documents: list[str], top_n: int | None = None):
        await asyncio.sleep(0)
        if "片段 1" in query:
            raise RuntimeError("temporary rerank failure")
        await asyncio.sleep(0.02)
        return await super().rerank(query, documents, top_n)


class _AsymmetricRankReranker:
    """Expose a strong reciprocal pair whose second direction ranks third."""

    provider_config = {"id": "rank-rerank", "model": "test"}

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None):
        query_index = int(query.split("片段 ", 1)[1].split(" ", 1)[0])
        document_indexes = [
            int(document.split("片段 ", 1)[1].split(" ", 1)[0])
            for document in documents
        ]
        scores = {document_index: 0.1 for document_index in document_indexes}
        if query_index == 0 and 1 in scores:
            scores[1] = 0.999
        elif query_index == 1 and 0 in scores:
            higher_ranked = [item for item in document_indexes if item != 0][:2]
            for offset, document_index in enumerate(higher_ranked):
                scores[document_index] = 0.999 - offset * 0.001
            scores[0] = 0.997
        ranked = sorted(
            enumerate(document_indexes),
            key=lambda item: (-scores[item[1]], item[1]),
        )
        limit = len(ranked) if top_n is None else min(len(ranked), top_n)
        return [
            _RerankResult(local_index, scores[document_index])
            for local_index, document_index in ranked[:limit]
        ]


class _FragmentStore:
    def __init__(self):
        self.fragments = []

    async def begin_group_job(self, *_args, **_kwargs):
        return True

    async def replace_group_fragments(self, _run_uid, _group_uid, fragments):
        self.fragments = fragments

    async def finish_group_job(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_full_build_splits_merges_and_materializes_exact_sources(tmp_path: Path):
    db_path, space_id = await _create_timeline_db(tmp_path)
    store = TopicMemoryStore(db_path)
    manager = TopicBuildManager(
        db_path,
        store,
        TopicMaintenanceManager(db_path, store),
        llm_provider=_GroundedLLM(),
        embedding_provider=_Embedding(),
        config={"fragment_similarity_threshold": 0.78},
    )

    result = await manager.build_space(space_id)

    assert result["status"] == "completed"
    assert result["fragment_count"] == 3
    assert result["topic_count"] == 2
    topics = await store.list_topics(space_id)
    travel = next(topic for topic in topics if topic.title == "京都旅行")
    provenance = await store.get_topic_provenance(travel.topic_uid)
    assert len(provenance["fragments"]) == 2
    assert all(fragment["facts"] for fragment in provenance["fragments"])
    assert {row["timeline_uid"] for row in provenance["links"]} == {
        "timeline-travel-1",
        "timeline-travel-2",
    }
    assert len(provenance["atoms"]) == 1
    sources_by_atom = {}
    for row in provenance["atom_sources"]:
        sources_by_atom.setdefault(row["topic_atom_uid"], set()).add(
            row["timeline_uid"]
        )
    assert next(iter(sources_by_atom.values())) == {
        "timeline-travel-1",
        "timeline-travel-2",
    }
    run = await store.get_maintenance_run(result["run_uid"])
    assert run["stage"] == "completed"
    assert run["status"] == "completed"
    matching = await store.get_build_checkpoint(result["run_uid"], "fragment_matching")
    assert matching["payload"]["matching_algorithm_version"] == 6
    assert "singleton_reason_counts" in matching["payload"]["audit"]


@pytest.mark.asyncio
async def test_reset_full_build_clears_old_snapshots_and_runs_before_rebuilding(
    tmp_path: Path,
):
    db_path, space_id = await _create_timeline_db(tmp_path)
    store = TopicMemoryStore(db_path)
    manager = TopicBuildManager(
        db_path,
        store,
        TopicMaintenanceManager(db_path, store),
        llm_provider=_GroundedLLM(),
        embedding_provider=_Embedding(),
        config={"fragment_similarity_threshold": 0.78},
    )

    first = await manager.build_space(space_id)
    first_topics = await store.list_topics(space_id)
    assert first_topics

    rebuilt = await manager.build_space(space_id, reset_topics=True)
    rebuilt_topics = await store.list_topics(space_id)

    assert rebuilt["reset"] == {"deleted_topics": len(first_topics), "deleted_runs": 1}
    assert rebuilt["run_uid"] != first["run_uid"]
    assert all(topic.revision == 1 for topic in rebuilt_topics)
    runs = await store.list_maintenance_runs(space_id)
    assert [run["run_uid"] for run in runs] == [rebuilt["run_uid"]]


@pytest.mark.asyncio
async def test_full_build_extracts_candidate_groups_concurrently_with_monotonic_progress(
    tmp_path: Path,
):
    db_path, space_id = await _create_timeline_db(tmp_path)
    store = TopicMemoryStore(db_path)
    llm = _ConcurrentGroundedLLM()
    manager = TopicBuildManager(
        db_path,
        store,
        TopicMaintenanceManager(db_path, store),
        llm_provider=llm,
        embedding_provider=_Embedding(),
        config={"llm_concurrency": 2, "fragment_extraction_batch_size": 12},
    )
    events: list[dict] = []

    result = await manager.build_space(space_id, progress_callback=events.append)

    assert result["status"] == "completed"
    assert llm.max_active_fragment_calls == 2
    fragment_events = [
        event for event in events if event.get("stage") == "fragment_extraction"
    ]
    assert fragment_events
    currents = [int(event["current"]) for event in fragment_events]
    assert currents == sorted(currents)
    assert max(int(event.get("active_group_count", 0)) for event in fragment_events) == 2
    assert all(
        int(event.get("group_concurrency", 0)) == 2
        for event in fragment_events
    )
    assert fragment_events[-1]["current"] == fragment_events[-1]["total"] == 2


@pytest.mark.asyncio
async def test_build_falls_back_when_fragment_output_has_unknown_timeline(tmp_path: Path):
    db_path, space_id = await _create_timeline_db(tmp_path)
    store = TopicMemoryStore(db_path)
    manager = TopicBuildManager(
        db_path,
        store,
        TopicMaintenanceManager(db_path, store),
        llm_provider=_GroundedLLM(hallucinate=True),
        embedding_provider=_Embedding(),
    )

    result = await manager.build_space(space_id)

    assert result["status"] == "completed"
    fragments = await store.list_fragments(run_uid=result["run_uid"])
    assert fragments
    assert all(fragment.metadata.get("deterministic_fallback") for fragment in fragments)
    assert await store.list_topics(space_id)


def _validate_single_fact(
    *, fact_content: str, atom_content: str, output_timeline_uid: str = "timeline-1"
):
    fingerprint = TopicMaintenanceManager.fingerprint_text(f"factual:{atom_content}")
    candidate = TimelineTopicCandidate(
        memory_uid="timeline-1",
        document_id=1,
        source_revision=1,
        memory_space_id="space-1",
        session_id="session-1",
        content=atom_content,
        summary=atom_content,
        atom_fingerprints=[fingerprint],
        atom_contents=[atom_content],
        time_cluster_key="cluster-1",
    )
    group = TopicCandidateGroup(
        run_uid="run-1",
        group_index=0,
        memory_space_id="space-1",
        label="测试",
        timeline_uids=[candidate.memory_uid],
        time_cluster_keys=[candidate.time_cluster_key],
        cohesion=1.0,
        group_uid="group-1",
    )
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
    )
    fragments = manager._validate_fragments(
        {
            "fragments": [
                {
                    "label": "测试",
                    "summary": fact_content,
                    "timeline_uids": [output_timeline_uid],
                    "facts": [
                        {
                            "content": fact_content,
                            "source_timeline_uids": [output_timeline_uid],
                            "source_atom_fingerprints": ["invented-fingerprint"],
                        }
                    ],
                }
            ]
        },
        "run-1",
        group,
        [candidate],
        "prompt-hash",
        "input-hash",
        "provider-1",
        "model-1",
    )
    return manager, candidate, fingerprint, fragments[0]


def test_validation_still_rejects_unknown_timeline_provenance():
    with pytest.raises(TopicBuildValidationError, match="unknown Timeline UID"):
        _validate_single_fact(
            fact_content="来源原子正文",
            atom_content="来源原子正文",
            output_timeline_uid="invented-timeline",
        )


def test_validation_drops_unknown_atom_fingerprint_without_losing_fact_source():
    manager, candidate, _, fragment = _validate_single_fact(
        fact_content="概括后的事实",
        atom_content="来源原子正文",
    )

    fact = fragment.facts[0]
    assert fact["source_timeline_uids"] == [candidate.memory_uid]
    assert fact["source_atom_fingerprints"] == []
    assert fragment.metadata["validation_repairs"] == [
        {
            "fact_index": 0,
            "dropped_unknown_atom_fingerprints": 1,
            "inferred_exact_atom_fingerprints": 0,
        }
    ]

    synthesis = {
        "title": "测试",
        "summary": "概括后的事实",
        "confidence": 0.8,
        "atoms": [
            {
                "type": "factual",
                "content": fact["content"],
                "fragment_uids": [fragment.fragment_uid],
                "source_fact_uids": [fact["fact_uid"]],
            }
        ],
    }
    _, _, _, sources = manager._materialize_snapshot(
        "run-1",
        "space-1",
        synthesis,
        [fragment],
        {candidate.memory_uid: candidate},
        None,
    )
    assert sources
    assert all(source.source_kind == "fact_fingerprint" for source in sources)


def test_validation_infers_exact_atom_fingerprint_deterministically():
    _, candidate, expected, fragment = _validate_single_fact(
        fact_content="来源原子正文",
        atom_content="来源原子正文",
    )

    assert fragment.facts[0]["source_atom_fingerprints"] == [expected]
    assert fragment.facts[0]["source_timeline_uids_by_fingerprint"] == {
        expected: [candidate.memory_uid]
    }


def test_fragment_validation_rejects_timeline_without_supporting_fact():
    manager = TopicBuildManager(":memory:", None, None)
    candidates = [
        TimelineTopicCandidate(
            memory_uid=f"timeline-{index}",
            document_id=index,
            source_revision=1,
            memory_space_id="space-1",
            session_id="session-1",
            content=f"事实 {index}",
            summary=f"事实 {index}",
            key_facts=[f"事实 {index}"],
        )
        for index in (1, 2)
    ]
    group = TopicCandidateGroup(
        run_uid="run-1",
        group_index=1,
        memory_space_id="space-1",
        label="测试",
        timeline_uids=[item.memory_uid for item in candidates],
        time_cluster_keys=[],
        cohesion=0.8,
        group_uid="group-1",
    )

    with pytest.raises(
        TopicBuildValidationError,
        match="Timeline refs without supporting facts",
    ):
        manager._validate_fragments(
            {
                "fragments": [
                    {
                        "label": "错误合并",
                        "summary": "第二条 Timeline 没有事实证据",
                        "timeline_uids": ["timeline-1", "timeline-2"],
                        "facts": [
                            {
                                "content": "事实 1",
                                "source_timeline_uids": ["timeline-1"],
                            }
                        ],
                    }
                ]
            },
            "run-1",
            group,
            candidates,
            "prompt-hash",
            "input-hash",
            "provider-1",
            "model-1",
        )


@pytest.mark.asyncio
async def test_fragment_matching_reports_its_own_progress_stage():
    manager = TopicBuildManager(":memory:", None, None)
    fragments = [
        TopicFragmentDraft(
            run_uid="run-1",
            candidate_group_uid="group-1",
            memory_space_id="space-1",
            label=f"片段 {index}",
            summary="测试",
            timeline_uids=[f"timeline-{index}"],
            source_revisions={f"timeline-{index}": 1},
            facts=[],
            embedding=[1.0, float(index)],
        )
        for index in range(3)
    ]
    events = []

    await manager._match_fragments(fragments, progress_callback=events.append)

    assert events
    assert all(event["stage"] == "fragment_matching" for event in events)
    assert events[-1]["current"] == events[-1]["total"] == 3


@pytest.mark.asyncio
async def test_component_review_splits_large_component_and_preserves_scope():
    llm = _ComponentReviewLLM()
    manager = TopicBuildManager(":memory:", None, None, llm_provider=llm)
    fragments = [_topic_fragment(index) for index in range(6)]

    groups = await manager._review_component_direct(fragments)

    assert groups == [
        ["fragment-0", "fragment-1", "fragment-2"],
        ["fragment-3", "fragment-4", "fragment-5"],
    ]
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_component_review_repairs_invalid_fragment_coverage_once():
    llm = _ComponentReviewLLM(invalid_first=True)
    manager = TopicBuildManager(":memory:", None, None, llm_provider=llm)
    fragments = [_topic_fragment(index) for index in range(6)]

    groups = await manager._review_component_direct(fragments)

    assert [uid for group in groups for uid in group] == [
        f"fragment-{index}" for index in range(6)
    ]
    assert llm.calls == 2


def test_component_review_rejects_unknown_duplicate_and_missing_refs():
    manager = TopicBuildManager(":memory:", None, None)
    fragments = [_topic_fragment(index) for index in range(3)]
    _, refs = manager._component_review_llm_context(fragments)

    with pytest.raises(TopicBuildValidationError, match="invalid fragment refs"):
        manager._decode_component_review_refs(
            {
                "groups": [
                    {"fragment_refs": ["P1", "P2"]},
                    {"fragment_refs": ["P2", "P999"]},
                ]
            },
            refs,
            fragments,
        )

    with pytest.raises(TopicBuildValidationError, match="did not cover"):
        manager._decode_component_review_refs(
            {"groups": [{"fragment_refs": ["P1", "P2"]}]},
            refs,
            fragments,
        )


@pytest.mark.asyncio
async def test_component_review_failure_falls_back_and_checkpoint_is_reused():
    store = _CheckpointStore()
    llm = _ComponentReviewLLM(fail=True)
    manager = TopicBuildManager(
        ":memory:",
        store,
        None,
        llm_provider=llm,
        config={"llm_max_retries": 1},
    )
    fragments = [_topic_fragment(index) for index in range(6)]

    first = await manager._review_component_checkpointed("run-1", fragments)
    calls_after_first = llm.calls
    second = await manager._review_component_checkpointed("run-1", fragments)

    assert first == second == [[f"fragment-{index}" for index in range(6)]]
    assert calls_after_first == 1
    assert llm.calls == calls_after_first
    checkpoint = next(iter(store.checkpoints.values()))
    assert checkpoint["stage"] == "component_review"
    assert checkpoint["metadata"]["fallback"] is True


@pytest.mark.asyncio
async def test_component_review_recomputes_an_invalid_checkpoint():
    store = _CheckpointStore()
    llm = _ComponentReviewLLM()
    manager = TopicBuildManager(
        ":memory:",
        store,
        None,
        llm_provider=llm,
    )
    fragments = [_topic_fragment(index) for index in range(6)]
    _, fragment_refs = manager._component_review_llm_context(fragments)
    component_key = hashlib.sha256(
        "\n".join(sorted(item.fragment_uid for item in fragments)).encode()
    ).hexdigest()
    provider_id, model_id = manager._provider_identity(llm)
    input_payload, _ = manager._component_review_llm_context(fragments)
    input_hash = manager._checkpoint_hash(
        {
            "prompt_version": "topic-component-review-v1-retrieval-boundary",
            "provider_id": provider_id,
            "model_id": model_id,
            "fragments": input_payload,
        }
    )
    store.checkpoints[("run-1", f"component_review:{component_key}")] = {
        "input_hash": input_hash,
        "payload": {"groups": [[fragment_refs["P1"]]]},
    }

    groups = await manager._review_component_checkpointed("run-1", fragments)

    assert groups == [
        ["fragment-0", "fragment-1", "fragment-2"],
        ["fragment-3", "fragment-4", "fragment-5"],
    ]
    assert llm.calls == 1
    checkpoint = store.checkpoints[("run-1", f"component_review:{component_key}")]
    assert checkpoint["payload"]["groups"] == groups


@pytest.mark.asyncio
async def test_component_review_reports_progress_and_skips_small_components():
    store = _CheckpointStore()
    llm = _ComponentReviewLLM()
    manager = TopicBuildManager(
        ":memory:",
        store,
        None,
        llm_provider=llm,
        config={
            "component_review_min_fragments": 6,
            "llm_concurrency": 2,
        },
    )
    fragments = [_topic_fragment(index) for index in range(8)]
    events = []

    groups = await manager._review_components_checkpointed(
        "run-1",
        fragments,
        [list(range(6)), [6, 7]],
        progress_callback=events.append,
    )

    assert groups == [[0, 1, 2], [3, 4, 5], [6, 7]]
    assert llm.calls == 1
    assert events
    assert all(event["stage"] == "component_review" for event in events)
    assert events[-1]["current"] == events[-1]["total"] == 2
    assert any(event.get("review_output_groups") == 2 for event in events)


@pytest.mark.asyncio
async def test_fragment_matching_does_not_merge_a_similarity_chain():
    manager = TopicBuildManager(":memory:", None, None)
    fragments = [_topic_fragment(index) for index in range(3)]
    fragments[0].label = "A"
    fragments[1].label = "B"
    fragments[2].label = "C"
    fragments[0].embedding = [1.0, 0.0]
    fragments[1].embedding = [0.8660254, 0.5]
    fragments[2].embedding = [0.5, 0.8660254]

    components, _ = await manager._match_fragments(fragments)

    assert sorted(map(len, components), reverse=True) == [2, 1]


def test_component_size_penalty_blocks_marginal_bridge_between_established_groups():
    embedding_scores = {
        (left, right): (0.9 if left // 4 == right // 4 else 0.659)
        for left in range(8)
        for right in range(left + 1, 8)
    }
    seed_edges = [
        (0.9, 0, 1),
        (0.9, 1, 2),
        (0.9, 2, 3),
        (0.9, 4, 5),
        (0.9, 5, 6),
        (0.9, 6, 7),
        (0.8, 3, 4),
    ]

    without_penalty = TopicBuildManager._cluster_fragment_edges(
        8,
        embedding_scores,
        seed_edges,
        minimum_pair_similarity=0.52,
        minimum_average_similarity=0.65,
    )
    with_penalty = TopicBuildManager._cluster_fragment_edges(
        8,
        embedding_scores,
        seed_edges,
        minimum_pair_similarity=0.52,
        minimum_average_similarity=0.65,
        size_cohesion_penalty=0.005,
    )

    assert sorted(map(len, without_penalty)) == [8]
    assert sorted(map(len, with_penalty)) == [4, 4]


def test_component_size_penalty_does_not_block_single_fragment_attachment():
    components = TopicBuildManager._cluster_fragment_edges(
        3,
        {(0, 1): 0.9, (0, 2): 0.65, (1, 2): 0.65},
        [(0.9, 0, 1), (0.8, 1, 2)],
        minimum_pair_similarity=0.52,
        minimum_average_similarity=0.65,
        size_cohesion_penalty=0.05,
    )

    assert sorted(map(len, components)) == [3]


@pytest.mark.asyncio
async def test_rerank_can_promote_candidates_but_not_unrelated_fragments():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        rerank_provider=_AllPassReranker(),
    )
    related = [_topic_fragment(1), _topic_fragment(2)]
    related[0].label = "A"
    related[1].label = "B"
    related[0].embedding = [1.0, 0.0]
    related[1].embedding = [0.7, 0.714142842]

    related_components, _ = await manager._match_fragments(related)

    unrelated = [_topic_fragment(3), _topic_fragment(4)]
    unrelated[0].label = "C"
    unrelated[1].label = "D"
    unrelated[0].embedding = [1.0, 0.0]
    unrelated[1].embedding = [0.6, 0.8]
    unrelated_components, _ = await manager._match_fragments(unrelated)

    assert [len(component) for component in related_components] == [2]
    assert sorted(map(len, unrelated_components)) == [1, 1]


@pytest.mark.asyncio
async def test_rerank_uses_reciprocal_relative_rank_when_scores_are_saturated():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        rerank_provider=_AsymmetricRankReranker(),
        config={
            "fragment_similarity_threshold": 1.01,
            "rerank_candidate_floor": 0.63,
            "rerank_threshold": 0.55,
            "rerank_top_n": 2,
            "rerank_reciprocal_rank_threshold": 0.60,
        },
    )
    fragments = [_topic_fragment(index) for index in range(5)]
    for fragment in fragments:
        fragment.embedding = [1.0, 0.0]

    components, scores = await manager._match_fragments(fragments)

    assert [0, 1] in components
    assert sorted(map(len, components), reverse=True) == [2, 1, 1, 1]
    assert scores["rerank_rank:fragment-0|fragment-1"] == 1
    assert scores["rerank_rank:fragment-1|fragment-0"] == 3
    assert scores["rerank_relative:fragment-0|fragment-1"] == 1.0
    assert scores["rerank_relative:fragment-1|fragment-0"] > 0.3


def test_relative_rerank_percentiles_do_not_invent_order_for_ties():
    assert TopicBuildManager._relative_rank_scores([0.99, 0.99, 0.99]) == [
        0.5,
        0.5,
        0.5,
    ]


@pytest.mark.asyncio
async def test_rerank_queries_respect_independent_concurrency_and_report_progress():
    reranker = _ConcurrentReranker()
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        rerank_provider=reranker,
        config={
            "rerank_concurrency": 3,
            "rerank_candidate_floor": 0.5,
            "rerank_top_n": 5,
        },
    )
    fragments = [_topic_fragment(index) for index in range(6)]
    for index, fragment in enumerate(fragments):
        fragment.label = f"话题 {index}"
        fragment.embedding = [1.0, 0.0]
    events = []

    components, _ = await manager._match_fragments(
        fragments,
        progress_callback=events.append,
    )

    rerank_events = [
        event for event in events if event.get("item_kind") == "rerank_query"
    ]
    assert reranker.calls == 6
    assert reranker.max_active_calls == 3
    assert [len(component) for component in components] == [6]
    assert max(event["active_rerank_count"] for event in rerank_events) == 3
    assert max(event["rerank_call_current"] for event in rerank_events) == 6
    assert all(event["rerank_concurrency"] == 3 for event in rerank_events)


@pytest.mark.asyncio
async def test_concurrent_rerank_failure_discards_partial_scores_before_fallback():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        rerank_provider=_PartiallyFailingReranker(),
        config={
            "rerank_concurrency": 2,
            "rerank_candidate_floor": 0.63,
            "fragment_similarity_threshold": 0.78,
            "rerank_failure_fallback": True,
        },
    )
    fragments = [_topic_fragment(1), _topic_fragment(2)]
    fragments[0].label = "A"
    fragments[1].label = "B"
    fragments[0].embedding = [1.0, 0.0]
    fragments[1].embedding = [0.7, 0.714142842]

    components, scores = await manager._match_fragments(fragments)

    assert sorted(map(len, components)) == [1, 1]
    assert not any(key.startswith("rerank:") for key in scores)


def test_matching_audit_distinguishes_singleton_reasons():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        config={
            "rerank_candidate_floor": 0.63,
            "fragment_similarity_threshold": 0.78,
        },
    )
    fragments = [_topic_fragment(index) for index in range(3)]
    scores = {
        f"{fragments[0].fragment_uid}|{fragments[1].fragment_uid}": 0.70,
        f"{fragments[0].fragment_uid}|{fragments[2].fragment_uid}": 0.40,
        f"{fragments[1].fragment_uid}|{fragments[2].fragment_uid}": 0.50,
    }

    audit = manager._matching_audit(fragments, [[0], [1], [2]], scores)

    assert audit["singleton_reason_counts"] == {
        "no_mutual_rerank": 2,
        "below_rerank_candidate_floor": 1,
    }
    assert audit["parameters"]["component_min_average_similarity"] == 0.65


def test_matching_audit_keeps_raw_rerank_scores_out_of_embedding_pairs():
    manager = TopicBuildManager(":memory:", None, None)
    fragments = [_topic_fragment(1), _topic_fragment(2)]
    pair = f"{fragments[0].fragment_uid}|{fragments[1].fragment_uid}"
    audit = manager._matching_audit(
        fragments,
        [[0], [1]],
        {
            pair: 0.7,
            f"rerank:{pair}": 0.72,
            f"rerank_raw:{pair}": 0.72,
        },
    )

    distribution = audit["rerank_score_distribution"]
    assert distribution["mapped"]["count"] == 1
    assert distribution["raw"]["median"] == 0.72


def test_related_topic_graph_uses_reciprocal_neighbors_without_merging_topics():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        config={
            "related_topic_similarity_threshold": 0.60,
            "related_topic_top_n": 1,
        },
    )
    topics = [
        TopicMemory(
            topic_uid="topic-a",
            memory_space_id="space-1",
            title="BW 行程",
            summary="天气与出发准备",
            metadata={"embedding": [1.0, 0.0], "keywords": ["BW 2026"]},
        ),
        TopicMemory(
            topic_uid="topic-b",
            memory_space_id="space-1",
            title="BW 展台",
            summary="现场逛展",
            metadata={"embedding": [0.8, 0.6], "keywords": ["BW"]},
        ),
        TopicMemory(
            topic_uid="topic-c",
            memory_space_id="space-1",
            title="邮件工具",
            summary="开发邮件插件",
            metadata={"embedding": [0.0, 1.0]},
        ),
    ]

    relations = manager._derive_topic_relations("run-1", topics)

    assert len(relations) == 1
    assert {relations[0].left_topic_uid, relations[0].right_topic_uid} == {
        "topic-a",
        "topic-b",
    }
    assert relations[0].semantic_similarity == pytest.approx(0.8)
    assert relations[0].metadata["evidence_kind"] == "shared_distinctive_identifier"


def test_related_topic_graph_rejects_one_generic_keyword_without_semantic_support():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        config={
            "related_topic_similarity_threshold": 0.60,
            "related_topic_top_n": 1,
        },
    )
    topics = [
        TopicMemory(
            topic_uid="topic-a",
            memory_space_id="space-1",
            title="照片元数据处理",
            summary="读取照片的拍摄时间和位置信息",
            metadata={"embedding": [1.0, 0.0], "keywords": ["工具"]},
        ),
        TopicMemory(
            topic_uid="topic-b",
            memory_space_id="space-1",
            title="邮件发送边界",
            summary="发送邮件前需要获得用户确认",
            metadata={"embedding": [0.8, 0.6], "keywords": ["工具"]},
        ),
    ]

    assert manager._derive_topic_relations("run-1", topics) == []


def test_related_topic_graph_collapses_overlapping_ngrams_and_is_order_stable():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        config={
            "related_topic_similarity_threshold": 0.60,
            "related_topic_top_n": 2,
        },
    )
    topics = [
        TopicMemory(
            topic_uid="topic-identity",
            memory_space_id="space-1",
            title="人物身份核对",
            summary="确认人物资料",
            metadata={
                "embedding": [1.0, 0.0, 0.0],
                "keywords": ["机器人"],
            },
        ),
        TopicMemory(
            topic_uid="topic-debug",
            memory_space_id="space-1",
            title="机器人调试",
            summary="检查程序日志",
            metadata={
                "embedding": [0.64, 0.768, 0.0],
                "keywords": ["机器人"],
            },
        ),
        TopicMemory(
            topic_uid="topic-work-a",
            memory_space_id="space-1",
            title="项目上线准备",
            summary="部署 project-x 服务",
            metadata={
                "embedding": [0.0, 1.0, 0.0],
                "keywords": ["project-x", "部署"],
            },
        ),
        TopicMemory(
            topic_uid="topic-work-b",
            memory_space_id="space-1",
            title="项目部署复盘",
            summary="复盘 project-x 上线",
            metadata={
                "embedding": [0.0, 0.98, 0.2],
                "keywords": ["project-x", "上线"],
            },
        ),
    ]

    forward = manager._derive_topic_relations("run-1", topics)
    reversed_result = manager._derive_topic_relations("run-1", list(reversed(topics)))
    forward_pairs = {
        (item.left_topic_uid, item.right_topic_uid) for item in forward
    }
    reversed_pairs = {
        (item.left_topic_uid, item.right_topic_uid) for item in reversed_result
    }

    assert forward_pairs == reversed_pairs
    assert ("topic-identity", "topic-debug") not in forward_pairs
    assert ("topic-work-a", "topic-work-b") in forward_pairs
    assert all(item.metadata["algorithm_version"] == 6 for item in forward)


def test_related_topic_graph_accepts_strong_reciprocal_semantics_without_word_overlap():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        config={
            "related_topic_similarity_threshold": 0.60,
            "related_topic_top_n": 1,
        },
    )
    topics = [
        TopicMemory(
            topic_uid="topic-attendance",
            memory_space_id="space-1",
            title="连续多日到岗记录",
            summary="每天抵达办公室后报平安",
            metadata={"embedding": [1.0, 0.0]},
        ),
        TopicMemory(
            topic_uid="topic-overtime",
            memory_space_id="space-1",
            title="职场加班边界",
            summary="处理临时工作并保护休息时间",
            metadata={"embedding": [0.82, 0.5723635]},
        ),
    ]

    relations = manager._derive_topic_relations("run-1", topics)

    assert len(relations) == 1
    assert relations[0].metadata["evidence_kind"] == (
        "strong_reciprocal_semantics"
    )
    assert relations[0].metadata["directionality"] == "undirected"
    assert relations[0].metadata["hierarchical"] is False


def test_related_topic_graph_respects_per_topic_degree_budget():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        config={
            "related_topic_similarity_threshold": 0.60,
            "related_topic_top_n": 1,
        },
    )
    topics = [
        TopicMemory(
            topic_uid=f"topic-{index}",
            memory_space_id="space-1",
            title=f"项目 X 子事项 {index}",
            summary=f"项目 X 的连续工作内容 {index}",
            metadata={
                "embedding": [1.0, index * 0.01],
                "keywords": ["project-x", f"item-{index}"],
            },
        )
        for index in range(5)
    ]

    relations = manager._derive_topic_relations("run-1", topics)
    degree = Counter(
        uid
        for relation in relations
        for uid in (relation.left_topic_uid, relation.right_topic_uid)
    )

    assert relations
    assert max(degree.values()) <= 1


def test_attribution_confidence_is_capped_by_identity_evidence():
    fallback = TimelineTopicCandidate(
        memory_uid="fallback",
        document_id=1,
        source_revision=1,
        memory_space_id="space-1",
        session_id="qq:FriendMessage:user-1",
        content="content",
        summary="summary",
        features={"evidence_status": "unavailable"},
    )
    bound = replace(
        fallback,
        memory_uid="bound",
        role_bindings={
            "narrator_actor_id": "qq:assistant:bot-1",
            "actors": [{"actor_id": "qq:human:user-1"}],
        },
        features={"evidence_status": "not_needed"},
    )
    confirmed = replace(
        bound,
        memory_uid="confirmed",
        features={"evidence_status": "attached"},
    )

    assert TopicBuildManager._calibrated_attribution_confidence(
        [fallback], 0.99
    ) == 0.78
    assert TopicBuildManager._calibrated_attribution_confidence(
        [bound], 0.99
    ) == 0.95
    assert TopicBuildManager._calibrated_attribution_confidence(
        [confirmed], 0.99
    ) == 0.99


def test_related_topic_graph_does_not_treat_dates_as_semantic_evidence():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        config={
            "related_topic_similarity_threshold": 0.60,
            "related_topic_top_n": 1,
        },
    )
    topics = [
        TopicMemory(
            topic_uid="topic-work",
            memory_space_id="space-1",
            title="2026年7月15日项目排期",
            summary="处理客户方案",
            metadata={
                "embedding": [1.0, 0.0],
                "keywords": ["2026-07-15", "工作"],
            },
        ),
        TopicMemory(
            topic_uid="topic-sleep",
            memory_space_id="space-1",
            title="2026年7月15日夜间睡眠",
            summary="提前休息",
            metadata={
                "embedding": [0.8, 0.6],
                "keywords": ["20260715", "睡眠"],
            },
        ),
    ]

    assert manager._derive_topic_relations("run-1", topics) == []
    assert TopicBuildManager._topic_keyword_terms(topics[0]) == {"工作"}
    assert TopicBuildManager._topic_keyword_terms(topics[1]) == {"睡眠"}


def test_related_topic_context_uses_source_overlap_not_shared_source_alone():
    shared_event = TopicBuildManager._topic_relation_context(
        TopicMemory(
            memory_space_id="space-1",
            title="现场取餐",
            summary="领取活动期间预订的饮品",
            metadata={"source_timeline_uids": ["timeline-event"]},
        ),
        TopicMemory(
            memory_space_id="space-1",
            title="现场逛展",
            summary="浏览展台并购买周边",
            metadata={"source_timeline_uids": ["timeline-event"]},
        ),
        semantic_similarity=0.80,
    )
    accidental_overlap = TopicBuildManager._topic_relation_context(
        TopicMemory(
            memory_space_id="space-1",
            title="洗澡放松",
            summary="下班回家后洗澡",
            metadata={
                "source_timeline_uids": ["shared", "commute-1", "commute-2"]
            },
        ),
        TopicMemory(
            memory_space_id="space-1",
            title="长期陪伴需求",
            summary="多次讨论情绪陪伴",
            metadata={
                "source_timeline_uids": ["shared", *[f"chat-{i}" for i in range(15)]]
            },
        ),
    )

    assert shared_event["contextual_match"] is True
    assert shared_event["evidence_kind"] == "shared_timeline_with_semantic_support"
    assert accidental_overlap["contextual_match"] is False


def test_related_topic_graph_rescues_high_confidence_isolated_topic():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        config={
            "related_topic_similarity_threshold": 0.60,
            "related_topic_top_n": 2,
        },
    )
    topics = [
        TopicMemory(
            topic_uid="topic-a",
            memory_space_id="space-1",
            title="核心工作",
            summary="项目持续推进",
            metadata={
                "embedding": [1.0, 0.0],
                "keywords": ["core", "anchor-d"],
            },
        ),
        TopicMemory(
            topic_uid="topic-b",
            memory_space_id="space-1",
            title="核心事项 B",
            summary="工作步骤 B",
            metadata={"embedding": [0.995, 0.1], "keywords": ["core"]},
        ),
        TopicMemory(
            topic_uid="topic-c",
            memory_space_id="space-1",
            title="核心事项 C",
            summary="工作步骤 C",
            metadata={"embedding": [0.98, 0.2], "keywords": ["core"]},
        ),
        TopicMemory(
            topic_uid="topic-d",
            memory_space_id="space-1",
            title="补充事项 D",
            summary="相关工作的独立补充",
            metadata={"embedding": [0.95, -0.1], "keywords": ["anchor-d"]},
        ),
    ]

    relations = manager._derive_topic_relations("run-1", topics)
    degree = Counter(
        uid
        for relation in relations
        for uid in (relation.left_topic_uid, relation.right_topic_uid)
    )

    assert degree["topic-d"] >= 1
    assert all(degree[topic.topic_uid] >= 1 for topic in topics)
    assert max(degree.values()) <= 2


def test_confidence_calibration_counts_independent_time_clusters_more_strongly():
    one_episode, one_audit = TopicBuildManager._calibrate_confidence(
        0.99,
        independent_clusters=1,
        supporting_timelines=4,
    )
    repeated_episode, repeated_audit = TopicBuildManager._calibrate_confidence(
        0.99,
        independent_clusters=4,
        supporting_timelines=4,
    )

    assert 0.6 < one_episode < repeated_episode < 0.99
    assert one_audit["evidence_weight"] < repeated_audit["evidence_weight"]


@pytest.mark.asyncio
async def test_fragment_extraction_batches_a_wide_candidate_group():
    store = _FragmentStore()
    llm = _ConcurrentGroundedLLM()
    manager = TopicBuildManager(
        ":memory:",
        store,
        None,
        llm_provider=llm,
        config={"fragment_extraction_batch_size": 2, "llm_concurrency": 2},
    )
    candidates = [
        TimelineTopicCandidate(
            memory_uid=f"timeline-{index}",
            document_id=index,
            source_revision=1,
            memory_space_id="space-1",
            session_id="session-1",
            content=f"事实 {index}",
            summary=f"事实 {index}",
            key_facts=[f"事实 {index}"],
            time_cluster_key="cluster-1",
        )
        for index in range(5)
    ]
    group = TopicCandidateGroup(
        run_uid="run-1",
        group_index=1,
        memory_space_id="space-1",
        label="宽候选组",
        timeline_uids=[item.memory_uid for item in candidates],
        time_cluster_keys=["cluster-1"],
        cohesion=0.8,
        group_uid="group-1",
    )
    events = []

    await manager._extract_group_fragments(
        "run-1",
        group,
        {item.memory_uid: item for item in candidates},
        progress_callback=events.append,
        group_position=1,
        group_total=1,
    )

    assert len(store.fragments) == 5
    assert len({fragment.fragment_uid for fragment in store.fragments}) == 5
    assert llm.max_active_calls == 2
    assert {event["batch_index"] for event in events} == {1, 2, 3}
    assert all(event["batch_total"] == 3 for event in events)
    assert all(event["timeline_count"] <= 2 for event in events)
    assert max(event["llm_call_current"] for event in events) == 3
    assert all(event["llm_concurrency"] == 2 for event in events)


def _topic_fragment(index: int) -> TopicFragmentDraft:
    return TopicFragmentDraft(
        fragment_uid=f"fragment-{index}",
        run_uid="run-1",
        candidate_group_uid=f"group-{index}",
        memory_space_id="space-1",
        label="长期话题",
        summary=f"片段 {index} 的摘要",
        timeline_uids=[f"timeline-{index}"],
        source_revisions={f"timeline-{index}": 1},
        facts=[
            {
                "fact_uid": f"fact-{index}",
                "type": "factual",
                "content": f"事实 {index}",
                "importance": 0.6,
                "confidence": 0.9,
                "source_timeline_uids": [f"timeline-{index}"],
            }
        ],
        importance=0.6,
        confidence=0.9,
    )


def test_fragment_prompt_uses_local_refs_and_restores_exact_provenance():
    manager = TopicBuildManager(":memory:", None, None)
    fingerprint = "a" * 64
    candidate = TimelineTopicCandidate(
        memory_uid="timeline-private-uuid",
        document_id=1,
        source_revision=3,
        memory_space_id="space-1",
        session_id="session-1",
        content="用户喜欢黑咖啡",
        summary="用户讨论咖啡偏好",
        key_facts=["用户喜欢黑咖啡", "用户不加糖"],
        atom_contents=["用户喜欢黑咖啡"],
        atom_fingerprints=[fingerprint],
    )

    payload, timeline_refs, source_refs = manager._fragment_llm_context([candidate])
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "timeline-private-uuid" not in serialized
    assert fingerprint not in serialized
    assert payload["timelines"][0]["ref"] == "T1"
    assert [fact["ref"] for fact in payload["timelines"][0]["source_facts"]] == [
        "T1.A1",
        "T1.K1",
    ]

    decoded = manager._decode_fragment_refs(
        {
            "fragments": [
                {
                    "label": "咖啡偏好",
                    "summary": "用户偏好不加糖的黑咖啡",
                    "timeline_refs": ["T1"],
                    "facts": [
                        {
                            "type": "preference",
                            "content": "用户偏好不加糖的黑咖啡",
                            "source_refs": ["T1.A1", "T1.K1"],
                        }
                    ],
                }
            ]
        },
        timeline_refs,
        source_refs,
    )

    fact = decoded["fragments"][0]["facts"][0]
    assert decoded["fragments"][0]["timeline_uids"] == [candidate.memory_uid]
    assert fact["source_timeline_uids"] == [candidate.memory_uid]
    assert fact["source_atom_fingerprints"] == [fingerprint]


def test_fragment_prompt_defines_one_future_retrieval_intent_per_fragment():
    prompt = TopicBuildManager._fragment_prompt("{}")

    assert "one plausible future retrieval query" in prompt
    assert "Repeating its ref is preferable" in prompt
    assert "mixed fragment" in prompt


def test_synthesis_prompt_strips_nested_provenance_and_derives_fragment_scope():
    manager = TopicBuildManager(":memory:", None, None)
    fragments = [_topic_fragment(1), _topic_fragment(2)]
    fragments[0].facts[0]["source_atom_fingerprints"] = ["b" * 64]
    fragments[0].facts[0]["source_timeline_uids_by_fingerprint"] = {
        "b" * 64: ["timeline-1"]
    }

    payload, fact_refs = manager._synthesis_llm_context(fragments)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "fact-1" not in serialized
    assert "fragment-1" not in serialized
    assert "timeline-1" not in serialized
    assert "b" * 64 not in serialized
    assert set(fact_refs) == {"F1", "F2"}

    decoded = manager._decode_synthesis_refs(
        {
            "title": "长期话题",
            "summary": "合并摘要",
            "atoms": [
                {
                    "type": "factual",
                    "content": "合并后的事实",
                    "source_fact_refs": ["F1", "F2"],
                }
            ],
        },
        fact_refs,
        fragments,
    )

    assert decoded["fragment_uids"] == ["fragment-1", "fragment-2"]
    assert decoded["atoms"][0]["fragment_uids"] == [
        "fragment-1",
        "fragment-2",
    ]
    assert decoded["atoms"][0]["source_fact_uids"] == ["fact-1", "fact-2"]


def test_topic_prompts_include_only_matching_authoritative_identity_profiles():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        identity_profile_store=AuthoritativeIdentityStore(
            profiles=[
                {
                    "platform": "qq",
                    "user_id": "10000001",
                    "display_name": "示例甲",
                    "gender": "男性",
                    "pronouns": ["他", "他的"],
                },
                {
                    "platform": "qq",
                    "user_id": "999",
                    "display_name": "无关人物",
                    "gender": "女性",
                },
            ]
        ),
    )
    candidate = TimelineTopicCandidate(
        memory_uid="timeline-identity",
        document_id=1,
        source_revision=1,
        memory_space_id="space-1",
        session_id="QQ20000001:FriendMessage:10000001",
        content="示例甲讨论测试计划",
        summary="示例甲说他会完成测试",
        key_facts=["示例甲是测试负责人"],
        atom_contents=["示例甲会完成测试"],
        atom_fingerprints=["c" * 64],
    )

    fragment_payload, _, _ = manager._fragment_llm_context([candidate])

    assert fragment_payload["authoritative_identities"] == [
        {
            "platform": "qq",
            "user_id": "10000001",
            "display_name": "示例甲",
            "gender": "男性",
            "pronouns": ["他", "他的"],
        }
    ]

    fragment = _topic_fragment(1)
    fragment.summary = "示例甲说他会完成测试"
    fragment.facts[0]["content"] = "示例甲是测试负责人"
    synthesis_payload, _ = manager._synthesis_llm_context([fragment])

    assert synthesis_payload["authoritative_identities"] == [
        fragment_payload["authoritative_identities"][0]
    ]
    assert "authoritative_identities" in manager._fragment_prompt("{}")
    assert "authoritative_identities" in manager._synthesis_prompt("{}")


def test_fragment_role_map_preserves_bot_first_person_with_actor_anchor():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        identity_profile_store=AuthoritativeIdentityStore(
            profiles=[
                {
                    "user_id": "10000001",
                    "display_name": "示例甲",
                    "gender": "男性",
                    "pronouns": ["他"],
                }
            ]
        ),
    )
    candidate = TimelineTopicCandidate(
        memory_uid="timeline-role-map",
        document_id=1,
        source_revision=1,
        memory_space_id="space-1",
        session_id="QQ:FriendMessage:10000001",
        persona_id="测试助手",
        content="我陪示例甲核对了测试结果",
        summary="我陪示例甲核对测试结果",
        role_bindings={
            "schema_version": 1,
            "narrator_actor_id": "qq:assistant:bot-1",
            "actors": [
                {
                    "actor_id": "qq:assistant:bot-1",
                    "actor_type": "assistant",
                    "observed_names": ["测试助手"],
                },
                {
                    "actor_id": "qq:human:10000001",
                    "actor_type": "human",
                    "observed_names": ["示例甲"],
                },
            ],
        },
    )

    payload, _, _ = manager._fragment_llm_context([candidate])

    assert payload["conversation_roles"]["timeline_narration"] == (
        "first_person_assistant"
    )
    assert payload["conversation_roles"]["timeline_narrators"] == {
        "T1": "assistant-persona:测试助手"
    }
    assert payload["conversation_roles"]["human_participants"][0][
        "observed_names"
    ] == ["示例甲"]
    assert payload["conversation_roles"]["human_participants"][0][
        "resolution_sources"
    ] == ["timeline_role_bindings"]
    assert payload["conversation_roles"]["human_participants"][0][
        "identity_confidence"
    ] == 0.95
    manager._validate_role_anchored_fragment(
        "测试核对",
        "我陪示例甲核对测试结果",
        [],
        [candidate],
    )
    with pytest.raises(TopicBuildValidationError, match="display name"):
        manager._validate_role_anchored_fragment(
            "测试核对",
            "我陪用户核对测试结果",
            [],
            [candidate],
        )
    fragment = _topic_fragment(1)
    fragment.metadata = {
        "narrative_schema_version": "first_person_assistant_roles_v2",
        "conversation_roles": manager._conversation_role_payload([candidate]),
    }
    participant = manager._topic_participant_index([fragment])["participants"][0]
    assert participant["confidence"] == 0.95
    assert participant["resolution_status"] == "timeline_bound"
    assert participant["resolution_sources"] == ["timeline_role_bindings"]
    synthesis_payload, _ = manager._synthesis_llm_context([fragment])
    assert "assistant-persona:测试助手" in json.dumps(
        synthesis_payload, ensure_ascii=False
    )


def test_generic_human_role_repair_requires_one_unambiguous_human():
    manager = TopicBuildManager(":memory:", None, None)
    candidate = TimelineTopicCandidate(
        memory_uid="timeline-role-repair",
        document_id=1,
        source_revision=1,
        memory_space_id="space-1",
        session_id="qq:FriendMessage:u1",
        persona_id="测试助手",
        content="我陪示例甲核对结果",
        summary="我陪示例甲核对结果",
        role_bindings={
            "narrator_actor_id": "qq:assistant:bot-1",
            "actors": [
                {
                    "actor_id": "qq:assistant:bot-1",
                    "actor_type": "assistant",
                    "platform": "qq",
                    "sender_id": "bot-1",
                    "observed_names": ["测试助手"],
                },
                {
                    "actor_id": "qq:human:u1",
                    "actor_type": "human",
                    "platform": "qq",
                    "sender_id": "u1",
                    "observed_names": ["示例甲"],
                },
            ],
        },
    )

    repaired, audit = manager._repair_unambiguous_generic_human_roles(
        "我提醒用户检查用户设置，并等待对方回复", [candidate]
    )

    assert repaired == "我提醒示例甲检查用户设置，并等待示例甲回复"
    assert audit[0]["actor_id"] == "qq:human:u1"
    second_human = {
        "actor_id": "qq:human:u2",
        "actor_type": "human",
        "platform": "qq",
        "sender_id": "u2",
        "observed_names": ["小明"],
    }
    group_candidate = replace(
        candidate,
        role_bindings={
            **candidate.role_bindings,
            "actors": [*candidate.role_bindings["actors"], second_human],
        },
    )
    unchanged, audit = manager._repair_unambiguous_generic_human_roles(
        "用户提醒对方", [group_candidate]
    )
    assert unchanged == "用户提醒对方"
    assert audit == []


def test_fragment_validation_repairs_unambiguous_generic_human_role_in_place():
    manager = TopicBuildManager(":memory:", None, None)
    candidate = TimelineTopicCandidate(
        memory_uid="timeline-role-repair-validation",
        document_id=1,
        source_revision=1,
        memory_space_id="space-1",
        session_id="qq:FriendMessage:u1",
        persona_id="测试助手",
        content="我提醒示例甲检查结果",
        summary="我提醒示例甲检查结果",
        role_bindings={
            "narrator_actor_id": "qq:assistant:bot-1",
            "actors": [
                {
                    "actor_id": "qq:assistant:bot-1",
                    "actor_type": "assistant",
                    "platform": "qq",
                    "sender_id": "bot-1",
                    "observed_names": ["测试助手"],
                },
                {
                    "actor_id": "qq:human:u1",
                    "actor_type": "human",
                    "platform": "qq",
                    "sender_id": "u1",
                    "observed_names": ["示例甲"],
                },
            ],
        },
    )
    group = TopicCandidateGroup(
        run_uid="run-role-repair",
        group_index=0,
        memory_space_id="space-1",
        label="测试",
        timeline_uids=[candidate.memory_uid],
        time_cluster_keys=[],
        cohesion=1.0,
        group_uid="group-role-repair",
    )

    fragment = manager._validate_fragments(
        {
            "fragments": [
                {
                    "label": "提醒用户",
                    "summary": "我提醒对方检查结果",
                    "timeline_uids": [candidate.memory_uid],
                    "facts": [
                        {
                            "content": "用户已收到提醒",
                            "source_timeline_uids": [candidate.memory_uid],
                        }
                    ],
                }
            ]
        },
        "run-role-repair",
        group,
        [candidate],
        "prompt-hash",
        "input-hash",
        "provider-1",
        "model-1",
    )[0]

    assert fragment.label == "提醒示例甲"
    assert fragment.summary == "我提醒示例甲检查结果"
    assert fragment.facts[0]["content"] == "示例甲已收到提醒"
    assert fragment.metadata["narrative_schema_version"] == (
        "first_person_assistant_roles_v2"
    )
    assert fragment.metadata["validation_repairs"][-1]["type"] == (
        "unambiguous_generic_human_role_repair"
    )


def test_role_payload_merges_qq_adapter_alias_with_authoritative_profile():
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        identity_profile_store=AuthoritativeIdentityStore(
            profiles=[
                {
                    "platform": "qq",
                    "user_id": "10000001",
                    "display_name": "示例甲",
                }
            ]
        ),
    )
    candidate = TimelineTopicCandidate(
        memory_uid="timeline-alias-role-map",
        document_id=1,
        source_revision=1,
        memory_space_id="space-1",
        session_id="QQ20000001:FriendMessage:10000001",
        persona_id="测试助手",
        content="示例甲完成了测试",
        summary="示例甲完成了测试",
        role_bindings={
            "narrator_actor_id": "aiocqhttp:assistant:bot-1",
            "actors": [
                {
                    "actor_id": "aiocqhttp:assistant:bot-1",
                    "actor_type": "assistant",
                    "platform": "aiocqhttp",
                    "sender_id": "bot-1",
                    "observed_names": ["测试助手"],
                },
                {
                    "actor_id": "aiocqhttp:human:10000001",
                    "actor_type": "human",
                    "platform": "aiocqhttp",
                    "sender_id": "10000001",
                    "observed_names": ["示例甲"],
                },
            ],
        },
    )

    roles = manager._conversation_role_payload([candidate])

    assert roles["timeline_narrators"][candidate.memory_uid] == (
        "assistant-persona:测试助手"
    )
    assert len(roles["human_participants"]) == 1
    human = roles["human_participants"][0]
    assert human["actor_id"] == "qq:human:10000001"
    assert human["resolution_sources"] == [
        "timeline_role_bindings",
        "authoritative_profile_fallback",
    ]


@pytest.mark.asyncio
async def test_ambiguous_legacy_group_timeline_loads_raw_identity_evidence():
    messages = [
        Message(
            id=10,
            session_id="qq:GroupMessage:g1",
            role="user",
            content="我觉得应该测试",
            sender_id="u1",
            sender_name="示例甲",
            group_id="g1",
            platform="qq",
        ),
        Message(
            id=11,
            session_id="qq:GroupMessage:g1",
            role="assistant",
            content="我会协助测试",
            sender_id="bot1",
            sender_name="示例甲",
            group_id="g1",
            platform="qq",
            metadata={"is_bot_message": True},
        ),
    ]
    evidence_store = SimpleNamespace(
        get_messages_by_id_span=AsyncMock(return_value=messages)
    )
    manager = TopicBuildManager(
        ":memory:", None, None, conversation_store=evidence_store
    )
    candidate = TimelineTopicCandidate(
        memory_uid="legacy-group",
        document_id=1,
        source_revision=1,
        memory_space_id="space-1",
        session_id="qq:GroupMessage:g1",
        content="我和示例甲讨论测试",
        summary="我和示例甲讨论测试",
        source_window={"first_message_id": 10, "last_message_id": 11},
    )

    await manager._prepare_candidate_evidence([candidate])

    assert candidate.role_bindings["narrator_actor_id"] == "qq:assistant:bot1"
    assistant = next(
        actor
        for actor in candidate.role_bindings["actors"]
        if actor["actor_type"] == "assistant"
    )
    assert assistant["observed_names"] == ["助手"]
    assert "assistant_name_collides_with_human" in candidate.features[
        "ambiguity_flags"
    ]
    assert candidate.features["evidence_status"] == "attached"
    assert [item["message_id"] for item in candidate.features["raw_evidence"]] == [
        10,
        11,
    ]


@pytest.mark.asyncio
async def test_synthesis_retries_validation_once_with_specific_error():
    llm = _CorrectingSynthesisLLM()
    manager = TopicBuildManager(":memory:", None, None, llm_provider=llm)

    synthesis = await manager._synthesize_direct(
        [_topic_fragment(1), _topic_fragment(2)]
    )

    assert llm.synthesis_calls == 2
    assert synthesis["title"] == "长期话题"
    assert {uid for atom in synthesis["atoms"] for uid in atom["source_fact_uids"]} == {
        "fact-1",
        "fact-2",
    }
    assert synthesis["validation_repairs"] == []


def test_synthesis_repairs_missing_fragment_coverage_from_known_facts():
    manager = TopicBuildManager(":memory:", None, None)
    fragments = [_topic_fragment(1), _topic_fragment(2)]

    synthesis = manager._validate_synthesis(
        {
            "title": "长期话题",
            "summary": "合成摘要",
            "fragment_uids": ["fragment-1", "fragment-2"],
            "atoms": [
                {
                    "content": "事实 1",
                    "fragment_uids": ["fragment-1"],
                    "source_fact_uids": ["fact-1"],
                }
            ],
        },
        fragments,
    )

    assert {uid for atom in synthesis["atoms"] for uid in atom["source_fact_uids"]} == {
        "fact-1",
        "fact-2",
    }
    assert synthesis["validation_repairs"] == [
        {
            "type": "missing_fragment_atom_coverage",
            "fragment_uid": "fragment-2",
            "added_passthrough_atoms": 1,
            "merged_source_facts": 0,
        }
    ]


def test_synthesis_repairs_missing_timeline_atom_coverage():
    manager = TopicBuildManager(":memory:", None, None)
    fragment = _topic_fragment(1)
    fragment.timeline_uids = ["timeline-1", "timeline-2"]
    fragment.source_revisions = {"timeline-1": 1, "timeline-2": 1}
    fragment.facts.append(
        {
            "fact_uid": "fact-2",
            "type": "factual",
            "content": "事实 2",
            "importance": 0.7,
            "confidence": 0.9,
            "source_timeline_uids": ["timeline-2"],
        }
    )

    synthesis = manager._validate_synthesis(
        {
            "title": "长期话题",
            "summary": "合成摘要",
            "fragment_uids": [fragment.fragment_uid],
            "atoms": [
                {
                    "content": "事实 1",
                    "fragment_uids": [fragment.fragment_uid],
                    "source_fact_uids": ["fact-1"],
                }
            ],
        },
        [fragment],
    )

    assert {uid for atom in synthesis["atoms"] for uid in atom["source_fact_uids"]} == {
        "fact-1",
        "fact-2",
    }
    assert any(
        repair["type"] == "missing_timeline_atom_coverage"
        and repair["timeline_uid"] == "timeline-2"
        for repair in synthesis["validation_repairs"]
    )


def test_materialization_adds_fact_fallback_for_unmapped_timeline():
    manager = TopicBuildManager(":memory:", None, None)
    fingerprint = "d" * 64
    fragment = _topic_fragment(1)
    fragment.timeline_uids = ["timeline-1", "timeline-2"]
    fragment.source_revisions = {"timeline-1": 1, "timeline-2": 1}
    fragment.time_cluster_keys = ["cluster-1"]
    fragment.facts[0]["source_timeline_uids"] = ["timeline-1", "timeline-2"]
    fragment.facts[0]["source_atom_fingerprints"] = [fingerprint]
    fragment.facts[0]["source_timeline_uids_by_fingerprint"] = {
        fingerprint: ["timeline-1"]
    }
    candidates = {
        timeline_uid: TimelineTopicCandidate(
            memory_uid=timeline_uid,
            document_id=index,
            source_revision=1,
            memory_space_id="space-1",
            session_id="session-1",
            content=f"事实 {index}",
            summary=f"事实 {index}",
            time_cluster_key="cluster-1",
        )
        for index, timeline_uid in enumerate(fragment.timeline_uids, 1)
    }
    synthesis = {
        "title": "长期话题",
        "summary": "合成摘要",
        "confidence": 0.9,
        "atoms": [
            {
                "type": "factual",
                "content": "合并事实",
                "fragment_uids": [fragment.fragment_uid],
                "source_fact_uids": ["fact-1"],
            }
        ],
    }

    _, _, _, sources = manager._materialize_snapshot(
        "run-1",
        "space-1",
        synthesis,
        [fragment],
        candidates,
        None,
    )

    assert {source.timeline_uid for source in sources} == {
        "timeline-1",
        "timeline-2",
    }
    assert next(
        source for source in sources if source.timeline_uid == "timeline-1"
    ).source_kind == "atom_fingerprint"
    assert next(
        source for source in sources if source.timeline_uid == "timeline-2"
    ).source_kind == "fact_fingerprint"


def test_synthesis_drops_unknown_fact_and_restores_grounded_fact():
    manager = TopicBuildManager(":memory:", None, None)
    fragment = _topic_fragment(1)

    synthesis = manager._validate_synthesis(
        {
            "title": "长期话题",
            "summary": "合成摘要",
            "fragment_uids": [fragment.fragment_uid],
            "atoms": [
                {
                    "content": "虚构事实",
                    "fragment_uids": [fragment.fragment_uid],
                    "source_fact_uids": ["unknown-fact"],
                }
            ],
        },
        [fragment],
    )

    assert synthesis["atoms"][0]["source_fact_uids"] == ["fact-1"]
    repair_types = {item["type"] for item in synthesis["validation_repairs"]}
    assert "dropped_unknown_source_fact_uids" in repair_types
    assert "dropped_ungrounded_synthesis_atom" in repair_types
    assert "missing_fragment_atom_coverage" in repair_types


def test_synthesis_repairs_missing_top_level_fragment_scope():
    manager = TopicBuildManager(":memory:", None, None)
    fragments = [_topic_fragment(1), _topic_fragment(2)]

    synthesis = manager._validate_synthesis(
        {
            "title": "长期话题",
            "summary": "合成摘要",
            "fragment_uids": ["fragment-1", "invented-fragment"],
            "atoms": [],
        },
        fragments,
    )

    assert synthesis["fragment_uids"] == ["fragment-1", "fragment-2"]
    scope_repair = next(
        item
        for item in synthesis["validation_repairs"]
        if item["type"] == "normalized_synthesis_fragment_scope"
    )
    assert scope_repair["added_missing_fragment_uids"] == ["fragment-2"]
    assert scope_repair["dropped_unknown_fragment_uids"] == ["invented-fragment"]


@pytest.mark.asyncio
async def test_large_component_uses_bounded_hierarchical_synthesis():
    llm = _ConcurrentGroundedLLM()
    manager = TopicBuildManager(
        ":memory:",
        None,
        None,
        llm_provider=llm,
        config={"synthesis_batch_size": 4, "llm_concurrency": 2},
    )
    fragments = [_topic_fragment(index) for index in range(10)]
    events = []

    synthesis = await manager._synthesize_component(
        fragments,
        progress_callback=lambda current, total, size, level: events.append(
            (current, total, size, level)
        ),
    )

    assert llm.max_active_calls == 2
    assert len(events) == 8
    assert all(size <= 4 for _, _, size, _ in events)
    assert max(current for current, _, _, _ in events) == 4
    assert {uid for atom in synthesis["atoms"] for uid in atom["source_fact_uids"]} == {
        f"fact-{index}" for index in range(10)
    }
    assert synthesis["fragment_uids"] == [f"fragment-{index}" for index in range(10)]


@pytest.mark.asyncio
async def test_incremental_build_preserves_topic_uid_and_prior_sources(tmp_path: Path):
    db_path, space_id = await _create_timeline_db(tmp_path)
    store = TopicMemoryStore(db_path)
    manager = TopicBuildManager(
        db_path,
        store,
        TopicMaintenanceManager(db_path, store),
        llm_provider=_GroundedLLM(),
        embedding_provider=_Embedding(),
    )
    await manager.build_space(space_id)
    travel = next(
        topic for topic in await store.list_topics(space_id) if topic.title == "京都旅行"
    )

    added_at = time.time()
    metadata = {
        "session_id": "bot:FriendMessage:user-1",
        "persona_id": "persona-1",
        "canonical_summary": "用户再次讨论京都旅行预算。",
        "topics": ["旅行计划"],
        "key_facts": ["用户正在规划京都旅行预算"],
    }
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO documents(id, doc_id, text, metadata) VALUES (4, ?, ?, ?)",
            ("doc-4", metadata["canonical_summary"], json.dumps(metadata)),
        )
        await db.commit()
    identity = MemoryIdentityStore(db_path)
    await identity.upsert_memory(
        memory_uid="timeline-travel-3",
        document_id=4,
        memory_layer="timeline",
        memory_space_id=space_id,
        revision=1,
        created_at=added_at,
        updated_at=added_at,
    )
    await identity.upsert_source_span(
        "timeline-travel-3",
        {
            "session_id": metadata["session_id"],
            "started_at": added_at,
            "ended_at": added_at + 10,
        },
    )

    result = await manager.build_space(
        space_id,
        mode=TopicMaintenanceMode.INCREMENTAL,
        since=added_at - 1,
    )

    assert result["topic_count"] == 1
    updated = await store.get_topic(travel.topic_uid)
    assert updated is not None
    assert updated.revision == 2
    provenance = await store.get_topic_provenance(travel.topic_uid)
    assert {row["timeline_uid"] for row in provenance["links"]} == {
        "timeline-travel-1",
        "timeline-travel-2",
        "timeline-travel-3",
    }


@pytest.mark.asyncio
async def test_failed_run_resumes_without_repeating_completed_fragment_stage(
    tmp_path: Path,
):
    db_path, space_id = await _create_timeline_db(tmp_path)
    store = TopicMemoryStore(db_path)
    llm = _ToggleFailingSynthesisLLM()
    manager = TopicBuildManager(
        db_path,
        store,
        TopicMaintenanceManager(db_path, store),
        llm_provider=llm,
        embedding_provider=_Embedding(),
        config={"llm_max_retries": 1},
    )

    with pytest.raises(RuntimeError, match="simulated provider timeout"):
        await manager.build_space(space_id)

    failed_run = (await store.list_maintenance_runs(space_id, limit=1))[0]
    assert failed_run["status"] == "failed"
    assert failed_run["stage"] == "topic_synthesis"
    fragment_calls_before_resume = llm.fragment_calls
    matching_checkpoint = await store.get_build_checkpoint(
        failed_run["run_uid"],
        "fragment_matching",
    )
    assert matching_checkpoint is not None

    llm.fail_synthesis = False
    result = await manager.resume_run(failed_run["run_uid"])

    assert result["status"] == "completed"
    assert result["run_uid"] == failed_run["run_uid"]
    assert llm.fragment_calls == fragment_calls_before_resume
    resumed_run = await store.get_maintenance_run(failed_run["run_uid"])
    assert resumed_run["status"] == "completed"
    assert resumed_run["completed_at"] is not None
