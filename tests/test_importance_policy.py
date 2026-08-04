from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.importance_policy import (
    aggregate_source_importance,
    evidence_strength,
    fragment_semantic_importance,
    topic_base_importance,
    topic_effective_importance,
    topic_semantic_importance,
)
from astrbot_plugin_livingmemory.core.managers.topic_build_manager import (
    TopicBuildManager,
)
from astrbot_plugin_livingmemory.core.models.memory_identity import (
    resolve_memory_space,
)
from astrbot_plugin_livingmemory.core.models.topic_memory import (
    TopicFragmentDraft,
    TopicMemory,
    TopicTimelineLink,
)
from astrbot_plugin_livingmemory.core.retrieval.topic_retriever import TopicRetriever
from astrbot_plugin_livingmemory.storage.memory_identity_store import (
    MemoryIdentityStore,
)
from astrbot_plugin_livingmemory.storage.topic_memory_store import TopicMemoryStore


def test_importance_projection_is_stateless_and_reversible():
    semantic = topic_semantic_importance([(0.9, 0.8), (0.55, 0.9)])
    source = aggregate_source_importance(
        [
            {
                "timeline_uid": "timeline-1",
                "time_cluster_key": "cluster-1",
                "base_importance": 0.8,
                "effective_importance": 0.4,
                "importance_revision": 3,
                "weight": 1.0,
            }
        ]
    )
    base = topic_base_importance(semantic, source["source_base_component"])
    decayed = topic_effective_importance(base, source["dynamic_factor"])

    repeated = topic_effective_importance(base, source["dynamic_factor"])
    restored_source = aggregate_source_importance(
        [
            {
                "timeline_uid": "timeline-1",
                "time_cluster_key": "cluster-1",
                "base_importance": 0.8,
                "effective_importance": 0.8,
                "importance_revision": 3,
                "weight": 1.0,
            }
        ]
    )
    restored = topic_effective_importance(
        base, restored_source["dynamic_factor"]
    )

    assert repeated == decayed
    assert restored == base
    assert decayed < restored


def test_same_time_cluster_does_not_inflate_source_importance():
    one = aggregate_source_importance(
        [
            {
                "timeline_uid": "timeline-1",
                "time_cluster_key": "same-conversation",
                "base_importance": 0.8,
                "effective_importance": 0.6,
                "weight": 1.0,
            }
        ]
    )
    split = aggregate_source_importance(
        [
            {
                "timeline_uid": f"timeline-{index}",
                "time_cluster_key": "same-conversation",
                "base_importance": 0.8,
                "effective_importance": 0.6,
                "weight": 1.0,
            }
            for index in range(1, 8)
        ]
    )

    assert split["source_base_component"] == one["source_base_component"]
    assert split["dynamic_factor"] == one["dynamic_factor"]
    assert evidence_strength(cluster_count=1, timeline_count=7) > evidence_strength(
        cluster_count=1, timeline_count=1
    )


def test_zero_fact_contribution_source_does_not_change_topic_projection():
    projection = aggregate_source_importance(
        [
            {
                "timeline_uid": "timeline-with-topic-facts",
                "time_cluster_key": "cluster-1",
                "base_importance": 0.8,
                "effective_importance": 0.8,
                "weight": 1.0,
            },
            {
                "timeline_uid": "timeline-without-topic-facts",
                "time_cluster_key": "cluster-2",
                "base_importance": 0.9,
                "effective_importance": 0.1,
                "weight": 0.0,
            },
        ]
    )

    assert projection["source_base_component"] == pytest.approx(0.8)
    assert projection["dynamic_factor"] == pytest.approx(1.0)


def test_fragment_importance_reuses_fact_scores_without_second_total_score():
    facts = [
        {"content": "长期健康风险", "importance": 0.95},
        {"content": "普通日常细节", "importance": 0.35},
    ]
    assert fragment_semantic_importance(facts) == pytest.approx(0.86)


