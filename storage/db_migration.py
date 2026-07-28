"""
数据库迁移管理器 - 处理数据库版本升级和数据迁移
"""

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import aiosqlite

from astrbot.api import logger

from ..core.models.memory_identity import resolve_memory_space
from ..core.topic_fragment_identity import (
    fragment_semantic_discriminator,
    logical_fragment_uid,
)
from .memory_identity_store import MemoryIdentityStore
from .topic_memory_store import TopicMemoryStore


class DBMigration:
    """数据库迁移管理器"""

    # 当前数据库版本
    CURRENT_VERSION = "10"

    # 版本历史记录
    VERSION_HISTORY = {
        1: "初始版本 - 基础记忆存储",
        2: "FTS5索引预处理 - 添加分词和停用词支持",
        3: "会话ID迁移 - 标记需要session_id格式升级",
        4: "Schema v2 - 双通道总结字段 + source_window 溯源支持",
        5: "Graph memory - graph tables and dual-route retrieval metadata",
        6: "插件 FTS 表统一 livingmemory 前缀，旧 documents_fts 安全重命名备份",
        7: "Storage indexes and FTS optimization for graph and atom data",
        8: "Write-operation log and access-aware metadata indexes",
        9: "Stable timeline identity registry and source-span provenance",
        "10": "Stable Timeline and Topic memory architecture release",
    }

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.migration_lock = asyncio.Lock()

    @classmethod
    def normalize_version(cls, version: Any) -> str:
        """Return a canonical dotted version without using float arithmetic."""
        text = str(version).strip().lower()
        if text.startswith("v"):
            text = text[1:]
        parts = text.split(".")
        if not parts or any(not part.isdigit() for part in parts):
            raise ValueError(f"无效数据库版本号: {version}")
        normalized = [str(int(part)) for part in parts]
        while len(normalized) > 1 and normalized[-1] == "0":
            normalized.pop()
        return ".".join(normalized)

    @classmethod
    def version_key(cls, version: Any) -> tuple[int, ...]:
        """Build a segment-wise comparison key (9.10 is newer than 9.2)."""
        return tuple(int(part) for part in cls.normalize_version(version).split("."))

    @classmethod
    def storage_version(cls, version: Any) -> str:
        """Force TEXT storage even in legacy INTEGER-affinity version columns."""
        return f"v{cls.normalize_version(version)}"

    @classmethod
    def version_at_least(cls, version: Any, target: Any) -> bool:
        return cls.version_key(version) >= cls.version_key(target)

    async def get_db_version(self) -> str:
        """
        获取当前数据库版本

        Returns:
            str: 规范化数据库版本号，如果不存在版本表则返回 "1"
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # 检查版本表是否存在
                cursor = await db.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='db_version'
                """)
                table_exists = await cursor.fetchone()

                if not table_exists or len(table_exists) == 0:
                    # 没有版本表，检查是否有documents表（判断是否为旧数据库）
                    cursor = await db.execute("""
                        SELECT name FROM sqlite_master
                        WHERE type='table' AND name='documents'
                    """)
                    has_documents = await cursor.fetchone()

                    if has_documents:
                        # 有documents表但没有版本表，检查是否有数据
                        cursor = await db.execute("SELECT COUNT(*) FROM documents")
                        doc_count_row = await cursor.fetchone()
                        doc_count = doc_count_row[0] if doc_count_row else 0

                        if doc_count > 0:
                            # 有数据但无版本表，判定为v1旧数据库
                            # 注意：v2数据库在初始化时会自动创建版本表，不会出现这种情况
                            logger.info(
                                f"检测到旧版本数据库（无版本表，有{doc_count}条数据），当前版本: 1"
                            )
                            return "1"
                        else:
                            # 空数据库，视为最新版本
                            logger.info(
                                "检测到空数据库（已初始化但无数据），视为最新版本"
                            )
                            return self.CURRENT_VERSION
                    else:
                        # 全新数据库，没有任何表，视为最新版本
                        logger.info("检测到全新数据库，视为最新版本")
                        return self.CURRENT_VERSION

                # 读取版本号
                cursor = await db.execute(
                    "SELECT version FROM db_version ORDER BY id DESC LIMIT 1"
                )
                row = await cursor.fetchone()

                if row and len(row) > 0:
                    version = self.normalize_version(row[0])
                    logger.info(f"当前数据库版本: {version}")
                    return version
                else:
                    return "1"

        except Exception as e:
            logger.error(f"获取数据库版本失败: {e}", exc_info=True)
            return "1"

    async def initialize_version_table(self):
        """初始化版本管理表"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS db_version (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        version TEXT NOT NULL,
                        description TEXT,
                        migrated_at TEXT NOT NULL,
                        migration_duration_seconds REAL
                    )
                """)
                await db.commit()
                logger.info("数据库版本管理表初始化完成")
        except Exception as e:
            logger.error(f"初始化版本表失败: {e}", exc_info=True)
            raise

    async def set_db_version(
        self, version: str | int, description: str = "", duration: float = 0.0
    ):
        """
        设置数据库版本

        Args:
            version: 版本号
            description: 版本描述
            duration: 迁移耗时（秒）
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO db_version (version, description, migrated_at, migration_duration_seconds)
                    VALUES (?, ?, ?, ?)
                """,
                    (
                        self.storage_version(version),
                        description,
                        datetime.now(timezone.utc).isoformat(),
                        duration,
                    ),
                )
                await db.commit()
                logger.info(
                    f"数据库版本已更新至: {self.normalize_version(version)}"
                )
        except Exception as e:
            logger.error(f"设置数据库版本失败: {e}", exc_info=True)
            raise

    async def needs_migration(self) -> bool:
        """
        检查是否需要迁移

        Returns:
            bool: True表示需要迁移
        """
        current_version = await self.get_db_version()
        needs_migration = self.version_key(current_version) < self.version_key(
            self.CURRENT_VERSION
        )

        if needs_migration:
            logger.warning(
                f"数据库需要迁移: v{current_version} -> v{self.CURRENT_VERSION}"
            )
        else:
            logger.info(f"数据库版本最新: v{current_version}")

        return needs_migration

    async def migrate(
        self,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """
        执行数据库迁移

        Args:
            progress_callback: 进度回调函数 (message, current, total)

        Returns:
            Dict: 迁移结果
        """
        async with self.migration_lock:
            start_time = datetime.now()

            try:
                # 初始化版本表
                await self.initialize_version_table()

                # 获取当前版本
                current_version = await self.get_db_version()

                current_key = self.version_key(current_version)
                if current_key >= self.version_key(self.CURRENT_VERSION):
                    return {
                        "success": True,
                        "message": "数据库已是最新版本，无需迁移",
                        "from_version": current_version,
                        "to_version": self.CURRENT_VERSION,
                        "duration": 0,
                    }

                logger.info(
                    f"开始数据库迁移: v{current_version} -> v{self.CURRENT_VERSION}"
                )

                # 迁移前自动备份，确保数据安全
                backup_path = await self.create_backup()
                if backup_path:
                    logger.info(f"迁移前备份已创建: {backup_path}")
                else:
                    logger.warning(
                        "迁移前备份失败，迁移将继续执行。请确认磁盘空间与文件权限。"
                    )

                # 执行迁移步骤
                migration_steps = []

                # 从版本1升级到版本2
                if current_key == self.version_key("1"):
                    migration_steps.append(self._migrate_v1_to_v2)

                # 从版本2升级到版本3
                if current_key <= self.version_key("2"):
                    migration_steps.append(self._migrate_v2_to_v3)

                # 从版本3升级到版本4
                if current_key <= self.version_key("3"):
                    migration_steps.append(self._migrate_v3_to_v4)

                # 从版本4升级到版本5
                if current_key <= self.version_key("4"):
                    migration_steps.append(self._migrate_v4_to_v5)

                # 从版本5升级到版本6
                if current_key <= self.version_key("5"):
                    migration_steps.append(self._migrate_v5_to_v6)

                # 从版本6升级到版本7
                if current_key <= self.version_key("6"):
                    migration_steps.append(self._migrate_v6_to_v7)

                # 从版本7升级到版本8
                if current_key <= self.version_key("7"):
                    migration_steps.append(self._migrate_v7_to_v8)

                # 从版本8升级到版本9
                if current_key <= self.version_key("8"):
                    migration_steps.append(self._migrate_v8_to_v9)

                # v9.x 仅存在于开发期。正式迁移边界保持为 v8 -> v9 -> v10；
                # 对开发数据库仍按其实际小版本跳过已经执行过的内部阶段。
                if current_key < self.version_key("10"):
                    v10_start = (
                        "9" if current_key <= self.version_key("8") else current_version
                    )
                    migration_steps.append(
                        partial(self._migrate_v9_to_v10, from_version=v10_start)
                    )

                # 执行所有迁移步骤
                for step in migration_steps:
                    await step(progress_callback)

                # 计算耗时
                duration = (datetime.now() - start_time).total_seconds()

                # 更新版本号
                await self.set_db_version(
                    self.CURRENT_VERSION,
                    self.VERSION_HISTORY.get(self.CURRENT_VERSION, ""),
                    duration,
                )

                logger.info(f"数据库迁移成功完成，耗时: {duration:.2f}秒")

                return {
                    "success": True,
                    "message": f"数据库迁移成功: v{current_version} -> v{self.CURRENT_VERSION}",
                    "from_version": current_version,
                    "to_version": self.CURRENT_VERSION,
                    "duration": duration,
                    "backup_path": backup_path,
                }

            except Exception as e:
                logger.error(f"数据库迁移失败: {e}", exc_info=True)
                return {
                    "success": False,
                    "message": f"数据库迁移失败: {str(e)}",
                    "error": str(e),
                }

    async def _migrate_v1_to_v2(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        从版本1迁移到版本2
        主要变更：重建BM25索引和向量索引以支持新的检索架构
        """
        logger.info("执行迁移步骤: v1 -> v2 (重建索引)")

        try:
            # 检查是否有documents表
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM sqlite_master
                    WHERE type='table' AND name='documents'
                """)
                has_table_row = await cursor.fetchone()
                has_table = (
                    has_table_row[0] if has_table_row and len(has_table_row) > 0 else 0
                ) > 0

                if not has_table:
                    logger.info("未找到 documents 表，按新数据库处理")
                    return

                # 获取文档总数
                cursor = await db.execute("SELECT COUNT(*) FROM documents")
                total_docs_row = await cursor.fetchone()
                total_docs = total_docs_row[0] if total_docs_row else 0

                if total_docs == 0:
                    logger.info("数据库为空，无需重建索引")
                    return

                logger.info(f"发现 {total_docs} 条 v1 数据，标记待重建索引")

                # 获取所有文档数据
                cursor = await db.execute("SELECT id, text, metadata FROM documents")
                await cursor.fetchall()

            # 重建索引需要在插件初始化完成后进行
            # 这里只记录需要重建的标记，实际重建在插件启动时处理
            logger.warning(f"检测到 {total_docs} 条 v1 迁移数据需要重建索引")
            logger.warning("请在插件初始化完成后，使用 WebUI 数据迁移功能或执行命令:")
            logger.warning("/lmem rebuild-index")
            logger.info(f"数据库迁移完成（{total_docs} 条文档已保留在 documents 表）")

            # 创建迁移状态标记
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS migration_status (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TEXT
                    )
                """)
                await db.execute(
                    """
                    INSERT OR REPLACE INTO migration_status (key, value, updated_at)
                    VALUES (?, ?, ?)
                """,
                    ("needs_index_rebuild", "true", datetime.now(timezone.utc).isoformat()),
                )
                await db.execute(
                    """
                    INSERT OR REPLACE INTO migration_status (key, value, updated_at)
                    VALUES (?, ?, ?)
                """,
                    (
                        "pending_documents_count",
                        str(total_docs),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                await db.commit()

        except Exception as e:
            logger.error(f"数据库迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v2_to_v3(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        从版本2迁移到版本3
        主要变更：标记需要进行 session_id 格式升级

        策略说明：
        不在迁移阶段进行数据转换，原因：
        1. 大多数用户只有一个Bot，旧的session_id实际上就对应当前Bot的unified_msg_origin
        2. 迁移时无法获取运行时的platform信息，无法生成正确的unified_msg_origin
        3. 插件运行时会自动使用unified_msg_origin，旧数据保持不变不影响使用
        4. 只有多Bot用户才会遇到session_id冲突，这种情况下新消息会使用新格式

        此迁移步骤仅升级版本号，不进行实际数据转换。
        """
        logger.info("执行迁移步骤: v2 -> v3 (session_id 格式升级)")

        try:
            logger.info(
                "插件现在使用 unified_msg_origin (格式:platform:type:id) 作为会话标识"
            )
            logger.info("旧数据保持不变，新消息自动使用新格式")
            logger.info("对于单 Bot 用户，这不会导致任何问题")
            logger.info("对于多 Bot 用户，新旧数据会自然分离，避免混淆")

            logger.info("v2 -> v3 迁移完成")

        except Exception as e:
            logger.error(f"v2 -> v3 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v3_to_v4(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        从版本3迁移到版本4
        主要变更：
        - 旧记录 metadata 中补充 summary_schema_version=v1（标记为旧格式）
        - 新写入记录将自动携带 canonical_summary / persona_summary / source_window
        - 无法回填 source_window 的旧数据不做处理（traceable=false 由读取方判断）
        """
        logger.info("执行迁移步骤: v3 -> v4 (Schema v2 双通道总结字段)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                # 检查 documents 表是否存在
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM sqlite_master
                    WHERE type='table' AND name='documents'
                """)
                row = await cursor.fetchone()
                if not row or row[0] == 0:
                    logger.info("未找到 documents 表，跳过 v4 迁移")
                    return

                # 为没有 summary_schema_version 的旧记录打上 v1 标记
                # 使用 JSON 函数更新 metadata 字段
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM documents WHERE metadata IS NULL OR metadata NOT LIKE '%summary_schema_version%'"
                )
                count_row = await cursor.fetchone()
                legacy_count = count_row[0] if count_row else 0

                if legacy_count > 0:
                    logger.info(
                        f"发现 {legacy_count} 条旧格式记录，补充 summary_schema_version=v1 标记"
                    )

                    # 批量更新：将旧记录的 metadata 中注入 schema 版本标记
                    # 使用 COALESCE(NULLIF(...)) 处理 NULL/空字符串，再用 json_set 追加字段
                    await db.execute("""
                        UPDATE documents
                        SET metadata = json_set(
                            COALESCE(NULLIF(TRIM(COALESCE(metadata, '')), ''), '{}'),
                            '$.summary_schema_version', 'v1',
                            '$.summary_quality', 'unknown'
                        )
                        WHERE metadata IS NULL OR metadata NOT LIKE '%summary_schema_version%'
                    """)
                    await db.commit()
                    logger.info(f"已为 {legacy_count} 条旧记录补充 schema 版本标记")
                else:
                    logger.info("所有记录已有 summary_schema_version，无需补充")

            logger.info("v3 -> v4 迁移完成")

        except Exception as e:
            logger.error(f"v3 -> v4 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v4_to_v5(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        Migrate from version 4 to version 5.

        Main changes:
        - Create graph-memory tables used by the dual-route retrieval layer
        - Keep legacy document memory data unchanged
        """
        logger.info("执行迁移步骤: v4 -> v5 (Graph memory tables)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_nodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        node_key TEXT NOT NULL UNIQUE,
                        node_type TEXT NOT NULL,
                        node_value TEXT NOT NULL,
                        canonical_value TEXT NOT NULL,
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_edges (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        edge_key TEXT NOT NULL UNIQUE,
                        source_node_id INTEGER NOT NULL,
                        target_node_id INTEGER NOT NULL,
                        relation_type TEXT NOT NULL,
                        source_memory_id INTEGER NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0,
                        confidence REAL NOT NULL DEFAULT 0.8,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
                        FOREIGN KEY(target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        entry_key TEXT NOT NULL UNIQUE,
                        source_memory_id INTEGER NOT NULL,
                        session_id TEXT,
                        persona_id TEXT,
                        entry_type TEXT NOT NULL,
                        relation_type TEXT,
                        content TEXT NOT NULL,
                        metadata TEXT DEFAULT '{}',
                        edge_id INTEGER,
                        vector_doc_id INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(edge_id) REFERENCES graph_edges(id) ON DELETE CASCADE
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_entry_nodes (
                        entry_id INTEGER NOT NULL,
                        node_id INTEGER NOT NULL,
                        PRIMARY KEY(entry_id, node_id),
                        FOREIGN KEY(entry_id) REFERENCES graph_entries(id) ON DELETE CASCADE,
                        FOREIGN KEY(node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS livingmemory_graph_entries_fts
                    USING fts5(content, entry_id UNINDEXED, tokenize='unicode61')
                    """
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_graph_nodes_canonical ON graph_nodes(canonical_value)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_graph_edges_memory_id ON graph_edges(source_memory_id)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_graph_entries_memory_id ON graph_entries(source_memory_id)"
                )
                await db.commit()

            logger.info("v4 -> v5 迁移完成")

        except Exception as e:
            logger.error(f"v4 -> v5 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v5_to_v6(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        从版本5迁移到版本6
        主要变更：插件 FTS 表统一 livingmemory 前缀，旧 documents_fts 仅在精确匹配旧结构时重命名备份。
        """
        logger.info("执行迁移步骤: v5 -> v6 (FTS 表前缀化与旧表备份)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS livingmemory_memories_fts
                    USING fts5(content, doc_id UNINDEXED, tokenize='unicode61')
                """)
                await db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS livingmemory_graph_entries_fts
                    USING fts5(content, entry_id UNINDEXED, tokenize='unicode61')
                """)

                await self._copy_fts_rows_if_exists(
                    db,
                    source_table="memories_fts",
                    target_table="livingmemory_memories_fts",
                    columns=("doc_id", "content"),
                )
                await self._copy_fts_rows_if_exists(
                    db,
                    source_table="graph_entries_fts",
                    target_table="livingmemory_graph_entries_fts",
                    columns=("entry_id", "content"),
                )

                await self._backup_legacy_documents_fts_if_safe(db)

                await db.commit()
                logger.info("v5 -> v6 FTS 表前缀化完成")

        except Exception as e:
            logger.error(f"v5 -> v6 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v6_to_v7(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Add storage indexes and run lightweight FTS maintenance."""
        logger.info("执行迁移步骤: v6 -> v7 (storage indexes and FTS maintenance)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 10000")
                await db.execute("PRAGMA foreign_keys = ON")

                if await self._table_exists(db, "graph_edges"):
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_graph_edges_semantic
                        ON graph_edges(source_node_id, target_node_id, relation_type)
                        """
                    )
                if await self._table_exists(db, "graph_entries"):
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_graph_entries_scope_latest
                        ON graph_entries(session_id, persona_id, source_memory_id, id DESC)
                        """
                    )
                if await self._table_exists(db, "graph_entry_nodes"):
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_graph_entry_nodes_node ON graph_entry_nodes(node_id)"
                    )
                if await self._table_exists(db, "memory_atoms"):
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_atoms_persona ON memory_atoms(persona_id)"
                    )
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_atoms_scope_status
                        ON memory_atoms(status, session_id, persona_id)
                        """
                    )
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_atoms_status_expires
                        ON memory_atoms(status, expires_at)
                        """
                    )

                for table_name in (
                    "livingmemory_memories_fts",
                    "livingmemory_graph_entries_fts",
                    "memory_atoms_fts",
                ):
                    try:
                        await db.execute(
                            f"INSERT INTO {table_name}({table_name}) VALUES ('optimize')"
                        )
                    except Exception:
                        logger.debug(
                            f"跳过 FTS optimize: {table_name}",
                            exc_info=True,
                        )

                await db.commit()
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            logger.info("v6 -> v7 迁移完成")

        except Exception as e:
            logger.error(f"v6 -> v7 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v7_to_v8(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Add write-operation log and expression indexes for hot metadata fields."""
        logger.info("执行迁移步骤: v7 -> v8 (write ops and hot metadata indexes)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 10000")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_write_ops (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        op_type TEXT NOT NULL,
                        memory_id INTEGER,
                        status TEXT NOT NULL DEFAULT 'pending',
                        step TEXT NOT NULL DEFAULT 'started',
                        payload TEXT DEFAULT '{}',
                        error TEXT,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_write_ops_status
                    ON memory_write_ops(status, updated_at)
                    """
                )
                await db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_write_ops_memory
                    ON memory_write_ops(memory_id, op_type)
                    """
                )

                if await self._table_exists(db, "documents"):
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_doc_persona_metadata
                        ON documents(json_extract(metadata, '$.persona_id'))
                        """
                    )
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_doc_importance_metadata
                        ON documents(json_extract(metadata, '$.importance'))
                        """
                    )
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_doc_last_access_metadata
                        ON documents(json_extract(metadata, '$.last_access_time'))
                        """
                    )
                    await db.execute(
                        """
                        UPDATE documents
                        SET metadata = json_set(
                            COALESCE(NULLIF(TRIM(COALESCE(metadata, '')), ''), '{}'),
                            '$.access_count',
                            COALESCE(json_extract(metadata, '$.access_count'), 0)
                        )
                        WHERE json_valid(
                            COALESCE(NULLIF(TRIM(COALESCE(metadata, '')), ''), '{}')
                        )
                        """
                    )

                await db.commit()

            logger.info("v7 -> v8 迁移完成")

        except Exception as e:
            logger.error(f"v7 -> v8 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v8_to_v9(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Backfill stable timeline UIDs, registry rows, and source spans."""
        logger.info("执行迁移步骤: v8 -> v9 (stable timeline identity)")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await MemoryIdentityStore.create_tables(db)

            if not await self._table_exists(db, "documents"):
                await db.commit()
                logger.info("未找到 documents 表，仅创建阶段一身份表")
                return

            cursor = await db.execute("PRAGMA table_info(documents)")
            document_columns = {str(row[1]) for row in await cursor.fetchall()}
            if "doc_id" not in document_columns:
                await db.execute("ALTER TABLE documents ADD COLUMN doc_id TEXT")
                await db.execute(
                    "UPDATE documents SET doc_id = 'legacy-' || id WHERE doc_id IS NULL"
                )

            cursor = await db.execute(
                "SELECT id, doc_id, metadata FROM documents ORDER BY id ASC"
            )
            rows = await cursor.fetchall()
            total = len(rows)
            used_uids: set[str] = set()
            now = time.time()

            def optional_int(value: Any) -> int | None:
                try:
                    return int(value) if value is not None else None
                except (TypeError, ValueError):
                    return None

            def optional_float(value: Any) -> float | None:
                try:
                    return float(value) if value is not None else None
                except (TypeError, ValueError):
                    return None

            for index, row in enumerate(rows, 1):
                raw_metadata = row["metadata"]
                try:
                    metadata = (
                        json.loads(raw_metadata)
                        if isinstance(raw_metadata, str) and raw_metadata.strip()
                        else raw_metadata
                    )
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}

                physical_id = int(row["id"])
                current_uid = str(metadata.get("memory_uid") or "").strip()
                if not current_uid or current_uid in used_uids:
                    stable_key = (
                        f"livingmemory:v9:{physical_id}:{row['doc_id'] or ''}"
                    )
                    current_uid = str(uuid.uuid5(uuid.NAMESPACE_URL, stable_key))
                used_uids.add(current_uid)

                try:
                    revision = max(1, int(metadata.get("revision", 1)))
                except (TypeError, ValueError):
                    revision = 1
                memory_layer = str(metadata.get("memory_layer") or "timeline")
                session_id = metadata.get("session_id")
                persona_id = metadata.get("persona_id")
                space = resolve_memory_space(session_id, persona_id)
                created_at = optional_float(metadata.get("create_time")) or now
                updated_at = optional_float(metadata.get("updated_at")) or created_at

                metadata.update(
                    {
                        "memory_uid": current_uid,
                        "revision": revision,
                        "memory_layer": memory_layer,
                        "memory_space_id": space.memory_space_id,
                        "memory_space_version": 1,
                    }
                )
                await db.execute(
                    "UPDATE documents SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), physical_id),
                )
                await db.execute(
                    """
                    INSERT INTO memory_registry (
                        memory_uid, document_id, memory_layer, memory_space_id,
                        revision, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                    ON CONFLICT(memory_uid) DO UPDATE SET
                        document_id = excluded.document_id,
                        memory_layer = excluded.memory_layer,
                        memory_space_id = excluded.memory_space_id,
                        revision = excluded.revision,
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    """,
                    (
                        current_uid,
                        physical_id,
                        memory_layer,
                        space.memory_space_id,
                        revision,
                        created_at,
                        updated_at,
                    ),
                )

                source = metadata.get("source_window")
                source = source if isinstance(source, dict) else {}
                first_message_id = optional_int(source.get("first_message_id"))
                last_message_id = optional_int(source.get("last_message_id"))
                if first_message_id is not None and last_message_id is not None:
                    traceability = "full"
                elif source:
                    traceability = "partial"
                else:
                    traceability = "none"
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
                        current_uid,
                        source.get("session_id") or session_id,
                        first_message_id,
                        last_message_id,
                        optional_int(source.get("start_index")),
                        optional_int(source.get("end_index")),
                        optional_float(source.get("started_at")) or created_at,
                        optional_float(source.get("ended_at")) or created_at,
                        traceability,
                        json.dumps(source, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

                if progress_callback and (index == total or index % 100 == 0):
                    progress_callback("补齐稳定记忆身份", index, total)

            await db.commit()
        logger.info(f"v8 -> v9 迁移完成，共处理 {total} 条 Timeline 记忆")

    async def _migrate_v9_to_v9_1(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Create the inactive Topic-memory storage foundation."""
        logger.info("执行迁移步骤: v9 -> v9.1 (topic-memory storage foundation)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await MemoryIdentityStore.create_tables(db)
            await TopicMemoryStore.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("创建 Topic 记忆存储结构", 1, 1)
        logger.info("v9 -> v9.1 迁移完成，未生成或修改任何 Topic 记忆")

    async def _migrate_v9_1_to_v9_2(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Add resumable scan items and deterministic candidate previews."""
        logger.info("执行迁移步骤: v9.1 -> v9.2 (Topic candidate discovery)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await MemoryIdentityStore.create_tables(db)
            await TopicMemoryStore.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("创建 Topic 候选扫描结构", 1, 1)
        logger.info("v9.1 -> v9.2 迁移完成，未执行扫描或生成正式 Topic")

    async def _migrate_v9_2_to_v9_3(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Add source-grounded fragment drafts and build audit tables."""
        logger.info("执行迁移步骤: v9.2 -> v9.3 (automatic Topic construction)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            if await self._table_exists(db, "topic_maintenance_runs"):
                cursor = await db.execute("PRAGMA table_info(topic_maintenance_runs)")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                additions = {
                    "stage": "TEXT NOT NULL DEFAULT 'candidate_scan'",
                    "current_group_index": "INTEGER NOT NULL DEFAULT 0",
                    "total_groups": "INTEGER NOT NULL DEFAULT 0",
                }
                for name, declaration in additions.items():
                    if name not in columns:
                        await db.execute(
                            f"ALTER TABLE topic_maintenance_runs "
                            f"ADD COLUMN {name} {declaration}"
                        )
            await MemoryIdentityStore.create_tables(db)
            await TopicMemoryStore.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("创建自动 Topic 构建与审计结构", 1, 1)
        logger.info("v9.2 -> v9.3 迁移完成，未自动执行模型调用")

    async def _migrate_v9_3_to_v9_4(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Add non-destructive relations between focused Topic memories."""
        logger.info("执行迁移步骤: v9.3 -> v9.4 (related Topic graph)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await TopicMemoryStore.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("创建 Topic 相关子话题关系结构", 1, 1)
        logger.info("v9.3 -> v9.4 迁移完成，未自动修改已有 Topic")

    async def _migrate_v9_4_to_v9_5(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Add sparse runtime Topic settings without rewriting Topic data."""
        logger.info("执行迁移步骤: v9.4 -> v9.5 (Topic runtime settings)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await TopicMemoryStore.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("创建 Topic 运行参数覆盖结构", 1, 1)
        logger.info("v9.4 -> v9.5 迁移完成，未自动修改已有 Topic")

    async def _migrate_v9_5_to_v9_6(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Create formal fragment links and backfill existing v9.5 provenance."""
        logger.info("执行迁移步骤: v9.5 -> v9.6 (formal Topic fragments)")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await TopicMemoryStore.create_tables(db)
            rows = await (
                await db.execute(
                    "SELECT topic_uid, revision, metadata FROM topic_memories"
                )
            ).fetchall()
            now = time.time()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                fragment_uids = [
                    str(value).strip()
                    for value in metadata.get("fragment_uids", [])
                    if str(value).strip()
                ]
                if not fragment_uids:
                    continue
                placeholders = ",".join("?" * len(fragment_uids))
                await db.execute(
                    f"""
                    INSERT OR IGNORE INTO topic_fragments (
                        fragment_uid, run_uid, candidate_group_uid,
                        memory_space_id, label, summary, timeline_uids,
                        source_revisions, facts, keywords, time_cluster_keys,
                        importance, confidence, embedding, started_at,
                        ended_at, status, prompt_hash, input_hash, provider_id,
                        model_id, created_at, updated_at, metadata
                    )
                    SELECT fragment_uid, run_uid, candidate_group_uid,
                           memory_space_id, label, summary, timeline_uids,
                           source_revisions, facts, keywords, time_cluster_keys,
                           importance, confidence, embedding, started_at,
                           ended_at, status, prompt_hash, input_hash, provider_id,
                           model_id, created_at, updated_at, metadata
                    FROM topic_fragment_drafts
                    WHERE fragment_uid IN ({placeholders})
                    """,
                    fragment_uids,
                )
                existing = await (
                    await db.execute(
                        f"""
                        SELECT fragment_uid FROM topic_fragments
                        WHERE fragment_uid IN ({placeholders})
                        """,
                        fragment_uids,
                    )
                ).fetchall()
                existing_uids = sorted(
                    {str(item["fragment_uid"]) for item in existing}
                )
                weight = 1.0 / max(1, len(existing_uids))
                for fragment_uid in existing_uids:
                    await db.execute(
                        """
                        INSERT INTO topic_fragment_links (
                            topic_uid, fragment_uid, topic_revision,
                            contribution_weight, status, created_at, updated_at,
                            metadata
                        ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                        ON CONFLICT(topic_uid, fragment_uid) DO UPDATE SET
                            topic_revision = excluded.topic_revision,
                            contribution_weight = excluded.contribution_weight,
                            status = 'active',
                            updated_at = excluded.updated_at,
                            metadata = excluded.metadata
                        """,
                        (
                            str(row["topic_uid"]),
                            fragment_uid,
                            int(row["revision"]),
                            weight,
                            now,
                            now,
                            json.dumps(
                                {"backfilled_from": "topic_metadata_v9_5"},
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    await db.execute(
                        """
                        UPDATE topic_fragments
                        SET status = 'active', updated_at = ?
                        WHERE fragment_uid = ?
                        """,
                        (now, fragment_uid),
                    )
            await db.commit()
        if progress_callback:
            progress_callback("创建正式 Topic 片段关系并回填来源", 1, 1)
        logger.info("v9.5 -> v9.6 迁移完成；旧片段需重建后才参与召回")

    async def _migrate_v9_6_to_v9_7(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Enable actor-anchored fragments without rewriting existing memories."""
        logger.info("执行迁移步骤: v9.6 -> v9.7 (stable actor bindings)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await TopicMemoryStore.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("启用稳定人物身份与按需证据片段", 1, 1)
        logger.info("v9.6 -> v9.7 迁移完成，未自动改写现有 Timeline 或 Topic")

    async def _migrate_v9_7_to_v9_8(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Create authoritative Topic and fact actor-relation indexes."""
        logger.info("执行迁移步骤: v9.7 -> v9.8 (fact-level actor relations)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await TopicMemoryStore.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("创建 Topic 人物与事实人物关系索引", 1, 1)
        logger.info("v9.7 -> v9.8 迁移完成；旧 Topic 在维护或重建后回填人物关系")

    async def _migrate_v9_8_to_v9_9(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Create sparse WebUI-managed Timeline runtime settings."""
        logger.info("执行迁移步骤: v9.8 -> v9.9 (Timeline runtime settings)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await TopicMemoryStore.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("创建 Timeline 运行时参数存储", 1, 1)
        logger.info("v9.8 -> v9.9 迁移完成；旧插件配置将在启动时按需导入")

    async def _migrate_v9_9_to_v9_10(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Persist model and input-format signatures for Topic vectors."""
        logger.info("执行迁移步骤: v9.9 -> v9.10 (Topic embedding signatures)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            for table in ("topic_memories", "topic_fragment_drafts", "topic_fragments"):
                if not await self._table_exists(db, table):
                    continue
                if not await self._column_exists(db, table, "embedding_signature"):
                    await db.execute(
                        f"ALTER TABLE {table} ADD COLUMN "
                        "embedding_signature TEXT NOT NULL DEFAULT '{}'"
                    )
            await db.commit()
        if progress_callback:
            progress_callback("创建 Topic 向量签名字段", 1, 1)
        logger.info("v9.9 -> v9.10 迁移完成；旧向量需在 WebUI 中重新向量化")

    async def _migrate_v9_10_to_v9_11(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Add stable fragment identities and bounded-maintenance review state."""
        logger.info("执行迁移步骤: v9.10 -> v9.11 (bounded Topic maintenance)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            for table in ("topic_fragment_drafts", "topic_fragments"):
                if not await self._column_exists(db, table, "logical_fragment_uid"):
                    await db.execute(
                        f"ALTER TABLE {table} ADD COLUMN "
                        "logical_fragment_uid TEXT NOT NULL DEFAULT ''"
                    )
                if not await self._column_exists(db, table, "fragment_revision"):
                    await db.execute(
                        f"ALTER TABLE {table} ADD COLUMN "
                        "fragment_revision INTEGER NOT NULL DEFAULT 1"
                    )
                await db.execute(
                    f"UPDATE {table} SET logical_fragment_uid = fragment_uid "
                    "WHERE logical_fragment_uid = ''"
                )
            await TopicMemoryStore.create_tables(db)
            await db.execute(
                "UPDATE topic_relations SET relation_type = 'related' "
                "WHERE relation_type = 'related_subtopic'"
            )
            await db.commit()
        if progress_callback:
            progress_callback("创建稳定片段身份与维护审查队列", 1, 1)
        logger.info("v9.10 -> v9.11 迁移完成")

    async def _migrate_v9_11_to_v9_12(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Record the unified runtime-settings and summary-state data contract."""
        logger.info("执行迁移步骤: v9.11 -> v9.12 (unified settings and idle summary)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await TopicMemoryStore.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("初始化统一运行时设置", 1, 1)
        logger.info("v9.11 -> v9.12 迁移完成")

    async def _migrate_v9_12_to_v9_13(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Add auditable user decisions to the Topic maintenance queue."""
        logger.info("执行迁移步骤: v9.12 -> v9.13 (Topic review governance)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await TopicMemoryStore.create_tables(db)
            columns = {
                str(row[1])
                for row in await (await db.execute(
                    "PRAGMA table_info(topic_maintenance_queue)"
                )).fetchall()
            }
            additions = {
                "resolved_at": "REAL",
                "resolution_action": "TEXT NOT NULL DEFAULT ''",
                "resolution_payload": "TEXT NOT NULL DEFAULT '{}'",
                "expected_topic_revisions": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    await db.execute(
                        f"ALTER TABLE topic_maintenance_queue ADD COLUMN {name} {definition}"
                    )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_topic_actor_display_name
                ON topic_actor_links(display_name_snapshot, topic_uid)
                """
            )
            await db.commit()
        if progress_callback:
            progress_callback("创建 Topic 审查决策与人物治理字段", 1, 1)
        logger.info("v9.12 -> v9.13 迁移完成")

    async def _migrate_v9_13_to_v9_14(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Add an idempotent journal for maintenance spanning two SQLite files."""
        logger.info("执行迁移步骤: v9.13 -> v9.14 (session maintenance journal)")
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
                CREATE INDEX IF NOT EXISTS idx_session_maintenance_task_status
                ON session_maintenance_tasks(status, updated_at)
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
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_maintenance_event_task
                ON session_maintenance_events(task_uid, id)
                """
            )
            await db.commit()
        if progress_callback:
            progress_callback("创建会话审计与可恢复维护任务", 1, 1)
        logger.info("v9.13 -> v9.14 迁移完成")

    async def _migrate_v9_14_to_v9_15(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Add bounded production/test recall audit storage."""
        from .recall_trace_store import RecallTraceStore

        logger.info("执行迁移步骤: v9.14 -> v9.15 (recall trace history)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await RecallTraceStore.create_schema(db)
            await db.commit()
        if progress_callback:
            progress_callback("创建召回记录与测试历史", 1, 1)
        logger.info("v9.14 -> v9.15 迁移完成")

    async def _migrate_v9_15_to_v9_16(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Clear obsolete profile-triggered rebuild flags without touching memory."""
        logger.info("执行迁移步骤: v9.15 -> v9.16 (supplemental identity hints)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            if await self._table_exists(db, "topic_memories"):
                rows = await (
                    await db.execute("SELECT topic_uid, metadata FROM topic_memories")
                ).fetchall()
                for topic_uid, raw_metadata in rows:
                    try:
                        metadata = json.loads(raw_metadata or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(metadata, dict):
                        continue
                    changed = False
                    for key in (
                        "identity_sync_pending",
                        "identity_sync_requested_at",
                    ):
                        if key in metadata:
                            metadata.pop(key, None)
                            changed = True
                    if changed:
                        await db.execute(
                            "UPDATE topic_memories SET metadata = ? WHERE topic_uid = ?",
                            (
                                json.dumps(metadata, ensure_ascii=False),
                                str(topic_uid),
                            ),
                        )
            await db.commit()
        if progress_callback:
            progress_callback("清理旧人物资料同步标记", 1, 1)
        logger.info("v9.15 -> v9.16 迁移完成")

    async def _migrate_v9_16_to_v9_17(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Add stateless Topic importance projection and Timeline anchors."""
        logger.info("执行迁移步骤: v9.16 -> v9.17 (unified importance projection)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            if await self._table_exists(db, "topic_memories"):
                columns = {
                    str(row[1])
                    for row in await (
                        await db.execute("PRAGMA table_info(topic_memories)")
                    ).fetchall()
                }
                additions = {
                    "semantic_importance": "REAL NOT NULL DEFAULT 0.5",
                    "source_base_component": "REAL NOT NULL DEFAULT 0.5",
                    "evidence_strength": "REAL NOT NULL DEFAULT 0.5",
                    "importance_policy_version": "INTEGER NOT NULL DEFAULT 1",
                    "source_importance_hash": "TEXT NOT NULL DEFAULT ''",
                }
                for name, definition in additions.items():
                    if name not in columns:
                        await db.execute(
                            f"ALTER TABLE topic_memories ADD COLUMN {name} {definition}"
                        )
                await db.execute(
                    """
                    UPDATE topic_memories
                    SET semantic_importance = base_importance,
                        source_base_component = base_importance,
                        evidence_strength = confidence,
                        importance_policy_version = 2
                    WHERE importance_policy_version < 2
                    """
                )
            if await self._table_exists(db, "documents"):
                timeline_filter = ""
                if await self._table_exists(db, "memory_registry"):
                    timeline_filter = (
                        " WHERE id IN (SELECT document_id FROM memory_registry "
                        "WHERE memory_layer = 'timeline')"
                    )
                rows = await (
                    await db.execute(
                        "SELECT id, metadata FROM documents" + timeline_filter
                    )
                ).fetchall()
                updates: list[tuple[str, int]] = []
                for document_id, raw_metadata in rows:
                    try:
                        metadata = json.loads(raw_metadata or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(metadata, dict):
                        continue
                    try:
                        effective = max(
                            0.0,
                            min(1.0, float(metadata.get("importance", 0.5))),
                        )
                    except (TypeError, ValueError):
                        effective = 0.5
                    changed = False
                    defaults = {
                        "base_importance": effective,
                        "importance_revision": 1,
                        "importance_reason": "migrated",
                        "importance_policy_version": 2,
                    }
                    for key, value in defaults.items():
                        if key not in metadata:
                            metadata[key] = value
                            changed = True
                    if changed:
                        updates.append(
                            (
                                json.dumps(metadata, ensure_ascii=False),
                                int(document_id),
                            )
                        )
                if updates:
                    await db.executemany(
                        "UPDATE documents SET metadata = ? WHERE id = ?",
                        updates,
                    )
            await db.commit()
        if progress_callback:
            progress_callback("建立统一重要性投影与 Timeline 基础值", 1, 1)
        logger.info("v9.16 -> v9.17 迁移完成")

    async def _migrate_v9_17_to_v9_18(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Add empty affect contracts without guessing emotion for legacy rows."""
        logger.info("执行迁移步骤: v9.17 -> v9.18 (source-grounded affect memory)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            additions = {
                "topic_memories": {
                    "affect_profile": "TEXT NOT NULL DEFAULT '[]'",
                    "affective_salience": "REAL NOT NULL DEFAULT 0.0",
                    "affect_signature": "TEXT NOT NULL DEFAULT '{}'",
                },
                "topic_fragment_drafts": {
                    "affect_events": "TEXT NOT NULL DEFAULT '[]'",
                    "affect_signature": "TEXT NOT NULL DEFAULT '{}'",
                },
                "topic_fragments": {
                    "affect_events": "TEXT NOT NULL DEFAULT '[]'",
                    "affect_signature": "TEXT NOT NULL DEFAULT '{}'",
                },
            }
            for table_name, columns in additions.items():
                if not await self._table_exists(db, table_name):
                    continue
                existing = {
                    str(row[1])
                    for row in await (
                        await db.execute(f"PRAGMA table_info({table_name})")
                    ).fetchall()
                }
                for column_name, definition in columns.items():
                    if column_name not in existing:
                        await db.execute(
                            f"ALTER TABLE {table_name} ADD COLUMN "
                            f"{column_name} {definition}"
                        )
            await db.commit()
        if progress_callback:
            progress_callback("建立可溯源情感事件与 Topic 情感画像字段", 1, 1)
        logger.info("v9.17 -> v9.18 迁移完成")

    async def _migrate_v9_18_to_v9_19(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Add persistent source-verified Timeline reconstruction journals."""
        logger.info("执行迁移步骤: v9.18 -> v9.19 (Timeline reconstruction)")
        from ..core.managers.timeline_rebuild_manager import TimelineRebuildManager

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await TimelineRebuildManager.create_tables(db)
            await db.commit()
        if progress_callback:
            progress_callback("建立可恢复的 Timeline 重构任务", 1, 1)
        logger.info("v9.18 -> v9.19 迁移完成")

    async def _migrate_v9_19_to_v9_20(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Repair ambiguous fragment identities and detached affect actors."""
        logger.info(
            "执行迁移步骤: v9.19 -> v9.20 "
            "(fragment identity and affect provenance repair)"
        )
        repaired_identities = 0
        repaired_affect_events = 0
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            if not await self._table_exists(db, "topic_fragments"):
                if progress_callback:
                    progress_callback("修复 Topic 片段身份与情绪溯源", 1, 1)
                return

            formal_rows = list(
                await (
                    await db.execute(
                        """
                        SELECT fragment_uid, memory_space_id, label, summary,
                               timeline_uids, facts, logical_fragment_uid,
                               fragment_revision, metadata, affect_events
                        FROM topic_fragments
                        WHERE status = 'active'
                        ORDER BY memory_space_id, logical_fragment_uid, fragment_uid
                        """
                    )
                ).fetchall()
            )
            draft_table_exists = await self._table_exists(
                db, "topic_fragment_drafts"
            )
            rows_by_table = {"topic_fragments": formal_rows}
            if draft_table_exists:
                rows_by_table["topic_fragment_drafts"] = list(
                    await (
                        await db.execute(
                            """
                            SELECT fragment_uid, memory_space_id, label, summary,
                                   timeline_uids, facts, logical_fragment_uid,
                                   fragment_revision, metadata, affect_events
                            FROM topic_fragment_drafts
                            ORDER BY memory_space_id, logical_fragment_uid,
                                     fragment_uid
                            """
                        )
                    ).fetchall()
                )
            collision_groups: dict[
                tuple[str, str, str], list[aiosqlite.Row]
            ] = {}
            for table_name, table_rows in rows_by_table.items():
                for row in table_rows:
                    logical_uid = str(row["logical_fragment_uid"] or "").strip()
                    if logical_uid:
                        collision_groups.setdefault(
                            (
                                table_name,
                                str(row["memory_space_id"]),
                                logical_uid,
                            ),
                            [],
                        ).append(row)

            for (
                table_name,
                memory_space_id,
                base_uid,
            ), group in collision_groups.items():
                if len(group) < 2:
                    continue
                seen_discriminators: set[str] = set()
                for row in group:
                    facts = self._migration_json_object_list(row["facts"])
                    timeline_uids = self._migration_json_string_list(
                        row["timeline_uids"]
                    )
                    discriminator = fragment_semantic_discriminator(
                        label=str(row["label"] or ""),
                        summary=str(row["summary"] or ""),
                        facts=facts,
                    )
                    if discriminator in seen_discriminators:
                        # Legacy data may contain exact duplicate snapshots. Keep
                        # both rows addressable; a later maintenance run can decide
                        # whether one should be archived.
                        discriminator = fragment_semantic_discriminator(
                            label=str(row["label"] or ""),
                            summary=(
                                str(row["summary"] or "")
                                + "\nlegacy-fragment:"
                                + str(row["fragment_uid"])
                            ),
                            facts=facts,
                        )
                    seen_discriminators.add(discriminator)
                    new_uid = logical_fragment_uid(
                        memory_space_id=memory_space_id,
                        timeline_uids=timeline_uids,
                        facts=facts,
                        semantic_discriminator=discriminator,
                    )
                    metadata = self._migration_json_object(row["metadata"])
                    marker = {
                        "base_logical_fragment_uid": base_uid,
                        "semantic_discriminator": discriminator,
                        "reason": "v9.20_existing_source_split_repair",
                    }
                    if (
                        str(row["logical_fragment_uid"] or "") == new_uid
                        and metadata.get("logical_identity_disambiguation") == marker
                    ):
                        continue
                    metadata["logical_identity_disambiguation"] = marker
                    await db.execute(
                        f"""
                        UPDATE {table_name}
                        SET logical_fragment_uid = ?, fragment_revision = 1,
                            metadata = ?, updated_at = ?
                        WHERE fragment_uid = ?
                        """,
                        (
                            new_uid,
                            json.dumps(metadata, ensure_ascii=False),
                            time.time(),
                            str(row["fragment_uid"]),
                        ),
                    )
                    repaired_identities += 1

            event_actor_repairs: dict[str, str] = {}
            for table_name, table_rows in rows_by_table.items():
                for row in table_rows:
                    facts = self._migration_json_object_list(row["facts"])
                    metadata = self._migration_json_object(row["metadata"])
                    actors = [
                        *self._migration_json_actor_list(
                            metadata.get("participant_refs")
                        ),
                        *self._migration_json_actor_list(
                            metadata.get("mentioned_actor_refs")
                        ),
                        *[
                            actor
                            for fact in facts
                            for actor in self._migration_json_actor_list(
                                fact.get("actor_refs")
                            )
                        ],
                    ]
                    actor_ids_by_name: dict[str, set[str]] = {}
                    for actor in actors:
                        name = self._migration_identity_name(
                            actor.get("display_name_snapshot")
                        )
                        actor_id = str(actor.get("actor_id") or "").strip()
                        if name and actor_id:
                            actor_ids_by_name.setdefault(name, set()).add(actor_id)
                    unique_actor_ids = {
                        name: next(iter(actor_ids))
                        for name, actor_ids in actor_ids_by_name.items()
                        if len(actor_ids) == 1
                    }
                    events = self._migration_json_object_list(row["affect_events"])
                    changed = False
                    for event in events:
                        name = self._migration_identity_name(
                            event.get("display_name_snapshot")
                        )
                        target_actor_id = unique_actor_ids.get(name)
                        if not target_actor_id:
                            continue
                        current_actor_id = str(event.get("actor_id") or "").strip()
                        if current_actor_id == target_actor_id:
                            continue
                        if not self._migration_is_detached_affect_actor(
                            current_actor_id
                        ):
                            continue
                        event["actor_id"] = target_actor_id
                        event_uid = str(event.get("event_uid") or "").strip()
                        if event_uid:
                            event_actor_repairs[event_uid] = target_actor_id
                        repaired_affect_events += 1
                        changed = True
                    if changed:
                        await db.execute(
                            f"""
                            UPDATE {table_name}
                            SET affect_events = ?, updated_at = ?
                            WHERE fragment_uid = ?
                            """,
                            (
                                json.dumps(events, ensure_ascii=False),
                                time.time(),
                                str(row["fragment_uid"]),
                            ),
                        )

            if (
                event_actor_repairs
                and await self._table_exists(db, "topic_memories")
                and await self._table_exists(db, "topic_actor_links")
            ):
                topic_rows = await (
                    await db.execute(
                        """
                        SELECT topic_uid, affect_profile
                        FROM topic_memories
                        WHERE status = 'active' AND affect_profile != '[]'
                        """
                    )
                ).fetchall()
                for topic_row in topic_rows:
                    topic_uid = str(topic_row["topic_uid"])
                    actor_rows = await (
                        await db.execute(
                            """
                            SELECT actor_id FROM topic_actor_links
                            WHERE topic_uid = ?
                            """,
                            (topic_uid,),
                        )
                    ).fetchall()
                    valid_actor_ids = {
                        str(actor_row["actor_id"]) for actor_row in actor_rows
                    }
                    profile = self._migration_json_object_list(
                        topic_row["affect_profile"]
                    )
                    changed = False
                    for event in profile:
                        event_uid = str(event.get("event_uid") or "").strip()
                        target_actor_id = event_actor_repairs.get(event_uid)
                        if (
                            target_actor_id
                            and target_actor_id in valid_actor_ids
                            and str(event.get("actor_id") or "") != target_actor_id
                        ):
                            event["actor_id"] = target_actor_id
                            changed = True
                    if changed:
                        await db.execute(
                            """
                            UPDATE topic_memories
                            SET affect_profile = ?, updated_at = ?
                            WHERE topic_uid = ?
                            """,
                            (
                                json.dumps(profile, ensure_ascii=False),
                                time.time(),
                                topic_uid,
                            ),
                        )
            await db.commit()
        if progress_callback:
            progress_callback("修复 Topic 片段身份与情绪溯源", 1, 1)
        logger.info(
            "v9.19 -> v9.20 迁移完成 "
            f"(fragment_identities={repaired_identities}, "
            f"affect_events={repaired_affect_events})"
        )

    async def _migrate_v9_20_to_v9_21(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Normalize graph-edge evidence without mutating existing orphans."""
        logger.info(
            "执行迁移步骤: v9.20 -> v9.21 "
            "(multi-source graph edges and manual database repair)"
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            if await self._table_exists(db, "graph_edges"):
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_edge_sources (
                        edge_id INTEGER NOT NULL,
                        source_memory_id INTEGER NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0,
                        confidence REAL NOT NULL DEFAULT 0.8,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(edge_id, source_memory_id),
                        FOREIGN KEY(edge_id) REFERENCES graph_edges(id) ON DELETE CASCADE
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_graph_edge_sources_memory
                    ON graph_edge_sources(source_memory_id, edge_id)
                    """
                )
                now = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    """
                    INSERT OR IGNORE INTO graph_edge_sources(
                        edge_id, source_memory_id, weight, confidence, status,
                        metadata, created_at, updated_at
                    )
                    SELECT id, source_memory_id, weight, confidence, status,
                           json_object('backfill', 'edge_owner'), ?, ?
                    FROM graph_edges
                    """,
                    (now, now),
                )
                if await self._table_exists(db, "graph_entries"):
                    # Broken edge references remain for explicit user repair.
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO graph_edge_sources(
                            edge_id, source_memory_id, weight, confidence, status,
                            metadata, created_at, updated_at
                        )
                        SELECT DISTINCT entry.edge_id, entry.source_memory_id,
                               1.0, edge.confidence, edge.status,
                               json_object('backfill', 'graph_entry'), ?, ?
                        FROM graph_entries AS entry
                        JOIN graph_edges AS edge ON edge.id = entry.edge_id
                        WHERE entry.edge_id IS NOT NULL
                        """,
                        (now, now),
                    )
            await db.commit()
        if progress_callback:
            progress_callback("建立图谱边的多来源索引", 1, 1)
        logger.info("v9.20 -> v9.21 迁移完成（孤立引用未自动修复）")

    async def _migrate_v9_21_to_v9_22(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        logger.info("执行迁移步骤: v9.21 -> v9.22")
        async with aiosqlite.connect(self.db_path) as db:
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
            await db.commit()
        if progress_callback:
            progress_callback("建立 Timeline 暂存修改队列", 1, 1)
        logger.info("v9.21 -> v9.22 迁移完成")

    async def _migrate_v9_to_v10(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
        *,
        from_version: str = "9",
    ) -> None:
        """Apply the unpublished v9.x schema work as one public v9 -> v10 step."""
        start_version = self.normalize_version(from_version)
        start_key = self.version_key(start_version)
        if start_key < self.version_key("9") or start_key >= self.version_key("10"):
            raise ValueError(f"v9 -> v10 迁移不支持起始版本: {from_version}")

        logger.info(f"执行迁移步骤: v{start_version} -> v10")
        internal_stages = (
            ("9", self._migrate_v9_to_v9_1),
            ("9.1", self._migrate_v9_1_to_v9_2),
            ("9.2", self._migrate_v9_2_to_v9_3),
            ("9.3", self._migrate_v9_3_to_v9_4),
            ("9.4", self._migrate_v9_4_to_v9_5),
            ("9.5", self._migrate_v9_5_to_v9_6),
            ("9.6", self._migrate_v9_6_to_v9_7),
            ("9.7", self._migrate_v9_7_to_v9_8),
            ("9.8", self._migrate_v9_8_to_v9_9),
            ("9.9", self._migrate_v9_9_to_v9_10),
            ("9.10", self._migrate_v9_10_to_v9_11),
            ("9.11", self._migrate_v9_11_to_v9_12),
            ("9.12", self._migrate_v9_12_to_v9_13),
            ("9.13", self._migrate_v9_13_to_v9_14),
            ("9.14", self._migrate_v9_14_to_v9_15),
            ("9.15", self._migrate_v9_15_to_v9_16),
            ("9.16", self._migrate_v9_16_to_v9_17),
            ("9.17", self._migrate_v9_17_to_v9_18),
            ("9.18", self._migrate_v9_18_to_v9_19),
            ("9.19", self._migrate_v9_19_to_v9_20),
            ("9.20", self._migrate_v9_20_to_v9_21),
            ("9.21", self._migrate_v9_21_to_v9_22),
        )
        for stage_version, stage in internal_stages:
            if start_key <= self.version_key(stage_version):
                await stage(progress_callback)

        if progress_callback:
            progress_callback("完成 v10 数据库版本收束", 1, 1)
        logger.info(f"v{start_version} -> v10 迁移完成")

    @staticmethod
    def _migration_json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    @staticmethod
    def _migration_json_object_list(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            parsed = value
        else:
            try:
                parsed = json.loads(value or "[]")
            except (TypeError, ValueError):
                return []
        return [dict(item) for item in parsed if isinstance(item, dict)]

    @staticmethod
    def _migration_json_string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            parsed = value
        else:
            try:
                parsed = json.loads(value or "[]")
            except (TypeError, ValueError):
                return []
        return [str(item) for item in parsed if str(item)]

    @staticmethod
    def _migration_json_actor_list(value: Any) -> list[dict[str, Any]]:
        return DBMigration._migration_json_object_list(value)

    @staticmethod
    def _migration_identity_name(value: Any) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    @staticmethod
    def _migration_is_detached_affect_actor(actor_id: str) -> bool:
        value = str(actor_id or "").strip()
        return value == "unresolved" or (
            value.startswith("unresolved:") and ":affect:" in value
        )

    async def _table_exists(self, db: aiosqlite.Connection, table_name: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return await cursor.fetchone() is not None

    async def _column_exists(
        self,
        db: aiosqlite.Connection,
        table_name: str,
        column_name: str,
    ) -> bool:
        if not await self._table_exists(db, table_name):
            return False
        cursor = await db.execute(f"PRAGMA table_info({table_name})")
        return any(str(row[1]) == column_name for row in await cursor.fetchall())

    async def _copy_fts_rows_if_exists(
        self,
        db: aiosqlite.Connection,
        source_table: str,
        target_table: str,
        columns: tuple[str, str],
    ):
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (source_table,),
        )
        if not await cursor.fetchone():
            return

        first_column, second_column = columns
        await db.execute(f"DELETE FROM {target_table}")
        await db.execute(
            f"""
            INSERT INTO {target_table}({first_column}, {second_column})
            SELECT {first_column}, {second_column} FROM {source_table}
            """
        )
        await db.execute(f"DROP TABLE IF EXISTS {source_table}")
        logger.info(f"已迁移并删除旧 FTS 表: {source_table} -> {target_table}")

    async def _backup_legacy_documents_fts_if_safe(self, db: aiosqlite.Connection):
        cursor = await db.execute("""
            SELECT sql FROM sqlite_master
            WHERE type='table' AND name='documents_fts'
        """)
        row = await cursor.fetchone()
        if not row:
            logger.info("未发现 documents_fts 表，跳过旧表备份")
            return

        if not await self._is_legacy_livingmemory_documents_fts(db, row[0] or ""):
            logger.warning(
                "documents_fts 不完全匹配旧 LivingMemory FTS 结构，保留不处理"
            )
            return

        cursor = await db.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='livingmemory_legacy_documents_fts_backup'
        """)
        if await cursor.fetchone():
            logger.warning(
                "旧表备份 livingmemory_legacy_documents_fts_backup 已存在，保留 documents_fts 不处理"
            )
            return

        await db.execute(
            "ALTER TABLE documents_fts RENAME TO livingmemory_legacy_documents_fts_backup"
        )
        logger.warning(
            "已将旧 LivingMemory documents_fts 重命名为 livingmemory_legacy_documents_fts_backup"
        )

    async def _is_legacy_livingmemory_documents_fts(
        self,
        db: aiosqlite.Connection,
        create_sql: str,
    ) -> bool:
        normalized_sql = " ".join(create_sql.lower().replace("\n", " ").split())
        expected_sql = "create virtual table documents_fts using fts5(content, doc_id, tokenize='unicode61')"
        if normalized_sql != expected_sql:
            return False

        cursor = await db.execute("PRAGMA table_xinfo(documents_fts)")
        rows = await cursor.fetchall()
        visible_columns = [row[1] for row in rows if int(row[6]) == 0]
        return visible_columns == ["content", "doc_id"]

    async def get_migration_info(self) -> dict[str, Any]:
        """
        获取迁移信息

        Returns:
            Dict: 迁移信息
        """
        try:
            current_version = await self.get_db_version()
            needs_migration = await self.needs_migration()

            # 获取迁移历史
            migration_history = []
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    cursor = await db.execute("""
                        SELECT version, description, migrated_at, migration_duration_seconds
                        FROM db_version
                        ORDER BY id DESC
                        LIMIT 10
                    """)
                    rows = await cursor.fetchall()

                    for row in rows:
                        migration_history.append(
                            {
                                "version": row[0],
                                "description": row[1],
                                "migrated_at": row[2],
                                "duration": row[3],
                            }
                        )
            except Exception as e:
                logger.error(f"获取迁移历史失败: {e}", exc_info=True)

            return {
                "current_version": current_version,
                "latest_version": self.CURRENT_VERSION,
                "needs_migration": needs_migration,
                "version_history": self.VERSION_HISTORY,
                "migration_history": migration_history,
                "db_path": self.db_path,
            }

        except Exception as e:
            logger.error(f"获取迁移信息失败: {e}", exc_info=True)
            return {"error": str(e)}

    async def create_backup(self) -> str | None:
        """
        创建数据库备份

        Returns:
            Optional[str]: 备份文件路径，失败返回None
        """
        try:
            db_path = Path(self.db_path)
            backup_dir = db_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = (
                backup_dir / f"{db_path.stem}_backup_{timestamp}{db_path.suffix}"
            )

            logger.info(f"正在创建数据库备份: {backup_path}")

            # 使用SQLite的备份API
            async with aiosqlite.connect(self.db_path) as source:
                async with aiosqlite.connect(str(backup_path)) as dest:
                    await source.backup(dest)

            logger.info(f"数据库备份成功: {backup_path}")
            return str(backup_path)

        except Exception as e:
            logger.error(f"数据库备份失败: {e}", exc_info=True)
            return None
