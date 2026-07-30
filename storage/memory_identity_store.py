"""SQLite storage for stable memory identities and source spans."""

from __future__ import annotations

import json
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiosqlite


@dataclass(frozen=True, slots=True)
class MemoryRegistryRecord:
    memory_uid: str
    document_id: int
    memory_layer: str
    memory_space_id: str
    revision: int
    status: str
    created_at: float
    updated_at: float


class MemoryIdentityStore:
    """Keep logical identity independent from replaceable document IDs."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        async with self._connect() as db:
            await self.create_tables(db)
            await db.commit()

    @staticmethod
    async def create_tables(db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_registry (
                memory_uid TEXT PRIMARY KEY,
                document_id INTEGER NOT NULL UNIQUE,
                memory_layer TEXT NOT NULL DEFAULT 'timeline',
                memory_space_id TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_registry_space_layer
            ON memory_registry(memory_space_id, memory_layer, status)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_source_spans (
                memory_uid TEXT PRIMARY KEY,
                session_id TEXT,
                first_message_id INTEGER,
                last_message_id INTEGER,
                start_index INTEGER,
                end_index INTEGER,
                started_at REAL,
                ended_at REAL,
                traceability TEXT NOT NULL DEFAULT 'partial',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(memory_uid) REFERENCES memory_registry(memory_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_source_spans_session_time
            ON memory_source_spans(session_id, started_at, ended_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_source_snapshots (
                memory_uid TEXT PRIMARY KEY,
                source_revision INTEGER NOT NULL DEFAULT 1,
                source_json TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                retention_reason TEXT NOT NULL DEFAULT 'importance_threshold',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(memory_uid) REFERENCES memory_registry(memory_uid)
                    ON DELETE CASCADE
            )
            """
        )

    async def upsert_memory(
        self,
        *,
        memory_uid: str,
        document_id: int,
        memory_layer: str,
        memory_space_id: str,
        revision: int,
        created_at: float,
        updated_at: float | None = None,
        status: str = "active",
    ) -> None:
        now = time.time() if updated_at is None else float(updated_at)
        async with self._connect() as db:
            # A physical replacement can move one logical UID to a new document
            # ID. Remove only stale rows occupying that new physical ID.
            await db.execute(
                "DELETE FROM memory_registry WHERE document_id = ? AND memory_uid <> ?",
                (int(document_id), memory_uid),
            )
            await db.execute(
                """
                INSERT INTO memory_registry (
                    memory_uid, document_id, memory_layer, memory_space_id,
                    revision, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_uid) DO UPDATE SET
                    document_id = excluded.document_id,
                    memory_layer = excluded.memory_layer,
                    memory_space_id = excluded.memory_space_id,
                    revision = excluded.revision,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    memory_uid,
                    int(document_id),
                    memory_layer,
                    memory_space_id,
                    max(1, int(revision)),
                    status,
                    float(created_at),
                    now,
                ),
            )
            await db.commit()

    async def upsert_source_span(
        self,
        memory_uid: str,
        source_window: dict[str, Any] | None,
        *,
        fallback_session_id: str | None = None,
        fallback_time: float | None = None,
    ) -> None:
        source = source_window if isinstance(source_window, dict) else {}
        now = time.time()

        def optional_int(key: str) -> int | None:
            value = source.get(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def optional_float(key: str) -> float | None:
            value = source.get(key)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        first_message_id = optional_int("first_message_id")
        last_message_id = optional_int("last_message_id")
        start_index = optional_int("start_index")
        end_index = optional_int("end_index")
        started_at = optional_float("started_at")
        ended_at = optional_float("ended_at")
        if started_at is None:
            started_at = fallback_time
        if ended_at is None:
            ended_at = fallback_time

        if first_message_id is not None and last_message_id is not None:
            traceability = "full"
        elif source:
            traceability = "partial"
        else:
            traceability = "none"

        async with self._connect() as db:
            if not source and fallback_session_id is None:
                await db.execute(
                    "DELETE FROM memory_source_spans WHERE memory_uid = ?",
                    (memory_uid,),
                )
                await db.commit()
                return
            await db.execute(
                """
                INSERT INTO memory_source_spans (
                    memory_uid, session_id, first_message_id, last_message_id,
                    start_index, end_index, started_at, ended_at, traceability,
                    metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_uid) DO UPDATE SET
                    session_id = excluded.session_id,
                    first_message_id = excluded.first_message_id,
                    last_message_id = excluded.last_message_id,
                    start_index = excluded.start_index,
                    end_index = excluded.end_index,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    traceability = excluded.traceability,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    memory_uid,
                    source.get("session_id") or fallback_session_id,
                    first_message_id,
                    last_message_id,
                    start_index,
                    end_index,
                    started_at,
                    ended_at,
                    traceability,
                    json.dumps(source, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            await db.commit()

    async def get_by_uid(self, memory_uid: str) -> MemoryRegistryRecord | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM memory_registry WHERE memory_uid = ?", (memory_uid,)
            )
            row = await cursor.fetchone()
        return self._row_to_record(row) if row else None

    async def get_by_document_id(
        self, document_id: int
    ) -> MemoryRegistryRecord | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM memory_registry WHERE document_id = ?",
                (int(document_id),),
            )
            row = await cursor.fetchone()
        return self._row_to_record(row) if row else None

    async def get_source_span(self, memory_uid: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM memory_source_spans WHERE memory_uid = ?",
                (memory_uid,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def save_source_snapshot(
        self,
        memory_uid: str,
        source_messages: list[dict[str, Any]],
        *,
        source_revision: int,
        retention_reason: str,
    ) -> None:
        """Persist a source snapshot against the stable Timeline identity."""
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO memory_source_snapshots (
                    memory_uid, source_revision, source_json, message_count,
                    retention_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_uid) DO UPDATE SET
                    source_revision = excluded.source_revision,
                    source_json = excluded.source_json,
                    message_count = excluded.message_count,
                    retention_reason = excluded.retention_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    memory_uid,
                    max(1, int(source_revision)),
                    json.dumps(source_messages, ensure_ascii=False),
                    len(source_messages),
                    str(retention_reason or "importance_threshold"),
                    now,
                    now,
                ),
            )
            await db.commit()

    async def get_source_snapshot(self, memory_uid: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM memory_source_snapshots WHERE memory_uid = ?",
                    (memory_uid,),
                )
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            messages = json.loads(result.pop("source_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            messages = []
        result["messages"] = (
            [dict(item) for item in messages if isinstance(item, dict)]
            if isinstance(messages, list)
            else []
        )
        return result

    async def get_source_snapshot_by_document_id(
        self, document_id: int
    ) -> dict[str, Any] | None:
        record = await self.get_by_document_id(document_id)
        if record is None:
            return None
        return await self.get_source_snapshot(record.memory_uid)

    async def get_time_anchors_by_document_ids(
        self, document_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Resolve Timeline event times without depending on raw chat storage."""
        normalized = sorted({int(item) for item in document_ids})
        if not normalized:
            return {}
        placeholders = ",".join("?" * len(normalized))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT r.document_id, r.memory_uid, r.created_at,
                           s.started_at, s.ended_at, s.traceability,
                           s.metadata AS source_metadata
                    FROM memory_registry r
                    LEFT JOIN memory_source_spans s
                      ON s.memory_uid = r.memory_uid
                    WHERE r.document_id IN ({placeholders})
                      AND r.memory_layer = 'timeline'
                      AND r.status = 'active'
                    """,
                    normalized,
                )
            ).fetchall()
        output: dict[int, dict[str, Any]] = {}
        for row in rows:
            try:
                source_metadata = json.loads(row["source_metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                source_metadata = {}
            if not isinstance(source_metadata, dict):
                source_metadata = {}
            def finite(value: Any) -> float | None:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    return None
                return parsed if math.isfinite(parsed) else None

            source_start = finite(source_metadata.get("started_at"))
            source_end = finite(source_metadata.get("ended_at"))
            if source_start is not None or source_end is not None:
                started_at = source_start if source_start is not None else source_end
                ended_at = source_end if source_end is not None else source_start
                time_basis = "timeline_source_span"
                fallback = False
            else:
                started_at = row["created_at"]
                ended_at = row["created_at"]
                time_basis = "timeline_created_at"
                fallback = True
            output[int(row["document_id"])] = {
                "memory_uid": str(row["memory_uid"]),
                "started_at": float(started_at) if started_at is not None else None,
                "ended_at": float(ended_at) if ended_at is not None else None,
                "time_basis": time_basis,
                "time_fallback": fallback,
                "traceability": str(row["traceability"] or "none"),
            }
        return output

    async def list_timeline_document_ids(
        self,
        *,
        session_ids: list[str] | None = None,
        persona_id: str | None = None,
        limit: int = 2000,
    ) -> list[int]:
        """List active Timeline IDs in an explicit recall scope."""
        clauses = ["r.memory_layer = 'timeline'", "r.status = 'active'"]
        params: list[Any] = []
        normalized_sessions = sorted(
            {str(item) for item in (session_ids or []) if str(item)}
        )
        if normalized_sessions:
            placeholders = ",".join("?" * len(normalized_sessions))
            clauses.append(
                f"json_extract(d.metadata, '$.session_id') IN ({placeholders})"
            )
            params.extend(normalized_sessions)
        if persona_id is not None:
            clauses.append("json_extract(d.metadata, '$.persona_id') = ?")
            params.append(str(persona_id))
        params.append(max(1, min(int(limit), 10000)))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT r.document_id
                    FROM memory_registry r
                    JOIN documents d ON d.id = r.document_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY r.created_at, r.document_id
                    LIMIT ?
                    """,
                    params,
                )
            ).fetchall()
        return [int(row["document_id"]) for row in rows]

    async def delete_by_document_id(self, document_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM memory_registry WHERE document_id = ?",
                (int(document_id),),
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def delete_by_document_ids(self, document_ids: list[int]) -> int:
        normalized = sorted({int(item) for item in document_ids})
        if not normalized:
            return 0
        placeholders = ",".join("?" * len(normalized))
        async with self._connect() as db:
            cursor = await db.execute(
                f"DELETE FROM memory_registry WHERE document_id IN ({placeholders})",
                normalized,
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    @staticmethod
    def _row_to_record(row: aiosqlite.Row) -> MemoryRegistryRecord:
        return MemoryRegistryRecord(
            memory_uid=str(row["memory_uid"]),
            document_id=int(row["document_id"]),
            memory_layer=str(row["memory_layer"]),
            memory_space_id=str(row["memory_space_id"]),
            revision=int(row["revision"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


__all__ = ["MemoryIdentityStore", "MemoryRegistryRecord"]