@pytest.mark.asyncio
async def test_store_projects_topic_from_live_timeline_without_drift(tmp_path: Path):
    db_path = str(tmp_path / "live-importance.db")
    identity_store = MemoryIdentityStore(db_path)
    await identity_store.initialize()
    space_id = resolve_memory_space(
        "bot:FriendMessage:user", "persona"
    ).memory_space_id
    await identity_store.upsert_memory(
        memory_uid="timeline-1",
        document_id=1,
        memory_layer="timeline",
        memory_space_id=space_id,
        revision=1,
        created_at=time.time(),
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)"
        )
        await db.execute(
            "INSERT INTO documents VALUES (1, ?, ?)",
            (
                "Timeline",
                json.dumps(
                    {
                        "importance": 0.4,
                        "base_importance": 0.8,
                        "importance_revision": 2,
                    }
                ),
            ),
        )
        await db.commit()

    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-1",
        memory_space_id=space_id,
        title="Topic",
        summary="Summary",
        semantic_importance=0.9,
        base_importance=0.88,
        importance=0.88,
    )
    await store.save_topic_snapshot(
        topic,
        atoms=[],
        links=[
            TopicTimelineLink(
                topic_uid=topic.topic_uid,
                timeline_uid="timeline-1",
                time_cluster_key="cluster-1",
            )
        ],
        atom_sources=[],
    )

    first = await store.get_topic(topic.topic_uid)
    second = await store.get_topic(topic.topic_uid)
    assert first.base_importance == second.base_importance == pytest.approx(0.9)
    assert first.importance == second.importance == pytest.approx(0.8325)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE documents SET metadata = ? WHERE id = 1",
            (
                json.dumps(
                    {
                        "importance": 0.8,
                        "base_importance": 0.8,
                        "importance_revision": 2,
                    }
                ),
            ),
        )
        await db.commit()
    restored = await store.get_topic(topic.topic_uid)
    assert restored.base_importance == pytest.approx(0.9)
    assert restored.importance == pytest.approx(0.9)
    assert restored.metadata["importance_projection"]["live"] is True


@pytest.mark.asyncio
async def test_high_importance_timeline_does_not_raise_unrelated_sibling_topic(
    tmp_path: Path,
):
    db_path = str(tmp_path / "sibling-importance.db")
    identity_store = MemoryIdentityStore(db_path)
    await identity_store.initialize()
    space_id = resolve_memory_space(
        "bot:FriendMessage:user", "persona"
    ).memory_space_id
    await identity_store.upsert_memory(
        memory_uid="timeline-shared",
        document_id=1,
        memory_layer="timeline",
        memory_space_id=space_id,
        revision=1,
        created_at=time.time(),
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)"
        )
        await db.execute(
            "INSERT INTO documents VALUES (1, ?, ?)",
            (
                "One Timeline containing unrelated subjects",
                json.dumps(
                    {
                        "importance": 0.95,
                        "base_importance": 0.95,
                        "importance_revision": 1,
                    }
                ),
            ),
        )
        await db.commit()

    store = TopicMemoryStore(db_path)
    await store.initialize()
    for topic_uid, semantic in (("topic-high", 0.9), ("topic-low", 0.2)):
        await store.save_topic_snapshot(
            TopicMemory(
                topic_uid=topic_uid,
                memory_space_id=space_id,
                title=topic_uid,
                summary=topic_uid,
                semantic_importance=semantic,
                base_importance=semantic,
                importance=semantic,
            ),
            atoms=[],
            links=[
                TopicTimelineLink(
                    topic_uid=topic_uid,
                    timeline_uid="timeline-shared",
                    time_cluster_key="cluster-shared",
                )
            ],
            atom_sources=[],
        )

    high = await store.get_topic("topic-high")
    low = await store.get_topic("topic-low")

    assert high.base_importance == pytest.approx(0.9)
    assert low.base_importance == pytest.approx(0.2)
    assert high.importance == pytest.approx(0.9)
    assert low.importance == pytest.approx(0.2)


def test_timeline_importance_weights_follow_topic_fact_provenance():
    fragments = [
        TopicFragmentDraft(
            run_uid="run-1",
            candidate_group_uid="group-1",
            memory_space_id="space-1",
            label="Topic",
            summary="Summary",
            timeline_uids=["timeline-high", "timeline-low"],
            source_revisions={"timeline-high": 1, "timeline-low": 1},
            facts=[
                {
                    "fact_uid": "fact-high",
                    "content": "Important fact",
                    "importance": 0.9,
                    "confidence": 1.0,
                    "source_timeline_uids": ["timeline-high"],
                },
                {
                    "fact_uid": "fact-low",
                    "content": "Supporting detail",
                    "importance": 0.3,
                    "confidence": 0.5,
                    "source_timeline_uids": ["timeline-low"],
                },
            ],
        )
    ]

    weights = TopicBuildManager._timeline_importance_contribution_weights(
        ["timeline-high", "timeline-low"], fragments
    )

    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["timeline-high"] == pytest.approx(0.857142857)
    assert weights["timeline-low"] == pytest.approx(0.142857143)


def test_live_source_projection_still_uses_topic_access_decay():
    now = time.time()
    base = dict(
        memory_space_id="space-1",
        topic_uid="topic-1",
        title="Topic",
        summary="Summary",
        semantic_importance=0.8,
        base_importance=0.8,
        importance=0.76,
        created_at=now - 30 * 86400,
        updated_at=now - 30 * 86400,
        decay_anchor_at=now - 30 * 86400,
        last_accessed_at=now - 30 * 86400,
        metadata={"importance_projection": {"live": True}},
    )
    without_access = TopicMemory(**base, access_count=0)
    with_access = TopicMemory(**{**base, "topic_uid": "topic-2"}, access_count=20)
    retriever = SimpleNamespace(config={"recall_decay_rate": 0.05})

    cold = TopicRetriever._effective_importance(retriever, without_access)
    protected = TopicRetriever._effective_importance(retriever, with_access)

    assert cold < protected < 0.76


