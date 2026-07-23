"""Auditable, resumable maintenance across raw conversations and memories."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import aiosqlite

from astrbot.api import logger


class SessionMaintenanceManager:
    """Coordinate user-confirmed session work without pretending two DBs are atomic."""

    OPERATIONS = {
        "cleanup_summarized",
        "delete_raw_keep_memory",
        "merge_aliases",
        "delete_memory_chain",
    }

    def __init__(self, db_path: str, conversation_manager: Any, memory_engine: Any):
        self.db_path = db_path
        self.conversation_manager = conversation_manager
        self.memory_engine = memory_engine
        self._tasks: dict[str, asyncio.Task] = {}
        self._write_lock = asyncio.Lock()
        self._auto_cleanup_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS session_maintenance_tasks (
                    task_uid TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_session_ids TEXT NOT NULL DEFAULT '[]',
                    canonical_session_id TEXT,
                    current_step TEXT NOT NULL DEFAULT 'planned',
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
                CREATE TABLE IF NOT EXISTS session_maintenance_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_uid TEXT NOT NULL,
                    step TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(task_uid) REFERENCES session_maintenance_tasks(task_uid)
                        ON DELETE CASCADE
                )
                """
            )
            await db.commit()
            rows = await (
                await db.execute(
                    """
                    SELECT task_uid FROM session_maintenance_tasks
                    WHERE status = 'running' ORDER BY created_at ASC
                    """
                )
            ).fetchall()
        for row in rows:
            self._launch(str(row[0]))
        self._auto_cleanup_task = asyncio.create_task(self._auto_cleanup_loop())

    async def shutdown(self) -> None:
        if self._auto_cleanup_task and not self._auto_cleanup_task.done():
            self._auto_cleanup_task.cancel()
            await asyncio.gather(self._auto_cleanup_task, return_exceptions=True)
        self._auto_cleanup_task = None
        pending = [task for task in self._tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    async def _auto_cleanup_loop(self) -> None:
        """Periodically apply the explicit raw-retention policy; disabled by default."""
        while True:
            try:
                await asyncio.sleep(3600)
                if not self.conversation_manager.auto_delete_raw_sessions:
                    continue
                retention_days = int(
                    self.conversation_manager.raw_message_retention_days or 0
                )
                if retention_days <= 0:
                    continue
                cutoff = time.time() - retention_days * 86400
                audit = await self.audit_sessions(limit=10000)
                candidates = [
                    item["session_id"]
                    for item in audit["items"]
                    if not item.get("raw_session_missing")
                    and not item.get("active")
                    and int(item.get("message_count", 0)) > 0
                    and int(item.get("unsummarized_message_count", 0)) == 0
                    and float(item.get("last_active_at", 0.0)) < cutoff
                ]
                if candidates:
                    await self.start_task("delete_raw_keep_memory", candidates)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[SessionMaintenance] 自动原始会话清理失败", exc_info=True)

    @staticmethod
    def _loads(value: Any, fallback: Any) -> Any:
        try:
            loaded = json.loads(value or "")
            return loaded
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback

    @staticmethod
    def _parse_session_id(session_id: str) -> dict[str, str]:
        parts = str(session_id or "").split(":", 2)
        bot_account = parts[0] if parts else ""
        message_type = parts[1] if len(parts) > 1 else ""
        target_id = parts[2] if len(parts) > 2 else str(session_id or "")
        folded = message_type.casefold()
        chat_type = (
            "group"
            if "group" in folded
            else "private"
            if "friend" in folded or "private" in folded
            else "other"
        )
        return {
            "bot_account": bot_account,
            "message_type": message_type,
            "chat_type": chat_type,
            "target_id": target_id,
        }

    async def audit_sessions(self, *, limit: int = 1000) -> dict[str, Any]:
        raw_rows = await self.conversation_manager.store.list_session_audit_rows(limit)
        aliases = await self.conversation_manager.store.list_session_aliases()
        active_ids = await self.conversation_manager.active_session_ids()
        impacts = await self._memory_impacts()
        alias_by_id = {str(row["alias_session_id"]): row for row in aliases}

        items: list[dict[str, Any]] = []
        raw_ids: set[str] = set()
        for row in raw_rows:
            session_id = str(row.get("session_id") or "")
            raw_ids.add(session_id)
            parsed = self._parse_session_id(session_id)
            metadata = self._loads(row.get("metadata"), {})
            actual_count = int(row.get("actual_message_count") or 0)
            try:
                summarized = max(
                    0, min(actual_count, int(metadata.get("last_summarized_index", 0) or 0))
                )
            except (TypeError, ValueError):
                summarized = 0
            unsummarized = max(0, actual_count - summarized)
            canonical = await self.conversation_manager.resolve_session_id(session_id)
            memory = impacts.get(session_id, {})
            reasons: list[str] = []
            if session_id in active_ids or canonical in active_ids:
                reasons.append("当前会话仍在内存缓存中活跃")
            if unsummarized:
                reasons.append(f"仍有 {unsummarized} 条消息尚未总结")
            items.append(
                {
                    "session_id": session_id,
                    "canonical_session_id": canonical,
                    "is_alias": session_id in alias_by_id,
                    "platform": str(row.get("platform") or "unknown"),
                    **parsed,
                    "created_at": float(row.get("created_at") or 0.0),
                    "last_active_at": float(row.get("last_active_at") or 0.0),
                    "message_count": actual_count,
                    "summarized_message_count": summarized,
                    "unsummarized_message_count": unsummarized,
                    "summary_job_status": row.get("summary_job_status"),
                    "timeline_count": int(memory.get("timeline_count", 0)),
                    "topic_count": int(memory.get("topic_count", 0)),
                    "fragment_count": int(memory.get("fragment_count", 0)),
                    "raw_evidence_reference_count": int(
                        memory.get("raw_evidence_reference_count", 0)
                    ),
                    "active": session_id in active_ids or canonical in active_ids,
                    "safe_to_cleanup": not reasons,
                    "cleanup_block_reasons": reasons,
                    "possible_aliases": [],
                }
            )

        # Include memory-only sessions whose raw conversation was already removed.
        for session_id, memory in impacts.items():
            if session_id in raw_ids:
                continue
            parsed = self._parse_session_id(session_id)
            canonical = await self.conversation_manager.resolve_session_id(session_id)
            items.append(
                {
                    "session_id": session_id,
                    "canonical_session_id": canonical,
                    "is_alias": session_id in alias_by_id,
                    "platform": "unknown",
                    **parsed,
                    "created_at": 0.0,
                    "last_active_at": float(memory.get("last_active_at", 0.0)),
                    "message_count": 0,
                    "summarized_message_count": 0,
                    "unsummarized_message_count": 0,
                    "summary_job_status": None,
                    "timeline_count": int(memory.get("timeline_count", 0)),
                    "topic_count": int(memory.get("topic_count", 0)),
                    "fragment_count": int(memory.get("fragment_count", 0)),
                    "raw_evidence_reference_count": 0,
                    "active": canonical in active_ids,
                    "safe_to_cleanup": canonical not in active_ids,
                    "cleanup_block_reasons": (
                        ["当前会话仍在内存缓存中活跃"] if canonical in active_ids else []
                    ),
                    "possible_aliases": [],
                    "raw_session_missing": True,
                }
            )

        groups: dict[tuple[str, str, str], list[str]] = {}
        for item in items:
            key = (item["bot_account"], item["chat_type"], item["target_id"])
            groups.setdefault(key, []).append(item["session_id"])
        for item in items:
            key = (item["bot_account"], item["chat_type"], item["target_id"])
            item["possible_aliases"] = [
                value for value in groups.get(key, []) if value != item["session_id"]
            ]
        items.sort(key=lambda item: float(item.get("last_active_at") or 0.0), reverse=True)
        return {"items": items[:limit], "aliases": aliases, "total": len(items)}

    async def _memory_impacts(self) -> dict[str, dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT s.session_id,
                           COUNT(DISTINCT r.memory_uid) AS timeline_count,
                           COUNT(DISTINCT ttl.topic_uid) AS topic_count,
                           COUNT(DISTINCT tfl.fragment_uid) AS fragment_count,
                           SUM(CASE WHEN s.first_message_id IS NOT NULL
                                         AND s.last_message_id IS NOT NULL
                                    THEN 1 ELSE 0 END) AS raw_evidence_reference_count,
                           MAX(COALESCE(s.ended_at, s.started_at, r.updated_at)) AS last_active_at
                    FROM memory_source_spans s
                    JOIN memory_registry r ON r.memory_uid = s.memory_uid
                    LEFT JOIN topic_timeline_links ttl
                      ON ttl.timeline_uid = r.memory_uid AND ttl.status = 'active'
                    LEFT JOIN topic_fragment_links tfl
                      ON tfl.topic_uid = ttl.topic_uid AND tfl.status = 'active'
                    WHERE s.session_id IS NOT NULL AND s.session_id <> ''
                    GROUP BY s.session_id
                    """
                )
            ).fetchall()
        return {str(row["session_id"]): dict(row) for row in rows}

    async def preview(
        self,
        operation: str,
        session_ids: list[str],
        *,
        canonical_session_id: str | None = None,
    ) -> dict[str, Any]:
        operation = str(operation or "")
        if operation not in self.OPERATIONS:
            raise ValueError("不支持的会话维护操作")
        normalized = list(
            dict.fromkeys(
                str(item).strip() for item in session_ids if str(item).strip()
            )
        )
        if not normalized:
            raise ValueError("至少选择一个会话")
        if operation == "delete_memory_chain":
            expanded: list[str] = []
            for session_id in normalized:
                expanded.extend(
                    await self.conversation_manager.store.list_session_alias_group(
                        session_id
                    )
                )
            normalized = list(dict.fromkeys(expanded))
        audit = await self.audit_sessions(limit=10000)
        by_id = {item["session_id"]: item for item in audit["items"]}
        items = [
            dict(by_id.get(item, {"session_id": item, "raw_session_missing": True}))
            for item in normalized
        ]
        blocked: list[str] = []
        warnings: list[str] = []
        if operation == "merge_aliases":
            canonical = str(canonical_session_id or "").strip()
            if not canonical or canonical not in normalized:
                blocked.append("规范会话必须包含在所选会话中")
            if len(normalized) < 2:
                blocked.append("合并别名至少需要两个会话")
            warnings.append("逻辑合并不会改写旧 Timeline/Topic；旧来源将通过别名范围继续召回")
        for item in items:
            if operation == "cleanup_summarized":
                cleanup = await self.conversation_manager.store.summarized_cleanup_preview(
                    item["session_id"]
                )
                item["eligible_message_count"] = int(
                    cleanup.get("eligible_count", 0)
                )
                item["eligible_first_message_id"] = int(
                    cleanup.get("first_message_id", 0)
                )
                item["eligible_last_message_id"] = int(
                    cleanup.get("last_message_id", 0)
                )
            if item.get("active"):
                blocked.append(f"{item['session_id']} 当前仍活跃")
            if item.get("unsummarized_message_count", 0) and operation in {
                "cleanup_summarized",
                "delete_raw_keep_memory",
            }:
                if operation == "delete_raw_keep_memory":
                    blocked.append(f"{item['session_id']} 存在未总结消息")
            if operation == "delete_raw_keep_memory":
                warnings.append("删除原始会话后，既有 Timeline/Topic 保留，但无法再补取原始消息证据")
            elif operation == "delete_memory_chain":
                warnings.append("完整删除会移除原始消息和 Timeline，并使受影响 Topic 进入局部维护")
        return {
            "operation": operation,
            "session_ids": normalized,
            "canonical_session_id": canonical_session_id,
            "items": items,
            "blocked_reasons": list(dict.fromkeys(blocked)),
            "warnings": list(dict.fromkeys(warnings)),
            "requires_force": bool(blocked),
        }

    async def start_task(
        self,
        operation: str,
        session_ids: list[str],
        *,
        canonical_session_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        preview = await self.preview(
            operation, session_ids, canonical_session_id=canonical_session_id
        )
        if preview["blocked_reasons"] and not force:
            raise ValueError("；".join(preview["blocked_reasons"]))
        task_uid = str(uuid.uuid4())
        now = time.time()
        payload = {"force": bool(force), "preview": preview}
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            running_rows = await (
                await db.execute(
                    """
                    SELECT task_uid, source_session_ids
                    FROM session_maintenance_tasks
                    WHERE status = 'running'
                    """
                )
            ).fetchall()
            requested = set(preview["session_ids"])
            for row in running_rows:
                occupied = set(self._loads(row["source_session_ids"], []))
                overlap = sorted(requested & occupied)
                if overlap:
                    raise ValueError(
                        "所选会话已有维护任务正在执行: " + "、".join(overlap)
                    )
            await db.execute(
                """
                INSERT INTO session_maintenance_tasks (
                    task_uid, operation, status, source_session_ids,
                    canonical_session_id, current_step, payload, result,
                    created_at, updated_at
                ) VALUES (?, ?, 'running', ?, ?, 'planned', ?, '{}', ?, ?)
                """,
                (
                    task_uid,
                    operation,
                    json.dumps(preview["session_ids"], ensure_ascii=False),
                    canonical_session_id,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            await db.commit()
        self._launch(task_uid)
        return await self.get_task(task_uid) or {"task_uid": task_uid, "status": "running"}

    def _launch(self, task_uid: str) -> None:
        existing = self._tasks.get(task_uid)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_task(task_uid))
        self._tasks[task_uid] = task
        task.add_done_callback(lambda _task: self._tasks.pop(task_uid, None))

    async def get_task(self, task_uid: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM session_maintenance_tasks WHERE task_uid = ?",
                    (task_uid,),
                )
            ).fetchone()
            if not row:
                return None
            events = await (
                await db.execute(
                    "SELECT * FROM session_maintenance_events WHERE task_uid = ? ORDER BY id",
                    (task_uid,),
                )
            ).fetchall()
        item = dict(row)
        for key, fallback in (
            ("source_session_ids", []),
            ("payload", {}),
            ("result", {}),
        ):
            item[key] = self._loads(item.get(key), fallback)
        item["events"] = [
            {**dict(event), "details": self._loads(event["details"], {})}
            for event in events
        ]
        return item

    async def list_tasks(self, *, limit: int = 30) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM session_maintenance_tasks ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(int(limit), 100)),),
                )
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source_session_ids"] = self._loads(item["source_session_ids"], [])
            item["result"] = self._loads(item["result"], {})
            item.pop("payload", None)
            result.append(item)
        return result

    async def _checkpoint(
        self,
        task_uid: str,
        step: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        async with self._write_lock, aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE session_maintenance_tasks
                SET status = ?, current_step = ?, result = ?, error = ?,
                    updated_at = ?, completed_at = ?
                WHERE task_uid = ?
                """,
                (
                    status,
                    step,
                    json.dumps(result or {}, ensure_ascii=False),
                    error,
                    now,
                    now if status in {"completed", "failed"} else None,
                    task_uid,
                ),
            )
            await db.execute(
                """
                INSERT INTO session_maintenance_events (
                    task_uid, step, status, details, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (task_uid, step, status, json.dumps(result or {}, ensure_ascii=False), now),
            )
            await db.commit()

    async def _run_task(self, task_uid: str) -> None:
        task = await self.get_task(task_uid)
        if not task:
            return
        operation = str(task["operation"])
        session_ids = [str(item) for item in task["source_session_ids"]]
        result = dict(task.get("result") or {})
        completed = set(str(item) for item in result.get("completed_sessions", []))
        try:
            await self._checkpoint(task_uid, "validating", "running", result=result)
            if operation == "merge_aliases":
                canonical = str(task.get("canonical_session_id") or "")
                aliases = [item for item in session_ids if item != canonical]
                group = await self.conversation_manager.store.set_session_aliases(
                    canonical,
                    aliases,
                    metadata={"maintenance_task_uid": task_uid},
                )
                result.update({"alias_group": group, "completed_sessions": session_ids})
                await self.memory_engine.invalidate_session_alias_cache()
                await self.conversation_manager.invalidate_session_cache(session_ids)
            else:
                for session_id in session_ids:
                    if session_id in completed:
                        continue
                    await self._checkpoint(
                        task_uid,
                        f"processing:{session_id}",
                        "running",
                        result=result,
                    )
                    if operation == "cleanup_summarized":
                        preview = await self.conversation_manager.store.summarized_cleanup_preview(
                            session_id
                        )
                        deleted = await self.conversation_manager.store.trim_session_messages(
                            session_id, int(preview["eligible_count"])
                        )
                        if deleted:
                            await self._mark_evidence_unavailable(
                                session_id,
                                int(preview["first_message_id"]),
                                int(preview["last_message_id"]),
                                partial=True,
                            )
                            await self.conversation_manager.invalidate_session_cache(
                                [session_id]
                            )
                        result.setdefault("deleted_messages", {})[session_id] = deleted
                    elif operation == "delete_raw_keep_memory":
                        result.setdefault("raw_deleted", {})[session_id] = (
                            await self.conversation_manager.store.delete_raw_session(session_id)
                        )
                        await self.conversation_manager.invalidate_session_cache(
                            [session_id]
                        )
                        await self._mark_evidence_unavailable(
                            session_id, 0, 0, partial=False
                        )
                    elif operation == "delete_memory_chain":
                        document_ids = await self._timeline_document_ids(session_id)
                        deleted_memories = await self.memory_engine.batch_delete_memories(document_ids)
                        raw_deleted = await self.conversation_manager.store.delete_raw_session(
                            session_id
                        )
                        await self.conversation_manager.invalidate_session_cache(
                            [session_id]
                        )
                        result.setdefault("memory_deleted", {})[session_id] = deleted_memories
                        result.setdefault("raw_deleted", {})[session_id] = raw_deleted
                    completed.add(session_id)
                    result["completed_sessions"] = sorted(completed)
                if operation == "delete_memory_chain":
                    result["aliases_removed"] = (
                        await self.conversation_manager.store.remove_session_aliases(session_ids)
                    )
            await self._checkpoint(task_uid, "completed", "completed", result=result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[SessionMaintenance] 任务失败: %s", task_uid, exc_info=True)
            await self._checkpoint(
                task_uid, "failed", "failed", result=result, error=str(exc)[:2000]
            )

    async def _timeline_document_ids(self, session_id: str) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT r.document_id
                    FROM memory_registry r
                    JOIN memory_source_spans s ON s.memory_uid = r.memory_uid
                    WHERE s.session_id = ? AND r.memory_layer = 'timeline'
                      AND r.status = 'active'
                    ORDER BY r.document_id
                    """,
                    (session_id,),
                )
            ).fetchall()
        return [int(row[0]) for row in rows]

    async def _mark_evidence_unavailable(
        self,
        session_id: str,
        first_message_id: int,
        last_message_id: int,
        *,
        partial: bool,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if partial:
                rows = await (
                    await db.execute(
                        """
                        SELECT memory_uid, first_message_id, last_message_id, metadata
                        FROM memory_source_spans
                        WHERE session_id = ? AND first_message_id <= ?
                          AND last_message_id >= ?
                        """,
                        (session_id, last_message_id, first_message_id),
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        """
                        SELECT memory_uid, first_message_id, last_message_id, metadata
                        FROM memory_source_spans WHERE session_id = ?
                        """,
                        (session_id,),
                    )
                ).fetchall()
            for row in rows:
                metadata = self._loads(row["metadata"], {})
                fully_removed = not partial or (
                    int(row["first_message_id"] or 0) >= first_message_id
                    and int(row["last_message_id"] or 0) <= last_message_id
                )
                metadata["raw_evidence_available"] = False if fully_removed else "partial"
                metadata["raw_evidence_lost_at"] = time.time()
                await db.execute(
                    """
                    UPDATE memory_source_spans
                    SET traceability = ?, metadata = ?, updated_at = ?
                    WHERE memory_uid = ?
                    """,
                    (
                        "unavailable" if fully_removed else "partial",
                        json.dumps(metadata, ensure_ascii=False),
                        time.time(),
                        row["memory_uid"],
                    ),
                )
            await db.commit()
        return len(rows)


__all__ = ["SessionMaintenanceManager"]
