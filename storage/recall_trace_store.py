"""Persistent, bounded audit records for production and test recalls."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import aiosqlite


class RecallTraceStore:
    """Store recall snapshots without putting recording on the critical path."""

    TRACE_TYPES = {"production", "test"}

    def __init__(self, db_path: str, *, max_records_per_type: int = 200) -> None:
        self.db_path = db_path
        self.max_records_per_type = max(20, min(int(max_records_per_type), 2000))
        self._production_enabled = False

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await self.create_schema(db)
            await db.commit()
            row = await (
                await db.execute(
                    "SELECT production_enabled FROM recall_trace_settings WHERE id = 1"
                )
            ).fetchone()
            self._production_enabled = bool(row and row[0])

    @staticmethod
    async def create_schema(db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS recall_trace_records (
                trace_uid TEXT PRIMARY KEY,
                trace_type TEXT NOT NULL,
                status TEXT NOT NULL,
                session_id TEXT,
                persona_id TEXT,
                query_text TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'current',
                result_count INTEGER NOT NULL DEFAULT 0,
                elapsed_ms REAL NOT NULL DEFAULT 0,
                request_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                diagnostics_json TEXT NOT NULL DEFAULT '{}',
                injection_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recall_trace_type_created
            ON recall_trace_records(trace_type, created_at DESC)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS recall_trace_settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                production_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT OR IGNORE INTO recall_trace_settings (
                id, production_enabled, updated_at
            ) VALUES (1, 0, ?)
            """,
            (time.time(),),
        )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)

    @staticmethod
    def _loads(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(value) if isinstance(value, str) else value
        except (TypeError, json.JSONDecodeError):
            return fallback

    async def production_enabled(self) -> bool:
        return self._production_enabled

    async def set_production_enabled(self, enabled: bool) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO recall_trace_settings (id, production_enabled, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    production_enabled = excluded.production_enabled,
                    updated_at = excluded.updated_at
                """,
                (1 if enabled else 0, time.time()),
            )
            await db.commit()
        self._production_enabled = bool(enabled)
        return bool(enabled)

    async def record(
        self,
        *,
        trace_type: str,
        status: str,
        query_text: str,
        mode: str = "current",
        session_id: str | None = None,
        persona_id: str | None = None,
        result_count: int = 0,
        elapsed_ms: float = 0.0,
        request_data: Any = None,
        result_data: Any = None,
        diagnostics: Any = None,
        injection: Any = None,
        error: str | None = None,
        trace_uid: str | None = None,
    ) -> str:
        if trace_type not in self.TRACE_TYPES:
            raise ValueError(f"unsupported recall trace type: {trace_type}")
        uid = trace_uid or str(uuid.uuid4())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute(
                """
                INSERT INTO recall_trace_records (
                    trace_uid, trace_type, status, session_id, persona_id,
                    query_text, mode, result_count, elapsed_ms, request_json,
                    result_json, diagnostics_json, injection_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    trace_type,
                    str(status or "unknown"),
                    session_id,
                    persona_id,
                    str(query_text or ""),
                    str(mode or "current"),
                    max(0, int(result_count or 0)),
                    max(0.0, float(elapsed_ms or 0.0)),
                    self._json(request_data),
                    self._json(result_data),
                    self._json(diagnostics),
                    self._json(injection),
                    str(error)[:4000] if error else None,
                    time.time(),
                ),
            )
            await db.execute(
                """
                DELETE FROM recall_trace_records
                WHERE trace_type = ? AND trace_uid NOT IN (
                    SELECT trace_uid FROM recall_trace_records
                    WHERE trace_type = ?
                    ORDER BY created_at DESC LIMIT ?
                )
                """,
                (trace_type, trace_type, self.max_records_per_type),
            )
            await db.commit()
        return uid

    async def list_records(
        self, trace_type: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if trace_type not in self.TRACE_TYPES:
            raise ValueError("trace_type must be production or test")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT trace_uid, trace_type, status, session_id, persona_id,
                           query_text, mode, result_count, elapsed_ms, error, created_at
                    FROM recall_trace_records
                    WHERE trace_type = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (trace_type, max(1, min(int(limit), 200))),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def get_record(self, trace_uid: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM recall_trace_records WHERE trace_uid = ?",
                    (trace_uid,),
                )
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        for column, key in (
            ("request_json", "request"),
            ("result_json", "result"),
            ("diagnostics_json", "diagnostics"),
            ("injection_json", "injection"),
        ):
            item[key] = self._loads(item.pop(column, None), {})
        return item

    async def delete_record(self, trace_uid: str, *, trace_type: str | None = None) -> bool:
        params: list[Any] = [trace_uid]
        where = "trace_uid = ?"
        if trace_type is not None:
            if trace_type not in self.TRACE_TYPES:
                raise ValueError("trace_type must be production or test")
            where += " AND trace_type = ?"
            params.append(trace_type)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"DELETE FROM recall_trace_records WHERE {where}", params
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def clear_records(self, trace_type: str) -> int:
        if trace_type not in self.TRACE_TYPES:
            raise ValueError("trace_type must be production or test")
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM recall_trace_records WHERE trace_type = ?", (trace_type,)
            )
            await db.commit()
        return max(0, int(cursor.rowcount or 0))


__all__ = ["RecallTraceStore"]
