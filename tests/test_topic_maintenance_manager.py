from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.managers.topic_maintenance_manager import (
    TopicMaintenanceManager,
)
from astrbot_plugin_livingmemory.core.models.memory_identity import resolve_memory_space
from astrbot_plugin_livingmemory.core.models.topic_memory import TopicMaintenanceMode
from astrbot_plugin_livingmemory.storage.memory_identity_store import (
    MemoryIdentityStore,
)
from astrbot_plugin_livingmemory.storage.topic_memory_store import TopicMemoryStore


@pytest.mark.asyncio
async def test_selected_incremental_scan_persists_uid_scope_without_since():
    store = SimpleNamespace(create_maintenance_run=AsyncMock())
    manager = TopicMaintenanceManager(":memory:", store)
    manager._count_timelines = AsyncMock(return_value=2)
    manager.resume_scan = AsyncMock(return_value={"status": "completed"})

    result = await manager.start_scan(
        "space-1",
        mode=TopicMaintenanceMode.INCREMENTAL,
        timeline_uids=["timeline-2", "timeline-1", "timeline-2"],
        only_unindexed=True,
    )

    assert result == {"status": "completed"}
    run = store.create_maintenance_run.await_args.args[0]
    assert run.config["since"] is None
    assert run.config["timeline_uids"] == ["timeline-2", "timeline-1"]
    assert run.config["only_unindexed"] is True
    manager._count_timelines.assert_awaited_once_with(
        "space-1",
        since=None,
        timeline_uids=["timeline-2", "timeline-1"],
        only_unindexed=True,
    )


@pytest.mark.asyncio
async def test_incremental_scope_stays_bounded_to_selected_timelines():
    from astrbot_plugin_livingmemory.core.models.topic_memory import (
        TimelineTopicCandidate,
    )

    candidates = [
        TimelineTopicCandidate(
            memory_uid=f"timeline-{index}",
            document_id=index,
            source_revision=1,
            memory_space_id="space-1",
            session_id="session-1",
            content="京都旅行" if index < 3 else "Rust 学习",
            summary="京都旅行" if index < 3 else "Rust 学习",
            started_at=started,
            ended_at=started + 100,
            features={"normalized_topics": [], "fact_fingerprints": [], "lexical_tokens": []},
        )
        for index, started in ((1, 1000.0), (2, 1200.0), (3, 50000.0))
    ]
    store = SimpleNamespace(
        find_topics_linked_to_timelines=AsyncMock(return_value=[])
    )
    manager = TopicMaintenanceManager(":memory:", store)
    manager.load_candidates = AsyncMock(return_value=candidates)
    seeds = [candidates[1]]

    scope = await manager.prepare_incremental_scope(
        "space-1",
        seeds,
        time_gap_seconds=6 * 3600,
        max_timelines=20,
    )

    assert scope["seed_timeline_uids"] == ["timeline-2"]
    assert scope["seed_topic_uids"] == []
    assert scope["timeline_uids"] == ["timeline-2"]
    assert set(scope["time_cluster_keys"]) == {"timeline-2"}
    assert scope["pipeline"] == "delta_first"


@pytest.mark.asyncio
async def test_candidate_uid_scan_avoids_loading_timeline_documents(tmp_path: Path):
    db_path, space_id = await _create_timeline_db(tmp_path)
    store = TopicMemoryStore(db_path)
    manager = TopicMaintenanceManager(db_path, store)

    uids = await manager.list_candidate_uids(
        space_id,
        only_unindexed=True,
    )

    assert uids == [
        "timeline-travel-1",
        "timeline-travel-2",
        "timeline-code-1",
    ]


