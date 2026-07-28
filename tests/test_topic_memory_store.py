from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.models.memory_identity import (
    resolve_memory_space,
)
from astrbot_plugin_livingmemory.core.models.topic_memory import (
    TimelineTopicCandidate,
    TopicActorLink,
    TopicAtomActorLink,
    TopicFragmentDraft,
    TopicAtomSource,
    TopicMaintenanceMode,
    TopicMaintenanceRun,
    TopicMaintenanceStatus,
    TopicMemory,
    TopicMemoryAtom,
    TopicMemoryStatus,
    TopicRelation,
    TopicTimelineLink,
)
from astrbot_plugin_livingmemory.storage.memory_identity_store import (
    MemoryIdentityStore,
)
from astrbot_plugin_livingmemory.storage.topic_memory_store import (
    TopicMemoryStore,
    TopicRevisionConflict,
    TopicSourceValidationError,
)


def test_topic_actor_schema_is_valid_without_aiosqlite_runtime():
    connection = sqlite3.connect(":memory:")

    class _ConnectionAdapter:
        async def execute(self, sql, params=()):
            return connection.execute(sql, params)

    asyncio.run(TopicMemoryStore.create_tables(_ConnectionAdapter()))
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "topic_actor_links" in tables
    assert "topic_atom_actor_links" in tables
    actor_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(topic_actor_links)")
    }
    assert {"topic_uid", "actor_id", "relation_type", "resolution_status"} <= actor_columns
    atom_actor_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(topic_atom_actor_links)")
    }
    assert {"topic_atom_uid", "fragment_uid", "timeline_uid"} <= atom_actor_columns
    connection.close()


def test_actor_provenance_rejects_fragment_outside_publish_snapshot():
    atom = TopicMemoryAtom(
        atom_uid="atom-1",
        topic_uid="topic-1",
        atom_type="factual",
        content="事实",
    )
    actor = TopicActorLink(
        topic_uid="topic-1",
        actor_id="qq:human:u1",
        actor_type="human",
        relation_type="subject",
    )
    atom_actor = TopicAtomActorLink(
        topic_atom_uid="atom-1",
        actor_id=actor.actor_id,
        relation_type=actor.relation_type,
        fragment_uid="existing:topic-1:r1",
        timeline_uid="timeline-1",
    )
    fragment = TopicFragmentDraft(
        fragment_uid="formal-fragment",
        run_uid="run-1",
        candidate_group_uid="group-1",
        memory_space_id="space-1",
        label="正式片段",
        summary="正式片段",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[],
    )

    with pytest.raises(ValueError, match="outside the snapshot"):
        TopicMemoryStore._validate_actor_links(
            "topic-1", [atom], [actor], [atom_actor], fragments=[fragment]
        )


def test_candidate_checkpoint_preserves_actor_and_source_provenance():
    candidate = TimelineTopicCandidate(
        memory_uid="timeline-1",
        document_id=1,
        source_revision=2,
        memory_space_id="space-1",
        session_id="qq:FriendMessage:user-1",
        persona_id="persona-1",
        content="content",
        summary="summary",
        role_bindings={
            "narrator_actor_id": "qq:assistant:bot-1",
            "actors": [{"actor_id": "qq:human:user-1"}],
        },
        source_window={"first_message_id": 10, "last_message_id": 20},
        edit_origin="automatic",
        traceability="full",
    )

    restored = TopicMemoryStore._dict_to_candidate(
        TopicMemoryStore._candidate_to_dict(candidate)
    )

    assert restored.role_bindings == candidate.role_bindings
    assert restored.source_window == candidate.source_window
    assert restored.edit_origin == "automatic"
    assert restored.traceability == "full"


@pytest.mark.asyncio
async def test_replace_group_fragments_reports_missing_parent(tmp_path: Path):
    store = TopicMemoryStore(str(tmp_path / "missing-fragment-parent.db"))
    await store.initialize()
    fragment = TopicFragmentDraft(
        fragment_uid="fragment-1",
        run_uid="missing-run",
        candidate_group_uid="missing-group",
        memory_space_id="space-1",
        label="Fragment",
        summary="Summary",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[],
        prompt_hash="prompt",
        input_hash="input",
    )

    with pytest.raises(ValueError, match="parent run/group no longer exists"):
        await store.replace_group_fragments(
            "missing-run",
            "missing-group",
            [fragment],
        )


@pytest.mark.asyncio
async def test_formal_fragment_logical_identity_tracks_revisions(tmp_path: Path):
    db_path = str(tmp_path / "fragment-logical-revisions.db")
    space_id = await _register_timeline(
        db_path,
        memory_uid="timeline-1",
        document_id=1,
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-1",
        memory_space_id=space_id,
        title="Topic",
        summary="Topic summary",
    )
    link = TopicTimelineLink(
        topic_uid=topic.topic_uid,
        timeline_uid="timeline-1",
        time_cluster_key="cluster-1",
    )

    def fragment(uid: str, *, summary: str, input_hash: str):
        return TopicFragmentDraft(
            fragment_uid=uid,
            logical_fragment_uid="logical-fragment-1",
            run_uid=f"run-{uid}",
            candidate_group_uid=f"group-{uid}",
            memory_space_id=space_id,
            label="Fragment",
            summary=summary,
            timeline_uids=["timeline-1"],
            source_revisions={"timeline-1": 1},
            facts=[{"content": "stable fact"}],
            input_hash=input_hash,
        )

    first = fragment("fragment-1", summary="same", input_hash="input-1")
    saved = await store.save_topic_snapshot(
        topic,
        atoms=[],
        links=[link],
        atom_sources=[],
        fragments=[first],
    )
    second = fragment("fragment-2", summary="same", input_hash="input-1")
    saved = await store.save_topic_snapshot(
        replace(topic, revision=saved.revision),
        atoms=[],
        links=[link],
        atom_sources=[],
        fragments=[second],
        expected_revision=saved.revision,
    )
    third = fragment("fragment-3", summary="changed", input_hash="input-2")
    await store.save_topic_snapshot(
        replace(topic, revision=saved.revision),
        atoms=[],
        links=[link],
        atom_sources=[],
        fragments=[third],
        expected_revision=saved.revision,
    )

    async with aiosqlite.connect(db_path) as db:
        rows = await (
            await db.execute(
                "SELECT fragment_uid, logical_fragment_uid, fragment_revision "
                "FROM topic_fragments ORDER BY fragment_uid"
            )
        ).fetchall()

    assert rows == [
        ("fragment-1", "logical-fragment-1", 1),
        ("fragment-2", "logical-fragment-1", 1),
        ("fragment-3", "logical-fragment-1", 2),
    ]


