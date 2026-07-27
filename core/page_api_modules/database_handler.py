"""Database health inspection and explicitly confirmed maintenance actions."""

from __future__ import annotations

import time
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

    async def repair(
        self,
        memory_engine: MemoryEngine,
        conversation_manager: ConversationManager | None,
    ) -> dict[str, Any]:
        """Apply only user-selected repairs that still match current issues."""
        payload = await request.get_json(silent=True) or {}
        selected = payload.get("issues") or []
        if not isinstance(selected, list) or not selected:
            return self.utils.error("请选择需要修复的数据库问题")

        current_response = await self.check_health(memory_engine, conversation_manager)
        if current_response.get("status") != "ok":
            return current_response
        current_issues = {
            str(issue["issue_uid"]): issue
            for issue in current_response["data"].get("issues", [])
            if issue.get("repairable")
        }

        selected_issues: list[dict[str, Any]] = []
        for raw in selected:
            issue_uid = str(raw.get("issue_uid") if isinstance(raw, dict) else raw)
            issue = current_issues.get(issue_uid)
            if issue is not None:
                selected_issues.append(issue)
        if not selected_issues:
            return self.utils.error("所选问题已不存在或不支持自动修复")

        repaired: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        rebuilt_memory_ids: set[int] = set()
        for issue in selected_issues:
            try:
                action = issue.get("repair_action")
                if action == "rebuild_graph_memory":
                    memory_id = int(issue["memory_id"])
                    if memory_id in rebuilt_memory_ids:
                        continue
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
                    repaired.append(
                        {
                            "issue_uid": issue["issue_uid"],
                            "action": action,
                            "memory_id": memory_id,
                        }
                    )
                elif action == "delete_orphan_graph_entries":
                    manager = getattr(memory_engine, "graph_memory_manager", None)
                    if manager is None:
                        raise RuntimeError("图谱记忆未启用")
                    entry_ids = [int(item) for item in issue.get("entry_ids", [])]
                    await manager.delete_orphaned_entries(entry_ids)
                    repaired.append(
                        {
                            "issue_uid": issue["issue_uid"],
                            "action": action,
                            "deleted_entry_count": len(entry_ids),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - continue other selected repairs.
                logger.error(
                    "[PageAPI] 数据库手动修复失败: %s",
                    issue.get("issue_uid"),
                    exc_info=True,
                )
                failed.append({"issue_uid": issue.get("issue_uid"), "error": str(exc)})

        if hasattr(memory_engine, "_invalidate_search_cache"):
            memory_engine._invalidate_search_cache()
        health = await self.check_health(memory_engine, conversation_manager)
        return self.utils.ok(
            {
                "repaired": repaired,
                "failed": failed,
                "health": health.get("data") if health.get("status") == "ok" else None,
            }
        )


__all__ = ["DatabaseHandler"]
