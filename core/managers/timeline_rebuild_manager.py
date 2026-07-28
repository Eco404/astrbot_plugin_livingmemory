"""Resumable, source-verified Timeline reconstruction tasks."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from typing import Any, Iterable

import aiosqlite

from astrbot.api import logger

from ..models.conversation_identity import audit_message_identity
from ..models.topic_memory import TopicMaintenanceMode


class TimelineRebuildManager:
    """Rebuild Timeline memories from immutable raw-message spans.

    The source snapshot is checked both during preview and immediately before
    writing.  A task never substitutes offset-based or partial conversation
    data when the original message-ID span is no longer complete.
    """

    TOPIC_MODES = {"local", "full"}
    TERMINAL_STATUSES = {
        "completed",
        "completed_with_review",
        "completed_with_errors",
        "failed",
        "cancelled",
    }

    def __init__(
        self,
        db_path: str,
        conversation_manager: Any,
        memory_engine: Any,
        memory_processor: Any,
    ) -> None:
        self.db_path = db_path
        self.conversation_manager = conversation_manager
        self.memory_engine = memory_engine
        self.memory_processor = memory_processor
        self._tasks: dict[str, asyncio.Task] = {}
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await self.create_tables(db)
            await db.commit()
            rows = await (
                await db.execute(
                    "SELECT task_uid FROM timeline_rebuild_tasks "
                    "WHERE status IN ('running', 'queued', 'cancelling') "
                    "ORDER BY created_at"
                )
            ).fetchall()
        for row in rows:
            self._launch(str(row[0]))

    @staticmethod
    async def create_tables(db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS timeline_rebuild_tasks (
                task_uid TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                topic_mode TEXT NOT NULL DEFAULT 'local',
                current_step TEXT NOT NULL DEFAULT 'planned',
                total_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL DEFAULT '{}',
                result TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS timeline_staged_edits (
                edit_uid TEXT PRIMARY KEY,
                memory_id INTEGER NOT NULL,
                memory_uid TEXT NOT NULL,
                memory_space_id TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                prepared_payload TEXT NOT NULL DEFAULT '{}',
                field_changes TEXT NOT NULL DEFAULT '[]',
                reason TEXT NOT NULL DEFAULT '',
                source_excerpt TEXT NOT NULL DEFAULT '',
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                applied_at REAL
            )
            """
        )
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_timeline_staged_edits_pending
            ON timeline_staged_edits(memory_id)
            WHERE status IN ('pending', 'failed')
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS timeline_rebuild_items (
                task_uid TEXT NOT NULL,
                memory_id INTEGER NOT NULL,
                memory_uid TEXT NOT NULL,
                memory_space_id TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                source_snapshot TEXT NOT NULL DEFAULT '{}',
                result TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY(task_uid, memory_id),
                FOREIGN KEY(task_uid) REFERENCES timeline_rebuild_tasks(task_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_timeline_rebuild_items_status
            ON timeline_rebuild_items(task_uid, status)
            """
        )

    async def shutdown(self) -> None:
        pending = [task for task in self._tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    @staticmethod
    def _loads(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback

    async def preview(
        self,
        memory_ids: Iterable[int] | None = None,
        *,
        memory_space_id: str | None = None,
        quality_filter: str = "all",
        limit: int = 500,
    ) -> dict[str, Any]:
        quality_filter = str(quality_filter or "all").strip().lower()
        if quality_filter not in {"all", "low"}:
            raise ValueError("quality_filter 仅支持 all 或 low")
        records = await self._candidate_records(
            memory_ids,
            memory_space_id=memory_space_id,
            quality_filter=quality_filter,
            limit=limit,
        )
        items = [await self._inspect_record(record) for record in records]
        if quality_filter == "low":
            items = [
                item for item in items if item.get("summary_quality") == "low"
            ]
        return {
            "items": items,
            "total_count": len(items),
            "reconstructable_count": sum(
                1 for item in items if item["reconstructable"]
            ),
            "blocked_count": sum(
                1 for item in items if not item["reconstructable"]
            ),
            "quality_filter": quality_filter,
        }

    async def _candidate_records(
        self,
        memory_ids: Iterable[int] | None,
        *,
        memory_space_id: str | None,
        quality_filter: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        ids_were_supplied = memory_ids is not None
        normalized_ids = list(
            dict.fromkeys(
                int(value)
                for value in (memory_ids or [])
                if str(value).strip().lstrip("-").isdigit()
            )
        )
        if ids_were_supplied and not normalized_ids:
            return []
        clauses = ["r.memory_layer = 'timeline'", "r.status = 'active'"]
        params: list[Any] = []
        if normalized_ids:
            placeholders = ",".join("?" for _ in normalized_ids)
            clauses.append(f"r.document_id IN ({placeholders})")
            params.extend(normalized_ids)
        if memory_space_id:
            clauses.append("r.memory_space_id = ?")
            params.append(str(memory_space_id))
        params.append(max(1, min(int(limit), 2000)))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"""
                    SELECT r.document_id, r.memory_uid, r.memory_space_id,
                           r.revision, r.updated_at,
                           s.session_id, s.first_message_id, s.last_message_id,
                           s.start_index, s.end_index, s.traceability,
                           s.metadata AS source_metadata,
                           (SELECT COUNT(DISTINCT l.topic_uid)
                              FROM topic_timeline_links l
                             WHERE l.timeline_uid = r.memory_uid
                               AND l.status = 'active') AS topic_count
                    FROM memory_registry r
                    LEFT JOIN memory_source_spans s ON s.memory_uid = r.memory_uid
                    WHERE {' AND '.join(clauses)}
                    ORDER BY r.document_id DESC
                    LIMIT ?
                    """,
                    params,
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_inactive_timelines(
        self,
        memory_space_id: str,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        memory_space_id = str(memory_space_id or "").strip()
        if not memory_space_id:
            raise ValueError("memory_space_id is required")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT r.document_id, r.memory_uid, r.memory_space_id,
                           r.revision, r.status, r.updated_at,
                           d.text, d.metadata,
                           (SELECT COUNT(DISTINCT l.topic_uid)
                              FROM topic_timeline_links l
                             WHERE l.timeline_uid = r.memory_uid
                               AND l.status = 'active') AS topic_count
                    FROM memory_registry r
                    JOIN documents d ON d.id = r.document_id
                    WHERE r.memory_layer = 'timeline'
                      AND r.memory_space_id = ?
                      AND r.status != 'active'
                    ORDER BY r.updated_at DESC, r.document_id DESC
                    LIMIT ?
                    """,
                    (memory_space_id, max(1, min(int(limit), 2000))),
                )
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            metadata = self._loads(item.get("metadata"), {})
            text = str(item.get("text") or "")
            result.append(
                {
                    "memory_id": int(item["document_id"]),
                    "memory_uid": str(item["memory_uid"]),
                    "memory_space_id": str(item["memory_space_id"]),
                    "revision": int(item["revision"]),
                    "status": str(item["status"]),
                    "updated_at": float(item["updated_at"] or 0.0),
                    "topic_count": int(item.get("topic_count") or 0),
                    "summary_quality": str(
                        (metadata if isinstance(metadata, dict) else {}).get(
                            "summary_quality", "unknown"
                        )
                    ),
                    "excerpt": text[:180] + ("..." if len(text) > 180 else ""),
                }
            )
        return result

    async def restore_inactive_timelines(
        self,
        memory_space_id: str,
        memory_ids: Iterable[int],
    ) -> dict[str, Any]:
        memory_space_id = str(memory_space_id or "").strip()
        normalized_ids = sorted({int(value) for value in memory_ids})
        if not memory_space_id or not normalized_ids:
            raise ValueError("至少选择一条不活跃 Timeline")
        placeholders = ",".join("?" * len(normalized_ids))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"""
                    SELECT document_id, memory_uid
                    FROM memory_registry
                    WHERE memory_layer = 'timeline'
                      AND memory_space_id = ?
                      AND status != 'active'
                      AND document_id IN ({placeholders})
                    """,
                    [memory_space_id, *normalized_ids],
                )
            ).fetchall()
        eligible = {int(row["document_id"]): str(row["memory_uid"]) for row in rows}
        restored_uids: list[str] = []
        failed_ids: list[int] = []
        for memory_id in normalized_ids:
            memory_uid = eligible.get(memory_id)
            if not memory_uid:
                failed_ids.append(memory_id)
                continue
            try:
                if await self.memory_engine.update_memory(
                    memory_id, {"metadata": {"status": "active"}}
                ):
                    restored_uids.append(memory_uid)
                else:
                    failed_ids.append(memory_id)
            except Exception:
                failed_ids.append(memory_id)
        if restored_uids:
            self.memory_engine._schedule_topic_maintenance(
                memory_space_id,
                full=False,
                timeline_uids=restored_uids,
            )
        return {
            "restored_count": len(restored_uids),
            "failed_count": len(failed_ids),
            "failed_ids": failed_ids,
            "timeline_uids": restored_uids,
        }

    async def _inspect_record(self, record: dict[str, Any]) -> dict[str, Any]:
        memory_id = int(record["document_id"])
        memory = await self.memory_engine.get_memory(memory_id)
        metadata = self._loads(
            memory.get("metadata") if memory else None,
            memory.get("metadata", {}) if memory else {},
        )
        if not isinstance(metadata, dict):
            metadata = {}
        source = self._loads(record.get("source_metadata"), {})
        if not isinstance(source, dict):
            source = {}
        session_id = str(record.get("session_id") or source.get("session_id") or "")
        reasons: list[str] = []
        messages: list[Any] = []
        first_id = record.get("first_message_id")
        last_id = record.get("last_message_id")
        if not memory:
            reasons.append("Timeline 正文不存在")
        if str(record.get("traceability") or "") != "full":
            reasons.append("缺少完整的原始消息 ID 边界")
        if not session_id:
            reasons.append("缺少来源会话 ID")
        if first_id is None or last_id is None:
            reasons.append("缺少首尾原始消息 ID")
        if not reasons:
            expected = self._expected_message_count(source, record)
            messages = await self.conversation_manager.get_messages_by_id_span(
                session_id,
                int(first_id),
                int(last_id),
                limit=max(100, expected + 1, 2000),
            )
            if not messages:
                reasons.append("原始消息已不存在")
            else:
                ids = [int(message.id) for message in messages]
                if ids[0] != int(first_id) or ids[-1] != int(last_id):
                    reasons.append("原始消息首尾边界不完整")
                if expected and len(messages) != expected:
                    reasons.append(
                        f"原始消息数量不完整（期望 {expected}，实际 {len(messages)}）"
                    )
        identity_issues = sorted(
            {
                issue
                for message in messages
                for issue in audit_message_identity(message)
            }
        )
        excerpt = str(memory.get("text") or "") if memory else ""
        snapshot = {
            "memory_id": memory_id,
            "memory_uid": str(record["memory_uid"]),
            "memory_space_id": str(record["memory_space_id"]),
            "revision": int(record["revision"]),
            "session_id": session_id,
            "first_message_id": int(first_id) if first_id is not None else None,
            "last_message_id": int(last_id) if last_id is not None else None,
            "message_count": len(messages),
            "expected_message_count": self._expected_message_count(source, record),
            "persona_id": str(metadata.get("persona_id") or "default"),
            "is_group_chat": bool(messages and messages[0].group_id)
            or "GroupMessage" in session_id,
            "topic_count": int(record.get("topic_count") or 0),
            "summary_quality": str(metadata.get("summary_quality") or "unknown"),
        }
        return {
            **snapshot,
            "excerpt": excerpt[:180],
            "topic_count": int(record.get("topic_count") or 0),
            "identity_warnings": identity_issues,
            "summary_quality": snapshot["summary_quality"],
            "reconstructable": not reasons,
            "blocked_reasons": reasons,
            "source_snapshot": snapshot,
        }

    @staticmethod
    def _expected_message_count(
        source: dict[str, Any], record: dict[str, Any]
    ) -> int:
        for value in (
            source.get("message_count"),
            (
                int(record["end_index"]) - int(record["start_index"])
                if record.get("start_index") is not None
                and record.get("end_index") is not None
                else 0
            ),
        ):
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return 0

    async def start_task(
        self,
        memory_ids: Iterable[int],
        *,
        topic_mode: str = "local",
    ) -> dict[str, Any]:
        topic_mode = str(topic_mode or "local")
        if topic_mode not in self.TOPIC_MODES:
            raise ValueError("Topic 同步方式必须是 local 或 full")
        preview = await self.preview(memory_ids, limit=2000)
        selected = [item for item in preview["items"] if item["reconstructable"]]
        blocked = [item for item in preview["items"] if not item["reconstructable"]]
        if blocked:
            details = "；".join(
                f"ID {item['memory_id']}: {'、'.join(item['blocked_reasons'])}"
                for item in blocked[:8]
            )
            raise ValueError("所选 Timeline 中包含不可重构项：" + details)
        if not selected:
            raise ValueError("至少选择一条可重构 Timeline")
        task_uid = str(uuid.uuid4())
        now = time.time()
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            selected_ids = [int(item["memory_id"]) for item in selected]
            placeholders = ",".join("?" for _ in selected_ids)
            overlaps = await (
                await db.execute(
                    f"""
                    SELECT DISTINCT i.memory_id
                    FROM timeline_rebuild_items i
                    JOIN timeline_rebuild_tasks t ON t.task_uid = i.task_uid
                    WHERE t.status IN ('queued', 'running', 'cancelling')
                      AND i.memory_id IN ({placeholders})
                    """,
                    selected_ids,
                )
            ).fetchall()
            if overlaps:
                raise ValueError(
                    "以下 Timeline 已有重构任务："
                    + "、".join(str(row[0]) for row in overlaps)
                )
            await db.execute(
                """
                INSERT INTO timeline_rebuild_tasks (
                    task_uid, status, topic_mode, current_step, total_count,
                    payload, created_at, updated_at
                ) VALUES (?, 'queued', ?, 'planned', ?, ?, ?, ?)
                """,
                (
                    task_uid,
                    topic_mode,
                    len(selected),
                    json.dumps({"memory_ids": selected_ids}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            await db.executemany(
                """
                INSERT INTO timeline_rebuild_items (
                    task_uid, memory_id, memory_uid, memory_space_id,
                    source_revision, status, source_snapshot, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                [
                    (
                        task_uid,
                        int(item["memory_id"]),
                        item["memory_uid"],
                        item["memory_space_id"],
                        int(item["revision"]),
                        json.dumps(item["source_snapshot"], ensure_ascii=False),
                        now,
                    )
                    for item in selected
                ],
            )
            await db.commit()
        self._launch(task_uid)
        return await self.get_task(task_uid) or {"task_uid": task_uid, "status": "queued"}

    async def stage_edit(
        self,
        *,
        memory_id: int,
        prepared_payload: dict[str, Any],
        field_changes: list[dict[str, Any]] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        memory = await self.memory_engine.get_memory(int(memory_id))
        if not memory:
            raise ValueError("Timeline 记忆不存在")
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT memory_uid, memory_space_id, revision, memory_layer
                    FROM memory_registry
                    WHERE document_id = ? AND status = 'active'
                    """,
                    (int(memory_id),),
                )
            ).fetchone()
            if not row or str(row["memory_layer"] or "") != "timeline":
                raise ValueError("只能暂存 Timeline 记忆的修改")
            active_task = await (
                await db.execute(
                    """
                    SELECT t.task_uid
                    FROM timeline_rebuild_items i
                    JOIN timeline_rebuild_tasks t ON t.task_uid = i.task_uid
                    WHERE i.memory_id = ?
                      AND t.status IN ('queued', 'running', 'cancelling')
                    LIMIT 1
                    """,
                    (int(memory_id),),
                )
            ).fetchone()
            if active_task is not None:
                raise ValueError("Timeline 正在执行维护任务，请等待任务结束后再暂存")
            applying_edit = await (
                await db.execute(
                    "SELECT edit_uid FROM timeline_staged_edits "
                    "WHERE memory_id = ? AND status = 'applying' LIMIT 1",
                    (int(memory_id),),
                )
            ).fetchone()
            if applying_edit is not None:
                raise ValueError("Timeline 的暂存修改正在应用，请等待任务结束")
            now = time.time()
            edit_uid = str(uuid.uuid4())
            await db.execute(
                "DELETE FROM timeline_staged_edits "
                "WHERE memory_id = ? AND status IN ('pending', 'failed')",
                (int(memory_id),),
            )
            await db.execute(
                """
                INSERT INTO timeline_staged_edits(
                    edit_uid, memory_id, memory_uid, memory_space_id,
                    source_revision, status, prepared_payload, field_changes,
                    reason, source_excerpt, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    edit_uid,
                    int(memory_id),
                    str(row["memory_uid"]),
                    str(row["memory_space_id"]),
                    int(row["revision"]),
                    json.dumps(prepared_payload, ensure_ascii=False),
                    json.dumps(field_changes or [], ensure_ascii=False),
                    str(reason or ""),
                    str(memory.get("text") or "")[:240],
                    now,
                    now,
                ),
            )
            await db.commit()
        return {
            "edit_uid": edit_uid,
            "memory_id": int(memory_id),
            "memory_uid": str(row["memory_uid"]),
            "memory_space_id": str(row["memory_space_id"]),
            "source_revision": int(row["revision"]),
            "status": "pending",
        }

    async def list_staged_edits(
        self,
        *,
        memory_space_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses = ["status IN ('pending', 'failed')"]
        params: list[Any] = []
        if memory_space_id:
            clauses.append("memory_space_id = ?")
            params.append(str(memory_space_id))
        params.append(max(1, min(int(limit), 2000)))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"SELECT * FROM timeline_staged_edits WHERE {' AND '.join(clauses)} "
                    "ORDER BY created_at DESC LIMIT ?",
                    params,
                )
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            prepared = self._loads(item.pop("prepared_payload", None), {})
            item["field_changes"] = self._loads(item.get("field_changes"), [])
            item["preview"] = str(prepared.get("content") or "")[:240]
            result.append(item)
        return result

    async def delete_staged_edits(self, edit_uids: Iterable[str]) -> int:
        normalized = sorted({str(uid).strip() for uid in edit_uids if str(uid).strip()})
        if not normalized:
            return 0
        placeholders = ",".join("?" for _ in normalized)
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"DELETE FROM timeline_staged_edits WHERE edit_uid IN ({placeholders}) "
                "AND status IN ('pending', 'failed')",
                normalized,
            )
            await db.commit()
        return max(0, int(cursor.rowcount or 0))

    async def start_staged_task(
        self,
        edit_uids: Iterable[str],
        *,
        topic_mode: str = "local",
    ) -> dict[str, Any]:
        topic_mode = str(topic_mode or "local")
        if topic_mode not in self.TOPIC_MODES:
            raise ValueError("Topic 同步方式必须是 local 或 full")
        normalized = sorted({str(uid).strip() for uid in edit_uids if str(uid).strip()})
        if not normalized:
            raise ValueError("至少选择一条暂存修改")
        placeholders = ",".join("?" for _ in normalized)
        task_uid = str(uuid.uuid4())
        now = time.time()
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            rows = await (
                await db.execute(
                    f"""
                    SELECT e.*, r.revision AS current_revision, r.status AS memory_status,
                           (SELECT COUNT(DISTINCT l.topic_uid)
                              FROM topic_timeline_links l
                             WHERE l.timeline_uid = e.memory_uid
                               AND l.status = 'active') AS topic_count
                    FROM timeline_staged_edits e
                    LEFT JOIN memory_registry r
                      ON r.document_id = e.memory_id AND r.memory_uid = e.memory_uid
                    WHERE e.edit_uid IN ({placeholders})
                      AND e.status IN ('pending', 'failed')
                    """,
                    normalized,
                )
            ).fetchall()
            if len(rows) != len(normalized):
                raise ValueError("部分暂存修改已不存在或正在处理")
            stale = [
                row for row in rows
                if str(row["memory_status"] or "") != "active"
                or int(row["current_revision"] or 0) != int(row["source_revision"])
            ]
            if stale:
                raise ValueError(
                    "以下 Timeline 在暂存后已发生变化："
                    + "、".join(str(row["memory_id"]) for row in stale)
                )
            selected_ids = sorted({int(row["memory_id"]) for row in rows})
            memory_placeholders = ",".join("?" for _ in selected_ids)
            overlaps = await (
                await db.execute(
                    f"""
                    SELECT DISTINCT i.memory_id
                    FROM timeline_rebuild_items i
                    JOIN timeline_rebuild_tasks t ON t.task_uid = i.task_uid
                    WHERE t.status IN ('queued', 'running', 'cancelling')
                      AND i.memory_id IN ({memory_placeholders})
                    """,
                    selected_ids,
                )
            ).fetchall()
            if overlaps:
                raise ValueError(
                    "以下 Timeline 已有维护任务："
                    + "、".join(str(row[0]) for row in overlaps)
                )
            await db.execute(
                """
                INSERT INTO timeline_rebuild_tasks(
                    task_uid, status, topic_mode, current_step, total_count,
                    payload, created_at, updated_at
                ) VALUES (?, 'queued', ?, 'planned', ?, ?, ?, ?)
                """,
                (
                    task_uid,
                    topic_mode,
                    len(rows),
                    json.dumps(
                        {"task_kind": "staged_edit", "edit_uids": normalized},
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
            await db.executemany(
                """
                INSERT INTO timeline_rebuild_items(
                    task_uid, memory_id, memory_uid, memory_space_id,
                    source_revision, status, source_snapshot, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                [
                    (
                        task_uid,
                        int(row["memory_id"]),
                        str(row["memory_uid"]),
                        str(row["memory_space_id"]),
                        int(row["source_revision"]),
                        json.dumps(
                            {
                                "revision": int(row["source_revision"]),
                                "topic_count": int(row["topic_count"] or 0),
                                "staged_edit_uid": str(row["edit_uid"]),
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    )
                    for row in rows
                ],
            )
            await db.execute(
                f"UPDATE timeline_staged_edits SET status = 'applying', "
                f"last_error = NULL, updated_at = ? WHERE edit_uid IN ({placeholders})",
                [now, *normalized],
            )
            await db.commit()
        self._launch(task_uid)
        return await self.get_task(task_uid) or {"task_uid": task_uid, "status": "queued"}

    async def resume_task(self, task_uid: str) -> dict[str, Any]:
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT status, payload FROM timeline_rebuild_tasks WHERE task_uid = ?",
                    (task_uid,),
                )
            ).fetchone()
            if not row:
                raise ValueError("Timeline 重构任务不存在")
            if str(row[0]) not in {"failed", "completed_with_errors"}:
                raise ValueError("只有失败或部分失败的任务可以重试")
            await db.execute(
                "UPDATE timeline_rebuild_items SET status = 'queued', error = NULL "
                "WHERE task_uid = ? AND status = 'failed'",
                (task_uid,),
            )
            payload = self._loads(row[1], {})
            if str(payload.get("task_kind") or "") == "staged_edit":
                await db.execute(
                    """
                    UPDATE timeline_staged_edits
                    SET status = 'applying', last_error = NULL, updated_at = ?
                    WHERE edit_uid IN (
                        SELECT json_extract(source_snapshot, '$.staged_edit_uid')
                        FROM timeline_rebuild_items
                        WHERE task_uid = ? AND status = 'queued'
                    )
                    """,
                    (time.time(), task_uid),
                )
            await db.execute(
                """
                UPDATE timeline_rebuild_tasks
                SET status = 'queued', current_step = 'resume', error = NULL,
                    completed_at = NULL, updated_at = ?
                WHERE task_uid = ?
                """,
                (time.time(), task_uid),
            )
            await db.commit()
        self._launch(task_uid)
        return await self.get_task(task_uid) or {"task_uid": task_uid, "status": "queued"}

    async def cancel_task(self, task_uid: str) -> bool:
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE timeline_rebuild_tasks
                SET status = 'cancelling', current_step = 'cancelling',
                    updated_at = ?
                WHERE task_uid = ? AND status IN ('queued', 'running')
                """,
                (time.time(), task_uid),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def _cancel_requested(self, task_uid: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT status FROM timeline_rebuild_tasks WHERE task_uid = ?",
                    (task_uid,),
                )
            ).fetchone()
        return bool(row and str(row[0]) == "cancelling")

    def _launch(self, task_uid: str) -> None:
        existing = self._tasks.get(task_uid)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_task(task_uid))
        self._tasks[task_uid] = task
        task.add_done_callback(lambda _task: self._tasks.pop(task_uid, None))

    async def _run_task(self, task_uid: str) -> None:
        try:
            task_data = await self.get_task(task_uid)
            if not task_data:
                return
            if str(task_data.get("status") or "") != "cancelling":
                await self._set_task(
                    task_uid, status="running", step="rebuilding"
                )
            rebuilt_by_space: dict[str, list[str]] = defaultdict(list)
            for item in task_data["items"]:
                if await self._cancel_requested(task_uid):
                    break
                if item["status"] == "completed":
                    rebuilt_by_space[item["memory_space_id"]].append(item["memory_uid"])
                    continue
                if item["status"] not in {"queued", "failed"}:
                    continue
                try:
                    staged_edit_uid = str(
                        (item.get("source_snapshot") or {}).get(
                            "staged_edit_uid", ""
                        )
                    )
                    result = (
                        await self._apply_staged_edit(task_uid, item)
                        if staged_edit_uid
                        else await self._rebuild_item(task_uid, item)
                    )
                    rebuilt_by_space[item["memory_space_id"]].append(item["memory_uid"])
                    await self._set_item(
                        task_uid,
                        int(item["memory_id"]),
                        status="completed",
                        result=result,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "[TimelineRebuild] 重构失败 (task=%s, memory_id=%s)",
                        task_uid,
                        item["memory_id"],
                        exc_info=True,
                    )
                    await self._set_item(
                        task_uid,
                        int(item["memory_id"]),
                        status="failed",
                        error=str(exc),
                    )
                    staged_edit_uid = str(
                        (item.get("source_snapshot") or {}).get(
                            "staged_edit_uid", ""
                        )
                    )
                    if staged_edit_uid:
                        await self._set_staged_status(
                            staged_edit_uid, "failed", error=str(exc)
                        )
                await self._refresh_counts(task_uid)

            cancel_requested_before_sync = await self._cancel_requested(task_uid)
            if cancel_requested_before_sync:
                await self._restore_unapplied_staged_items(task_uid)
                await self._set_task(
                    task_uid,
                    status="cancelled",
                    step="cancelled",
                    result={"topic_results": {}},
                    completed=True,
                )
                return
            await self._set_task(task_uid, status="running", step="topic_sync")
            topic_mode = str(task_data.get("topic_mode") or "local")
            topic_results: dict[str, Any] = {}
            for memory_space_id, timeline_uids in rebuilt_by_space.items():
                if not timeline_uids:
                    continue
                if topic_mode == "local":
                    linked_uids = {
                        str(item["memory_uid"])
                        for item in task_data["items"]
                        if item["memory_space_id"] == memory_space_id
                        and int(
                            (item.get("source_snapshot") or {}).get(
                                "topic_count", 0
                            )
                        )
                        > 0
                    }
                    timeline_uids = [
                        uid for uid in timeline_uids if uid in linked_uids
                    ]
                    if not timeline_uids:
                        topic_results[memory_space_id] = {
                            "status": "not_required",
                            "reason": "selected Timelines have no linked Topic",
                        }
                        continue
                if topic_mode == "full":
                    topic_results[memory_space_id] = (
                        await self.memory_engine.topic_build_manager.build_space(
                            memory_space_id,
                            mode=TopicMaintenanceMode.FULL,
                        )
                    )
                else:
                    topic_results[memory_space_id] = (
                        await self.memory_engine.topic_build_manager.build_space(
                            memory_space_id,
                            mode=TopicMaintenanceMode.INCREMENTAL,
                            timeline_uids=list(dict.fromkeys(timeline_uids)),
                        )
                    )
            counts = await self._refresh_counts(task_uid)
            cancel_requested = (
                cancel_requested_before_sync
                or await self._cancel_requested(task_uid)
            )
            has_review = any(
                str(result.get("status") or "") == "completed_with_review"
                for result in topic_results.values()
                if isinstance(result, dict)
            )
            final_status = "cancelled" if cancel_requested else (
                "completed_with_errors" if counts["failed_count"] else (
                    "completed_with_review" if has_review else "completed"
                )
            )
            await self._set_task(
                task_uid,
                status=final_status,
                step="cancelled" if cancel_requested else "completed",
                result={"topic_results": topic_results},
                completed=True,
            )
        except asyncio.CancelledError:
            await self._set_task(
                task_uid,
                status="queued",
                step="interrupted",
                completed=False,
            )
            raise
        except Exception as exc:
            logger.error(
                "[TimelineRebuild] 任务失败 (task=%s)", task_uid, exc_info=True
            )
            await self._set_task(
                task_uid,
                status="failed",
                step="topic_sync_failed",
                error=str(exc),
                completed=True,
            )

    async def _rebuild_item(
        self, task_uid: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        snapshot = item.get("source_snapshot") or {}
        preview = await self.preview([int(item["memory_id"])], limit=1)
        if not preview["items"]:
            raise ValueError("Timeline 已不存在")
        current = preview["items"][0]
        if not current["reconstructable"]:
            raise ValueError("；".join(current["blocked_reasons"]))
        current_memory = await self.memory_engine.get_memory(int(item["memory_id"]))
        current_metadata = self._loads(
            current_memory.get("metadata") if current_memory else None, {}
        )
        rebuild_marker = current_metadata.get("timeline_rebuild", {})
        if (
            int(current.get("revision", 0))
            == int(snapshot.get("revision", 0)) + 1
            and str(rebuild_marker.get("task_uid") or "") == task_uid
        ):
            return {
                "memory_id": int(item["memory_id"]),
                "old_revision": int(snapshot["revision"]),
                "new_revision": int(current["revision"]),
                "message_count": int(snapshot["message_count"]),
                "resumed_after_write": True,
                "summary_quality": str(
                    current_metadata.get("summary_quality") or "unknown"
                ),
            }
        for key in (
            "memory_uid",
            "revision",
            "session_id",
            "first_message_id",
            "last_message_id",
            "message_count",
        ):
            if current.get(key) != snapshot.get(key):
                raise ValueError(f"预检后来源发生变化：{key}")
        messages = await self.conversation_manager.get_messages_by_id_span(
            snapshot["session_id"],
            int(snapshot["first_message_id"]),
            int(snapshot["last_message_id"]),
            limit=max(100, int(snapshot["message_count"]) + 1, 2000),
        )
        content, metadata, importance = await self.memory_processor.process_conversation(
            messages=messages,
            is_group_chat=bool(snapshot.get("is_group_chat")),
            persona_id=str(snapshot.get("persona_id") or "default"),
        )
        atoms = self.memory_processor.classify_atoms_from_metadata(
            metadata=metadata,
            parent_importance=importance,
            session_id=snapshot["session_id"],
            persona_id=str(snapshot.get("persona_id") or "default"),
        )
        original = await self.memory_engine.get_memory(int(item["memory_id"]))
        original_metadata = self._loads(original.get("metadata") if original else None, {})
        source_window = dict(original_metadata.get("source_window") or {})
        metadata["source_window"] = source_window
        metadata["timeline_rebuild"] = {
            "task_uid": task_uid,
            "rebuilt_at": time.time(),
            "source_revision": int(snapshot["revision"]),
            "source_message_count": len(messages),
            "quality_contract": str(
                (metadata.get("summary_quality_report") or {}).get(
                    "contract_version", "unknown"
                )
            ),
            "summary_schema_version": str(
                metadata.get("summary_schema_version") or "unknown"
            ),
        }
        await self.memory_engine.rewrite_memory_in_place(
            int(item["memory_id"]),
            content=content,
            metadata=metadata,
            importance=importance,
            atoms=atoms,
            schedule_topic_maintenance=False,
        )
        return {
            "memory_id": int(item["memory_id"]),
            "old_revision": int(snapshot["revision"]),
            "new_revision": int(snapshot["revision"]) + 1,
            "message_count": len(messages),
            "importance": float(importance),
            "summary_quality": str(metadata.get("summary_quality") or "unknown"),
            "rebuild_recommended": bool(
                metadata.get("summary_rebuild_recommended", False)
            ),
        }

    async def _apply_staged_edit(
        self, task_uid: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        snapshot = item.get("source_snapshot") or {}
        edit_uid = str(snapshot.get("staged_edit_uid") or "")
        if not edit_uid:
            raise ValueError("缺少暂存修改标识")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM timeline_staged_edits WHERE edit_uid = ?",
                    (edit_uid,),
                )
            ).fetchone()
        if not row:
            raise ValueError("暂存修改已不存在")
        prepared = self._loads(row["prepared_payload"], {})
        current = await self.memory_engine.get_memory(int(item["memory_id"]))
        if not current:
            raise ValueError("Timeline 记忆已不存在")
        current_metadata = self._loads(current.get("metadata"), {})
        applied_marker = current_metadata.get("timeline_staged_edit") or {}
        current_revision = await self._current_memory_revision(
            int(item["memory_id"]), str(item["memory_uid"])
        )
        if (
            str(applied_marker.get("edit_uid") or "") == edit_uid
            and current_revision == int(item["source_revision"]) + 1
        ):
            await self._set_staged_status(edit_uid, "applied")
            return {
                "memory_id": int(item["memory_id"]),
                "old_revision": int(item["source_revision"]),
                "new_revision": current_revision,
                "staged_edit_uid": edit_uid,
                "resumed_after_write": True,
            }
        if current_revision != int(item["source_revision"]):
            raise ValueError("暂存后 Timeline 已被其他操作修改")
        metadata = dict(prepared.get("metadata") or {})
        metadata["timeline_staged_edit"] = {
            "edit_uid": edit_uid,
            "task_uid": task_uid,
            "applied_at": time.time(),
            "source_revision": int(item["source_revision"]),
        }
        importance = float(prepared.get("importance", 0.5))
        atoms = self.memory_processor.classify_atoms_from_metadata(
            metadata=metadata,
            parent_importance=importance,
            session_id=prepared.get("session_id"),
            persona_id=prepared.get("persona_id"),
        )
        await self.memory_engine.rewrite_memory_in_place(
            int(item["memory_id"]),
            content=str(prepared.get("content") or ""),
            metadata=metadata,
            importance=importance,
            atoms=atoms,
            schedule_topic_maintenance=False,
        )
        await self._set_staged_status(edit_uid, "applied")
        return {
            "memory_id": int(item["memory_id"]),
            "old_revision": int(item["source_revision"]),
            "new_revision": int(item["source_revision"]) + 1,
            "staged_edit_uid": edit_uid,
        }

    async def _current_memory_revision(
        self, memory_id: int, memory_uid: str
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT revision FROM memory_registry "
                    "WHERE document_id = ? AND memory_uid = ?",
                    (int(memory_id), str(memory_uid)),
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def _set_staged_status(
        self, edit_uid: str, status: str, *, error: str | None = None
    ) -> None:
        now = time.time()
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE timeline_staged_edits
                SET status = ?, last_error = ?, updated_at = ?,
                    applied_at = CASE WHEN ? = 'applied' THEN ? ELSE applied_at END
                WHERE edit_uid = ?
                """,
                (status, error, now, status, now, edit_uid),
            )
            await db.commit()

    async def _restore_unapplied_staged_items(self, task_uid: str) -> None:
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE timeline_staged_edits
                SET status = 'pending', updated_at = ?
                WHERE status = 'applying' AND edit_uid IN (
                    SELECT json_extract(source_snapshot, '$.staged_edit_uid')
                    FROM timeline_rebuild_items
                    WHERE task_uid = ? AND status != 'completed'
                )
                """,
                (time.time(), task_uid),
            )
            await db.commit()

    async def _set_item(
        self,
        task_uid: str,
        memory_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE timeline_rebuild_items
                SET status = ?, result = ?, error = ?, updated_at = ?
                WHERE task_uid = ? AND memory_id = ?
                """,
                (
                    status,
                    json.dumps(result or {}, ensure_ascii=False),
                    error,
                    time.time(),
                    task_uid,
                    int(memory_id),
                ),
            )
            await db.commit()

    async def _refresh_counts(self, task_uid: str) -> dict[str, int]:
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT COUNT(*) AS total_count,
                           SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                    FROM timeline_rebuild_items WHERE task_uid = ?
                    """,
                    (task_uid,),
                )
            ).fetchone()
            counts = {
                "total_count": int(row[0] or 0),
                "completed_count": int(row[1] or 0),
                "failed_count": int(row[2] or 0),
            }
            await db.execute(
                """
                UPDATE timeline_rebuild_tasks
                SET total_count = ?, completed_count = ?, failed_count = ?, updated_at = ?
                WHERE task_uid = ?
                """,
                (*counts.values(), time.time(), task_uid),
            )
            await db.commit()
        return counts

    async def _set_task(
        self,
        task_uid: str,
        *,
        status: str,
        step: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        completed: bool = False,
    ) -> None:
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE timeline_rebuild_tasks
                SET status = ?, current_step = ?, result = COALESCE(?, result),
                    error = ?, updated_at = ?, completed_at = ?
                WHERE task_uid = ?
                """,
                (
                    status,
                    step,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    time.time(),
                    time.time() if completed else None,
                    task_uid,
                ),
            )
            await db.commit()

    async def get_task(self, task_uid: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            task = await (
                await db.execute(
                    "SELECT * FROM timeline_rebuild_tasks WHERE task_uid = ?",
                    (task_uid,),
                )
            ).fetchone()
            if not task:
                return None
            items = await (
                await db.execute(
                    "SELECT * FROM timeline_rebuild_items WHERE task_uid = ? "
                    "ORDER BY memory_id",
                    (task_uid,),
                )
            ).fetchall()
        result = dict(task)
        for key in ("payload", "result"):
            result[key] = self._loads(result.get(key), {})
        result["items"] = []
        for row in items:
            item = dict(row)
            item["source_snapshot"] = self._loads(item.get("source_snapshot"), {})
            item["result"] = self._loads(item.get("result"), {})
            result["items"].append(item)
        return result

    async def list_tasks(self, *, limit: int = 30) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM timeline_rebuild_tasks "
                    "ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(int(limit), 100)),),
                )
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["result"] = self._loads(item.get("result"), {})
            item.pop("payload", None)
            result.append(item)
        return result

    async def delete_task(self, task_uid: str) -> bool:
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            row = await (
                await db.execute(
                    "SELECT status FROM timeline_rebuild_tasks WHERE task_uid = ?",
                    (task_uid,),
                )
            ).fetchone()
            if not row:
                return False
            if str(row[0]) not in self.TERMINAL_STATUSES:
                raise ValueError("只能删除已结束的 Timeline 重构任务")
            cursor = await db.execute(
                "DELETE FROM timeline_rebuild_tasks WHERE task_uid = ?",
                (task_uid,),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def clear_finished_tasks(self) -> int:
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            placeholders = ",".join("?" for _ in self.TERMINAL_STATUSES)
            cursor = await db.execute(
                f"DELETE FROM timeline_rebuild_tasks WHERE status IN ({placeholders})",
                tuple(self.TERMINAL_STATUSES),
            )
            await db.commit()
        return max(0, int(cursor.rowcount or 0))


__all__ = ["TimelineRebuildManager"]