@pytest.mark.asyncio
async def test_affect_profile_and_fragment_events_round_trip(tmp_path: Path):
    db_path = str(tmp_path / "affect-round-trip.db")
    space_id = await _register_timeline(
        db_path,
        memory_uid="timeline-1",
        document_id=1,
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    event = {
        "event_uid": "affect-event-1",
        "actor_id": "qq:human:u1",
        "display_name_snapshot": "示例甲",
        "emotion": "感到安心",
        "description": "因有人陪伴而感到安心",
        "trigger": "获得陪伴",
        "target": "睡眠",
        "evidence_type": "explicit",
        "temporal_status": "historical",
        "valence": 0.78,
        "arousal": 0.24,
        "dominance": 0.62,
        "intensity": 0.8,
        "confidence": 0.92,
        "categories": [{"label": "relief", "score": 0.95}],
        "source_timeline_uids": ["timeline-1"],
        "source_atom_fingerprints": [],
        "source_fact_keys": [],
    }
    signature = {
        "schema_version": 1,
        "taxonomy": "livingmemory-affect-v1",
    }
    topic = TopicMemory(
        topic_uid="topic-1",
        memory_space_id=space_id,
        title="睡眠陪伴",
        summary="示例甲因获得陪伴而安心。",
        affect_profile=[{**event, "fragment_uid": "fragment-1"}],
        affective_salience=0.736,
        affect_signature=signature,
    )
    fragment = TopicFragmentDraft(
        fragment_uid="fragment-1",
        logical_fragment_uid="logical-fragment-1",
        run_uid="run-1",
        candidate_group_uid="group-1",
        memory_space_id=space_id,
        label="获得陪伴后的安心",
        summary="示例甲获得陪伴后感到安心。",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[{"content": "示例甲获得陪伴后感到安心。"}],
        affect_events=[event],
        affect_signature=signature,
    )
    await store.save_topic_snapshot(
        topic,
        atoms=[],
        links=[
            TopicTimelineLink(
                topic_uid="topic-1",
                timeline_uid="timeline-1",
                time_cluster_key="cluster-1",
            )
        ],
        atom_sources=[],
        actor_links=[
            TopicActorLink(
                topic_uid="topic-1",
                actor_id="qq:human:u1",
                actor_type="human",
                relation_type="subject",
                display_name_snapshot="示例甲",
                resolution_status="evidence_confirmed",
            )
        ],
        fragments=[fragment],
    )

    restored_topic = await store.get_topic("topic-1")
    restored_fragments = await store.list_active_fragments_for_topics(["topic-1"])

    assert restored_topic is not None
    assert restored_topic.affect_profile == topic.affect_profile
    assert restored_topic.affective_salience == pytest.approx(0.736)
    assert restored_topic.affect_signature == signature
    assert restored_fragments[0]["fragment"].affect_events == [event]
    assert restored_fragments[0]["fragment"].affect_signature == signature


def test_affect_profile_rejects_actor_outside_topic_actor_index():
    topic = TopicMemory(
        topic_uid="topic-1",
        memory_space_id="space-1",
        title="情绪溯源",
        summary="一条有人物引用的情绪记录。",
        affect_profile=[
            {
                "event_uid": "affect-event-1",
                "actor_id": "unresolved:fragment-1:affect:0",
                "source_timeline_uids": ["timeline-1"],
                "fragment_uid": "fragment-1",
            }
        ],
    )
    with pytest.raises(ValueError, match="matching actor link"):
        TopicMemoryStore._validate_affect_links(
            topic,
            [
                TopicTimelineLink(
                    topic_uid="topic-1",
                    timeline_uid="timeline-1",
                    time_cluster_key="cluster-1",
                )
            ],
            [
                TopicActorLink(
                    topic_uid="topic-1",
                    actor_id="unresolved:fragment-1:person-hash",
                    actor_type="human",
                    relation_type="subject",
                )
            ],
            fragments=[
                TopicFragmentDraft(
                    fragment_uid="fragment-1",
                    run_uid="run-1",
                    candidate_group_uid="group-1",
                    memory_space_id="space-1",
                    label="片段",
                    summary="片段",
                    timeline_uids=["timeline-1"],
                    source_revisions={"timeline-1": 1},
                    facts=[],
                )
            ],
        )


@pytest.mark.asyncio
async def test_maintenance_review_identity_ignores_volatile_run_details(tmp_path: Path):
    db_path = str(tmp_path / "maintenance-review.db")
    store = TopicMemoryStore(db_path)
    await store.initialize()
    await store.create_maintenance_run(
        TopicMaintenanceRun(
            run_uid="run-1",
            memory_space_id="space-1",
            mode=TopicMaintenanceMode.INCREMENTAL,
        )
    )
    await store.create_maintenance_run(
        TopicMaintenanceRun(
            run_uid="run-2",
            memory_space_id="space-1",
            mode=TopicMaintenanceMode.INCREMENTAL,
        )
    )

    first_uid = await store.enqueue_maintenance_review(
        memory_space_id="space-1",
        review_type="ambiguous_topic_match",
        timeline_uids=["timeline-1"],
        topic_uids=["topic-a", "topic-b"],
        details={"run_uid": "run-1", "scores": [0.61, 0.60]},
    )
    second_uid = await store.enqueue_maintenance_review(
        memory_space_id="space-1",
        review_type="ambiguous_topic_match",
        timeline_uids=["timeline-1"],
        topic_uids=["topic-b", "topic-a"],
        details={"run_uid": "run-2", "scores": [0.63, 0.62]},
    )
    await store.update_maintenance_run(
        "run-2",
        status=TopicMaintenanceStatus.COMPLETED_WITH_REVIEW,
    )
    reviews = await store.list_maintenance_reviews(
        "space-1",
        timeline_uids=["timeline-1"],
    )

    assert first_uid == second_uid
    assert len(reviews) == 1
    assert reviews[0]["timeline_uids"] == ["timeline-1"]
    assert reviews[0]["topic_uids"] == ["topic-a", "topic-b"]
    assert reviews[0]["details"]["run_uid"] == "run-2"


@pytest.mark.asyncio
async def test_maintenance_reviews_resolve_only_the_published_component(tmp_path: Path):
    store = TopicMemoryStore(str(tmp_path / "maintenance-review-resolution.db"))
    await store.initialize()
    first_uid = await store.enqueue_maintenance_review(
        memory_space_id="space-1",
        review_type="ambiguous_topic_match",
        timeline_uids=["timeline-1"],
        topic_uids=["topic-a", "topic-b"],
        component_uid="component-a",
        details={"fragment_uids": ["fragment-a"]},
    )
    second_uid = await store.enqueue_maintenance_review(
        memory_space_id="space-1",
        review_type="ambiguous_topic_match",
        timeline_uids=["timeline-1"],
        topic_uids=["topic-a", "topic-b"],
        component_uid="component-b",
        details={"fragment_uids": ["fragment-b"]},
    )

    assert first_uid != second_uid
    assert await store.resolve_maintenance_reviews(
        "space-1", component_uids=["component-a"]
    ) == 1
    pending = await store.list_maintenance_reviews("space-1")
    assert [item["review_uid"] for item in pending] == [second_uid]
    assert pending[0]["details"]["component_uid"] == "component-b"

    resolved = await store.list_maintenance_reviews("space-1", status="resolved")
    assert [item["review_uid"] for item in resolved] == [first_uid]


@pytest.mark.asyncio
async def test_pending_review_topic_is_preserved_and_marked_during_full_publish(
    tmp_path: Path,
):
    db_path = str(tmp_path / "pending-review-preservation.db")
    space_id = await _register_timeline(
        db_path,
        memory_uid="timeline-pending",
        document_id=1,
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    pending_topic = await store.save_topic_snapshot(
        TopicMemory(
            topic_uid="topic-pending",
            memory_space_id=space_id,
            title="Pending",
            summary="Pending",
        ),
        atoms=[],
        links=[],
        atom_sources=[],
    )
    other_topic = await store.save_topic_snapshot(
        TopicMemory(
            topic_uid="topic-other",
            memory_space_id=space_id,
            title="Other",
            summary="Other",
        ),
        atoms=[],
        links=[],
        atom_sources=[],
    )
    await store.create_maintenance_run(
        TopicMaintenanceRun(
            run_uid="run-review",
            memory_space_id=space_id,
            mode=TopicMaintenanceMode.FULL,
        )
    )

    publication = await store.publish_topic_build(
        run_uid="run-review",
        memory_space_id=space_id,
        mode=TopicMaintenanceMode.FULL,
        snapshots=[],
        relations=[],
        reset_topics=True,
        sync_pending_topic_uids={pending_topic.topic_uid},
        additional_decisions=[
            {
                "decision_uid": "decision-pending",
                "topic_uid": pending_topic.topic_uid,
                "action": "pending_review",
                "fragment_uids": ["fragment-1"],
                "metadata": {"component_uid": "component-1"},
            }
        ],
        completion_status=TopicMaintenanceStatus.COMPLETED_WITH_REVIEW,
    )

    preserved = await store.get_topic(pending_topic.topic_uid)
    archived = await store.get_topic(other_topic.topic_uid)
    run = await store.get_maintenance_run("run-review")
    assert preserved.status is TopicMemoryStatus.ACTIVE
    assert preserved.metadata["sync_pending"]["reason"] == "pending_review"
    assert archived.status is TopicMemoryStatus.ARCHIVED
    assert run["status"] == "completed_with_review"
    assert publication["reset"]["deferred"] is True
    assert publication["reset"]["reason"] == "pending_review"
    async with aiosqlite.connect(db_path) as db:
        decision = await (
            await db.execute(
                "SELECT action, metadata FROM topic_build_decisions "
                "WHERE decision_uid = 'decision-pending'"
            )
        ).fetchone()
    assert decision[0] == "pending_review"
    assert "component-1" in decision[1]


@pytest.mark.asyncio
async def test_build_owned_review_is_hidden_until_atomic_publication_completes(
    tmp_path: Path,
):
    store = TopicMemoryStore(str(tmp_path / "review-visibility.db"))
    await store.initialize()
    await store.create_maintenance_run(
        TopicMaintenanceRun(
            run_uid="run-pending-review",
            memory_space_id="space-1",
            mode=TopicMaintenanceMode.INCREMENTAL,
        )
    )
    review_uid = await store.enqueue_maintenance_review(
        memory_space_id="space-1",
        review_type="ambiguous_topic_match",
        timeline_uids=["timeline-1"],
        topic_uids=[],
        component_uid="component-1",
        details={"run_uid": "run-pending-review"},
    )

    assert await store.list_maintenance_reviews("space-1") == []

    await store.update_maintenance_run(
        "run-pending-review",
        status=TopicMaintenanceStatus.COMPLETED_WITH_REVIEW,
    )
    reviews = await store.list_maintenance_reviews("space-1")
    assert [item["review_uid"] for item in reviews] == [review_uid]


@pytest.mark.asyncio
async def test_review_publication_is_atomic_and_rejects_stale_candidate_revision(
    tmp_path: Path,
):
    db_path = str(tmp_path / "review-publication.db")
    space_id = await _register_timeline(
        db_path,
        memory_uid="timeline-1",
        document_id=1,
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-1",
        memory_space_id=space_id,
        title="工资",
        summary="旧摘要",
    )
    link = TopicTimelineLink(
        topic_uid=topic.topic_uid,
        timeline_uid="timeline-1",
        time_cluster_key="cluster-1",
    )
    saved = await store.save_topic_snapshot(
        topic,
        atoms=[],
        links=[link],
        atom_sources=[],
    )
    review_uid = await store.enqueue_maintenance_review(
        memory_space_id=space_id,
        review_type="ambiguous_topic_match",
        timeline_uids=["timeline-1"],
        topic_uids=[topic.topic_uid],
        details={"proposed_title": "工资补发"},
    )
    run = await store.create_maintenance_run(
        TopicMaintenanceRun(
            memory_space_id=space_id,
            mode=TopicMaintenanceMode.REPAIR,
        )
    )
    result = await store.publish_topic_build(
        run_uid=run.run_uid,
        memory_space_id=space_id,
        mode=TopicMaintenanceMode.REPAIR,
        snapshots=[
            {
                "topic": replace(saved, title="工资补发", summary="新摘要"),
                "atoms": [],
                "links": [link],
                "atom_sources": [],
                "expected_revision": saved.revision,
            }
        ],
        relations=[],
        review_resolution={
            "review_uid": review_uid,
            "action": "merge",
            "payload": {"target_topic_uid": topic.topic_uid},
        },
    )

    assert result["topics"][0].revision == 2
    review = await store.get_maintenance_review(review_uid)
    assert review["status"] == "resolved"
    assert review["resolution_action"] == "merge"
    assert review["resolution_payload"] == {"target_topic_uid": "topic-1"}

    stale_review_uid = await store.enqueue_maintenance_review(
        memory_space_id=space_id,
        review_type="ambiguous_topic_match",
        timeline_uids=["timeline-1"],
        topic_uids=[topic.topic_uid],
        details={"proposed_title": "过期决策"},
    )
    current = await store.get_topic(topic.topic_uid)
    advanced = await store.save_topic_snapshot(
        replace(current, summary="外部更新"),
        atoms=[],
        links=[link],
        atom_sources=[],
        expected_revision=current.revision,
    )
    stale_run = await store.create_maintenance_run(
        TopicMaintenanceRun(
            memory_space_id=space_id,
            mode=TopicMaintenanceMode.REPAIR,
        )
    )
    with pytest.raises(TopicRevisionConflict, match="preview is stale"):
        await store.publish_topic_build(
            run_uid=stale_run.run_uid,
            memory_space_id=space_id,
            mode=TopicMaintenanceMode.REPAIR,
            snapshots=[
                {
                    "topic": replace(advanced, summary="不应写入"),
                    "atoms": [],
                    "links": [link],
                    "atom_sources": [],
                    "expected_revision": advanced.revision,
                }
            ],
            relations=[],
            review_resolution={
                "review_uid": stale_review_uid,
                "action": "merge",
                "payload": {},
            },
        )

    assert (await store.get_topic(topic.topic_uid)).summary == "外部更新"
    stale_review = await store.get_maintenance_review(stale_review_uid)
    assert stale_review["status"] == "pending"


@pytest.mark.asyncio
async def test_actor_filter_and_fact_groups_expose_concrete_provenance(tmp_path: Path):
    db_path = str(tmp_path / "actor-filter.db")
    space_id = await _register_timeline(
        db_path,
        memory_uid="timeline-1",
        document_id=1,
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-actor",
        memory_space_id=space_id,
        title="工资补发",
        summary="示例甲的工资补发记录",
    )
    atom = TopicMemoryAtom(
        atom_uid="atom-salary",
        topic_uid=topic.topic_uid,
        atom_type="factual",
        content="示例甲的六月工资少发了 600 元",
    )
    link = TopicTimelineLink(
        topic_uid=topic.topic_uid,
        timeline_uid="timeline-1",
        time_cluster_key="cluster-1",
    )
    actor = TopicActorLink(
        topic_uid=topic.topic_uid,
        actor_id="qq:human:10000001",
        actor_type="human",
        relation_type="subject",
        display_name_snapshot="示例甲",
        resolution_status="profile_inferred",
        metadata={
            "fragment_uids": ["fragment-1"],
            "timeline_uids": ["timeline-1"],
            "identity_sources": ["authoritative_profile"],
        },
    )
    atom_actor = TopicAtomActorLink(
        topic_atom_uid=atom.atom_uid,
        actor_id=actor.actor_id,
        relation_type=actor.relation_type,
        fragment_uid="fragment-1",
        timeline_uid="timeline-1",
        metadata={"identity_source": "authoritative_profile"},
    )
    fragment = TopicFragmentDraft(
        fragment_uid="fragment-1",
        run_uid="run-actor",
        candidate_group_uid="group-actor",
        memory_space_id=space_id,
        label="工资补发",
        summary="示例甲的六月工资补发片段",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[{"content": atom.content}],
    )
    await store.save_topic_snapshot(
        topic,
        atoms=[atom],
        links=[link],
        atom_sources=[],
        actor_links=[actor],
        atom_actor_links=[atom_actor],
        fragments=[fragment],
    )

    matched = await store.list_topics(space_id, actor_id=actor.actor_id)
    assert [item.topic_uid for item in matched] == [topic.topic_uid]
    assert await store.list_topics(space_id, actor_id="qq:human:other") == []
    catalog = await store.list_topic_actors(space_id)
    assert catalog[0]["display_name"] == "示例甲"
    assert catalog[0]["topic_count"] == 1
    for index in (1, 2):
        unresolved_topic = TopicMemory(
            topic_uid=f"topic-unresolved-{index}",
            memory_space_id=space_id,
            title=f"HR {index}",
            summary="一个无法稳定解析的提及。",
        )
        await store.save_topic_snapshot(
            unresolved_topic,
            atoms=[],
            links=[
                TopicTimelineLink(
                    topic_uid=unresolved_topic.topic_uid,
                    timeline_uid="timeline-1",
                    time_cluster_key="cluster-1",
                )
            ],
            atom_sources=[],
            actor_links=[
                TopicActorLink(
                    topic_uid=unresolved_topic.topic_uid,
                    actor_id=f"unresolved:fragment-{index}:hr",
                    actor_type="human",
                    relation_type="mentioned",
                    display_name_snapshot="HR",
                    resolution_status="unresolved",
                )
            ],
        )
    catalog = await store.list_topic_actors(space_id)
    hr = next(item for item in catalog if item["display_name"] == "HR")
    assert hr["catalog_group"] == "unresolved"
    assert len(hr["actor_ids"]) == 2
    assert hr["topic_count"] == 2
    grouped = await store.list_topics(space_id, actor_id=hr["actor_id"])
    assert {item.topic_uid for item in grouped} == {
        "topic-unresolved-1",
        "topic-unresolved-2",
    }
    provenance = await store.get_topic_provenance(topic.topic_uid)
    group = provenance["actor_fact_groups"][0]
    assert group["resolution_status"] == "profile_inferred"
    assert group["identity_sources"] == ["authoritative_profile"]
    assert group["facts"] == [
        {
            "atom_uid": "atom-salary",
            "content": "示例甲的六月工资少发了 600 元",
            "atom_type": "factual",
            "fragment_uids": ["fragment-1"],
            "timeline_uids": ["timeline-1"],
        }
    ]


@pytest.mark.asyncio
async def test_actor_catalog_prefers_persona_name_over_numeric_snapshot(
    tmp_path: Path,
):
    db_path = str(tmp_path / "actor-persona-catalog.db")
    space_id = await _register_timeline(
        db_path,
        memory_uid="timeline-persona",
        document_id=1,
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    actor_id = "assistant-persona:测试助手"
    for index, snapshot in enumerate(("20000001", "测试助手"), start=1):
        topic = TopicMemory(
            topic_uid=f"topic-persona-{index}",
            memory_space_id=space_id,
            title=f"人格目录 {index}",
            summary="同一稳定人格的不同历史名称快照。",
        )
        await store.save_topic_snapshot(
            topic,
            atoms=[],
            links=[
                TopicTimelineLink(
                    topic_uid=topic.topic_uid,
                    timeline_uid="timeline-persona",
                    time_cluster_key="cluster-persona",
                )
            ],
            atom_sources=[],
            actor_links=[
                TopicActorLink(
                    topic_uid=topic.topic_uid,
                    actor_id=actor_id,
                    actor_type="assistant",
                    relation_type="narrator",
                    display_name_snapshot=snapshot,
                    resolution_status="timeline_bound",
                )
            ],
        )

    catalog = await store.list_topic_actors(space_id)
    persona = next(item for item in catalog if item["actor_id"] == actor_id)
    assert persona["display_name"] == "测试助手"
    assert persona["topic_count"] == 2


async def _register_timeline(
    db_path: str,
    *,
    memory_uid: str,
    document_id: int,
    session_id: str = "bot:FriendMessage:user",
    persona_id: str = "persona",
    revision: int = 1,
) -> str:
    identity_store = MemoryIdentityStore(db_path)
    await identity_store.initialize()
    space_id = resolve_memory_space(session_id, persona_id).memory_space_id
    await identity_store.upsert_memory(
        memory_uid=memory_uid,
        document_id=document_id,
        memory_layer="timeline",
        memory_space_id=space_id,
        revision=revision,
        created_at=time.time(),
    )
    return space_id


@pytest.mark.asyncio
async def test_topic_snapshot_has_independent_atoms_and_cluster_metrics(
    tmp_path: Path,
):
    db_path = str(tmp_path / "topic.db")
    space_id = await _register_timeline(
        db_path, memory_uid="timeline-1", document_id=1
    )
    await _register_timeline(db_path, memory_uid="timeline-2", document_id=2)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT NOT NULL)"
        )
        await db.executemany(
            "INSERT INTO documents (id, text) VALUES (?, ?)",
            [
                (1, "第一条 Timeline 的完整内容，用于 Topic 来源预览。"),
                (2, "第二条 Timeline 的完整内容。"),
            ],
        )
        await db.commit()
    store = TopicMemoryStore(db_path)
    await store.initialize()

    topic = TopicMemory(
        topic_uid="topic-1",
        memory_space_id=space_id,
        title="旅行计划",
        summary="用户在同一次持续对话中讨论旅行计划。",
        importance=0.8,
    )
    atom = TopicMemoryAtom(
        atom_uid="topic-atom-1",
        topic_uid=topic.topic_uid,
        atom_type="planned",
        content="用户计划秋季旅行",
    )
    links = [
        TopicTimelineLink(
            topic_uid=topic.topic_uid,
            timeline_uid="timeline-1",
            time_cluster_key="cluster-2026-07-19",
            contribution_weight=0.8,
        ),
        TopicTimelineLink(
            topic_uid=topic.topic_uid,
            timeline_uid="timeline-2",
            time_cluster_key="cluster-2026-07-19",
            contribution_weight=0.6,
        ),
    ]
    source = TopicAtomSource(
        source_uid="source-1",
        topic_atom_uid=atom.atom_uid,
        timeline_uid="timeline-1",
        source_atom_id=41,
        source_atom_fingerprint="sha256:example",
    )

    saved = await store.save_topic_snapshot(
        topic, atoms=[atom], links=links, atom_sources=[source]
    )

    assert saved.revision == 1
    loaded = await store.get_topic(topic.topic_uid)
    assert loaded is not None
    assert loaded.summary == topic.summary
    provenance = await store.get_topic_provenance(topic.topic_uid)
    assert len(provenance["atoms"]) == 1
    assert len(provenance["atom_sources"]) == 1
    assert provenance["atom_sources"][0]["source_atom_id"] == 41
    assert len(provenance["links"]) == 2
    assert provenance["links"][0]["timeline_available"] is True
    assert provenance["links"][0]["timeline_document_id"] in {1, 2}
    assert "Timeline 的完整内容" in provenance["links"][0]["timeline_preview"]
    assert "Timeline 的完整内容" in provenance["links"][0]["timeline_content"]

    metrics = await store.get_topic_support_metrics(topic.topic_uid)
    assert metrics["timeline_count"] == 2
    assert metrics["time_cluster_count"] == 1
    assert metrics["contribution_weight"] == pytest.approx(1.4)

    async with aiosqlite.connect(db_path) as db:
        timeline_atom_count = (
            await (await db.execute("SELECT COUNT(*) FROM memory_atoms")).fetchone()
        )[0] if await _table_exists(db, "memory_atoms") else 0
        topic_atom_count = (
            await (
                await db.execute("SELECT COUNT(*) FROM topic_memory_atoms")
            ).fetchone()
        )[0]
    assert timeline_atom_count == 0
    assert topic_atom_count == 1


@pytest.mark.asyncio
async def test_get_topic_counts_for_timelines_only_counts_active_links(
    tmp_path: Path,
):
    db_path = str(tmp_path / "topic-counts.db")
    space_id = await _register_timeline(
        db_path, memory_uid="timeline-counted", document_id=1
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-counted",
        memory_space_id=space_id,
        title="Counted Topic",
        summary="This Topic is linked to one Timeline.",
    )
    await store.save_topic_snapshot(
        topic,
        atoms=[],
        links=[
            TopicTimelineLink(
                topic_uid=topic.topic_uid,
                timeline_uid="timeline-counted",
                time_cluster_key="cluster-1",
            )
        ],
        atom_sources=[],
    )

    assert await store.get_topic_counts_for_timelines(
        ["timeline-counted", "timeline-counted", "missing"]
    ) == {"timeline-counted": 1}

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE topic_timeline_links SET status = 'archived' WHERE topic_uid = ?",
            (topic.topic_uid,),
        )
        await db.commit()

    assert await store.get_topic_counts_for_timelines(["timeline-counted"]) == {}