async def _create_timeline_db(tmp_path: Path) -> tuple[str, str]:
    db_path = str(tmp_path / "candidate_scan.db")
    session_id = "bot:FriendMessage:user-1"
    persona_id = "persona-1"
    space_id = resolve_memory_space(session_id, persona_id).memory_space_id
    memories = [
        {
            "uid": "timeline-travel-1",
            "text": "用户计划秋季去京都旅行。",
            "topics": ["旅行计划"],
            "facts": ["用户计划秋季去京都"],
            "started": 1000.0,
            "ended": 1100.0,
        },
        {
            "uid": "timeline-travel-2",
            "text": "继续讨论京都住宿和交通。",
            "topics": ["旅行计划"],
            "facts": ["用户计划秋季去京都"],
            "started": 1200.0,
            "ended": 1300.0,
        },
        {
            "uid": "timeline-code-1",
            "text": "用户开始学习 Rust 所有权。",
            "topics": ["Rust学习"],
            "facts": ["用户正在学习 Rust"],
            "started": 50000.0,
            "ended": 50100.0,
        },
    ]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                doc_id TEXT,
                text TEXT NOT NULL,
                metadata TEXT NOT NULL
            )
            """
        )
        for index, item in enumerate(memories, 1):
            metadata = {
                "session_id": session_id,
                "persona_id": persona_id,
                "canonical_summary": item["text"],
                "topics": item["topics"],
                "key_facts": item["facts"],
            }
            await db.execute(
                "INSERT INTO documents(id, doc_id, text, metadata) VALUES (?, ?, ?, ?)",
                (index, f"doc-{index}", item["text"], json.dumps(metadata)),
            )
        await db.commit()

    identity = MemoryIdentityStore(db_path)
    await identity.initialize()
    for index, item in enumerate(memories, 1):
        await identity.upsert_memory(
            memory_uid=item["uid"],
            document_id=index,
            memory_layer="timeline",
            memory_space_id=space_id,
            revision=1,
            created_at=item["started"],
            updated_at=item["ended"],
        )
        await identity.upsert_source_span(
            item["uid"],
            {
                "session_id": session_id,
                "first_message_id": index * 10,
                "last_message_id": index * 10 + 1,
                "started_at": item["started"],
                "ended_at": item["ended"],
            },
        )
    store = TopicMemoryStore(db_path)
    await store.initialize()
    return db_path, space_id


@pytest.mark.asyncio
async def test_full_scan_is_resumable_and_only_creates_preview(tmp_path: Path):
    db_path, space_id = await _create_timeline_db(tmp_path)
    store = TopicMemoryStore(db_path)
    manager = TopicMaintenanceManager(db_path, store)
    progress: list[dict] = []

    paused = await manager.start_scan(
        space_id,
        batch_size=1,
        max_batches=1,
        progress_callback=progress.append,
    )
    assert paused["status"] == "pending"
    assert paused["processed_items"] == 1
    assert paused["candidate_groups"] == 0

    completed = await manager.resume_scan(
        paused["run_uid"], progress_callback=progress.append
    )
    assert completed["status"] == "completed"
    assert completed["processed_items"] == 3
    assert completed["candidate_groups"] == 2
    assert progress[-1]["status"] == "completed"

    groups = completed["groups"]
    travel = next(group for group in groups if "timeline-travel-1" in group.timeline_uids)
    assert set(travel.timeline_uids) == {
        "timeline-travel-1",
        "timeline-travel-2",
    }
    assert len(travel.time_cluster_keys) == 1
    assert travel.metadata["time_cluster_count"] == 1
    assert travel.metadata["candidate_count"] == 2
    assert travel.metadata["requires_llm_review"] is True

    async with aiosqlite.connect(db_path) as db:
        topic_count = (
            await (await db.execute("SELECT COUNT(*) FROM topic_memories")).fetchone()
        )[0]
        item_count = (
            await (
                await db.execute("SELECT COUNT(*) FROM topic_maintenance_items")
            ).fetchone()
        )[0]
    assert topic_count == 0
    assert item_count == 3

    repeated = await manager.resume_scan(paused["run_uid"])
    assert [group.group_uid for group in repeated["groups"]] == [
        group.group_uid for group in completed["groups"]
    ]


@pytest.mark.asyncio
async def test_resume_reprocesses_changed_timeline_revision(tmp_path: Path):
    db_path, space_id = await _create_timeline_db(tmp_path)
    store = TopicMemoryStore(db_path)
    manager = TopicMaintenanceManager(db_path, store)
    paused = await manager.start_scan(space_id, batch_size=1, max_batches=1)

    identity = MemoryIdentityStore(db_path)
    current = await identity.get_by_uid("timeline-travel-1")
    assert current is not None
    await identity.upsert_memory(
        memory_uid=current.memory_uid,
        document_id=current.document_id,
        memory_layer="timeline",
        memory_space_id=current.memory_space_id,
        revision=2,
        created_at=current.created_at,
        updated_at=time.time(),
    )
    async with aiosqlite.connect(db_path) as db:
        metadata = {
            "session_id": "bot:FriendMessage:user-1",
            "persona_id": "persona-1",
            "canonical_summary": "用户取消了秋季京都旅行。",
            "topics": ["旅行计划"],
            "key_facts": ["用户取消秋季京都旅行"],
        }
        await db.execute(
            "UPDATE documents SET text = ?, metadata = ? WHERE id = 1",
            (metadata["canonical_summary"], json.dumps(metadata)),
        )
        await db.commit()

    completed = await manager.resume_scan(paused["run_uid"])
    assert completed["status"] == "completed"
    items = await store.get_scan_items(paused["run_uid"])
    changed = next(item for item in items if item.memory_uid == "timeline-travel-1")
    assert changed.source_revision == 2
    assert "取消" in changed.summary
    assert len({item.memory_uid for item in items}) == 3


def test_unrelated_nearby_fragments_have_low_score_but_share_review_window():
    from astrbot_plugin_livingmemory.core.models.topic_memory import (
        TimelineTopicCandidate,
    )

    left = TimelineTopicCandidate(
        memory_uid="left",
        document_id=1,
        source_revision=1,
        memory_space_id="space",
        session_id="session",
        content="讨论晚餐",
        summary="用户想吃寿司",
        topics=["饮食"],
        time_cluster_key="same-time",
        features={
            "normalized_topics": ["饮食"],
            "fact_fingerprints": ["fact-food"],
            "lexical_tokens": ["寿司"],
        },
    )
    right = TimelineTopicCandidate(
        memory_uid="right",
        document_id=2,
        source_revision=1,
        memory_space_id="space",
        session_id="session",
        content="讨论代码",
        summary="用户调试 Python",
        topics=["编程"],
        time_cluster_key="same-time",
        features={
            "normalized_topics": ["编程"],
            "fact_fingerprints": ["fact-code"],
            "lexical_tokens": ["python"],
        },
    )
    assert TopicMaintenanceManager.candidate_similarity(left, right) == 0.15
    groups = TopicMaintenanceManager.build_candidate_groups(
        "run",
        "space",
        [left, right],
        similarity_threshold=0.52,
    )
    assert len(groups) == 1
    assert groups[0].metadata["requires_llm_review"] is True
    assert groups[0].metadata["time_cluster_is_broad_context"] is True
