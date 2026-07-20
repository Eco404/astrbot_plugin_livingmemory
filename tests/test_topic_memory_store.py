from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.models.memory_identity import (
    resolve_memory_space,
)
from astrbot_plugin_livingmemory.core.models.topic_memory import (
    TopicAtomSource,
    TopicMaintenanceMode,
    TopicMaintenanceRun,
    TopicMaintenanceStatus,
    TopicMemory,
    TopicMemoryAtom,
    TopicMemoryStatus,
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

    assert result == {"deleted_topics": 1, "deleted_runs": 1}
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