@pytest.mark.asyncio
async def test_archiving_topic_archives_visible_topic_dependencies(tmp_path: Path):
    db_path = str(tmp_path / "archive-topic-dependencies.db")
    space_id = await _register_timeline(
        db_path, memory_uid="timeline-archive", document_id=1
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-archive",
        memory_space_id=space_id,
        title="Archived Topic",
        summary="This Topic and all of its visible projections are archived together.",
    )
    atom = TopicMemoryAtom(
        atom_uid="atom-archive",
        topic_uid=topic.topic_uid,
        atom_type="factual",
        content="Archived fact",
    )
    fragment = TopicFragmentDraft(
        fragment_uid="fragment-archive",
        run_uid="run-archive",
        candidate_group_uid="group-archive",
        memory_space_id=space_id,
        label="Archived fragment",
        summary="Archived fragment summary",
        timeline_uids=["timeline-archive"],
        source_revisions={"timeline-archive": 1},
        facts=[{"content": "Archived fact"}],
    )
    await store.save_topic_snapshot(
        topic,
        atoms=[atom],
        links=[
            TopicTimelineLink(
                topic_uid=topic.topic_uid,
                timeline_uid="timeline-archive",
                time_cluster_key="cluster-archive",
            )
        ],
        atom_sources=[
            TopicAtomSource(
                topic_atom_uid=atom.atom_uid,
                timeline_uid="timeline-archive",
                source_atom_fingerprint="archive-fingerprint",
            )
        ],
        fragments=[fragment],
    )

    assert await store.archive_topic_uids_not_in(
        space_id, {topic.topic_uid}, set()
    ) == 1

    async with aiosqlite.connect(db_path) as db:
        statuses = {}
        for table, key_column, key_value in (
            ("topic_memories", "topic_uid", topic.topic_uid),
            ("topic_memory_atoms", "topic_uid", topic.topic_uid),
            ("topic_timeline_links", "topic_uid", topic.topic_uid),
            ("topic_fragment_links", "topic_uid", topic.topic_uid),
            ("topic_fragments", "fragment_uid", fragment.fragment_uid),
        ):
            statuses[table] = (
                await (
                    await db.execute(
                        f"SELECT status FROM {table} WHERE {key_column} = ?",
                        (key_value,),
                    )
                ).fetchone()
            )[0]

    assert statuses == {
        "topic_memories": "archived",
        "topic_memory_atoms": "archived",
        "topic_timeline_links": "archived",
        "topic_fragment_links": "archived",
        "topic_fragments": "archived",
    }


