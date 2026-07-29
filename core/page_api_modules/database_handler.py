"""Database health inspection and explicitly confirmed maintenance actions."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from astrbot.api import logger
from quart import request

if TYPE_CHECKING:
    from ..managers.conversation_manager import ConversationManager
    from ..managers.memory_engine import MemoryEngine
    from .utils import PageApiUtils


class DatabaseHandler:
    """Expose read-only checks and narrowly scoped manual repairs."""

    def __init__(self, utils: PageApiUtils) -> None:
        self.utils = utils
        self._repair_jobs: dict[str, dict[str, Any]] = {}
        self._repair_tasks: dict[str, asyncio.Task] = {}
        self._latest_repair_job_uid: str | None = None
        self._storage_jobs: dict[str, dict[str, Any]] = {}
        self._storage_tasks: dict[str, asyncio.Task] = {}
        self._latest_storage_job_uid: str | None = None

    @staticmethod
    async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        )
        return await cursor.fetchone() is not None

    async def _inspect_sqlite(self, path: str, label: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "label": label,
            "filename": Path(path).name,
            "exists": Path(path).exists(),
            "size_bytes": Path(path).stat().st_size if Path(path).exists() else 0,
            "integrity": "missing",
            "foreign_key_violations": [],
        }
        if not result["exists"]:
            return result

        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 10000")
            integrity_rows = await (
                await db.execute("PRAGMA integrity_check")
            ).fetchall()
            integrity_messages = [str(row[0]) for row in integrity_rows]
            result["integrity"] = (
                "ok" if integrity_messages == ["ok"] else "; ".join(integrity_messages)
            )
            fk_rows = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
            result["foreign_key_violations"] = [
                {
                    "table": str(row[0]),
                    "row_id": int(row[1]) if row[1] is not None else None,
                    "parent_table": str(row[2]),
                    "constraint_index": int(row[3]),
                }
                for row in fk_rows[:5000]
            ]
            if await self._table_exists(db, "db_version"):
                version_row = await (
                    await db.execute(
                        "SELECT version FROM db_version ORDER BY id DESC LIMIT 1"
                    )
                ).fetchone()
                result["schema_version"] = str(version_row[0]) if version_row else None
        return result

    async def _main_consistency_issues(self, db_path: str) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 10000")

            has_documents = await self._table_exists(db, "documents")
            if await self._table_exists(
                db, "graph_entries"
            ) and await self._table_exists(db, "graph_edges"):
                source_exists_sql = (
                    "EXISTS(SELECT 1 FROM documents "
                    "WHERE documents.id = entry.source_memory_id)"
                    if has_documents
                    else "0"
                )
                orphan_rows = await (
                    await db.execute(
                        f"""
                        SELECT entry.id, entry.source_memory_id, entry.edge_id,
                               entry.relation_type, entry.content,
                               {source_exists_sql} AS source_exists
                        FROM graph_entries AS entry
                        LEFT JOIN graph_edges AS edge ON edge.id = entry.edge_id
                        WHERE entry.edge_id IS NOT NULL AND edge.id IS NULL
                        ORDER BY entry.source_memory_id, entry.id
                        """
                    )
                ).fetchall()
                grouped: dict[int, list[aiosqlite.Row]] = defaultdict(list)
                for row in orphan_rows:
                    grouped[int(row["source_memory_id"])].append(row)
                for memory_id, rows in grouped.items():
                    source_exists = bool(rows[0]["source_exists"])
                    issues.append(
                        {
                            "issue_uid": f"graph_orphan_entries:{memory_id}",
                            "kind": "graph_orphan_entries",
                            "severity": "error",
                            "title": "图谱条目引用了不存在的边",
                            "description": (
                                "来源 Timeline 仍存在，可以从当前记忆重建该部分图谱。"
                                if source_exists
                                else "来源 Timeline 已不存在，只能清理这些孤立图谱条目。"
                            ),
                            "count": len(rows),
                            "memory_id": memory_id,
                            "entry_ids": [int(row["id"]) for row in rows],
                            "missing_edge_ids": sorted(
                                {int(row["edge_id"]) for row in rows}
                            ),
                            "excerpt": str(rows[0]["content"] or "")[:180],
                            "repairable": True,
                            "repair_action": (
                                "rebuild_graph_memory"
                                if source_exists
                                else "delete_orphan_graph_entries"
                            ),
                        }
                    )

            if await self._table_exists(
                db, "graph_edge_sources"
            ) and await self._table_exists(db, "graph_edges"):
                edge_rows = await (
                    await db.execute(
                        """
                        SELECT edge.id
                        FROM graph_edges AS edge
                        LEFT JOIN graph_edge_sources AS source
                          ON source.edge_id = edge.id
                        WHERE source.edge_id IS NULL
                        ORDER BY edge.id
                        """
                    )
                ).fetchall()
                if edge_rows:
                    issues.append(
                        {
                            "issue_uid": "graph_edges_without_sources",
                            "kind": "graph_edges_without_sources",
                            "severity": "error",
                            "title": "图谱边缺少来源记忆",
                            "description": "边存在，但多来源索引中没有任何证据来源。",
                            "count": len(edge_rows),
                            "edge_ids": [int(row["id"]) for row in edge_rows],
                            "repairable": False,
                            "repair_action": None,
                        }
                    )
                missing_source_rows = await (
                    await db.execute(
                        """
                        SELECT entry.source_memory_id, COUNT(*) AS item_count
                        FROM graph_entries AS entry
                        JOIN graph_edges AS edge ON edge.id = entry.edge_id
                        LEFT JOIN graph_edge_sources AS source
                          ON source.edge_id = entry.edge_id
                         AND source.source_memory_id = entry.source_memory_id
                        WHERE entry.edge_id IS NOT NULL
                          AND source.edge_id IS NULL
                        GROUP BY entry.source_memory_id
                        ORDER BY entry.source_memory_id
                        """
                    )
                ).fetchall()
                for row in missing_source_rows:
                    issues.append(
                        {
                            "issue_uid": f"graph_missing_source:{int(row['source_memory_id'])}",
                            "kind": "graph_missing_source",
                            "severity": "error",
                            "title": "图谱条目未登记边来源",
                            "description": "该 Timeline 的图谱条目引用了有效边，但边的来源索引不完整。",
                            "count": int(row["item_count"]),
                            "memory_id": int(row["source_memory_id"]),
                            "repairable": True,
                            "repair_action": "rebuild_graph_memory",
                        }
                    )

            if await self._table_exists(db, "memory_registry") and has_documents:
                count_row = await (
                    await db.execute(
                        """
                        SELECT COUNT(*) FROM memory_registry AS registry
                        LEFT JOIN documents AS document
                          ON document.id = registry.document_id
                        WHERE document.id IS NULL
                        """
                    )
                ).fetchone()
                if count_row and int(count_row[0]):
                    issues.append(
                        {
                            "issue_uid": "orphan_memory_registry",
                            "kind": "orphan_memory_registry",
                            "severity": "error",
                            "title": "Timeline 身份索引缺少原始文档",
                            "description": "memory_registry 中存在指向不存在 documents 的记录。",
                            "count": int(count_row[0]),
                            "repairable": False,
                            "repair_action": None,
                        }
                    )

            if await self._table_exists(db, "memory_atoms") and has_documents:
                count_row = await (
                    await db.execute(
                        """
                        SELECT COUNT(*) FROM memory_atoms AS atom
                        LEFT JOIN documents AS document
                          ON document.id = atom.parent_memory_id
                        WHERE document.id IS NULL
                        """
                    )
                ).fetchone()
                if count_row and int(count_row[0]):
                    issues.append(
                        {
                            "issue_uid": "orphan_memory_atoms",
                            "kind": "orphan_memory_atoms",
                            "severity": "error",
                            "title": "事实原子缺少父 Timeline",
                            "description": "memory_atoms 中存在无法追溯到 Timeline 的记录。",
                            "count": int(count_row[0]),
                            "repairable": False,
                            "repair_action": None,
                        }
                    )
        return issues

    async def check_health(
        self,
        memory_engine: MemoryEngine,
        conversation_manager: ConversationManager | None,
    ) -> dict[str, Any]:
        """Run explicit, read-only SQLite and application-level checks."""
        try:
            databases = [await self._inspect_sqlite(memory_engine.db_path, "主记忆库")]
            conversation_path = getattr(
                getattr(conversation_manager, "store", None), "db_path", None
            )
            if conversation_path:
                databases.append(
                    await self._inspect_sqlite(conversation_path, "原始会话库")
                )
            issues = await self._main_consistency_issues(memory_engine.db_path)

            graph_orphan_keys = {
                ("graph_entries", int(entry_id))
                for issue in issues
                if issue.get("kind") == "graph_orphan_entries"
                for entry_id in issue.get("entry_ids", [])
            }
            for database in databases:
                for violation in database["foreign_key_violations"]:
                    key = (violation["table"], violation["row_id"])
                    if key in graph_orphan_keys:
                        continue
                    issues.append(
                        {
                            "issue_uid": (
                                f"foreign_key:{database['filename']}:"
                                f"{violation['table']}:{violation['row_id']}:"
                                f"{violation['constraint_index']}"
                            ),
                            "kind": "foreign_key_violation",
                            "severity": "error",
                            "title": "数据库外键约束不一致",
                            "description": (
                                f"{violation['table']} 的记录 "
                                f"{violation['row_id']} 无法引用 "
                                f"{violation['parent_table']}。"
                            ),
                            "count": 1,
                            "repairable": False,
                            "repair_action": None,
                        }
                    )

            integrity_failed = any(db["integrity"] != "ok" for db in databases)
            repairable_count = sum(
                int(issue.get("count") or 1)
                for issue in issues
                if issue.get("repairable")
            )
            return self.utils.ok(
                {
                    "checked_at": time.time(),
                    "summary": {
                        "status": "error" if integrity_failed or issues else "healthy",
                        "issue_group_count": len(issues),
                        "issue_count": sum(
                            int(issue.get("count") or 1) for issue in issues
                        ),
                        "repairable_count": repairable_count,
                    },
                    "databases": databases,
                    "issues": issues,
                }
            )
        except Exception as exc:  # noqa: BLE001 - API boundary must report check failures.
            logger.error("[PageAPI] 数据库健康检查失败", exc_info=True)
            return self.utils.error(str(exc))

    def _active_repair_job(self) -> dict[str, Any] | None:
        return next(
            (
                job
                for job in self._repair_jobs.values()
                if job.get("status") in {"pending", "running"}
            ),
            None,
        )

    def _prune_repair_jobs(self, *, keep: int = 20) -> None:
        terminal = sorted(
            (
                job
                for job in self._repair_jobs.values()
                if job.get("status")
                in {"completed", "completed_with_errors", "failed", "cancelled"}
            ),
            key=lambda job: float(job.get("completed_at") or 0),
            reverse=True,
        )
        for job in terminal[keep:]:
            self._repair_jobs.pop(str(job["job_uid"]), None)

    async def start_repair(
        self,
        memory_engine: MemoryEngine,
        conversation_manager: ConversationManager | None,
    ) -> dict[str, Any]:
        """Start a selected repair in the background and return its task state."""
        payload = await request.get_json(silent=True) or {}
        selected = payload.get("issues") or []
        if not isinstance(selected, list) or not selected:
            return self.utils.error("请选择需要修复的数据库问题")
        active = self._active_repair_job()
        if active is not None:
            return self.utils.error("已有数据库修复任务正在运行")
        if self._active_storage_job() is not None:
            return self.utils.error("数据库存储维护期间不能执行修复")

        selected_uids = list(
            dict.fromkeys(
                str(raw.get("issue_uid") if isinstance(raw, dict) else raw).strip()
                for raw in selected
                if str(raw.get("issue_uid") if isinstance(raw, dict) else raw).strip()
            )
        )
        if not selected_uids:
            return self.utils.error("请选择需要修复的数据库问题")

        job_uid = str(uuid.uuid4())
        now = time.time()
        job: dict[str, Any] = {
            "job_uid": job_uid,
            "status": "pending",
            "stage": "validating",
            "current": 0,
            "total": len(selected_uids) + 2,
            "percent": 0.0,
            "current_step": "正在重新检查所选问题",
            "selected_issue_uids": selected_uids,
            "repaired": [],
            "failed": [],
            "created_at": now,
            "updated_at": now,
        }
        self._repair_jobs[job_uid] = job
        self._latest_repair_job_uid = job_uid
        self._prune_repair_jobs()
        task = asyncio.create_task(
            self._run_repair_job(
                job_uid,
                memory_engine,
                conversation_manager,
            ),
            name=f"livingmemory-database-repair-{job_uid[:8]}",
        )
        self._repair_tasks[job_uid] = task
        task.add_done_callback(lambda _task: self._repair_tasks.pop(job_uid, None))
        return self.utils.ok(dict(job))

    def _update_repair_job(
        self,
        job_uid: str,
        *,
        stage: str,
        current: int,
        current_step: str,
    ) -> None:
        job = self._repair_jobs[job_uid]
        total = max(1, int(job.get("total") or 1))
        job.update(
            {
                "status": "running",
                "stage": stage,
                "current": max(0, min(total, int(current))),
                "percent": round(max(0.0, min(100.0, current / total * 100)), 1),
                "current_step": current_step,
                "updated_at": time.time(),
            }
        )

    async def _run_repair_job(
        self,
        job_uid: str,
        memory_engine: MemoryEngine,
        conversation_manager: ConversationManager | None,
    ) -> None:
        job = self._repair_jobs[job_uid]
        try:
            self._update_repair_job(
                job_uid,
                stage="validating",
                current=0,
                current_step="正在重新检查所选问题",
            )
            current_response = await self.check_health(
                memory_engine, conversation_manager
            )
            if current_response.get("status") != "ok":
                raise RuntimeError(
                    str(current_response.get("message") or "数据库健康检查失败")
                )
            current_issues = {
                str(issue["issue_uid"]): issue
                for issue in current_response["data"].get("issues", [])
                if issue.get("repairable")
            }
            selected_issues = [
                current_issues[issue_uid]
                for issue_uid in job["selected_issue_uids"]
                if issue_uid in current_issues
            ]
            missing = [
                issue_uid
                for issue_uid in job["selected_issue_uids"]
                if issue_uid not in current_issues
            ]
            for issue_uid in missing:
                job["failed"].append(
                    {"issue_uid": issue_uid, "error": "该问题已不存在或不支持自动修复"}
                )
            # Validation and the final health check are explicit progress steps.
            job["total"] = len(selected_issues) + 2
            self._update_repair_job(
                job_uid,
                stage="repairing",
                current=1,
                current_step=(
                    f"检查完成，将处理 {len(selected_issues)} 项修复"
                    if selected_issues
                    else "所选问题已不存在"
                ),
            )

            rebuilt_memory_ids: set[int] = set()
            for index, issue in enumerate(selected_issues, 1):
                self._update_repair_job(
                    job_uid,
                    stage="repairing",
                    current=index,
                    current_step=f"正在修复 {issue.get('title') or issue['issue_uid']} ({index}/{len(selected_issues)})",
                )
                try:
                    result = await self._repair_issue(
                        issue,
                        memory_engine,
                        rebuilt_memory_ids,
                    )
                    if result is not None:
                        job["repaired"].append(result)
                except Exception as exc:  # noqa: BLE001 - continue selected repairs.
                    logger.error(
                        "[PageAPI] 数据库手动修复失败: %s",
                        issue.get("issue_uid"),
                        exc_info=True,
                    )
                    job["failed"].append(
                        {"issue_uid": issue.get("issue_uid"), "error": str(exc)}
                    )
                self._update_repair_job(
                    job_uid,
                    stage="repairing",
                    current=index + 1,
                    current_step=f"已处理 {index}/{len(selected_issues)} 项修复",
                )

            if hasattr(memory_engine, "_invalidate_search_cache"):
                memory_engine._invalidate_search_cache()
            self._update_repair_job(
                job_uid,
                stage="verifying",
                current=max(1, int(job["total"]) - 1),
                current_step="正在验证修复后的数据库状态",
            )
            health = await self.check_health(memory_engine, conversation_manager)
            if health.get("status") != "ok":
                raise RuntimeError(str(health.get("message") or "修复后验证失败"))
            now = time.time()
            job.update(
                {
                    "status": (
                        "completed_with_errors" if job["failed"] else "completed"
                    ),
                    "stage": "completed",
                    "current": int(job["total"]),
                    "percent": 100.0,
                    "current_step": "数据库修复与验证已完成",
                    "health": health["data"],
                    "updated_at": now,
                    "completed_at": now,
                }
            )
        except asyncio.CancelledError:
            now = time.time()
            job.update(
                {
                    "status": "cancelled",
                    "stage": "cancelled",
                    "current_step": "插件停止，数据库修复任务已取消",
                    "updated_at": now,
                    "completed_at": now,
                }
            )
            raise
        except Exception as exc:  # noqa: BLE001 - task boundary records failures.
            logger.error("[PageAPI] 数据库修复任务失败", exc_info=True)
            now = time.time()
            job.update(
                {
                    "status": "failed",
                    "stage": "failed",
                    "error": str(exc),
                    "current_step": "数据库修复任务失败",
                    "updated_at": now,
                    "completed_at": now,
                }
            )

    async def _repair_issue(
        self,
        issue: dict[str, Any],
        memory_engine: MemoryEngine,
        rebuilt_memory_ids: set[int],
    ) -> dict[str, Any] | None:
        """Apply one validated repair issue."""
        action = issue.get("repair_action")
        if action == "rebuild_graph_memory":
            memory_id = int(issue["memory_id"])
            if memory_id in rebuilt_memory_ids:
                return None
            memory = await memory_engine.get_memory(memory_id)
            if memory is None:
                raise RuntimeError(f"Timeline {memory_id} 已不存在")
            metadata = memory.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = self.utils.normalize_metadata(metadata)
            atoms = None
            atom_store = getattr(memory_engine, "atom_store", None)
            if atom_store is not None:
                atoms = await atom_store.get_by_parent(memory_id)
            manager = getattr(memory_engine, "graph_memory_manager", None)
            if manager is None:
                raise RuntimeError("图谱记忆未启用")
            await manager.index_memory(
                memory_id,
                str(memory.get("text") or ""),
                metadata,
                atoms or None,
            )
            rebuilt_memory_ids.add(memory_id)
            return {
                "issue_uid": issue["issue_uid"],
                "action": action,
                "memory_id": memory_id,
            }
        if action == "delete_orphan_graph_entries":
            manager = getattr(memory_engine, "graph_memory_manager", None)
            if manager is None:
                raise RuntimeError("图谱记忆未启用")
            entry_ids = [int(item) for item in issue.get("entry_ids", [])]
            await manager.delete_orphaned_entries(entry_ids)
            return {
                "issue_uid": issue["issue_uid"],
                "action": action,
                "deleted_entry_count": len(entry_ids),
            }
        raise RuntimeError(f"不支持的数据库修复操作: {action}")

    async def get_repair_progress(self) -> dict[str, Any]:
        job_uid = str(request.args.get("job_uid") or "").strip()
        if not job_uid:
            job_uid = self._latest_repair_job_uid or ""
        if not job_uid or job_uid not in self._repair_jobs:
            return self.utils.ok({"status": "idle"})
        return self.utils.ok(dict(self._repair_jobs[job_uid]))

    def _active_storage_job(self) -> dict[str, Any] | None:
        return next(
            (
                job
                for job in self._storage_jobs.values()
                if job.get("status") in {"pending", "running"}
            ),
            None,
        )

    async def get_storage_preview(self, memory_engine: MemoryEngine) -> dict[str, Any]:
        """Preview removable build artifacts without modifying SQLite."""
        try:
            return self.utils.ok(await memory_engine.preview_storage_maintenance())
        except Exception as exc:  # noqa: BLE001 - page boundary.
            logger.error("[PageAPI] 数据库存储维护预览失败", exc_info=True)
            return self.utils.error(str(exc))

    def _update_storage_job(
        self,
        job_uid: str,
        *,
        stage: str,
        current: int,
        total: int,
        current_step: str,
    ) -> None:
        job = self._storage_jobs[job_uid]
        safe_total = max(1, int(total))
        safe_current = max(0, min(safe_total, int(current)))
        job.update(
            {
                "status": "running",
                "stage": stage,
                "current": safe_current,
                "total": safe_total,
                "percent": round(safe_current / safe_total * 100, 1),
                "current_step": current_step,
                "updated_at": time.time(),
            }
        )

    async def start_storage_maintenance(
        self, memory_engine: MemoryEngine
    ) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip()
        if action not in {"cleanup_completed_artifacts", "vacuum"}:
            return self.utils.error("不支持的数据库存储维护操作")
        if self._active_storage_job() is not None:
            return self.utils.error("已有数据库存储维护任务正在运行")
        if self._active_repair_job() is not None:
            return self.utils.error("数据库修复期间不能执行存储维护")
        if action == "vacuum" and memory_engine.topic_build_manager.has_active_builds():
            return self.utils.error("Topic 构建期间不能压缩数据库，请等待构建完成")

        job_uid = str(uuid.uuid4())
        now = time.time()
        job = {
            "job_uid": job_uid,
            "action": action,
            "status": "pending",
            "stage": "preparing",
            "current": 0,
            "total": 1,
            "percent": 0.0,
            "current_step": "正在准备数据库维护",
            "created_at": now,
            "updated_at": now,
        }
        self._storage_jobs[job_uid] = job
        self._latest_storage_job_uid = job_uid
        task = asyncio.create_task(
            self._run_storage_maintenance_job(job_uid, memory_engine),
            name=f"livingmemory-storage-maintenance-{job_uid[:8]}",
        )
        self._storage_tasks[job_uid] = task
        task.add_done_callback(lambda _task: self._storage_tasks.pop(job_uid, None))
        return self.utils.ok(dict(job))

    async def _run_storage_maintenance_job(
        self,
        job_uid: str,
        memory_engine: MemoryEngine,
    ) -> None:
        job = self._storage_jobs[job_uid]
        action = str(job["action"])

        async def progress(
            stage: str, current: int, total: int, step: str
        ) -> None:
            self._update_storage_job(
                job_uid,
                stage=stage,
                current=current,
                total=total,
                current_step=step,
            )

        try:
            if action == "cleanup_completed_artifacts":
                result = await memory_engine.cleanup_completed_storage_artifacts(
                    progress_callback=progress
                )
            else:
                result = await memory_engine.maintain_storage(
                    vacuum=True,
                    progress_callback=progress,
                )
                if not result.get("success"):
                    raise RuntimeError(
                        str(result.get("error") or "数据库压缩失败")
                    )
            preview = await memory_engine.preview_storage_maintenance()
            now = time.time()
            job.update(
                {
                    "status": "completed",
                    "stage": "completed",
                    "current": int(job.get("total") or 1),
                    "percent": 100.0,
                    "current_step": "数据库存储维护已完成",
                    "result": result,
                    "preview": preview,
                    "updated_at": now,
                    "completed_at": now,
                }
            )
        except asyncio.CancelledError:
            now = time.time()
            job.update(
                {
                    "status": "cancelled",
                    "stage": "cancelled",
                    "current_step": "插件停止，数据库维护任务已取消",
                    "updated_at": now,
                    "completed_at": now,
                }
            )
            raise
        except Exception as exc:  # noqa: BLE001 - task boundary.
            logger.error("[PageAPI] 数据库存储维护任务失败", exc_info=True)
            now = time.time()
            job.update(
                {
                    "status": "failed",
                    "stage": "failed",
                    "error": str(exc),
                    "current_step": "数据库存储维护任务失败",
                    "updated_at": now,
                    "completed_at": now,
                }
            )

    async def get_storage_maintenance_progress(self) -> dict[str, Any]:
        job_uid = str(request.args.get("job_uid") or "").strip()
        if not job_uid:
            job_uid = self._latest_storage_job_uid or ""
        if not job_uid or job_uid not in self._storage_jobs:
            return self.utils.ok({"status": "idle"})
        return self.utils.ok(dict(self._storage_jobs[job_uid]))

    async def clear_storage_maintenance_progress(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        job_uid = str(
            payload.get("job_uid") or self._latest_storage_job_uid or ""
        ).strip()
        if not job_uid or job_uid not in self._storage_jobs:
            return self.utils.ok({"cleared": False, "job_uid": job_uid})
        job = self._storage_jobs[job_uid]
        if job.get("status") in {"pending", "running"}:
            return self.utils.error("数据库存储维护仍在运行，不能清除进度")
        self._storage_jobs.pop(job_uid, None)
        self._storage_tasks.pop(job_uid, None)
        if self._latest_storage_job_uid == job_uid:
            self._latest_storage_job_uid = next(reversed(self._storage_jobs), None)
        return self.utils.ok({"cleared": True, "job_uid": job_uid})

    async def shutdown(self) -> None:
        tasks = [task for task in self._repair_tasks.values() if not task.done()]
        tasks.extend(
            task for task in self._storage_tasks.values() if not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._repair_tasks.clear()
        self._storage_tasks.clear()

    async def repair(
        self,
        memory_engine: MemoryEngine,
        conversation_manager: ConversationManager | None,
    ) -> dict[str, Any]:
        """Compatibility helper: run selected repairs and wait for completion."""
        response = await self.start_repair(memory_engine, conversation_manager)
        if response.get("status") != "ok":
            return response
        job_uid = str(response["data"]["job_uid"])
        task = self._repair_tasks.get(job_uid)
        if task is not None:
            await task
        job = self._repair_jobs[job_uid]
        if job.get("status") == "failed":
            return self.utils.error(str(job.get("error") or "数据库修复失败"))
        return self.utils.ok(
            {
                "repaired": job.get("repaired", []),
                "failed": job.get("failed", []),
                "health": job.get("health"),
            }
        )


__all__ = ["DatabaseHandler"]
