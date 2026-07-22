from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.models.memory_identity import (
    resolve_memory_space,
)
from astrbot_plugin_livingmemory.core.models.topic_memory import (
    TimelineTopicCandidate,
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

    result = await store.discard_maintenance_run(
        run.run_uid,
        memory_space_id=space_id,
    )

    assert result["deleted_run"] == 1
    assert result["deleted_intermediate_items"] == 1
    assert result["deleted_by_table"]["topic_build_checkpoints"] == 1
    assert await store.get_maintenance_run(run.run_uid) is None
    assert await store.get_build_checkpoint(run.run_uid, "topic_synthesis:abc") is None
    assert await store.get_topic(topic.topic_uid) is not None


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