@pytest.mark.asyncio
async def test_delete_archived_topics_never_deletes_active_topics(tmp_path: Path):
    db_path = str(tmp_path / "delete-archived-topics.db")
    space_id = await _register_timeline(
        db_path, memory_uid="timeline-archive", document_id=1
    )
    await _register_timeline(
        db_path, memory_uid="timeline-active", document_id=2
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    archived = TopicMemory(
        topic_uid="topic-archived",
        memory_space_id=space_id,
        title="Archived",
        summary="Archived summary",
    )
    active = TopicMemory(
        topic_uid="topic-active",
        memory_space_id=space_id,
        title="Active",
        summary="Active summary",
    )
    fragment = TopicFragmentDraft(
        fragment_uid="fragment-to-delete",
        run_uid="run-delete",
        candidate_group_uid="group-delete",
        memory_space_id=space_id,
        label="Archived fragment",
        summary="Archived fragment",
        timeline_uids=["timeline-archive"],
        source_revisions={"timeline-archive": 1},
        facts=[],
    )
    await store.save_topic_snapshot(
        archived,
        atoms=[],
        links=[TopicTimelineLink(topic_uid=archived.topic_uid, timeline_uid="timeline-archive", time_cluster_key="archive")],
        atom_sources=[],
        fragments=[fragment],
    )
    await store.save_topic_snapshot(
        active,
        atoms=[],
        links=[TopicTimelineLink(topic_uid=active.topic_uid, timeline_uid="timeline-active", time_cluster_key="active")],
        atom_sources=[],
    )
    await store.archive_topic_uids_not_in(space_id, {archived.topic_uid}, set())

    deleted = await store.delete_archived_topics(
        space_id, [archived.topic_uid, active.topic_uid]
    )

    assert deleted == 1
    assert await store.get_topic(archived.topic_uid) is None
    assert (await store.get_topic(active.topic_uid)).status is TopicMemoryStatus.ACTIVE
    async with aiosqlite.connect(db_path) as db:
        orphan = await (
            await db.execute(
                "SELECT 1 FROM topic_fragments WHERE fragment_uid = ?",
                (fragment.fragment_uid,),
            )
        ).fetchone()
    assert orphan is None


@pytest.mark.asyncio
async def test_clear_space_removes_only_topic_derivatives_and_build_history(
    tmp_path: Path,
):
    db_path = str(tmp_path / "clear-space.db")
    space_a = await _register_timeline(
        db_path,
        memory_uid="timeline-a",
        document_id=1,
        session_id="bot:FriendMessage:a",
    )
    space_b = await _register_timeline(
        db_path,
        memory_uid="timeline-b",
        document_id=2,
        session_id="bot:FriendMessage:b",
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()

    for suffix, space_id in (("a", space_a), ("b", space_b)):
        topic = TopicMemory(
            topic_uid=f"topic-{suffix}",
            memory_space_id=space_id,
            title=f"Topic {suffix}",
            summary=f"Summary {suffix}",
        )
        atom = TopicMemoryAtom(
            atom_uid=f"atom-{suffix}",
            topic_uid=topic.topic_uid,
            atom_type="factual",
            content=f"Fact {suffix}",
        )
        link = TopicTimelineLink(
            topic_uid=topic.topic_uid,
            timeline_uid=f"timeline-{suffix}",
            time_cluster_key=f"cluster-{suffix}",
        )
        source = TopicAtomSource(
            source_uid=f"source-{suffix}",
            topic_atom_uid=atom.atom_uid,
            timeline_uid=link.timeline_uid,
            source_atom_fingerprint=f"fingerprint-{suffix}",
        )
        await store.save_topic_snapshot(
            topic, atoms=[atom], links=[link], atom_sources=[source]
        )
        await store.create_maintenance_run(
            TopicMaintenanceRun(
                memory_space_id=space_id,
                mode=TopicMaintenanceMode.FULL,
            )
        )

    result = await store.clear_space(space_a)

    assert result == {
        "deleted_topics": 1,
        "deleted_runs": 1,
        "deleted_fragments": 0,
    }
    assert await store.get_topic("topic-a") is None
    assert await store.get_topic("topic-b") is not None
    assert await store.list_maintenance_runs(space_a) == []
    assert len(await store.list_maintenance_runs(space_b)) == 1
    assert (await store.get_overview(space_a))["topic_count"] == 0
    async with aiosqlite.connect(db_path) as db:
        timeline_count = (
            await (
                await db.execute(
                    "SELECT COUNT(*) FROM memory_registry WHERE memory_uid = 'timeline-a'"
                )
            ).fetchone()
        )[0]
        source_count = (
            await (
                await db.execute(
                    "SELECT COUNT(*) FROM topic_atom_sources WHERE source_uid = 'source-a'"
                )
            ).fetchone()
        )[0]
    assert timeline_count == 1
    assert source_count == 0


@pytest.mark.asyncio
async def test_publish_topic_build_rolls_back_every_snapshot_on_mid_publish_failure(
    tmp_path: Path,
    monkeypatch,
):
    db_path = str(tmp_path / "atomic-topic-publication.db")
    space_id = await _register_timeline(
        db_path,
        memory_uid="timeline-1",
        document_id=1,
    )
    await _register_timeline(
        db_path,
        memory_uid="timeline-2",
        document_id=2,
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()

    old_topic = TopicMemory(
        topic_uid="topic-old",
        memory_space_id=space_id,
        title="Old Topic",
        summary="Published before the failed build.",
    )
    old_atom = TopicMemoryAtom(
        atom_uid="atom-old",
        topic_uid=old_topic.topic_uid,
        atom_type="factual",
        content="old fact",
    )
    old_link = TopicTimelineLink(
        topic_uid=old_topic.topic_uid,
        timeline_uid="timeline-1",
        time_cluster_key="cluster-old",
    )
    old_source = TopicAtomSource(
        source_uid="source-old",
        topic_atom_uid=old_atom.atom_uid,
        timeline_uid="timeline-1",
        source_atom_fingerprint="old-fingerprint",
    )
    await store.save_topic_snapshot(
        old_topic,
        atoms=[old_atom],
        links=[old_link],
        atom_sources=[old_source],
    )
    run = await store.create_maintenance_run(
        TopicMaintenanceRun(memory_space_id=space_id, mode=TopicMaintenanceMode.FULL)
    )

    def snapshot(suffix: str, timeline_uid: str) -> dict:
        topic = TopicMemory(
            topic_uid=f"topic-{suffix}",
            memory_space_id=space_id,
            title=f"Topic {suffix}",
            summary=f"Summary {suffix}",
        )
        atom = TopicMemoryAtom(
            atom_uid=f"atom-{suffix}",
            topic_uid=topic.topic_uid,
            atom_type="factual",
            content=f"fact {suffix}",
        )
        link = TopicTimelineLink(
            topic_uid=topic.topic_uid,
            timeline_uid=timeline_uid,
            time_cluster_key=f"cluster-{suffix}",
        )
        source = TopicAtomSource(
            source_uid=f"source-{suffix}",
            topic_atom_uid=atom.atom_uid,
            timeline_uid=timeline_uid,
            source_atom_fingerprint=f"fingerprint-{suffix}",
        )
        return {
            "topic": topic,
            "atoms": [atom],
            "links": [link],
            "atom_sources": [source],
        }

    original = store._save_topic_snapshot_tx
    calls = 0

    async def fail_second_snapshot(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected publication failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "_save_topic_snapshot_tx", fail_second_snapshot)
    with pytest.raises(RuntimeError, match="injected publication failure"):
        await store.publish_topic_build(
            run_uid=run.run_uid,
            memory_space_id=space_id,
            mode=TopicMaintenanceMode.FULL,
            snapshots=[
                snapshot("new-1", "timeline-1"),
                snapshot("new-2", "timeline-2"),
            ],
            relations=[],
            reset_topics=True,
        )

    topics = await store.list_topics(space_id, limit=20)
    assert [(topic.topic_uid, topic.status.value) for topic in topics] == [
        ("topic-old", "active")
    ]
    persisted_run = await store.get_maintenance_run(run.run_uid)
    assert persisted_run["status"] == "pending"


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    row = await (
        await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        )
    ).fetchone()
    return row is not None


@pytest.mark.asyncio
async def test_topic_snapshot_uses_optimistic_revision_and_atomic_replacement(
    tmp_path: Path,
):
    db_path = str(tmp_path / "revisions.db")
    space_id = await _register_timeline(
        db_path, memory_uid="timeline-1", document_id=1
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-1",
        memory_space_id=space_id,
        title="初始标题",
        summary="初始摘要",
    )
    link = TopicTimelineLink(
        topic_uid=topic.topic_uid,
        timeline_uid="timeline-1",
        time_cluster_key="cluster-1",
    )
    saved = await store.save_topic_snapshot(
        topic, atoms=[], links=[link], atom_sources=[]
    )
    updated = await store.save_topic_snapshot(
        TopicMemory(
            topic_uid=saved.topic_uid,
            memory_space_id=space_id,
            title="更新标题",
            summary="更新摘要",
            revision=saved.revision,
        ),
        atoms=[],
        links=[link],
        atom_sources=[],
        expected_revision=1,
    )
    assert updated.revision == 2

    with pytest.raises(TopicRevisionConflict):
        await store.save_topic_snapshot(
            TopicMemory(
                topic_uid=saved.topic_uid,
                memory_space_id=space_id,
                title="过期写入",
                summary="不应覆盖",
                revision=1,
            ),
            atoms=[],
            links=[link],
            atom_sources=[],
            expected_revision=1,
        )
    current = await store.get_topic(saved.topic_uid)
    assert current is not None
    assert current.revision == 2
    assert current.summary == "更新摘要"


@pytest.mark.asyncio
async def test_topic_snapshot_rejects_cross_space_timeline(tmp_path: Path):
    db_path = str(tmp_path / "scope.db")
    space_a = await _register_timeline(
        db_path,
        memory_uid="timeline-a",
        document_id=1,
        session_id="bot:FriendMessage:a",
    )
    await _register_timeline(
        db_path,
        memory_uid="timeline-b",
        document_id=2,
        session_id="bot:FriendMessage:b",
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-a",
        memory_space_id=space_a,
        title="隔离测试",
        summary="不允许跨会话来源。",
    )
    bad_link = TopicTimelineLink(
        topic_uid=topic.topic_uid,
        timeline_uid="timeline-b",
        time_cluster_key="cluster-b",
    )

    with pytest.raises(TopicSourceValidationError):
        await store.save_topic_snapshot(
            topic, atoms=[], links=[bad_link], atom_sources=[]
        )
    assert await store.get_topic(topic.topic_uid) is None


@pytest.mark.asyncio
async def test_timeline_edit_marks_only_linked_topics_stale(tmp_path: Path):
    db_path = str(tmp_path / "stale.db")
    space_id = await _register_timeline(
        db_path, memory_uid="timeline-1", document_id=1
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    for index in (1, 2):
        topic = TopicMemory(
            topic_uid=f"topic-{index}",
            memory_space_id=space_id,
            title=f"主题 {index}",
            summary=f"摘要 {index}",
        )
        links = (
            [
                TopicTimelineLink(
                    topic_uid=topic.topic_uid,
                    timeline_uid="timeline-1",
                    time_cluster_key="cluster-1",
                )
            ]
            if index == 1
            else []
        )
        await store.save_topic_snapshot(
            topic, atoms=[], links=links, atom_sources=[]
        )

    affected = await store.mark_timeline_stale("timeline-1")
    assert affected == ["topic-1"]
    assert (await store.get_topic("topic-1")).status is TopicMemoryStatus.STALE
    assert (await store.get_topic("topic-2")).status is TopicMemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_topic_maintenance_run_is_resumable(tmp_path: Path):
    db_path = str(tmp_path / "runs.db")
    store = TopicMemoryStore(db_path)
    await store.initialize()
    run = TopicMaintenanceRun(
        run_uid="run-1",
        memory_space_id="space-1",
        mode=TopicMaintenanceMode.FULL,
        total_items=39,
    )
    await store.create_maintenance_run(run)
    assert await store.update_maintenance_run(
        run.run_uid,
        status=TopicMaintenanceStatus.RUNNING,
        cursor_memory_uid="timeline-8",
        processed_items=8,
        created_topics=2,
    )
    loaded = await store.get_maintenance_run(run.run_uid)
    assert loaded is not None
    assert loaded["status"] == "running"
    assert loaded["cursor_memory_uid"] == "timeline-8"
    assert loaded["processed_items"] == 8
    assert loaded["started_at"] is not None


@pytest.mark.asyncio
async def test_store_startup_marks_interrupted_run_as_resumable(tmp_path: Path):
    db_path = str(tmp_path / "interrupted.db")
    store = TopicMemoryStore(db_path)
    await store.initialize()
    run = TopicMaintenanceRun(
        run_uid="run-interrupted",
        memory_space_id="space-1",
        mode=TopicMaintenanceMode.FULL,
    )
    await store.create_maintenance_run(run)
    await store.update_maintenance_run(
        run.run_uid,
        status=TopicMaintenanceStatus.RUNNING,
        stage="topic_synthesis",
        current_group_index=3,
        total_groups=8,
    )

    await store.initialize()

    recovered = await store.get_maintenance_run(run.run_uid)
    assert recovered["status"] == "pending"
    assert recovered["stage"] == "topic_synthesis"
    assert recovered["current_group_index"] == 3
    assert recovered["completed_at"] is None
    assert "stopped" in recovered["error"]


@pytest.mark.asyncio
async def test_build_checkpoint_round_trip(tmp_path: Path):
    db_path = str(tmp_path / "checkpoint.db")
    store = TopicMemoryStore(db_path)
    await store.initialize()
    run = TopicMaintenanceRun(
        run_uid="run-checkpoint",
        memory_space_id="space-1",
        mode=TopicMaintenanceMode.FULL,
    )
    await store.create_maintenance_run(run)

    await store.save_build_checkpoint(
        run_uid=run.run_uid,
        checkpoint_key="topic_synthesis:abc",
        stage="topic_synthesis",
        input_hash="hash-1",
        payload={"title": "测试", "fragment_uids": ["fragment-1"]},
        metadata={"fragment_count": 1},
    )

    checkpoint = await store.get_build_checkpoint(
        run.run_uid,
        "topic_synthesis:abc",
    )
    assert checkpoint["input_hash"] == "hash-1"
    assert checkpoint["payload"]["title"] == "测试"
    assert checkpoint["metadata"]["fragment_count"] == 1


@pytest.mark.asyncio
async def test_discard_maintenance_run_clears_progress_but_preserves_topics(
    tmp_path: Path,
):
    db_path = str(tmp_path / "discard-checkpoint.db")
    space_id = await _register_timeline(
        db_path, memory_uid="timeline-1", document_id=1
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-materialized",
        memory_space_id=space_id,
        title="已写入 Topic",
        summary="正式 Topic 不属于可安全删除的断点数据。",
        metadata={"build_run_uid": "run-discard"},
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
    run = TopicMaintenanceRun(
        run_uid="run-discard",
        memory_space_id=space_id,
        mode=TopicMaintenanceMode.FULL,
    )
    await store.create_maintenance_run(run)
    await store.update_maintenance_run(
        run.run_uid,
        status=TopicMaintenanceStatus.FAILED,
        stage="topic_synthesis",
        error="timeout",
    )
    await store.save_build_checkpoint(
        run_uid=run.run_uid,
        checkpoint_key="topic_synthesis:abc",
        stage="topic_synthesis",
        input_hash="hash-1",
        payload={"title": "未完成"},
    )
    review_uid = await store.enqueue_maintenance_review(
        memory_space_id=space_id,
        review_type="ambiguous_topic_match",
        timeline_uids=["timeline-1"],
        topic_uids=[topic.topic_uid],
        details={"run_uid": run.run_uid},
    )

    result = await store.discard_maintenance_run(
        run.run_uid,
        memory_space_id=space_id,
    )

    assert result["deleted_run"] == 1
    assert result["deleted_intermediate_items"] == 2
    assert result["deleted_by_table"]["topic_build_checkpoints"] == 1
    assert result["deleted_by_table"]["topic_maintenance_queue"] == 1
    assert await store.get_maintenance_run(run.run_uid) is None
    assert await store.get_build_checkpoint(run.run_uid, "topic_synthesis:abc") is None
    assert not any(
        item["review_uid"] == review_uid
        for item in await store.list_maintenance_reviews(space_id)
    )
    assert await store.get_topic(topic.topic_uid) is not None


@pytest.mark.asyncio
async def test_list_reviews_cleans_legacy_orphans_from_discarded_runs(
    tmp_path: Path,
) -> None:
    store = TopicMemoryStore(tmp_path / "topic.db")
    await store.initialize()

    review_uid = await store.enqueue_maintenance_review(
        memory_space_id="space-orphan",
        review_type="topic_match_ambiguity",
        timeline_uids=["timeline-1"],
        topic_uids=[],
        details={"run_uid": "discarded-run", "reason": "legacy residue"},
    )

    assert await store.get_maintenance_review(review_uid) is not None
    assert await store.list_maintenance_reviews("space-orphan") == []
    assert await store.get_maintenance_review(review_uid) is None


@pytest.mark.asyncio
async def test_discard_maintenance_run_rejects_running_and_completed_runs(
    tmp_path: Path,
):
    store = TopicMemoryStore(str(tmp_path / "discard-status.db"))
    await store.initialize()
    for status in (
        TopicMaintenanceStatus.RUNNING,
        TopicMaintenanceStatus.COMPLETED,
    ):
        run = TopicMaintenanceRun(
            run_uid=f"run-{status.value}",
            memory_space_id="space-1",
            mode=TopicMaintenanceMode.FULL,
        )
        await store.create_maintenance_run(run)
        await store.update_maintenance_run(run.run_uid, status=status)

        with pytest.raises(ValueError, match="只能取消失败"):
            await store.discard_maintenance_run(run.run_uid)

        assert await store.get_maintenance_run(run.run_uid) is not None


@pytest.mark.asyncio
async def test_related_topics_are_replaced_and_return_neighbor_details(tmp_path: Path):
    db_path = str(tmp_path / "relations.db")
    space_id = await _register_timeline(
        db_path, memory_uid="timeline-1", document_id=1
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    for index in (1, 2):
        topic = TopicMemory(
            topic_uid=f"topic-{index}",
            memory_space_id=space_id,
            title=f"子话题 {index}",
            summary=f"同一事件的部分 {index}",
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

    count = await store.replace_topic_relations(
        space_id,
        [
            TopicRelation(
                relation_uid="relation-1",
                memory_space_id=space_id,
                left_topic_uid="topic-2",
                right_topic_uid="topic-1",
                confidence=0.72,
                semantic_similarity=0.72,
            )
        ],
    )

    assert count == 1
    related = await store.list_topic_relations("topic-1")
    assert related[0]["related_topic_uid"] == "topic-2"
    assert related[0]["related_title"] == "子话题 2"
    assert related[0]["semantic_similarity"] == pytest.approx(0.72)
    assert (await store.get_overview(space_id))["relation_count"] == 1

    await store.replace_topic_relations(space_id, [])
    assert await store.list_topic_relations("topic-1") == []


@pytest.mark.asyncio
async def test_revector_publish_rolls_back_topic_update_when_fragment_changed(
    tmp_path: Path,
):
    db_path = str(tmp_path / "revector-rollback.db")
    space_id = await _register_timeline(
        db_path, memory_uid="timeline-1", document_id=1
    )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    topic = TopicMemory(
        topic_uid="topic-1",
        memory_space_id=space_id,
        title="Topic",
        summary="Summary",
        embedding_signature={"provider_id": "old"},
        metadata={"embedding": [1.0, 0.0]},
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
    saved = await store.get_topic(topic.topic_uid)

    with pytest.raises(TopicSourceValidationError, match="changed"):
        await store.replace_embeddings_and_relations(
            memory_space_id=space_id,
            topic_updates=[
                {
                    "topic_uid": topic.topic_uid,
                    "expected_revision": saved.revision,
                    "embedding": [0.0, 1.0],
                    "embedding_signature": {"provider_id": "new"},
                }
            ],
            fragment_updates=[
                {
                    "fragment_uid": "missing-fragment",
                    "embedding": [0.0, 1.0],
                    "embedding_signature": {"provider_id": "new"},
                }
            ],
            relations=[],
        )

    unchanged = await store.get_topic(topic.topic_uid)
    assert unchanged.metadata["embedding"] == [1.0, 0.0]
    assert unchanged.embedding_signature == {"provider_id": "old"}
