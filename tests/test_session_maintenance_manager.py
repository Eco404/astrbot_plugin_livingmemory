"""Focused tests for resumable raw-session maintenance."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.managers.conversation_manager import (
    ConversationManager,
)
from astrbot_plugin_livingmemory.core.managers.session_maintenance_manager import (
    SessionMaintenanceManager,
)
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore


class _FakeMemoryEngine:
    def __init__(self):
        self.deleted_document_ids: list[int] = []
        self.alias_cache_invalidations = 0

    async def batch_delete_memories(self, document_ids: list[int]) -> int:
        self.deleted_document_ids.extend(document_ids)
        return len(document_ids)

    async def invalidate_session_alias_cache(self) -> None:
        self.alias_cache_invalidations += 1


async def _create_living_db(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE memory_registry (
                memory_uid TEXT PRIMARY KEY,
                document_id INTEGER NOT NULL,
                memory_layer TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE memory_source_spans (
                memory_uid TEXT PRIMARY KEY,
                session_id TEXT,
                first_message_id INTEGER,
                last_message_id INTEGER,
                started_at REAL,
                ended_at REAL,
                traceability TEXT NOT NULL DEFAULT 'partial',
                metadata TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            );
            CREATE TABLE topic_timeline_links (
                topic_uid TEXT NOT NULL,
                timeline_uid TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE topic_fragment_links (
                topic_uid TEXT NOT NULL,
                fragment_uid TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        await db.commit()


async def _wait_for_task(
    manager: SessionMaintenanceManager, task_uid: str
) -> dict:
    for _ in range(100):
        task = await manager.get_task(task_uid)
        if task and task["status"] in {"completed", "failed"}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("maintenance task did not finish")


@pytest.mark.asyncio
async def test_cleanup_summarized_keeps_unsummarized_and_marks_evidence(
    tmp_path: Path,
):
    conversation_store = ConversationStore(str(tmp_path / "conversations.db"))
    await conversation_store.initialize()
    conversation_manager = ConversationManager(store=conversation_store)
    living_db = tmp_path / "livingmemory.db"
    await _create_living_db(living_db)
    engine = _FakeMemoryEngine()
    manager = SessionMaintenanceManager(
        str(living_db), conversation_manager, engine
    )
    await manager.initialize()

    session_id = "test:FriendMessage:42"
    for index in range(4):
        await conversation_manager.add_message(
            session_id, "user", f"message-{index}"
        )
    await conversation_manager.update_session_metadata(
        session_id, "last_summarized_index", 2
    )
    all_messages = await conversation_store.get_messages(session_id, limit=10)
    async with aiosqlite.connect(living_db) as db:
        await db.execute(
            "INSERT INTO memory_registry VALUES (?, ?, 'timeline', 'active', ?)",
            ("memory-1", 101, time.time()),
        )
        await db.execute(
            """
            INSERT INTO memory_source_spans (
                memory_uid, session_id, first_message_id, last_message_id,
                started_at, ended_at, traceability, metadata, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'exact', '{}', ?)
            """,
            (
                "memory-1",
                session_id,
                all_messages[0].id,
                all_messages[1].id,
                time.time(),
                time.time(),
                time.time(),
            ),
        )
        await db.commit()

    preview = await manager.preview("cleanup_summarized", [session_id])
    assert preview["blocked_reasons"] == []
    assert preview["items"][0]["eligible_message_count"] == 2
    await conversation_manager.get_messages(session_id, limit=10)
    started = await manager.start_task("cleanup_summarized", [session_id], force=True)
    task = await _wait_for_task(manager, started["task_uid"])

    assert task["status"] == "completed"
    remaining = await conversation_store.get_messages(session_id, limit=10)
    assert [item.content for item in remaining] == ["message-2", "message-3"]
    manager_visible = await conversation_manager.get_messages(session_id, limit=10)
    assert [item.content for item in manager_visible] == ["message-2", "message-3"]
    assert (
        await conversation_manager.get_session_metadata(
            session_id, "last_summarized_index", 0
        )
        == 0
    )
    async with aiosqlite.connect(living_db) as db:
        row = await (
            await db.execute(
                "SELECT traceability, metadata FROM memory_source_spans"
            )
        ).fetchone()
    assert row[0] == "unavailable"
    assert json.loads(row[1])["raw_evidence_available"] is False

    await manager.shutdown()
    await conversation_store.close()


@pytest.mark.asyncio
async def test_delete_raw_session_preserves_derived_memory(tmp_path: Path):
    conversation_store = ConversationStore(str(tmp_path / "conversations.db"))
    await conversation_store.initialize()
    conversation_manager = ConversationManager(store=conversation_store)
    living_db = tmp_path / "livingmemory.db"
    await _create_living_db(living_db)
    manager = SessionMaintenanceManager(
        str(living_db), conversation_manager, _FakeMemoryEngine()
    )
    await manager.initialize()

    session_id = "test:FriendMessage:99"
    await conversation_manager.add_message(session_id, "user", "summarized")
    await conversation_manager.update_session_metadata(
        session_id, "last_summarized_index", 1
    )
    message = (await conversation_store.get_messages(session_id, limit=1))[0]
    await conversation_manager.get_messages(session_id, limit=10)
    async with aiosqlite.connect(living_db) as db:
        await db.execute(
            "INSERT INTO memory_registry VALUES (?, ?, 'timeline', 'active', ?)",
            ("memory-2", 102, time.time()),
        )
        await db.execute(
            """
            INSERT INTO memory_source_spans VALUES (
                ?, ?, ?, ?, ?, ?, 'exact', '{}', ?
            )
            """,
            (
                "memory-2",
                session_id,
                message.id,
                message.id,
                time.time(),
                time.time(),
                time.time(),
            ),
        )
        await db.commit()

    started = await manager.start_task(
        "delete_raw_keep_memory", [session_id], force=True
    )
    task = await _wait_for_task(manager, started["task_uid"])
    assert task["status"] == "completed"
    assert await conversation_store.get_session(session_id) is None
    assert await conversation_manager.get_messages(session_id, limit=10) == []
    async with aiosqlite.connect(living_db) as db:
        registry_count = (
            await (await db.execute("SELECT COUNT(*) FROM memory_registry")).fetchone()
        )[0]
        source = await (
            await db.execute(
                "SELECT traceability, metadata FROM memory_source_spans"
            )
        ).fetchone()
    assert registry_count == 1
    assert source[0] == "unavailable"
    assert json.loads(source[1])["raw_evidence_available"] is False

    await manager.shutdown()
    await conversation_store.close()


@pytest.mark.asyncio
async def test_merge_aliases_is_persisted_and_invalidates_recall_scope(
    tmp_path: Path,
):
    conversation_store = ConversationStore(str(tmp_path / "conversations.db"))
    await conversation_store.initialize()
    conversation_manager = ConversationManager(store=conversation_store)
    living_db = tmp_path / "livingmemory.db"
    await _create_living_db(living_db)
    engine = _FakeMemoryEngine()
    manager = SessionMaintenanceManager(
        str(living_db), conversation_manager, engine
    )
    await manager.initialize()

    canonical = "test:FriendMessage:current"
    alias = "test:private:legacy"
    await conversation_manager.add_message(canonical, "user", "current")
    await conversation_manager.add_message(alias, "user", "legacy")
    started = await manager.start_task(
        "merge_aliases",
        [canonical, alias],
        canonical_session_id=canonical,
    )
    task = await _wait_for_task(manager, started["task_uid"])

    assert task["status"] == "completed"
    assert await conversation_manager.get_session_scope(alias) == [canonical, alias]
    assert engine.alias_cache_invalidations == 1

    await manager.shutdown()
    await conversation_store.close()


@pytest.mark.asyncio
async def test_delete_memory_chain_removes_raw_session_timeline_and_aliases(
    tmp_path: Path,
):
    conversation_store = ConversationStore(str(tmp_path / "conversations.db"))
    await conversation_store.initialize()
    conversation_manager = ConversationManager(store=conversation_store)
    living_db = tmp_path / "livingmemory.db"
    await _create_living_db(living_db)
    engine = _FakeMemoryEngine()
    manager = SessionMaintenanceManager(
        str(living_db), conversation_manager, engine
    )
    await manager.initialize()

    canonical = "test:FriendMessage:delete-me"
    alias = "test:private:delete-me"
    await conversation_manager.add_message(canonical, "user", "raw message")
    await conversation_manager.add_message(alias, "user", "legacy raw message")
    message = (await conversation_store.get_messages(canonical, limit=1))[0]
    alias_message = (await conversation_store.get_messages(alias, limit=1))[0]
    await conversation_store.set_session_aliases(canonical, [alias])
    async with aiosqlite.connect(living_db) as db:
        await db.execute(
            "INSERT INTO memory_registry VALUES (?, ?, 'timeline', 'active', ?)",
            ("memory-delete", 301, time.time()),
        )
        await db.execute(
            """
            INSERT INTO memory_source_spans VALUES (
                ?, ?, ?, ?, ?, ?, 'exact', '{}', ?
            )
            """,
            (
                "memory-delete",
                canonical,
                message.id,
                message.id,
                time.time(),
                time.time(),
                time.time(),
            ),
        )
        await db.execute(
            "INSERT INTO memory_registry VALUES (?, ?, 'timeline', 'active', ?)",
            ("memory-delete-alias", 302, time.time()),
        )
        await db.execute(
            """
            INSERT INTO memory_source_spans VALUES (
                ?, ?, ?, ?, ?, ?, 'exact', '{}', ?
            )
            """,
            (
                "memory-delete-alias",
                alias,
                alias_message.id,
                alias_message.id,
                time.time(),
                time.time(),
                time.time(),
            ),
        )
        await db.commit()

    started = await manager.start_task(
        "delete_memory_chain", [canonical], force=True
    )
    task = await _wait_for_task(manager, started["task_uid"])

    assert task["status"] == "completed"
    assert task["source_session_ids"] == [canonical, alias]
    assert engine.deleted_document_ids == [301, 302]
    assert await conversation_store.get_session(canonical) is None
    assert await conversation_store.get_session(alias) is None
    assert await conversation_store.resolve_session_id(alias) == alias

    await manager.shutdown()
    await conversation_store.close()


@pytest.mark.asyncio
async def test_running_maintenance_task_resumes_after_manager_restart(tmp_path: Path):
    conversation_store = ConversationStore(str(tmp_path / "conversations.db"))
    await conversation_store.initialize()
    conversation_manager = ConversationManager(store=conversation_store)
    living_db = tmp_path / "livingmemory.db"
    await _create_living_db(living_db)
    bootstrap = SessionMaintenanceManager(
        str(living_db), conversation_manager, _FakeMemoryEngine()
    )
    await bootstrap.initialize()
    await bootstrap.shutdown()

    session_id = "test:FriendMessage:resume"
    await conversation_manager.add_message(session_id, "user", "summarized")
    await conversation_manager.update_session_metadata(
        session_id, "last_summarized_index", 1
    )
    now = time.time()
    async with aiosqlite.connect(living_db) as db:
        await db.execute(
            """
            INSERT INTO session_maintenance_tasks (
                task_uid, operation, status, source_session_ids,
                current_step, payload, result, created_at, updated_at
            ) VALUES (?, 'cleanup_summarized', 'running', ?, 'planned', '{}', '{}', ?, ?)
            """,
            ("resume-task", json.dumps([session_id]), now, now),
        )
        await db.commit()

    resumed = SessionMaintenanceManager(
        str(living_db), conversation_manager, _FakeMemoryEngine()
    )
    await resumed.initialize()
    task = await _wait_for_task(resumed, "resume-task")

    assert task["status"] == "completed"
    assert await conversation_store.get_messages(session_id, limit=10) == []
    assert any(event["step"].startswith("processing:") for event in task["events"])

    await resumed.shutdown()
    await conversation_store.close()
