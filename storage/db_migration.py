"""
数据库迁移管理器 - 处理数据库版本升级和数据迁移
"""

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from astrbot.api import logger

from ..core.models.memory_identity import resolve_memory_space
from .memory_identity_store import MemoryIdentityStore
from .topic_memory_store import TopicMemoryStore


class DBMigration:
    """数据库迁移管理器"""

    # 当前数据库版本
    CURRENT_VERSION = "9.5"

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
        "9.1": "Derived topic-memory storage and Timeline provenance links",
        "9.2": "Resumable deterministic Topic candidate discovery",
        "9.3": "Automatic source-grounded Topic construction and maintenance",
        "9.4": "Related-subtopic graph and decoupled Topic matching thresholds",
        "9.5": "Runtime Topic settings and local incremental reconciliation",
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

                # 从版本9升级到版本9.1
                if current_key <= self.version_key("9"):
                    migration_steps.append(self._migrate_v9_to_v9_1)

                # 从版本9.1升级到版本9.2
                if current_key <= self.version_key("9.1"):
                    migration_steps.append(self._migrate_v9_1_to_v9_2)

                # 从版本9.2升级到版本9.3
                if current_key <= self.version_key("9.2"):
                    migration_steps.append(self._migrate_v9_2_to_v9_3)

                # 从版本9.3升级到版本9.4
                if current_key <= self.version_key("9.3"):
                    migration_steps.append(self._migrate_v9_3_to_v9_4)

                # 从版本9.4升级到版本9.5
                if current_key <= self.version_key("9.4"):
                    migration_steps.append(self._migrate_v9_4_to_v9_5)

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

    async def _table_exists(self, db: aiosqlite.Connection, table_name: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return await cursor.fetchone() is not None

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