@pytest.mark.asyncio
async def test_deleted_source_review_can_be_repaired_manually():
    store = SimpleNamespace(
        get_maintenance_review_context=AsyncMock(
            return_value={
                "review_uid": "review-1",
                "review_type": "deleted_timeline_source_repair",
                "status": "pending",
                "memory_space_id": "space-1",
                "topic_uids": ["topic-1"],
                "timeline_uids": ["timeline-1"],
            }
        )
    )
    manager = TopicBuildManager(":memory:", store, SimpleNamespace())
    manager.repair_deleted_timeline_sources = AsyncMock(
        return_value={"status": "completed"}
    )

    result = await manager.resolve_maintenance_review(
        "review-1", action="sync_sources"
    )

    assert result == {"status": "completed"}
    manager.repair_deleted_timeline_sources.assert_awaited_once_with(
        "space-1",
        affected_topic_uids=["topic-1"],
        deleted_timeline_uids=["timeline-1"],
        review_uid="review-1",
        progress_callback=None,
    )


@pytest.mark.asyncio
async def test_deleted_source_projection_drops_unsupported_facts_and_actor_roles():
    store = SimpleNamespace(
        get_topic_provenance=AsyncMock(
            return_value={
                "links": [
                    {
                        "timeline_uid": "timeline-deleted",
                        "time_cluster_key": "old",
                        "source_timeline_revision": 1,
                    },
                    {
                        "timeline_uid": "timeline-kept",
                        "time_cluster_key": "new",
                        "source_timeline_revision": 2,
                    },
                ],
                "atoms": [
                    {
                        "atom_uid": "atom-deleted",
                        "atom_type": "factual",
                        "content": "Only deleted source supports this",
                        "importance": 0.8,
                        "confidence": 0.9,
                    },
                    {
                        "atom_uid": "atom-kept",
                        "atom_type": "factual",
                        "content": "Remaining source supports this",
                        "importance": 0.7,
                        "confidence": 0.9,
                    },
                ],
                "atom_sources": [
                    {
                        "topic_atom_uid": "atom-deleted",
                        "timeline_uid": "timeline-deleted",
                        "source_atom_fingerprint": "deleted-fact",
                    },
                    {
                        "topic_atom_uid": "atom-kept",
                        "timeline_uid": "timeline-kept",
                        "source_atom_fingerprint": "kept-fact",
                    },
                ],
                "actor_links": [
                    {
                        "actor_id": "qq:human:deleted",
                        "actor_type": "human",
                        "relation_type": "subject",
                        "metadata": {"timeline_uids": ["timeline-deleted"]},
                    },
                    {
                        "actor_id": "qq:human:kept",
                        "actor_type": "human",
                        "relation_type": "subject",
                        "metadata": {"timeline_uids": ["timeline-kept"]},
                    },
                ],
                "atom_actor_links": [
                    {
                        "topic_atom_uid": "atom-deleted",
                        "actor_id": "qq:human:deleted",
                        "actor_type": "human",
                        "relation_type": "subject",
                        "timeline_uid": "timeline-deleted",
                    },
                    {
                        "topic_atom_uid": "atom-kept",
                        "actor_id": "qq:human:kept",
                        "actor_type": "human",
                        "relation_type": "subject",
                        "timeline_uid": "timeline-kept",
                    },
                ],
            }
        )
    )
    manager = TopicBuildManager(":memory:", store, SimpleNamespace())
    topic = TopicMemory(
        topic_uid="topic-1",
        memory_space_id="space-1",
        title="Topic",
        summary="Summary",
        semantic_importance=0.75,
    )

    fragment = await manager._existing_topic_fragment(
        "repair-run",
        topic,
        exclude_timeline_uids={"timeline-deleted"},
    )

    assert fragment is not None
    assert fragment.timeline_uids == ["timeline-kept"]
    assert [fact["content"] for fact in fragment.facts] == [
        "Remaining source supports this"
    ]
    assert fragment.metadata["mentioned_actor_refs"] == [
        {
            "actor_id": "qq:human:kept",
            "actor_type": "human",
            "relation_type": "subject",
            "metadata": {"timeline_uids": ["timeline-kept"]},
        }
    ]
    assert fragment.facts[0]["actor_refs"][0]["actor_id"] == "qq:human:kept"
