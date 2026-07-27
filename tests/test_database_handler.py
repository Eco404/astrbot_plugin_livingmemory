"""Tests for explicit database inspection and selected repairs."""

import asyncio
from pathlib import Path

import aiosqlite
import pytest
from astrbot_plugin_livingmemory.core.page_api_modules.database_handler import (
    DatabaseHandler,
)
from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils
from quart import Quart


async def _create_database_with_graph_orphan(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT)")
        await db.execute(
            """
            CREATE TABLE graph_edges (
                id INTEGER PRIMARY KEY,
                source_memory_id INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE graph_entries (
                id INTEGER PRIMARY KEY,
                source_memory_id INTEGER NOT NULL,
                edge_id INTEGER,
                relation_type TEXT,
                content TEXT,
                FOREIGN KEY(edge_id) REFERENCES graph_edges(id) ON DELETE CASCADE
            )
            """
        )
        await db.execute("INSERT INTO documents VALUES (7, 'timeline seven')")
        await db.execute("PRAGMA foreign_keys = OFF")
        await db.execute(
            "INSERT INTO graph_entries VALUES (1, 7, 999, 'related', 'orphan')"
        )
        await db.commit()


class _RepairingGraphManager:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.indexed: list[int] = []

    async def index_memory(self, memory_id, content, metadata, atoms) -> None:
        self.indexed.append(int(memory_id))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM graph_entries WHERE source_memory_id = ?", (memory_id,)
            )
            await db.commit()


class _FakeMemoryEngine:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.graph_memory_manager = _RepairingGraphManager(db_path)
        self.atom_store = None
        self.cache_invalidated = False

    async def get_memory(self, memory_id: int):
        if memory_id != 7:
            return None
        return {"id": 7, "text": "timeline seven", "metadata": {}}

    def _invalidate_search_cache(self) -> None:
        self.cache_invalidated = True


@pytest.mark.asyncio
async def test_database_health_keeps_graph_orphan_as_selectable_issue(tmp_path: Path):
    db_path = tmp_path / "health.db"
    await _create_database_with_graph_orphan(db_path)
    handler = DatabaseHandler(PageApiUtils())
    engine = _FakeMemoryEngine(str(db_path))

    result = await handler.check_health(engine, None)

    assert result["status"] == "ok"
    assert result["data"]["summary"]["issue_count"] == 1
    issue = result["data"]["issues"][0]
    assert issue["issue_uid"] == "graph_orphan_entries:7"
    assert issue["repair_action"] == "rebuild_graph_memory"
    assert issue["repairable"] is True

    async with aiosqlite.connect(db_path) as db:
        assert (
            await (await db.execute("SELECT COUNT(*) FROM graph_entries")).fetchone()
        )[0] == 1


@pytest.mark.asyncio
async def test_database_repair_only_processes_explicit_selection(tmp_path: Path):
    db_path = tmp_path / "repair.db"
    await _create_database_with_graph_orphan(db_path)
    handler = DatabaseHandler(PageApiUtils())
    engine = _FakeMemoryEngine(str(db_path))
    app = Quart(__name__)

    async with app.test_request_context(
        "/",
        method="POST",
        json={"issues": [{"issue_uid": "graph_orphan_entries:7"}]},
    ):
        result = await handler.repair(engine, None)

    assert result["status"] == "ok"
    assert result["data"]["failed"] == []
    assert result["data"]["repaired"][0]["memory_id"] == 7
    assert result["data"]["health"]["summary"]["status"] == "healthy"
    assert engine.graph_memory_manager.indexed == [7]
    assert engine.cache_invalidated is True


@pytest.mark.asyncio
async def test_database_repair_exposes_background_progress(tmp_path: Path):
    db_path = tmp_path / "repair-progress.db"
    await _create_database_with_graph_orphan(db_path)
    handler = DatabaseHandler(PageApiUtils())
    engine = _FakeMemoryEngine(str(db_path))
    app = Quart(__name__)

    async with app.test_request_context(
        "/",
        method="POST",
        json={"issues": ["graph_orphan_entries:7"]},
    ):
        started = await handler.start_repair(engine, None)

    assert started["status"] == "ok"
    job_uid = started["data"]["job_uid"]
    for _ in range(100):
        async with app.test_request_context(
            f"/?job_uid={job_uid}", method="GET"
        ):
            progress = await handler.get_repair_progress()
        if progress["data"]["status"] not in {"pending", "running"}:
            break
        await asyncio.sleep(0.01)

    assert progress["data"]["status"] == "completed"
    assert progress["data"]["percent"] == 100.0
    assert progress["data"]["current"] == progress["data"]["total"]
    assert progress["data"]["health"]["summary"]["status"] == "healthy"
    await handler.shutdown()
