"""
统一记忆引擎 - MemoryEngine
提供统一的记忆管理接口,整合所有底层组件
"""

import asyncio
import copy
import json
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable

import aiosqlite

from astrbot.api import logger

from ...storage.atom_store import AtomStore
from ...storage.graph_store import GraphStore
from ...storage.memory_identity_store import MemoryIdentityStore
from ...storage.topic_memory_store import TopicMemoryStore
from ...storage.user_profile_store import UserProfileStore
from ..managers.atom_lifecycle_manager import AtomLifecycleManager
from ..managers.graph_memory_manager import GraphMemoryManager
from ..models.memory_atom import AtomStatus, AtomType, DecayType, MemoryAtom
from ..models.memory_identity import resolve_memory_space
from ..models.user_profile import (
    UserProfileProjectionEvent,
    UserProfileProjectionOperation,
)
from ..models.identity_profile import SupplementalIdentityStore
from ..importance_policy import IMPORTANCE_POLICY_VERSION
from ..memory_source import serialize_source_messages
from ..memory_transfer import memory_import_key, portable_metadata
from ..processors.graph_extractor import GraphExtractor
from ..processors.text_processor import TextProcessor
from ..retrieval.atom_retriever import AtomRetriever
from ..retrieval.bm25_retriever import BM25Retriever
from ..retrieval.dual_route_retriever import DualRouteRetriever
from ..retrieval.graph_keyword_retriever import GraphKeywordRetriever
from ..retrieval.graph_retriever import GraphRetriever
from ..retrieval.graph_vector_retriever import GraphVectorRetriever
from ..retrieval.hybrid_retriever import HybridResult, HybridRetriever
from ..retrieval.rrf_fusion import RRFFusion
from ..retrieval.topic_recall_pipeline import TopicRecallPipeline
from ..retrieval.topic_retriever import TopicRetriever
from ..retrieval.vector_retriever import VectorRetriever
from ..utils.number_utils import clamp_float, safe_float
from ..topic_settings import (
    TOPIC_SETTING_DEFINITIONS,
    TOPIC_SETTINGS_REVISION,
    effective_topic_settings,
    topic_setting_defaults,
    validate_topic_setting,
)
from ..topic_vector_index import TopicVectorIndex
from ..user_profile_settings import (
    USER_PROFILE_SETTING_DEFINITIONS,
    USER_PROFILE_SETTINGS_REVISION,
    effective_user_profile_settings,
    validate_user_profile_setting,
    validate_user_profile_settings,
)
from .topic_build_manager import TopicBuildManager
from .topic_maintenance_manager import TopicMaintenanceManager
from .user_profile_maintenance_manager import UserProfileMaintenanceManager


class MemoryEngine:
    """
    统一记忆引擎

    整合BM25检索、向量检索和混合检索,提供完整的记忆管理接口。

    主要功能:
    1. 记忆CRUD操作(添加、检索、更新、删除)
    2. 自动化记忆整理和清理
    3. 重要性评估和时间衰减
    4. 会话隔离和统计

    ID管理体系说明：
    ==================
    本系统使用三层存储架构，统一使用整数ID作为主键：

    1. **DocumentStorage (FAISS内部)**
       - 表: documents (SQLite，由SQLAlchemy管理)
       - 主键: id (INTEGER, AUTOINCREMENT) - 这是统一的整数标识符
       - UUID字段: doc_id (TEXT) - FAISS内部使用的UUID字符串
       - 关系: id ←→ doc_id (一对一映射)

    2. **BM25 FTS5索引**
       - 表: livingmemory_memories_fts (SQLite FTS5虚拟表)
       - 字段: doc_id (UNINDEXED) - 引用documents.id的整数
       - 注意: 只存储分词后的内容，metadata从documents表读取

    3. **FAISS向量索引**
       - 存储: EmbeddingStorage (FAISS索引文件)
       - 索引ID: 使用documents.id作为向量的整数索引

    插件对外接口：
    - add_memory() 返回: int (documents.id)
    - search_memories() 返回: HybridResult包含doc_id (int)
    - update_memory(memory_id: int) 参数: documents.id
    - delete_memory(memory_id: int) 参数: documents.id

    同步保证：
    - 添加: 先插入DocumentStorage获取id，再用此id插入BM25和FAISS
    - 更新: 通过vector_retriever更新DocumentStorage (自动同步)
    - 删除: 先删除BM25，再通过FaissVecDB.delete()删除DocumentStorage和向量
    """

    def __init__(
        self,
        db_path: str,
        faiss_db,
        graph_vector_db=None,
        llm_provider=None,
        rerank_provider=None,
        config: dict[str, Any] | None = None,
        identity_profile_store: SupplementalIdentityStore | None = None,
        topic_provider_resolver: Callable[[], dict[str, Any]] | None = None,
        user_profile_provider_resolver: Callable[..., Any] | None = None,
    ):
        """
        初始化记忆引擎

        Args:
            db_path: SQLite数据库路径
            faiss_db: FAISS向量数据库实例
            llm_provider: LLM提供者(可选,用于高级功能)
            config: 配置字典,支持以下参数:
                - rrf_k: RRF参数,默认60
                - decay_rate: 时间衰减率,默认0.01
                - importance_weight: 重要性权重,默认1.0
                - fallback_enabled: 启用退化机制,默认True
                - cleanup_days_threshold: 清理天数阈值,默认30
                - cleanup_importance_threshold: 清理重要性阈值,默认0.3
                - stopwords_path: 停用词文件路径(可选)
        """
        self.db_path = db_path
        self.faiss_db = faiss_db
        self.graph_vector_db = graph_vector_db
        self.llm_provider = llm_provider
        self.rerank_provider = rerank_provider
        self.identity_profile_store = (
            identity_profile_store or SupplementalIdentityStore()
        )
        self.topic_provider_resolver = topic_provider_resolver
        self.user_profile_provider_resolver = (
            user_profile_provider_resolver or topic_provider_resolver
        )
        self.recall_trace_store = None
        self._session_scope_resolver: Callable[[str], Any] | None = None
        self.config = config or {}
        self.graph_enabled = bool(self.config.get("graph_memory_enabled", False))
        self.atom_enabled = bool(
            self.config.get(
                "atom_enabled",
                self.config.get("graph_memory_atom_enabled", True),
            )
        )

        # 确保数据库目录存在
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # 后台任务跟踪
        self._pending_tasks: set[asyncio.Task] = set()
        # Runtime migration is durable in SQLite, but recall requests can arrive
        # concurrently before the durable marker is written. Serialize the first
        # check and remember completed sessions for this process lifetime.
        self._session_migration_lock = asyncio.Lock()
        self._session_migration_checked: set[str] = set()
        # Serialize scheduled and user-triggered storage maintenance. VACUUM
        # needs exclusive database access and must not overlap another cleanup.
        self._storage_maintenance_lock = asyncio.Lock()
        self._user_profile_projection_suppressed = 0

        # 初始化组件(在initialize中完成)
        self.text_processor = None
        self.bm25_retriever = None
        self.vector_retriever = None
        self.rrf_fusion = None
        self.hybrid_retriever = None
        self.graph_store = None
        self.graph_extractor = None
        self.graph_keyword_retriever = None
        self.graph_vector_retriever = None
        self.graph_retriever = None
        self.graph_memory_manager = None
        self.dual_route_retriever = None
        self.atom_store = None
        self.atom_lifecycle_manager = None
        self.atom_retriever = None
        self.memory_identity_store = MemoryIdentityStore(self.db_path)
        self.topic_memory_store = TopicMemoryStore(self.db_path)
        self.user_profile_store = UserProfileStore(self.db_path)
        self.user_profile_config: dict[str, Any] = effective_user_profile_settings()
        self.user_profile_maintenance_manager = UserProfileMaintenanceManager(
            self.user_profile_store,
            provider=self.llm_provider,
            provider_resolver=self.user_profile_provider_resolver,
            config=self.user_profile_config,
        )
        self.topic_vector_index = TopicVectorIndex(self.topic_memory_store)
        self.topic_maintenance_manager = TopicMaintenanceManager(
            self.db_path,
            self.topic_memory_store,
        )
        topic_build_config = dict(self.config.get("topic_memory", {}))
        topic_build_config.setdefault(
            "recall_decay_rate", float(self.config.get("decay_rate", 0.01))
        )
        self.topic_build_manager = TopicBuildManager(
            self.db_path,
            self.topic_memory_store,
            self.topic_maintenance_manager,
            llm_provider=self.llm_provider,
            embedding_provider=getattr(self.faiss_db, "embedding_provider", None),
            rerank_provider=self.rerank_provider,
            config=topic_build_config,
            identity_profile_store=self.identity_profile_store,
            provider_resolver=self.topic_provider_resolver,
            vector_index=self.topic_vector_index,
        )
        self.topic_retriever = TopicRetriever(
            self.topic_memory_store,
            embedding_provider=getattr(self.faiss_db, "embedding_provider", None),
            rerank_provider=self.rerank_provider,
            config=topic_build_config,
            provider_resolver=self.topic_provider_resolver,
            vector_index=self.topic_vector_index,
        )
        self.topic_recall_pipeline = TopicRecallPipeline(
            self.topic_retriever,
            topic_build_config,
        )
        self.topic_memory_enabled = bool(
            self.config.get("topic_memory", {}).get("enabled", False)
        )
        self.topic_auto_maintenance = bool(
            self.config.get("topic_memory", {}).get("auto_maintenance", True)
        )
        self.db_connection = None
        self._search_cache_enabled = bool(self.config.get("search_cache_enabled", True))
        self._search_cache_ttl = float(
            self.config.get("search_cache_ttl_seconds", 45.0)
        )
        self._search_cache_max_size = int(self.config.get("search_cache_max_size", 256))
        self._search_cache_generation = 0
        self._search_cache: OrderedDict[
            tuple[Any, ...], tuple[float, list[HybridResult]]
        ] = OrderedDict()
        self._write_op_repair_enabled = bool(
            self.config.get("write_op_repair_enabled", True)
        )
        self._write_op_max_retries = int(self.config.get("write_op_max_retries", 3))

    async def initialize(self):
        """
        异步初始化引擎

        创建数据库表、初始化所有检索器组件
        """
        # 1. 连接数据库
        self.db_connection = await aiosqlite.connect(self.db_path)
        self.db_connection.row_factory = aiosqlite.Row
        await self.db_connection.execute("PRAGMA journal_mode = WAL")
        await self.db_connection.execute("PRAGMA busy_timeout = 10000")

        # 2. 创建表结构
        await self._create_tables()
        await self.memory_identity_store.initialize()
        await self.topic_memory_store.initialize()
        await self.user_profile_store.initialize()
        await self._initialize_topic_runtime_settings()
        await self._initialize_user_profile_runtime_settings()
        await self.user_profile_maintenance_manager.start()

        # 3. 初始化文本处理器
        stopwords_path = self.config.get("stopwords_path")
        self.text_processor = TextProcessor(stopwords_path)

        # 4. 初始化RRF融合器
        rrf_k = self.config.get("rrf_k", 60)
        self.rrf_fusion = RRFFusion(k=rrf_k)

        # 5. 初始化BM25检索器
        self.bm25_retriever = BM25Retriever(
            self.db_path, self.text_processor, self.config
        )
        await self.bm25_retriever.initialize()

        # 6. 初始化向量检索器
        self.vector_retriever = VectorRetriever(self.faiss_db, self.config)

        # 7. 初始化混合检索器
        self.hybrid_retriever = HybridRetriever(
            self.bm25_retriever, self.vector_retriever, self.rrf_fusion, self.config
        )

        if self.graph_enabled and self.graph_vector_db is not None:
            self.graph_store = GraphStore(self.db_path)
            await self.graph_store.initialize()

            self.atom_store = AtomStore(self.db_path)
            await self.atom_store.initialize()

            if self.atom_enabled:
                self.atom_lifecycle_manager = AtomLifecycleManager(
                    self.atom_store, self.config
                )
                self.atom_retriever = AtomRetriever(self.atom_store, self.config)
                await self.atom_lifecycle_manager.start()

            self.graph_extractor = GraphExtractor(self.config)
            self.graph_keyword_retriever = GraphKeywordRetriever(
                self.graph_store,
                self.text_processor,
                self.config,
            )
            self.graph_vector_retriever = GraphVectorRetriever(
                self.graph_vector_db,
                self.config,
            )
            self.graph_retriever = GraphRetriever(
                self.graph_keyword_retriever,
                self.graph_vector_retriever,
                self.rrf_fusion,
                self.config,
            )
            self.graph_memory_manager = GraphMemoryManager(
                self.graph_store,
                self.graph_vector_retriever,
                self.graph_extractor,
            )
            self.dual_route_retriever = DualRouteRetriever(
                self.hybrid_retriever,
                self.graph_retriever,
                self.get_memory,
                self.config,
            )

        if self._write_op_repair_enabled:
            await self._repair_incomplete_write_ops()
        if self.topic_memory_enabled and self.topic_auto_maintenance:
            await self._resume_deleted_timeline_repairs()

    async def close(self):
        """关闭数据库连接和清理资源"""
        await self.user_profile_maintenance_manager.close()
        await self.topic_build_manager.close()
        rerank_config = getattr(self.rerank_provider, "provider_config", {}) or {}
        close_rerank = getattr(self.rerank_provider, "aclose", None)
        if (
            rerank_config.get("id") == "cloudflare_workers_ai_rerank"
            and callable(close_rerank)
        ):
            await close_rerank()
        if self.atom_lifecycle_manager is not None:
            await self.atom_lifecycle_manager.stop()
        if self._pending_tasks:
            for task in self._pending_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()
        if self.db_connection:
            await self.db_connection.close()
        if self.graph_vector_db is not None:
            await self.graph_vector_db.close()

    def _create_tracked_task(self, coro) -> None:
        """Create and track a background task, auto-discarding on completion."""
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _create_write_ops_table(self) -> None:
        """Create the resumable write-operation log."""
        if self.db_connection is None:
            return
        await self.db_connection.execute("""
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
        """)
        await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_write_ops_status
            ON memory_write_ops(status, updated_at)
        """)
        await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_write_ops_memory
            ON memory_write_ops(memory_id, op_type)
        """)

    async def _start_write_op(
        self,
        op_type: str,
        payload: dict[str, Any] | None = None,
        memory_id: int | None = None,
    ) -> int | None:
        """Record the beginning of a multi-store write operation."""
        if self.db_connection is None:
            return None
        now = time.time()
        try:
            cursor = await self.db_connection.execute(
                """
                INSERT INTO memory_write_ops(
                    op_type, memory_id, status, step, payload,
                    created_at, updated_at
                ) VALUES (?, ?, 'pending', 'started', ?, ?, ?)
                """,
                (
                    op_type,
                    memory_id,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            await self.db_connection.commit()
            return int(cursor.lastrowid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[MemoryEngine] 写操作日志创建失败", exc_info=True)
            return None

    async def _advance_write_op(
        self,
        op_id: int | None,
        step: str,
        *,
        status: str = "pending",
        memory_id: int | None = None,
        error: str | None = None,
        payload_patch: dict[str, Any] | None = None,
    ) -> None:
        """Advance a write-operation log entry."""
        if op_id is None or self.db_connection is None:
            return

        try:
            if status == "completed":
                error = None
            current_payload: dict[str, Any] = {}
            if payload_patch:
                cursor = await self.db_connection.execute(
                    "SELECT payload FROM memory_write_ops WHERE id = ?",
                    (op_id,),
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    try:
                        loaded = json.loads(row[0])
                        current_payload = loaded if isinstance(loaded, dict) else {}
                    except (json.JSONDecodeError, TypeError):
                        current_payload = {}
                current_payload.update(payload_patch)

            fields = ["status = ?", "step = ?", "updated_at = ?"]
            params: list[Any] = [status, step, time.time()]
            if memory_id is not None:
                fields.append("memory_id = ?")
                params.append(memory_id)
            if error is not None:
                fields.append("error = ?")
                params.append(error[:1000])
                if status != "completed":
                    fields.append("retry_count = retry_count + 1")
            elif status == "completed":
                fields.append("error = NULL")
            if status == "completed":
                # Completed operations are no longer replayable work. Keep the
                # compact operation header while discarding the potentially
                # large document/atom recovery payload.
                fields.append("payload = '{}'")
            elif payload_patch:
                fields.append("payload = ?")
                params.append(json.dumps(current_payload, ensure_ascii=False))
            params.append(op_id)
            await self.db_connection.execute(
                f"UPDATE memory_write_ops SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            await self.db_connection.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[MemoryEngine] 写操作日志更新失败", exc_info=True)

    def _normalize_cache_query(self, query: str) -> str:
        return " ".join(query.casefold().split())

    def _search_cache_key(
        self,
        query: str,
        k: int,
        session_id: str | None,
        persona_id: str | None,
    ) -> tuple[Any, ...]:
        return (
            self._search_cache_generation,
            self._normalize_cache_query(query),
            int(k),
            session_id or "",
            persona_id or "",
            bool(self.dual_route_retriever is not None),
            round(float(self.config.get("document_route_weight", 0.65)), 4),
            round(float(self.config.get("graph_route_weight", 0.35)), 4),
            int(self.config.get("graph_expansion_hops", 1)),
        )

    def _get_cached_search_results(
        self,
        cache_key: tuple[Any, ...],
    ) -> list[HybridResult] | None:
        if (
            not self._search_cache_enabled
            or self._search_cache_ttl <= 0
            or self._search_cache_max_size <= 0
        ):
            return None

        cached = self._search_cache.get(cache_key)
        if cached is None:
            return None

        cached_at, results = cached
        if time.time() - cached_at > self._search_cache_ttl:
            self._search_cache.pop(cache_key, None)
            return None

        self._search_cache.move_to_end(cache_key)
        return copy.deepcopy(results)

    def _set_cached_search_results(
        self,
        cache_key: tuple[Any, ...],
        results: list[HybridResult],
    ) -> None:
        if (
            not self._search_cache_enabled
            or self._search_cache_ttl <= 0
            or self._search_cache_max_size <= 0
        ):
            return

        self._search_cache[cache_key] = (time.time(), copy.deepcopy(results))
        self._search_cache.move_to_end(cache_key)
        while len(self._search_cache) > self._search_cache_max_size:
            self._search_cache.popitem(last=False)

    def _invalidate_search_cache(self) -> None:
        """Invalidate cached retrieval results after memory writes."""
        self._search_cache_generation += 1
        self._search_cache.clear()

    def set_session_scope_resolver(self, resolver: Callable[[str], Any] | None) -> None:
        """Attach the explicit conversation-alias resolver after initialization."""
        self._session_scope_resolver = resolver

    async def resolve_session_scope(self, session_id: str | None) -> list[str]:
        if not session_id or self._session_scope_resolver is None:
            return [session_id] if session_id else []
        resolved = await self._session_scope_resolver(str(session_id))
        return list(dict.fromkeys(str(item) for item in (resolved or []) if str(item)))

    async def invalidate_session_alias_cache(self) -> None:
        self._invalidate_search_cache()

    def _apply_stable_identity(
        self,
        metadata: dict[str, Any],
        *,
        session_id: str | None,
        persona_id: str | None,
    ) -> dict[str, Any]:
        """Ensure every physical document carries a stable logical identity."""
        normalized = dict(metadata)
        normalized["memory_uid"] = str(
            normalized.get("memory_uid") or uuid.uuid4()
        )
        try:
            normalized["revision"] = max(1, int(normalized.get("revision", 1)))
        except (TypeError, ValueError):
            normalized["revision"] = 1
        normalized["memory_layer"] = str(
            normalized.get("memory_layer") or "timeline"
        )
        space = resolve_memory_space(session_id, persona_id)
        normalized["memory_space_id"] = space.memory_space_id
        normalized["memory_space_version"] = 1
        return normalized

    async def _register_memory_identity(
        self,
        document_id: int,
        metadata: dict[str, Any],
    ) -> None:
        """Persist the logical-to-physical mapping and optional source span."""
        created_at = safe_float(metadata.get("create_time"), time.time())
        updated_at = safe_float(metadata.get("updated_at"), created_at)
        status = str(metadata.get("status") or "active").strip().lower()
        if status not in {"active", "archived", "deleted"}:
            status = "active"
        await self.memory_identity_store.upsert_memory(
            memory_uid=str(metadata["memory_uid"]),
            document_id=int(document_id),
            memory_layer=str(metadata.get("memory_layer") or "timeline"),
            memory_space_id=str(metadata["memory_space_id"]),
            revision=int(metadata.get("revision", 1)),
            created_at=created_at,
            updated_at=updated_at,
            status=status,
        )
        await self.memory_identity_store.upsert_source_span(
            str(metadata["memory_uid"]),
            metadata.get("source_window"),
            fallback_session_id=metadata.get("session_id"),
            fallback_time=created_at,
        )

    async def _mark_dependent_topics_stale(
        self,
        memory_uid: str | None,
        *,
        reason: str,
    ) -> list[str]:
        """Record affected Topics while preserving atomic replacement on edits."""
        if not memory_uid:
            return []
        affected: list[str] = []
        try:
            if "deleted" in reason:
                affected = await self.topic_memory_store.mark_timeline_stale(
                    memory_uid
                )
            else:
                affected = [
                    str(row["topic_uid"])
                    for row in await self.topic_memory_store.get_topics_for_timeline(
                        memory_uid
                    )
                    if str(row.get("status") or "") == "active"
                    and str(row.get("link_status") or "") == "active"
                ]
            if affected:
                logger.info(
                    f"[TopicMemory] Timeline 变化影响 {len(affected)} 个 Topic；"
                    f"正式数据将在局部构建发布时原子替换 (reason={reason})"
                )
            return affected
        except asyncio.CancelledError:
            raise
        except Exception:
            # Topic is a derived layer. A bookkeeping failure must be visible but
            # must not roll back a successfully persisted source memory.
            logger.error(
                f"[TopicMemory] 标记关联 Topic 失败 "
                f"(memory_uid={memory_uid}, reason={reason})",
                exc_info=True,
            )
            return []

    async def _queue_deleted_timeline_repair(
        self,
        memory_space_id: str | None,
        *,
        deleted_timeline_uids: Iterable[str],
        affected_topic_uids: Iterable[str],
    ) -> str | None:
        """Persist source repair and run it immediately only when auto maintenance is on."""
        if not self.topic_memory_enabled or not memory_space_id:
            return None
        deleted = sorted(
            {str(uid).strip() for uid in deleted_timeline_uids if str(uid).strip()}
        )
        affected = sorted(
            {str(uid).strip() for uid in affected_topic_uids if str(uid).strip()}
        )
        if not deleted or not affected:
            return None
        review_uid = await self.topic_memory_store.enqueue_maintenance_review(
            memory_space_id=str(memory_space_id),
            review_type="deleted_timeline_source_repair",
            timeline_uids=deleted,
            topic_uids=affected,
            details={
                "automatic": bool(self.topic_auto_maintenance),
                "reason": "Timeline source deleted",
                "requires_llm": True,
            },
        )
        if self.topic_auto_maintenance:
            self._create_tracked_task(
                self._run_deleted_timeline_repair(
                    str(memory_space_id),
                    affected_topic_uids=affected,
                    deleted_timeline_uids=deleted,
                    review_uid=review_uid,
                )
            )
        return review_uid

    async def _run_deleted_timeline_repair(
        self,
        memory_space_id: str,
        *,
        affected_topic_uids: list[str],
        deleted_timeline_uids: list[str],
        review_uid: str,
    ) -> None:
        try:
            await self.topic_build_manager.repair_deleted_timeline_sources(
                memory_space_id,
                affected_topic_uids=affected_topic_uids,
                deleted_timeline_uids=deleted_timeline_uids,
                review_uid=review_uid,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[TopicMemory] Timeline 删除后的 Topic 来源修复失败 "
                "(memory_space_id=%s, review_uid=%s): %s",
                memory_space_id,
                review_uid,
                exc,
                exc_info=True,
            )

    async def _resume_deleted_timeline_repairs(self) -> None:
        for item in await self.topic_memory_store.list_pending_source_repairs():
            self._create_tracked_task(
                self._run_deleted_timeline_repair(
                    str(item["memory_space_id"]),
                    affected_topic_uids=[
                        str(uid) for uid in item.get("topic_uids", [])
                    ],
                    deleted_timeline_uids=[
                        str(uid) for uid in item.get("timeline_uids", [])
                    ],
                    review_uid=str(item["review_uid"]),
                )
            )

    def _schedule_topic_maintenance(
        self,
        memory_space_id: str | None,
        *,
        full: bool,
        since: float | None = None,
        timeline_uids: Iterable[str] | None = None,
    ) -> None:
        if (
            not self.topic_memory_enabled
            or not self.topic_auto_maintenance
            or not memory_space_id
        ):
            return
        self.topic_build_manager.schedule_space(
            str(memory_space_id),
            full=full,
            since=since,
            timeline_uids=timeline_uids,
        )

    async def _initialize_topic_runtime_settings(self) -> None:
        """Load sparse overrides and import only genuinely customized legacy values."""
        stored = await self.topic_memory_store.get_topic_setting_overrides()
        if not stored.get("__legacy_imported_v1__"):
            defaults = topic_setting_defaults()
            legacy = self.config.get("topic_memory_legacy_overrides", {})
            imported: dict[str, Any] = {}
            if isinstance(legacy, dict):
                for key, value in legacy.items():
                    if key not in TOPIC_SETTING_DEFINITIONS:
                        continue
                    try:
                        normalized = validate_topic_setting(key, value)
                    except ValueError:
                        continue
                    # Schema-generated old defaults must not pin future defaults.
                    if normalized != defaults[key]:
                        imported[key] = normalized
            imported["__legacy_imported_v1__"] = True
            stored = await self.topic_memory_store.update_topic_setting_overrides(
                imported,
                settings_revision=TOPIC_SETTINGS_REVISION,
            )
        self.apply_topic_runtime_settings(stored)

    def apply_topic_runtime_settings(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """Apply one validated effective configuration to every Topic consumer."""
        public_overrides = {
            key: value
            for key, value in overrides.items()
            if key in TOPIC_SETTING_DEFINITIONS
        }
        effective = effective_topic_settings(public_overrides)
        base = dict(self.config.get("topic_memory", {}))
        base.update(effective)
        self.config["topic_memory"] = base
        self.topic_build_manager.apply_config(base)
        self.topic_retriever.config = dict(base)
        self.topic_recall_pipeline.config = dict(base)
        self._invalidate_search_cache()
        return effective

    def apply_timeline_runtime_settings(self, effective: dict[str, Any]) -> None:
        """Apply validated Timeline settings to long-lived retrieval consumers."""
        mapping = {
            "fusion_strategy.rrf_k": "rrf_k",
            "importance_decay.decay_rate": "decay_rate",
            "importance_decay.access_decay_window_days": "access_decay_window_days",
            "importance_decay.access_decay_max_count": "access_decay_max_count",
            "importance_decay.access_count_decay_multiplier": "access_count_decay_multiplier",
            "recall_engine.importance_weight": "importance_weight",
            "recall_engine.candidate_multiplier": "candidate_multiplier",
            "recall_engine.min_relevance_score": "min_relevance_score",
            "recall_engine.relative_score_floor": "relative_score_floor",
            "recall_engine.mmr_lambda": "mmr_lambda",
            "recall_engine.search_cache_enabled": "search_cache_enabled",
            "recall_engine.search_cache_ttl_seconds": "search_cache_ttl_seconds",
            "recall_engine.search_cache_max_size": "search_cache_max_size",
            "recall_engine.fallback_to_vector": "fallback_enabled",
            "forgetting_agent.cleanup_days_threshold": "cleanup_days_threshold",
            "forgetting_agent.cleanup_importance_threshold": "cleanup_importance_threshold",
            "forgetting_agent.auto_cleanup_enabled": "auto_cleanup_enabled",
            "maintenance.auto_cleanup_completed_build_artifacts": (
                "auto_cleanup_completed_build_artifacts"
            ),
            "graph_memory.document_route_weight": "document_route_weight",
            "graph_memory.graph_route_weight": "graph_route_weight",
            "graph_memory.cross_route_bonus": "cross_route_bonus",
            "graph_memory.expansion_limit": "graph_expansion_limit",
            "graph_memory.expansion_hops": "graph_expansion_hops",
            "graph_memory.second_hop_weight": "graph_second_hop_weight",
            "graph_memory.dynamic_route_weighting": "dynamic_route_weighting",
            "graph_memory.max_topics_per_memory": "graph_max_topics",
            "graph_memory.max_participants_per_memory": "graph_max_participants",
            "graph_memory.max_facts_per_memory": "graph_max_facts",
            "graph_memory.atom_maintenance_interval_hours": "atom_maintenance_interval_hours",
            "graph_memory.atom_forget_delay_days": "atom_forget_delay_days",
            "graph_memory.atom_purge_delay_days": "atom_purge_delay_days",
            "index_rebuild_settings.batch_size": "index_rebuild_batch_size",
            "index_rebuild_settings.embedding_batch_size": "index_rebuild_embedding_batch_size",
            "index_rebuild_settings.tasks_limit": "index_rebuild_tasks_limit",
            "index_rebuild_settings.max_retries": "index_rebuild_max_retries",
            "index_rebuild_settings.retry_base_delay": "index_rebuild_retry_base_delay",
            "index_rebuild_settings.batch_delay": "index_rebuild_batch_delay",
            "index_rebuild_settings.request_delay": "index_rebuild_request_delay",
            "index_rebuild_settings.max_failure_ratio": "index_rebuild_max_failure_ratio",
        }
        for source, target in mapping.items():
            if source in effective:
                self.config[target] = effective[source]
        if self.rrf_fusion is not None:
            self.rrf_fusion.k = int(self.config.get("rrf_k", 60))
        if self.hybrid_retriever is not None:
            self.hybrid_retriever.decay_rate = float(
                self.config.get("decay_rate", 0.01)
            )
            self.hybrid_retriever.importance_weight = float(
                self.config.get("importance_weight", 1.0)
            )
            self.hybrid_retriever.fallback_enabled = bool(
                self.config.get("fallback_enabled", True)
            )
            self.hybrid_retriever.mmr_lambda = float(
                self.config.get("mmr_lambda", 0.72)
            )
        if self.graph_retriever is not None:
            self.graph_retriever.decay_rate = float(
                self.config.get("decay_rate", 0.01)
            )
        if self.dual_route_retriever is not None:
            document_weight = float(self.config.get("document_route_weight", 0.65))
            graph_weight = float(self.config.get("graph_route_weight", 0.35))
            total_weight = document_weight + graph_weight
            if total_weight <= 0:
                document_weight, graph_weight = 0.65, 0.35
            else:
                document_weight /= total_weight
                graph_weight /= total_weight
            self.dual_route_retriever.document_route_weight = document_weight
            self.dual_route_retriever.graph_route_weight = graph_weight
            self.dual_route_retriever.cross_route_bonus = float(
                self.config.get("cross_route_bonus", 0.08)
            )
            self.dual_route_retriever.dynamic_route_weighting = bool(
                self.config.get("dynamic_route_weighting", True)
            )
        if self.graph_keyword_retriever is not None:
            self.graph_keyword_retriever.expansion_limit = int(
                self.config.get("graph_expansion_limit", 24)
            )
            self.graph_keyword_retriever.expansion_hops = int(
                self.config.get("graph_expansion_hops", 1)
            )
            self.graph_keyword_retriever.second_hop_weight = float(
                self.config.get("graph_second_hop_weight", 0.4)
            )
        if self.graph_extractor is not None:
            self.graph_extractor.max_topics = int(
                self.config.get("graph_max_topics", 6)
            )
            self.graph_extractor.max_participants = int(
                self.config.get("graph_max_participants", 8)
            )
            self.graph_extractor.max_facts = int(
                self.config.get("graph_max_facts", 8)
            )
        if self.atom_lifecycle_manager is not None:
            self.atom_lifecycle_manager.apply_runtime_settings(self.config)
        topic_config = dict(self.topic_recall_pipeline.config)
        topic_config["recall_decay_rate"] = float(
            self.config.get("decay_rate", 0.01)
        )
        self.topic_retriever.config = dict(topic_config)
        self.topic_recall_pipeline.config = dict(topic_config)
        self._search_cache_enabled = bool(
            self.config.get("search_cache_enabled", True)
        )
        self._search_cache_ttl = float(
            self.config.get("search_cache_ttl_seconds", 45.0)
        )
        self._search_cache_max_size = int(
            self.config.get("search_cache_max_size", 256)
        )
        self._invalidate_search_cache()

    async def get_topic_runtime_settings(self) -> dict[str, Any]:
        stored = await self.topic_memory_store.get_topic_setting_overrides()
        overrides = {
            key: value
            for key, value in stored.items()
            if key in TOPIC_SETTING_DEFINITIONS
        }
        effective = effective_topic_settings(overrides)
        definitions = {
            key: {**value, "customized": key in overrides}
            for key, value in TOPIC_SETTING_DEFINITIONS.items()
        }
        return {
            "settings_revision": TOPIC_SETTINGS_REVISION,
            "definitions": definitions,
            "overrides": overrides,
            "effective": effective,
        }

    async def update_topic_runtime_settings(
        self,
        changes: dict[str, Any],
        *,
        reset_keys: list[str] | None = None,
        reset_all: bool = False,
    ) -> dict[str, Any]:
        if self.topic_build_manager.has_active_builds():
            raise RuntimeError("Topic 构建正在运行，暂时不能修改参数")
        normalized = {
            str(key): validate_topic_setting(str(key), value)
            for key, value in changes.items()
        }
        normalized_reset = [
            str(key)
            for key in (reset_keys or [])
            if str(key) in TOPIC_SETTING_DEFINITIONS
        ]
        stored = await self.topic_memory_store.update_topic_setting_overrides(
            normalized,
            reset_keys=normalized_reset,
            reset_all=bool(reset_all),
            settings_revision=TOPIC_SETTINGS_REVISION,
        )
        self.apply_topic_runtime_settings(stored)
        return await self.get_topic_runtime_settings()

    async def _initialize_user_profile_runtime_settings(self) -> None:
        stored = await self.user_profile_store.get_setting_overrides()
        if not stored.get("__plugin_config_imported_v1__"):
            imported: dict[str, Any] = {}
            initial = self.config.get("user_profile_initial_overrides", {})
            if isinstance(initial, dict):
                for short_key, value in initial.items():
                    key = f"user_profile.{short_key}"
                    if key not in USER_PROFILE_SETTING_DEFINITIONS:
                        continue
                    try:
                        imported[key] = validate_user_profile_setting(key, value)
                    except ValueError:
                        continue
            imported["__plugin_config_imported_v1__"] = True
            stored = await self.user_profile_store.update_setting_overrides(
                imported,
                settings_revision=USER_PROFILE_SETTINGS_REVISION,
            )
        self.apply_user_profile_runtime_settings(stored)

    def apply_user_profile_runtime_settings(
        self, overrides: dict[str, Any]
    ) -> dict[str, Any]:
        public_overrides = {
            key: value
            for key, value in overrides.items()
            if key in USER_PROFILE_SETTING_DEFINITIONS
        }
        effective = effective_user_profile_settings(public_overrides)
        self.user_profile_config = effective
        manager = getattr(self, "user_profile_maintenance_manager", None)
        if manager is not None:
            manager.apply_config(effective)
        return effective

    async def get_user_profile_runtime_settings(self) -> dict[str, Any]:
        stored = await self.user_profile_store.get_setting_overrides()
        overrides = {
            key: value
            for key, value in stored.items()
            if key in USER_PROFILE_SETTING_DEFINITIONS
        }
        effective = effective_user_profile_settings(overrides)
        definitions = {
            key: {**definition, "customized": key in overrides}
            for key, definition in USER_PROFILE_SETTING_DEFINITIONS.items()
        }
        return {
            "settings_revision": USER_PROFILE_SETTINGS_REVISION,
            "definitions": definitions,
            "overrides": overrides,
            "effective": effective,
        }

    async def update_user_profile_runtime_settings(
        self,
        changes: dict[str, Any],
        *,
        reset_keys: list[str] | None = None,
        reset_all: bool = False,
    ) -> dict[str, Any]:
        normalized = {
            str(key): validate_user_profile_setting(str(key), value)
            for key, value in changes.items()
        }
        normalized_reset = [
            str(key)
            for key in (reset_keys or [])
            if str(key) in USER_PROFILE_SETTING_DEFINITIONS
        ]
        current = await self.get_user_profile_runtime_settings()
        prospective_overrides = dict(current["overrides"])
        if reset_all:
            prospective_overrides.clear()
        else:
            for key in normalized_reset:
                prospective_overrides.pop(key, None)
        prospective_overrides.update(normalized)
        prospective = effective_user_profile_settings(prospective_overrides)
        validate_user_profile_settings(prospective)
        stored = await self.user_profile_store.update_setting_overrides(
            normalized,
            reset_keys=normalized_reset,
            reset_all=bool(reset_all),
            settings_revision=USER_PROFILE_SETTINGS_REVISION,
        )
        self.apply_user_profile_runtime_settings(stored)
        return await self.get_user_profile_runtime_settings()

    async def ensure_private_user_profile(
        self,
        *,
        session_id: str,
        persona_id: str | None,
        actor_id: str,
        display_name: str | None = None,
    ):
        """Create an empty profile only for a stable private-chat human actor."""
        if not bool(self.user_profile_config.get("user_profile.enabled", True)):
            return None
        if not bool(
            self.user_profile_config.get(
                "user_profile.auto_enable_private_users", True
            )
        ):
            return None
        space = resolve_memory_space(session_id, persona_id)
        if space.chat_type != "private" or not self._is_stable_profile_actor(actor_id):
            return None
        return await self.user_profile_store.ensure_private_scope(
            actor_id=actor_id,
            bot_account=space.bot_account,
            persona_id=space.persona_id,
            display_name=display_name,
            auto_enable=True,
        )

    async def _queue_user_profile_projection(
        self,
        *,
        operation: UserProfileProjectionOperation | str,
        metadata: dict[str, Any] | None = None,
        timeline_uid: str | None = None,
        timeline_revision: int | None = None,
        memory_space_id: str | None = None,
    ) -> list[str]:
        """Persist a post-commit Timeline projection without failing the Timeline write."""
        if self._user_profile_projection_suppressed > 0 or not bool(
            self.user_profile_config.get("user_profile.enabled", True)
        ):
            return []
        normalized = dict(metadata or {})
        if normalized and str(normalized.get("memory_layer") or "timeline") != "timeline":
            return []
        uid = str(timeline_uid or normalized.get("memory_uid") or "").strip()
        if not uid:
            return []
        try:
            revision = max(
                1,
                int(timeline_revision or normalized.get("revision") or 1),
            )
        except (TypeError, ValueError):
            revision = 1
        space = resolve_memory_space(
            normalized.get("session_id"), normalized.get("persona_id")
        ) if normalized else None
        resolved_space_id = str(
            memory_space_id
            or normalized.get("memory_space_id")
            or (space.memory_space_id if space else "")
        )
        scopes = []
        actor_id = ""
        display_name = None
        if normalized:
            if space is None or space.chat_type != "private":
                return []
            actor_id, display_name = self._profile_actor_from_metadata(normalized, space.target_id)
            if not actor_id:
                return []
            scope = await self.user_profile_store.get_scope_by_actor(
                actor_id=actor_id,
                bot_account=space.bot_account,
                persona_id=space.persona_id,
                include_disabled=True,
            )
            if scope is None:
                scope = await self.ensure_private_user_profile(
                    session_id=str(normalized.get("session_id") or ""),
                    persona_id=str(normalized.get("persona_id") or ""),
                    actor_id=actor_id,
                    display_name=display_name,
                )
            if scope is not None:
                scopes = [scope]
        else:
            scopes = await self.user_profile_store.find_projection_scopes(
                timeline_uid=uid,
                memory_space_id=resolved_space_id or None,
            )
        if not scopes:
            return []

        event_uids: list[str] = []
        for scope in scopes:
            if not scope.enabled:
                await self.user_profile_store.set_scope_state(
                    scope.profile_scope_uid, has_gap=True
                )
                continue
            payload = {
                "metadata": normalized,
                "profile_actor_id": actor_id,
                "profile_display_name": display_name,
            }
            try:
                event_uid = await self.user_profile_store.enqueue_projection_event(
                    UserProfileProjectionEvent(
                        timeline_uid=uid,
                        timeline_revision=revision,
                        operation=operation,
                        memory_space_id=resolved_space_id,
                        profile_scope_uid=scope.profile_scope_uid,
                        payload=payload,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.user_profile_store.set_scope_state(
                    scope.profile_scope_uid, has_gap=True
                )
                logger.error(
                    "[UserProfile] Timeline 投影事件写入失败 "
                    "(timeline_uid=%s, revision=%s): %s",
                    uid,
                    revision,
                    exc,
                    exc_info=True,
                )
                continue
            event_uids.append(event_uid)
            self.user_profile_maintenance_manager.schedule_scope(
                scope.profile_scope_uid
            )
        return event_uids

    async def _delete_memory_without_profile_projection(self, memory_id: int) -> bool:
        """Preserve the public delete signature during physical replacements."""
        self._user_profile_projection_suppressed += 1
        try:
            return await self.delete_memory(memory_id)
        finally:
            self._user_profile_projection_suppressed = max(
                0, self._user_profile_projection_suppressed - 1
            )

    @staticmethod
    def _is_stable_profile_actor(actor_id: str) -> bool:
        parts = str(actor_id or "").strip().split(":", 2)
        return (
            len(parts) == 3
            and parts[0] not in {"", "unknown"}
            and parts[1] == "human"
            and parts[2] not in {"", "unknown"}
        )

    @classmethod
    def _profile_actor_from_metadata(
        cls,
        metadata: dict[str, Any],
        private_target_id: str,
    ) -> tuple[str, str | None]:
        bindings = metadata.get("role_bindings")
        actors = bindings.get("actors") if isinstance(bindings, dict) else []
        humans = [
            item
            for item in (actors or [])
            if isinstance(item, dict)
            and str(item.get("actor_type") or "") == "human"
            and cls._is_stable_profile_actor(str(item.get("actor_id") or ""))
        ]
        target_matches = [
            item
            for item in humans
            if str(item.get("sender_id") or "") == str(private_target_id or "")
        ]
        selected = target_matches[0] if len(target_matches) == 1 else None
        if selected is None and len(humans) == 1:
            selected = humans[0]
        if selected is None:
            return "", None
        names = selected.get("observed_names") or []
        display_name = str(names[-1]).strip() if names else None
        return str(selected["actor_id"]), display_name

    def _serialize_atom_for_repair(self, atom: Any) -> dict[str, Any]:
        """Convert a MemoryAtom-like object into JSON-safe repair payload."""
        atom_type = getattr(atom, "atom_type", AtomType.UNKNOWN)
        decay_type = getattr(atom, "decay_type", DecayType.EXPONENTIAL)
        status = getattr(atom, "status", AtomStatus.ACTIVE)
        return {
            "parent_memory_id": int(getattr(atom, "parent_memory_id", 0) or 0),
            "atom_type": getattr(atom_type, "value", str(atom_type)),
            "content": str(getattr(atom, "content", "")),
            "entities": list(getattr(atom, "entities", []) or []),
            "importance": float(getattr(atom, "importance", 0.5) or 0.5),
            "confidence": float(getattr(atom, "confidence", 0.7) or 0.7),
            "created_at": float(
                getattr(atom, "created_at", time.time()) or time.time()
            ),
            "last_accessed_at": float(
                getattr(atom, "last_accessed_at", time.time()) or time.time()
            ),
            "last_reinforced_at": getattr(atom, "last_reinforced_at", None),
            "event_time": getattr(atom, "event_time", None),
            "ttl_days": float(getattr(atom, "ttl_days", 30.0) or 30.0),
            "expires_at": float(getattr(atom, "expires_at", 0.0) or 0.0),
            "status": getattr(status, "value", str(status)),
            "reinforcement_count": int(getattr(atom, "reinforcement_count", 0) or 0),
            "decay_type": getattr(decay_type, "value", str(decay_type)),
            "session_id": getattr(atom, "session_id", None),
            "persona_id": getattr(atom, "persona_id", None),
            "metadata": dict(getattr(atom, "metadata", {}) or {}),
        }

    def _deserialize_atom_from_repair(
        self,
        payload: dict[str, Any],
        parent_memory_id: int,
        session_id: str | None,
        persona_id: str | None,
    ) -> MemoryAtom | None:
        """Rebuild a MemoryAtom from repair payload."""
        content = str(payload.get("content") or "")
        if not content.strip():
            return None

        try:
            atom_type = AtomType(payload.get("atom_type") or AtomType.UNKNOWN.value)
        except ValueError:
            atom_type = AtomType.UNKNOWN
        try:
            decay_type = DecayType(
                payload.get("decay_type") or DecayType.EXPONENTIAL.value
            )
        except ValueError:
            decay_type = DecayType.EXPONENTIAL
        try:
            status = AtomStatus(payload.get("status") or AtomStatus.ACTIVE.value)
        except ValueError:
            status = AtomStatus.ACTIVE

        return MemoryAtom(
            parent_memory_id=parent_memory_id,
            atom_type=atom_type,
            content=content,
            entities=[str(item) for item in payload.get("entities", []) if item],
            importance=float(payload.get("importance", 0.5) or 0.5),
            confidence=float(payload.get("confidence", 0.7) or 0.7),
            created_at=float(payload.get("created_at", time.time()) or time.time()),
            last_accessed_at=float(
                payload.get("last_accessed_at", time.time()) or time.time()
            ),
            last_reinforced_at=payload.get("last_reinforced_at"),
            event_time=payload.get("event_time"),
            ttl_days=float(payload.get("ttl_days", 30.0) or 30.0),
            expires_at=float(payload.get("expires_at", 0.0) or 0.0),
            status=status,
            reinforcement_count=int(payload.get("reinforcement_count", 0) or 0),
            decay_type=decay_type,
            session_id=payload.get("session_id") or session_id,
            persona_id=payload.get("persona_id") or persona_id,
            metadata=dict(payload.get("metadata") or {}),
        )

    async def _repair_incomplete_write_ops(self) -> int:
        """Best-effort replay for incomplete add/delete operations."""
        if self.db_connection is None:
            return 0

        try:
            cursor = await self.db_connection.execute(
                """
                SELECT id, op_type, memory_id, status, step, payload, retry_count
                FROM memory_write_ops
                WHERE status IN ('pending', 'needs_repair')
                  AND retry_count < ?
                ORDER BY id ASC
                LIMIT 25
                """,
                (self._write_op_max_retries,),
            )
            rows = await cursor.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[MemoryEngine] 读取待修复写操作失败", exc_info=True)
            return 0

        repaired = 0
        for row in rows:
            payload = self._safe_json_dict(row["payload"])
            try:
                op_type = row["op_type"]
                memory_id = row["memory_id"]
                if op_type == "add":
                    ok = await self._repair_add_write_op(
                        int(row["id"]),
                        int(memory_id) if memory_id is not None else None,
                        payload,
                    )
                elif op_type == "delete":
                    ok = await self._repair_delete_write_op(
                        int(row["id"]),
                        int(memory_id) if memory_id is not None else None,
                    )
                elif op_type == "batch_delete":
                    ok = await self._repair_batch_delete_write_op(
                        int(row["id"]),
                        payload,
                    )
                else:
                    ok = False
                repaired += 1 if ok else 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"[MemoryEngine] 修复写操作失败 (op_id={row['id']})",
                    exc_info=True,
                )
                await self._advance_write_op(
                    int(row["id"]),
                    str(row["step"] or "repair_failed"),
                    status="needs_repair",
                    error=str(e),
                )

        if repaired:
            logger.info(f"[MemoryEngine] 已修复 {repaired} 个未完成写操作")
            self._invalidate_search_cache()
        return repaired

    async def _repair_add_write_op(
        self,
        op_id: int,
        memory_id: int | None,
        payload: dict[str, Any],
    ) -> bool:
        if memory_id is None:
            await self._advance_write_op(
                op_id,
                "unrepairable",
                status="failed",
                error="missing memory_id for add repair",
            )
            return False

        memory = await self.get_memory(int(memory_id))
        if memory is None:
            await self._advance_write_op(
                op_id,
                "source_missing",
                status="failed",
                memory_id=int(memory_id),
                error="source document missing",
            )
            return False

        metadata = memory.get("metadata") or payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = self._safe_json_dict(metadata)
        content = str(memory.get("text") or "")
        session_id = metadata.get("session_id") or payload.get("session_id")
        persona_id = metadata.get("persona_id") or payload.get("persona_id")

        atom_payloads = payload.get("failed_atoms") or payload.get("atoms", []) or []
        atoms: list[MemoryAtom] = []
        for atom_payload in atom_payloads:
            if isinstance(atom_payload, dict):
                atom = self._deserialize_atom_from_repair(
                    atom_payload,
                    int(memory_id),
                    session_id,
                    persona_id,
                )
                if atom is not None:
                    atoms.append(atom)

        if self.atom_store is not None and atoms and self.atom_enabled:
            existing_atoms = await self.atom_store.get_by_parent(int(memory_id))
            if payload.get("failed_atoms"):
                existing_keys = {
                    (
                        atom.content,
                        atom.atom_type.value,
                        atom.session_id,
                        atom.persona_id,
                    )
                    for atom in existing_atoms
                }
                atoms_to_insert = [
                    atom
                    for atom in atoms
                    if (
                        atom.content,
                        atom.atom_type.value,
                        atom.session_id,
                        atom.persona_id,
                    )
                    not in existing_keys
                ]
                if atoms_to_insert:
                    await self.atom_store.insert_many(atoms_to_insert)
            elif not existing_atoms:
                await self.atom_store.insert_many(atoms)
            await self._advance_write_op(op_id, "atoms_repaired", memory_id=memory_id)

        if self.graph_memory_manager is not None and content.strip():
            await self.graph_memory_manager.index_memory(
                int(memory_id),
                content,
                metadata,
                atoms or None,
            )
            await self._advance_write_op(op_id, "graph_repaired", memory_id=memory_id)

        metadata = self._apply_stable_identity(
            metadata,
            session_id=session_id,
            persona_id=persona_id,
        )
        await self._register_memory_identity(int(memory_id), metadata)
        await self._advance_write_op(op_id, "identity_repaired", memory_id=memory_id)

        await self._advance_write_op(
            op_id,
            "completed",
            status="completed",
            memory_id=int(memory_id),
        )
        await self._queue_user_profile_projection(
            operation=UserProfileProjectionOperation.UPSERT,
            metadata=metadata,
        )
        return True

    async def _repair_delete_write_op(
        self,
        op_id: int,
        memory_id: int | None,
    ) -> bool:
        if memory_id is None:
            await self._advance_write_op(
                op_id,
                "unrepairable",
                status="failed",
                error="missing memory_id for delete repair",
            )
            return False

        registry_record = await self.memory_identity_store.get_by_document_id(
            int(memory_id)
        )
        if self.graph_memory_manager is not None:
            await self.graph_memory_manager.delete_memory(int(memory_id))
        if self.atom_store is not None:
            await self.atom_store.delete_by_parent(int(memory_id))
        await self.memory_identity_store.delete_by_document_id(int(memory_id))

        await self._advance_write_op(
            op_id,
            "completed",
            status="completed",
            memory_id=int(memory_id),
        )
        if registry_record is not None:
            await self._queue_user_profile_projection(
                operation=UserProfileProjectionOperation.DELETE,
                timeline_uid=registry_record.memory_uid,
                timeline_revision=registry_record.revision,
                memory_space_id=registry_record.memory_space_id,
            )
        return True

    async def _repair_batch_delete_write_op(
        self,
        op_id: int,
        payload: dict[str, Any],
    ) -> bool:
        memory_ids_raw = payload.get("memory_ids") or []
        if not isinstance(memory_ids_raw, list):
            await self._advance_write_op(
                op_id,
                "unrepairable",
                status="failed",
                error="missing memory_ids for batch delete repair",
            )
            return False

        memory_ids: list[int] = []
        for raw_id in memory_ids_raw:
            try:
                memory_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue

        if not memory_ids:
            await self._advance_write_op(
                op_id,
                "unrepairable",
                status="failed",
                error="empty memory_ids for batch delete repair",
            )
            return False

        failed_vector_doc_uuids = [
            str(value).strip()
            for value in (payload.get("failed_vector_doc_uuids") or [])
            if str(value).strip()
        ]
        for uuid_doc_id in failed_vector_doc_uuids:
            await self.faiss_db.delete(uuid_doc_id)

        registry_records = []
        for memory_id in memory_ids:
            record = await self.memory_identity_store.get_by_document_id(memory_id)
            if record is not None:
                registry_records.append(record)
        await self._delete_document_indexes_for_batch(memory_ids)
        await self._delete_graph_and_atoms_for_batch(memory_ids)
        await self.memory_identity_store.delete_by_document_ids(memory_ids)
        await self._advance_write_op(
            op_id,
            "completed",
            status="completed",
            payload_patch={"deleted_count": len(memory_ids)},
        )
        for record in registry_records:
            await self._queue_user_profile_projection(
                operation=UserProfileProjectionOperation.DELETE,
                timeline_uid=record.memory_uid,
                timeline_revision=record.revision,
                memory_space_id=record.memory_space_id,
            )
        return True

    async def _delete_document_indexes_for_batch(self, memory_ids: list[int]) -> int:
        if not memory_ids or self.db_connection is None:
            return 0

        placeholders = ",".join("?" * len(memory_ids))
        await self.db_connection.execute(
            f"DELETE FROM livingmemory_memories_fts WHERE doc_id IN ({placeholders})",
            memory_ids,
        )

        cursor = await self.db_connection.execute(
            f"SELECT id, doc_id FROM documents WHERE id IN ({placeholders})",
            memory_ids,
        )
        uuid_rows = await cursor.fetchall()
        for row in uuid_rows:
            uuid_doc_id = row["doc_id"]
            if not uuid_doc_id:
                continue
            try:
                await self.faiss_db.delete(uuid_doc_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    f"[批量删除] FAISS 删除失败 (id={row['id']})",
                    exc_info=True,
                )

        cursor = await self.db_connection.execute(
            f"DELETE FROM documents WHERE id IN ({placeholders})",
            memory_ids,
        )
        await self.db_connection.commit()
        return int(cursor.rowcount or 0)

    async def _delete_graph_and_atoms_for_batch(self, memory_ids: list[int]) -> None:
        if not memory_ids:
            return
        if self.graph_memory_manager is not None:
            await self.graph_memory_manager.batch_delete_memories(memory_ids)
        if self.atom_store is not None:
            await self.atom_store.batch_delete_by_parent(memory_ids)

    @staticmethod
    def _safe_json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    async def _create_tables(self):
        """创建数据库表

        注意：documents 表主要由 FAISS 的 DocumentStorage 类创建和管理。
        这里使用 CREATE TABLE IF NOT EXISTS 确保兼容性：
        - 如果 FAISS 已创建，不会重复创建（IF NOT EXISTS）
        - 如果 FAISS 未创建（极端情况），插件仍能正常工作
        - 插件需要直接操作此表进行高频更新（如访问时间）
        """
        # documents表 - 与FAISS共享，IF NOT EXISTS确保不重复创建
        if self.db_connection is not None:
            await self._drop_legacy_documents_fts_triggers()

            await self.db_connection.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT,
                updated_at TEXT
            )
        """)

            # 兼容旧版插件创建的简化 documents 表，确保 FAISS DocumentStorage 所需字段存在
            cursor = await self.db_connection.execute("PRAGMA table_info(documents)")
            column_rows = await cursor.fetchall()
            existing_columns = {row[1] for row in column_rows}

            missing_columns = []
            if "doc_id" not in existing_columns:
                await self.db_connection.execute(
                    "ALTER TABLE documents ADD COLUMN doc_id TEXT"
                )
                missing_columns.append("doc_id")
            if "created_at" not in existing_columns:
                await self.db_connection.execute(
                    "ALTER TABLE documents ADD COLUMN created_at TEXT"
                )
                missing_columns.append("created_at")
            if "updated_at" not in existing_columns:
                await self.db_connection.execute(
                    "ALTER TABLE documents ADD COLUMN updated_at TEXT"
                )
                missing_columns.append("updated_at")

            if missing_columns:
                logger.warning(
                    "[MemoryEngine] 检测到旧版 documents 表结构，已补齐字段: "
                    f"{', '.join(missing_columns)}"
                )

            # 回填旧数据，避免 doc_id/timestamp 缺失导致删除与展示异常
            await self.db_connection.execute("""
            UPDATE documents
            SET doc_id = 'legacy-' || id
            WHERE doc_id IS NULL OR TRIM(doc_id) = ''
        """)
            await self.db_connection.execute("""
            UPDATE documents
            SET created_at = datetime('now')
            WHERE created_at IS NULL OR TRIM(CAST(created_at AS TEXT)) = ''
        """)
            await self.db_connection.execute("""
            UPDATE documents
            SET updated_at = COALESCE(created_at, datetime('now'))
            WHERE updated_at IS NULL OR TRIM(CAST(updated_at AS TEXT)) = ''
        """)

            # 创建索引以提升session_id查询性能
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_doc_metadata
            ON documents(json_extract(metadata, '$.session_id'))
        """)
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_doc_persona_metadata
            ON documents(json_extract(metadata, '$.persona_id'))
        """)
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_doc_importance_metadata
            ON documents(json_extract(metadata, '$.importance'))
        """)
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_doc_last_access_metadata
            ON documents(json_extract(metadata, '$.last_access_time'))
        """)
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_documents_doc_id
            ON documents(doc_id)
        """)

            await self._create_write_ops_table()
            await MemoryIdentityStore.create_tables(self.db_connection)
            await TopicMemoryStore.create_tables(self.db_connection)

            # 创建版本管理表
            await self.db_connection.execute("""
            CREATE TABLE IF NOT EXISTS db_version (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL,
                description TEXT,
                migrated_at TEXT NOT NULL,
                migration_duration_seconds REAL
            )
        """)

            # 创建迁移状态表
            await self.db_connection.execute("""
            CREATE TABLE IF NOT EXISTS migration_status (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)

            await self.db_connection.commit()

            # 检查是否需要初始化版本信息
            cursor = await self.db_connection.execute("SELECT COUNT(*) FROM db_version")
            version_result = await cursor.fetchone()
            version_count = version_result[0] if version_result else 0

            if version_count == 0:
                # 全新数据库，设置初始版本为最新迁移版本
                from datetime import datetime, timezone

                from ...storage.db_migration import DBMigration

                await self.db_connection.execute(
                    """
                    INSERT INTO db_version (version, description, migrated_at, migration_duration_seconds)
                    VALUES (?, ?, ?, ?)
                """,
                    (
                        DBMigration.storage_version(DBMigration.CURRENT_VERSION),
                        "初始版本 - 当前架构",
                        datetime.now(timezone.utc).isoformat(),
                        0.0,
                    ),
                )
                await self.db_connection.commit()

                logger.info(f"已初始化数据库版本信息: v{DBMigration.CURRENT_VERSION}")

    async def _drop_legacy_documents_fts_triggers(self):
        if self.db_connection is None:
            return

        cursor = await self.db_connection.execute("""
            SELECT name FROM sqlite_master
            WHERE type='trigger' AND tbl_name='documents'
              AND sql LIKE '%documents_fts%'
        """)
        rows = await cursor.fetchall()
        for row in rows:
            trigger_name = row[0]
            await self.db_connection.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
            logger.warning(f"已清理旧 LivingMemory FTS 触发器: {trigger_name}")

    # ==================== 核心记忆操作 ====================

    async def add_memory(
        self,
        content: str,
        session_id: str | None = None,
        persona_id: str | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        atoms: list | None = None,
        preserve_create_time: bool = False,
        source_messages: list[Any] | None = None,
        source_retention_reason: str = "importance_threshold",
        schedule_user_profile_projection: bool = True,
    ) -> int:
        """
        添加新记忆

        Args:
            content: 记忆内容
            session_id: 会话ID(支持多种格式,自动提取UUID)
            persona_id: 人格ID(支持多种格式,自动提取UUID)
            importance: 重要性(0-1)
            metadata: 额外元数据
            preserve_create_time: 内部替换操作是否保留 metadata 中的原创建时间
            source_messages: 可选的结构化来源消息；只保存到独立来源快照表

        Returns:
            int: 记忆ID(doc_id)
        """
        if not content or not content.strip():
            raise ValueError("记忆内容不能为空")

        if session_id:
            scope = await self.resolve_session_scope(session_id)
            if scope:
                session_id = scope[0]

        op_id = await self._start_write_op(
            "add",
            {
                "content_preview": content[:500],
                "session_id": session_id,
                "persona_id": persona_id,
                "importance": importance,
                "metadata": metadata or {},
                "atoms": [
                    self._serialize_atom_for_repair(atom) for atom in (atoms or [])
                ],
            },
        )

        # 准备完整元数据 - 保存完整的 unified_msg_origin，不提取UUID
        # 只在查询/过滤时才提取UUID进行匹配，存储时保留完整信息
        current_time = time.time()
        full_metadata = {
            "session_id": session_id,  # 保存完整的 unified_msg_origin
            "persona_id": persona_id,  # 保存完整的 persona_id
            "importance": max(0.0, min(1.0, importance)),  # 限制在0-1范围
            "create_time": current_time,
            "last_access_time": current_time,
        }

        # 合并用户提供的额外元数据
        # 注意：先合并外部metadata，再确保时间字段不被覆盖
        if metadata:
            full_metadata.update(metadata)

        effective_importance = clamp_float(
            full_metadata.get("importance"), default=importance
        )
        full_metadata["importance"] = effective_importance
        full_metadata["base_importance"] = clamp_float(
            full_metadata.get("base_importance"),
            default=effective_importance,
        )
        try:
            importance_revision = int(full_metadata.get("importance_revision", 1))
        except (TypeError, ValueError):
            importance_revision = 1
        full_metadata["importance_revision"] = max(1, importance_revision)
        full_metadata.setdefault("importance_reason", "generated")
        full_metadata.setdefault(
            "importance_policy_version", IMPORTANCE_POLICY_VERSION
        )

        # A confirmed alias only broadens reads for legacy data. New Timeline
        # records and their source spans must consistently use the canonical ID.
        if session_id:
            full_metadata["session_id"] = session_id
            source_window = full_metadata.get("source_window")
            if isinstance(source_window, dict):
                source_window = dict(source_window)
                source_window["session_id"] = session_id
                full_metadata["source_window"] = source_window

        # 普通新增始终使用当前时间；结构化替换可保留原始时间轴位置。
        preserved_create_time = None
        if preserve_create_time and metadata:
            try:
                preserved_create_time = float(metadata.get("create_time"))
            except (TypeError, ValueError):
                preserved_create_time = None
        full_metadata["create_time"] = (
            preserved_create_time
            if preserved_create_time is not None
            else current_time
        )
        full_metadata["last_access_time"] = current_time
        full_metadata = self._apply_stable_identity(
            full_metadata,
            session_id=session_id,
            persona_id=persona_id,
        )
        await self._advance_write_op(
            op_id,
            "identity_prepared",
            payload_patch={"metadata": full_metadata},
        )

        # 通过混合检索器添加(会同时添加到BM25和向量索引)
        if self.hybrid_retriever is None:
            raise RuntimeError("混合检索器未初始化")
        try:
            doc_id = await self.hybrid_retriever.add_memory(content, full_metadata)
            await self._advance_write_op(
                op_id,
                "document_indexed",
                memory_id=doc_id,
                payload_patch={"memory_id": doc_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "document_failed",
                status="failed",
                error=str(e),
            )
            raise

        # 写入记忆原子
        atom_write_failed = False
        if atoms and self.atom_store is not None and self.atom_enabled:
            prepared_atoms = []
            for atom in atoms:
                atom.session_id = session_id or atom.session_id
                atom.persona_id = atom.persona_id or persona_id
                atom.parent_memory_id = doc_id
                prepared_atoms.append(atom)
            try:
                await self.atom_store.insert_many(prepared_atoms)
                await self._advance_write_op(
                    op_id,
                    "atoms_indexed",
                    memory_id=doc_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[MemoryEngine] 批量写入记忆原子失败", exc_info=True)
                failed_atoms: list[dict[str, Any]] = []
                for atom in prepared_atoms:
                    if getattr(atom, "atom_id", 0):
                        continue
                    try:
                        await self.atom_store.insert(atom)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        failed_atoms.append(self._serialize_atom_for_repair(atom))
                        logger.error(
                            f"[MemoryEngine] 写入记忆原子失败: {atom.content[:80]}",
                            exc_info=True,
                        )
                if failed_atoms:
                    await self._advance_write_op(
                        op_id,
                        "atoms_partial",
                        status="needs_repair",
                        memory_id=doc_id,
                        error="atom insert failed",
                        payload_patch={"failed_atoms": failed_atoms},
                    )
                    atom_write_failed = True
                else:
                    await self._advance_write_op(
                        op_id,
                        "atoms_indexed",
                        memory_id=doc_id,
                    )
        else:
            await self._advance_write_op(op_id, "atoms_skipped", memory_id=doc_id)

        needs_repair = atom_write_failed
        if self.graph_memory_manager is not None:
            try:
                await self.graph_memory_manager.index_memory(
                    doc_id, content, full_metadata, atoms
                )
                await self._advance_write_op(
                    op_id,
                    "graph_indexed",
                    status="needs_repair" if needs_repair else "pending",
                    memory_id=doc_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._advance_write_op(
                    op_id,
                    "graph_failed",
                    status="needs_repair",
                    memory_id=doc_id,
                    error=str(e),
                )
                needs_repair = True
                logger.error(
                    f"[MemoryEngine] 图记忆索引失败，已标记待修复 (memory_id={doc_id})",
                    exc_info=True,
                )
        else:
            await self._advance_write_op(
                op_id,
                "graph_skipped",
                status="needs_repair" if needs_repair else "pending",
                memory_id=doc_id,
            )

        identity_registered = False
        try:
            await self._register_memory_identity(doc_id, full_metadata)
            identity_registered = True
            await self._advance_write_op(
                op_id,
                "identity_registered",
                status="needs_repair" if needs_repair else "pending",
                memory_id=doc_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            needs_repair = True
            await self._advance_write_op(
                op_id,
                "identity_failed",
                status="needs_repair",
                memory_id=doc_id,
                error=str(e),
            )
            logger.error(
                f"[MemoryEngine] 逻辑身份注册失败，已标记待修复 (memory_id={doc_id})",
                exc_info=True,
            )

        serialized_source = serialize_source_messages(list(source_messages or []))
        if serialized_source and identity_registered:
            try:
                await self.memory_identity_store.save_source_snapshot(
                    str(full_metadata["memory_uid"]),
                    serialized_source,
                    source_revision=int(full_metadata.get("revision", 1)),
                    retention_reason=source_retention_reason,
                )
                await self._advance_write_op(
                    op_id,
                    "source_snapshot_saved",
                    status="needs_repair" if needs_repair else "pending",
                    memory_id=doc_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                needs_repair = True
                await self._advance_write_op(
                    op_id,
                    "source_snapshot_failed",
                    status="needs_repair",
                    memory_id=doc_id,
                    error=str(e),
                )
                logger.error(
                    f"[MemoryEngine] Timeline 来源快照保存失败，已标记待修复 "
                    f"(memory_id={doc_id})",
                    exc_info=True,
                )

        if not needs_repair:
            await self._advance_write_op(
                op_id,
                "completed",
                status="completed",
                memory_id=doc_id,
            )
        self._schedule_topic_maintenance(
            str(full_metadata.get("memory_space_id") or ""),
            full=False,
            since=current_time - 1.0,
        )
        if schedule_user_profile_projection:
            status = str(full_metadata.get("status") or "active").strip().lower()
            operation = (
                UserProfileProjectionOperation.ARCHIVE
                if status in {"archived", "deleted"}
                else UserProfileProjectionOperation.UPSERT
            )
            await self._queue_user_profile_projection(
                operation=operation,
                metadata=full_metadata,
            )
        self._invalidate_search_cache()
        return doc_id

    async def get_memory_source_snapshot(
        self, memory_id: int
    ) -> dict[str, Any] | None:
        """Return the retained source snapshot for one physical Timeline ID."""
        return await self.memory_identity_store.get_source_snapshot_by_document_id(
            int(memory_id)
        )

    async def get_memory_source(self, memory_id: int) -> list[dict[str, Any]]:
        """Return retained source messages without exposing storage internals."""
        snapshot = await self.get_memory_source_snapshot(memory_id)
        return list(snapshot.get("messages") or []) if snapshot else []

    async def get_memory_transfer_records(
        self, memory_ids: list[int] | None = None
    ) -> list[dict[str, Any]]:
        """Return portable Timeline records without local index identities."""
        if self.db_connection is None:
            return []

        normalized_ids: list[int] | None = None
        if memory_ids is not None:
            normalized_ids = list(dict.fromkeys(int(item) for item in memory_ids))
            if not normalized_ids:
                return []

        async def fetch_rows(batch: list[int] | None):
            where_clause = ""
            params: list[Any] = []
            if batch is not None:
                placeholders = ",".join("?" * len(batch))
                where_clause = f"WHERE d.id IN ({placeholders})"
                params.extend(batch)
            cursor = await self.db_connection.execute(
                f"""
                SELECT d.id, d.text, d.metadata, d.created_at, d.updated_at,
                       ss.source_json
                FROM documents AS d
                LEFT JOIN memory_registry AS r ON r.document_id = d.id
                LEFT JOIN memory_source_snapshots AS ss
                       ON ss.memory_uid = r.memory_uid
                {where_clause}
                ORDER BY d.id ASC
                """,
                params,
            )
            return await cursor.fetchall()

        if normalized_ids is None:
            rows = await fetch_rows(None)
        else:
            rows = []
            for offset in range(0, len(normalized_ids), 500):
                rows.extend(await fetch_rows(normalized_ids[offset : offset + 500]))
            rows.sort(key=lambda row: int(row["id"]))

        records: list[dict[str, Any]] = []
        for row in rows:
            raw_metadata = self._safe_json_dict(row["metadata"])
            metadata = portable_metadata(raw_metadata)
            source_messages: list[dict[str, Any]] = []
            if row["source_json"]:
                try:
                    parsed_source = json.loads(row["source_json"])
                except (json.JSONDecodeError, TypeError):
                    parsed_source = []
                if isinstance(parsed_source, list):
                    source_messages = parsed_source
            records.append(
                {
                    "original_id": int(row["id"]),
                    "content": str(row["text"] or ""),
                    "importance": clamp_float(
                        raw_metadata.get("base_importance", raw_metadata.get("importance")),
                        default=0.5,
                    ),
                    "session_id": raw_metadata.get("session_id"),
                    "persona_id": raw_metadata.get("persona_id"),
                    "metadata": metadata,
                    "source_messages": source_messages,
                    "storage_created_at": row["created_at"],
                    "storage_updated_at": row["updated_at"],
                }
            )
        return records

    async def get_memory_import_keys(self) -> set[tuple[str, str, str]]:
        """Return exact duplicate keys for all existing Timeline memories."""
        if self.db_connection is None:
            return set()
        cursor = await self.db_connection.execute("SELECT text, metadata FROM documents")
        rows = await cursor.fetchall()
        keys: set[tuple[str, str, str]] = set()
        for row in rows:
            metadata = self._safe_json_dict(row["metadata"])
            keys.add(
                memory_import_key(
                    str(row["text"] or ""),
                    metadata.get("session_id"),
                    metadata.get("persona_id"),
                )
            )
        return keys

    async def search_memories(
        self,
        query: str,
        k: int = 5,
        session_id: str | None = None,
        persona_id: str | None = None,
        track_access: bool = True,
        _expand_aliases: bool = True,
    ) -> list[HybridResult]:
        """
        检索相关记忆

        Args:
            query: 查询字符串
            k: 返回数量
            session_id: 会话ID过滤(可选,应传入unified_msg_origin完整格式)
            persona_id: 人格ID过滤(可选)
            track_access: 是否把结果记录为一次真实召回访问

        Returns:
            List[HybridResult]: 检索结果列表
        """
        if not query or not query.strip():
            return []

        if session_id and _expand_aliases:
            session_scope = await self.resolve_session_scope(session_id)
            if len(session_scope) > 1:
                batches = await asyncio.gather(
                    *(
                        self.search_memories(
                            query,
                            k=max(k, k * 2),
                            session_id=scope_session_id,
                            persona_id=persona_id,
                            track_access=False,
                            _expand_aliases=False,
                        )
                        for scope_session_id in session_scope
                    )
                )
                merged: dict[int, HybridResult] = {}
                for batch in batches:
                    for result in batch:
                        previous = merged.get(int(result.doc_id))
                        if previous is None or result.final_score > previous.final_score:
                            merged[int(result.doc_id)] = result
                results = sorted(
                    merged.values(), key=lambda item: item.final_score, reverse=True
                )[:k]
                if track_access:
                    self.record_memory_access([item.doc_id for item in results])
                return results
            if session_scope:
                session_id = session_scope[0]

        cache_key = self._search_cache_key(query, k, session_id, persona_id)
        cached_results = self._get_cached_search_results(cache_key)
        if cached_results is not None:
            if track_access:
                for result in cached_results:
                    self._create_tracked_task(
                        self._update_access_time_internal(result.doc_id)
                    )
            return cached_results

        # 如果session_id是unified_msg_origin格式，自动触发旧数据迁移
        if session_id and ":" in session_id:
            # 异步触发迁移，不阻塞查询
            self._create_tracked_task(self._migrate_session_data_if_needed(session_id))

        # 【关键修改】不再提取UUID，直接使用完整的unified_msg_origin进行匹配
        # 因为现在数据库中存储的就是完整格式
        # session_id 和 persona_id 保持原样传递给检索器

        # 执行混合检索 / 双路检索
        if self.dual_route_retriever is not None:
            results = await self.dual_route_retriever.search(
                query,
                k,
                session_id,
                persona_id,
            )
        else:
            if self.hybrid_retriever is None:
                raise RuntimeError("混合检索器未初始化")
            results = await self.hybrid_retriever.search(
                query, k, session_id, persona_id
            )

        # 异步更新访问时间(不阻塞返回)
        if track_access:
            for result in results:
                self._create_tracked_task(
                    self._update_access_time_internal(result.doc_id)
                )

        self._set_cached_search_results(cache_key, results)
        return results

    def record_memory_access(self, memory_ids: list[int]) -> None:
        """Record access only for memories that survived final recall filtering."""
        seen: set[int] = set()
        for raw_memory_id in memory_ids:
            try:
                memory_id = int(raw_memory_id)
            except (TypeError, ValueError):
                continue
            if memory_id in seen:
                continue
            seen.add(memory_id)
            self._create_tracked_task(self._update_access_time_internal(memory_id))

    async def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        """
        根据ID获取记忆

        Args:
            memory_id: 记忆ID

        Returns:
            Optional[Dict]: 记忆数据,包含text和metadata
        """
        # 从faiss_db的document_storage获取文档
        try:
            # 使用 get_documents (复数) 并传入 ids 参数
            docs = await self.faiss_db.document_storage.get_documents(
                metadata_filters={}, ids=[memory_id], limit=1
            )

            if not docs or len(docs) == 0:
                return None

            doc = docs[0]
            return {
                "id": doc["id"],
                "text": doc["text"],
                "metadata": doc["metadata"],
            }
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[MemoryEngine] 获取记忆详情失败", exc_info=True)
            return None

    async def update_memory(
        self,
        memory_id: int,
        updates: dict[str, Any],
    ) -> bool:
        """
        更新记忆（确保多数据库同步）

        支持更新内容、重要性、元数据等。采用不同策略：
        - 内容更新：先创建后删除（避免数据丢失）+ 全库同步
        - 元数据更新：三库同步更新

        Args:
            memory_id: 记忆ID
            updates: 更新字典,可包含:
                - content: 新内容 (触发完整重建)
                - importance: 新重要性
                - metadata: 元数据更新

        Returns:
            bool: 是否更新成功
        """
        # 获取当前记忆
        memory = await self.get_memory(memory_id)
        if not memory:
            logger.error(f"[更新] 记忆不存在 (memory_id={memory_id})")
            return False

        # 解析 metadata（可能是JSON字符串）
        current_metadata = memory.get("metadata", {})
        if isinstance(current_metadata, str):
            import json

            try:
                current_metadata = json.loads(current_metadata)
            except (json.JSONDecodeError, TypeError):
                current_metadata = {}
        elif not isinstance(current_metadata, dict):
            current_metadata = {}

        # 处理内容更新 (需要重建所有索引)
        if "content" in updates:
            new_content = updates["content"]
            if not new_content or not new_content.strip():
                return False

            try:
                importance = clamp_float(
                    updates.get("importance", current_metadata.get("importance", 0.5)),
                    default=0.5,
                )

                # Legacy content-only callers cannot provide a newly classified
                # fact set. Clear every stale derived field so old facts/topics
                # cannot survive in graph entries or prompt injection.
                new_metadata = current_metadata.copy()
                new_metadata["updated_at"] = time.time()
                new_metadata["canonical_summary"] = new_content
                new_metadata["persona_summary"] = new_content
                new_metadata["topics"] = []
                new_metadata["key_facts"] = []

                logger.info(f"[更新] 开始安全内容替换 (old_id={memory_id})")
                new_memory_id = await self.replace_memory(
                    memory_id,
                    content=new_content,
                    metadata=new_metadata,
                    importance=importance,
                    atoms=[],
                )
                logger.info(
                    f"[更新] 内容更新完成 (old_id={memory_id} → new_id={new_memory_id})"
                )
                self._invalidate_search_cache()
                return True

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"[更新] 内容更新失败 (memory_id={memory_id}): {e}", exc_info=True
                )
                return False

        # 处理非内容的元数据更新（不需要重建索引）
        metadata_updates = {}

        if "importance" in updates:
            explicit_importance = clamp_float(
                updates["importance"], default=0.5
            )
            try:
                current_importance_revision = int(
                    current_metadata.get("importance_revision", 1)
                )
            except (TypeError, ValueError):
                current_importance_revision = 1
            metadata_updates.update(
                {
                    "importance": explicit_importance,
                    "base_importance": explicit_importance,
                    "importance_revision": current_importance_revision + 1,
                    "importance_reason": "manual",
                    "importance_anchor_at": time.time(),
                    "importance_policy_version": IMPORTANCE_POLICY_VERSION,
                }
            )

        if "metadata" in updates:
            metadata_updates.update(updates["metadata"])

        if metadata_updates:
            # 确保 current_metadata 是字典（再次检查）
            if not isinstance(current_metadata, dict):
                import json

                try:
                    current_metadata = (
                        json.loads(current_metadata)
                        if isinstance(current_metadata, str)
                        else {}
                    )
                except (json.JSONDecodeError, TypeError):
                    current_metadata = {}

            old_status = str(current_metadata.get("status") or "active").strip().lower()

            # 合并元数据
            current_metadata.update(metadata_updates)
            current_metadata["updated_at"] = time.time()
            projection_source_fields = {
                "status",
                "key_facts",
                "key_fact_evidence",
                "key_fact_attributions",
                "key_fact_profiles",
                "key_fact_temporal",
                "role_bindings",
                "summary_quality_report",
                "quality_report",
            }
            if projection_source_fields & metadata_updates.keys():
                try:
                    current_revision = int(current_metadata.get("revision", 1))
                except (TypeError, ValueError):
                    current_revision = 1
                current_metadata["revision"] = max(1, current_revision) + 1
            current_metadata = self._apply_stable_identity(
                current_metadata,
                session_id=current_metadata.get("session_id"),
                persona_id=current_metadata.get("persona_id"),
            )
            metadata_updates.update(
                {
                    "memory_uid": current_metadata["memory_uid"],
                    "revision": current_metadata["revision"],
                    "memory_layer": current_metadata["memory_layer"],
                    "memory_space_id": current_metadata["memory_space_id"],
                    "memory_space_version": current_metadata["memory_space_version"],
                    "updated_at": current_metadata["updated_at"],
                }
            )

            # 【改进】使用增强的update_metadata确保三库同步
            if self.hybrid_retriever is None:
                logger.error("混合检索器未初始化")
                return False
            success = await self.hybrid_retriever.update_metadata(
                memory_id, metadata_updates
            )

            if success:
                logger.info(f"[更新] 元数据更新成功 (memory_id={memory_id})")
                if self.graph_memory_manager is not None:
                    await self.graph_memory_manager.index_memory(
                        memory_id,
                        memory["text"],
                        current_metadata,
                    )
                await self._register_memory_identity(memory_id, current_metadata)
                topic_source_fields = {
                    "canonical_summary",
                    "persona_summary",
                    "topics",
                    "key_facts",
                }
                new_status = str(
                    current_metadata.get("status") or "active"
                ).strip().lower()
                status_changed = old_status != new_status
                if status_changed and new_status in {"archived", "deleted"}:
                    memory_uid = str(current_metadata.get("memory_uid") or "")
                    affected_topic_uids = await self._mark_dependent_topics_stale(
                        memory_uid,
                        reason=f"timeline_deleted_status_{new_status}",
                    )
                    await self._queue_deleted_timeline_repair(
                        str(current_metadata.get("memory_space_id") or ""),
                        deleted_timeline_uids=[memory_uid],
                        affected_topic_uids=affected_topic_uids,
                    )
                elif status_changed and new_status == "active":
                    self._schedule_topic_maintenance(
                        str(current_metadata.get("memory_space_id") or ""),
                        full=False,
                        timeline_uids=[
                            str(current_metadata.get("memory_uid") or "")
                        ],
                    )
                elif (
                    new_status == "active"
                    and topic_source_fields & metadata_updates.keys()
                ):
                    await self._mark_dependent_topics_stale(
                        str(current_metadata.get("memory_uid") or ""),
                        reason="timeline_metadata_updated",
                    )
                    self._schedule_topic_maintenance(
                        str(current_metadata.get("memory_space_id") or ""),
                        full=False,
                        timeline_uids=[
                            str(current_metadata.get("memory_uid") or "")
                        ],
                    )
                profile_operation = UserProfileProjectionOperation.UPSERT
                if new_status in {"archived", "deleted"}:
                    profile_operation = UserProfileProjectionOperation.ARCHIVE
                elif status_changed and new_status == "active":
                    profile_operation = UserProfileProjectionOperation.RESTORE
                await self._queue_user_profile_projection(
                    operation=profile_operation,
                    metadata=current_metadata,
                )
                self._invalidate_search_cache()
            else:
                logger.error(f"[更新] 元数据更新失败 (memory_id={memory_id})")

            return success

        return True

    async def rewrite_memory_in_place(
        self,
        memory_id: int,
        *,
        content: str,
        metadata: dict[str, Any],
        importance: float,
        atoms: list | None = None,
        schedule_topic_maintenance: bool = True,
        source_messages: list[Any] | None = None,
        source_retention_reason: str = "timeline_rebuild",
    ) -> int:
        """Rebuild a memory and every derived index while preserving its ID."""
        current = await self.get_memory(memory_id)
        if not current:
            raise ValueError(f"记忆不存在 (memory_id={memory_id})")
        if not content or not content.strip():
            raise ValueError("记忆内容不能为空")
        if self.hybrid_retriever is None:
            raise RuntimeError("混合检索器未初始化")

        current_metadata = self._safe_json_dict(current.get("metadata"))
        current_metadata = self._apply_stable_identity(
            current_metadata,
            session_id=current_metadata.get("session_id"),
            persona_id=current_metadata.get("persona_id"),
        )
        replacement_metadata = dict(metadata or {})
        for preserved_key in (
            "session_id",
            "persona_id",
            "source_window",
            "create_time",
            "memory_layer",
        ):
            if (
                preserved_key not in replacement_metadata
                and preserved_key in current_metadata
            ):
                replacement_metadata[preserved_key] = current_metadata[preserved_key]
        replacement_metadata["memory_uid"] = current_metadata.get(
            "memory_uid"
        ) or str(uuid.uuid4())
        try:
            current_revision = int(current_metadata.get("revision", 1))
        except (TypeError, ValueError):
            current_revision = 1
        replacement_metadata["revision"] = current_revision + 1
        replacement_metadata["updated_at"] = time.time()
        rebuilt_importance = clamp_float(importance, default=0.5)
        try:
            importance_revision = int(
                current_metadata.get("importance_revision", 1)
            )
        except (TypeError, ValueError):
            importance_revision = 1
        replacement_metadata.update(
            {
                "importance": rebuilt_importance,
                "base_importance": rebuilt_importance,
                "importance_revision": importance_revision + 1,
                "importance_reason": "timeline_rebuilt",
                "importance_anchor_at": replacement_metadata["updated_at"],
                "importance_policy_version": IMPORTANCE_POLICY_VERSION,
            }
        )
        replacement_metadata = self._apply_stable_identity(
            replacement_metadata,
            session_id=replacement_metadata.get("session_id")
            or current_metadata.get("session_id"),
            persona_id=replacement_metadata.get("persona_id")
            or current_metadata.get("persona_id"),
        )

        old_content = str(current.get("text") or "")
        old_atoms: list = []
        if self.atom_store is not None:
            old_atoms = await self.atom_store.get_by_parent(memory_id)

        hybrid_updated = False
        atoms_updated = False

        async def rollback() -> None:
            try:
                if hybrid_updated:
                    await self.hybrid_retriever.replace_memory_in_place(
                        memory_id,
                        old_content,
                        current_metadata,
                    )
                if atoms_updated and self.atom_store is not None:
                    await self.atom_store.replace_by_parent(memory_id, old_atoms)
                if self.graph_memory_manager is not None and (
                    hybrid_updated or atoms_updated
                ):
                    await self.graph_memory_manager.index_memory(
                        memory_id,
                        old_content,
                        current_metadata,
                        old_atoms or None,
                    )
                await self._register_memory_identity(memory_id, current_metadata)
            except Exception:
                logger.error(
                    f"[原位更新] 回滚不完整 (memory_id={memory_id})",
                    exc_info=True,
                )

        try:
            hybrid_updated = await self.hybrid_retriever.replace_memory_in_place(
                memory_id,
                content,
                replacement_metadata,
            )
            if not hybrid_updated:
                raise RuntimeError("正文或检索索引原位更新失败")

            if self.atom_store is not None:
                await self.atom_store.replace_by_parent(memory_id, list(atoms or []))
                atoms_updated = True

            if self.graph_memory_manager is not None:
                await self.graph_memory_manager.index_memory(
                    memory_id,
                    content,
                    replacement_metadata,
                    atoms,
                )

            await self._register_memory_identity(memory_id, replacement_metadata)
            serialized_source = serialize_source_messages(list(source_messages or []))
            if serialized_source:
                await self.memory_identity_store.save_source_snapshot(
                    str(replacement_metadata["memory_uid"]),
                    serialized_source,
                    source_revision=int(replacement_metadata.get("revision", 1)),
                    retention_reason=source_retention_reason,
                )
            await self._mark_dependent_topics_stale(
                str(replacement_metadata.get("memory_uid") or ""),
                reason="timeline_rewritten_in_place",
            )
            if schedule_topic_maintenance:
                self._schedule_topic_maintenance(
                    str(replacement_metadata.get("memory_space_id") or ""),
                    full=False,
                    timeline_uids=[
                        str(replacement_metadata.get("memory_uid") or "")
                    ],
                )
            await self._queue_user_profile_projection(
                operation=UserProfileProjectionOperation.UPSERT,
                metadata=replacement_metadata,
            )

            self._invalidate_search_cache()
            logger.info(f"[原位更新] 记忆及派生索引更新完成 (memory_id={memory_id})")
            return memory_id
        except asyncio.CancelledError:
            await asyncio.shield(rollback())
            self._invalidate_search_cache()
            raise
        except Exception:
            logger.error(
                f"[原位更新] 派生索引更新失败，开始回滚 (memory_id={memory_id})",
                exc_info=True,
            )
            await rollback()
            self._invalidate_search_cache()
            raise

    async def replace_memory(
        self,
        memory_id: int,
        *,
        content: str,
        metadata: dict[str, Any],
        importance: float,
        atoms: list | None = None,
    ) -> int:
        """Atomically-as-possible replace a memory and all derived indexes.

        FAISS and SQLite cannot share one transaction, so replacement follows the
        existing create-first strategy and rolls the new record back if deleting
        the old record fails. A stable memory_uid and monotonically increasing
        revision preserve logical identity across physical document IDs.
        """
        current = await self.get_memory(memory_id)
        if not current:
            raise ValueError(f"记忆不存在 (memory_id={memory_id})")
        if not content or not content.strip():
            raise ValueError("记忆内容不能为空")

        current_metadata = self._safe_json_dict(current.get("metadata"))
        current_metadata = self._apply_stable_identity(
            current_metadata,
            session_id=current_metadata.get("session_id"),
            persona_id=current_metadata.get("persona_id"),
        )
        replacement_metadata = dict(metadata or {})
        for preserved_key in (
            "session_id",
            "persona_id",
            "source_window",
            "memory_layer",
        ):
            if (
                preserved_key not in replacement_metadata
                and preserved_key in current_metadata
            ):
                replacement_metadata[preserved_key] = current_metadata[preserved_key]
        replacement_metadata["memory_uid"] = current_metadata.get(
            "memory_uid"
        ) or str(uuid.uuid4())
        try:
            current_revision = int(current_metadata.get("revision", 1))
        except (TypeError, ValueError):
            current_revision = 1
        replacement_metadata["revision"] = current_revision + 1
        replacement_metadata["previous_id"] = memory_id
        replacement_metadata["updated_at"] = time.time()
        replacement_importance = clamp_float(importance, default=0.5)
        replacement_metadata["importance"] = replacement_importance
        replacement_metadata["base_importance"] = replacement_importance
        try:
            importance_revision = int(
                current_metadata.get("importance_revision", 1) or 1
            )
        except (TypeError, ValueError):
            importance_revision = 1
        replacement_metadata["importance_revision"] = importance_revision + 1
        replacement_metadata["importance_reason"] = "memory_replaced"
        replacement_metadata["importance_anchor_at"] = time.time()
        replacement_metadata["importance_policy_version"] = (
            IMPORTANCE_POLICY_VERSION
        )
        # Physical replacement must keep the logical memory on its original
        # timeline even when callers only provide newly generated metadata.
        replacement_metadata["create_time"] = current_metadata.get("create_time")

        session_id = replacement_metadata.get("session_id") or current_metadata.get(
            "session_id"
        )
        persona_id = replacement_metadata.get("persona_id") or current_metadata.get(
            "persona_id"
        )
        new_memory_id: int | None = None
        try:
            new_memory_id = await self.add_memory(
                content=content,
                session_id=session_id,
                persona_id=persona_id,
                importance=replacement_importance,
                metadata=replacement_metadata,
                atoms=atoms,
                preserve_create_time=True,
                schedule_user_profile_projection=False,
            )
            if new_memory_id is None:
                raise RuntimeError("新记忆创建失败")
            if not await self._delete_memory_without_profile_projection(memory_id):
                await self._delete_memory_without_profile_projection(new_memory_id)
                await self._register_memory_identity(memory_id, current_metadata)
                raise RuntimeError("旧记忆删除失败，已回滚新记忆")
            await self._mark_dependent_topics_stale(
                str(replacement_metadata.get("memory_uid") or ""),
                reason="timeline_replaced",
            )
            await self._queue_user_profile_projection(
                operation=UserProfileProjectionOperation.UPSERT,
                metadata=replacement_metadata,
            )
            return new_memory_id
        except asyncio.CancelledError:
            raise
        except Exception:
            if new_memory_id is not None:
                try:
                    if await self.get_memory(new_memory_id):
                        await self._delete_memory_without_profile_projection(new_memory_id)
                except Exception:
                    logger.error(
                        f"[替换] 回滚新记忆失败 (memory_id={new_memory_id})",
                        exc_info=True,
                    )
            if await self.get_memory(memory_id):
                await self._register_memory_identity(memory_id, current_metadata)
            raise

    async def delete_memory(self, memory_id: int) -> bool:
        """
        删除记忆

        Args:
            memory_id: 记忆ID

        Returns:
            bool: 是否删除成功
        """

        registry_record = await self.memory_identity_store.get_by_document_id(memory_id)
        op_id = await self._start_write_op(
            "delete",
            {"memory_id": memory_id},
            memory_id=memory_id,
        )

        # hybrid_retriever.delete_memory() 内部已按顺序删除 BM25、向量索引和 documents 表
        if self.hybrid_retriever is None:
            logger.error("混合检索器未初始化")
            await self._advance_write_op(
                op_id,
                "document_delete_failed",
                status="failed",
                error="hybrid retriever not initialized",
            )
            return False
        success = await self.hybrid_retriever.delete_memory(memory_id)
        if not success:
            await self._advance_write_op(
                op_id,
                "document_delete_failed",
                status="failed",
                error="document/vector delete failed",
            )
            return False

        await self._advance_write_op(op_id, "document_deleted", memory_id=memory_id)

        needs_repair = False
        try:
            if self.graph_memory_manager is not None:
                await self.graph_memory_manager.delete_memory(memory_id)
            await self._advance_write_op(op_id, "graph_deleted", memory_id=memory_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "graph_delete_failed",
                status="needs_repair",
                memory_id=memory_id,
                error=str(e),
            )
            needs_repair = True
            logger.error(
                f"[MemoryEngine] 图记忆删除失败，已标记待修复 (memory_id={memory_id})",
                exc_info=True,
            )

        try:
            if self.atom_store is not None:
                await self.atom_store.delete_by_parent(memory_id)
            await self._advance_write_op(op_id, "atoms_deleted", memory_id=memory_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "atom_delete_failed",
                status="needs_repair",
                memory_id=memory_id,
                error=str(e),
            )
            needs_repair = True
            logger.error(
                f"[MemoryEngine] 记忆原子删除失败，已标记待修复 (memory_id={memory_id})",
                exc_info=True,
            )

        affected_topic_uids: list[str] = []
        try:
            affected_topic_uids = await self._mark_dependent_topics_stale(
                registry_record.memory_uid if registry_record else None,
                reason="timeline_deleted",
            )
            await self.memory_identity_store.delete_by_document_id(memory_id)
            await self._advance_write_op(op_id, "identity_deleted", memory_id=memory_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "identity_delete_failed",
                status="needs_repair",
                memory_id=memory_id,
                error=str(e),
            )
            needs_repair = True
            logger.error(
                f"[MemoryEngine] 逻辑身份删除失败，已标记待修复 (memory_id={memory_id})",
                exc_info=True,
            )

        if not needs_repair:
            await self._advance_write_op(
                op_id,
                "completed",
                status="completed",
                memory_id=memory_id,
            )
        if registry_record and affected_topic_uids:
            await self._queue_deleted_timeline_repair(
                registry_record.memory_space_id,
                deleted_timeline_uids=[registry_record.memory_uid],
                affected_topic_uids=affected_topic_uids,
            )
        if registry_record:
            await self._queue_user_profile_projection(
                operation=UserProfileProjectionOperation.DELETE,
                timeline_uid=registry_record.memory_uid,
                timeline_revision=registry_record.revision,
                memory_space_id=registry_record.memory_space_id,
            )
        self._invalidate_search_cache()
        return success

    async def rebuild_graph_index(self) -> dict[str, int]:
        """Rebuild graph-memory artifacts from stored documents."""
        if self.graph_memory_manager is None:
            return {"rebuilt": 0, "skipped": 0}

        total_count = await self.faiss_db.document_storage.count_documents(
            metadata_filters={}
        )
        batch_size = 200
        offset = 0
        rebuilt = 0
        skipped = 0

        while offset < total_count:
            docs = await self.faiss_db.document_storage.get_documents(
                metadata_filters={},
                limit=batch_size,
                offset=offset,
            )
            if not docs:
                break

            for doc in docs:
                metadata = doc.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}
                elif not isinstance(metadata, dict):
                    metadata = {}
                content = str(doc.get("text") or "")
                if not content.strip():
                    skipped += 1
                    continue
                await self.graph_memory_manager.index_memory(
                    doc["id"], content, metadata
                )
                rebuilt += 1

            offset += batch_size

        self._invalidate_search_cache()
        return {"rebuilt": rebuilt, "skipped": skipped}

    # ==================== 高级功能 ====================

    async def update_importance(self, memory_id: int, new_importance: float) -> bool:
        """
        更新记忆重要性

        Args:
            memory_id: 记忆ID
            new_importance: 新重要性值(0-1)

        Returns:
            bool: 是否更新成功
        """
        return await self.update_memory(memory_id, {"importance": new_importance})

    async def apply_daily_decay(self, decay_rate: float, days: int = 1) -> int:
        """
        批量应用重要性衰减

        Args:
            decay_rate: 每日衰减率 (0-1)
            days: 衰减天数（用于补偿错过的天数）

        Returns:
            int: 受影响的记忆数量
        """
        if decay_rate <= 0 or days <= 0:
            return 0

        if self.db_connection is None:
            logger.error("[衰减] 数据库连接未初始化")
            return 0

        try:
            if decay_rate >= 1:
                decay_rate = 1.0
            access_window_days = float(
                self.config.get("access_decay_window_days", 30.0)
            )
            max_access_count = float(self.config.get("access_decay_max_count", 10.0))
            access_decay_multiplier = float(
                self.config.get("access_count_decay_multiplier", 0.5)
            )
            access_window_start = time.time() - max(1.0, access_window_days) * 86400.0
            access_decay_multiplier = max(0.0, min(1.0, access_decay_multiplier))
            cursor = await self.db_connection.execute(
                "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.importance') IS NOT NULL OR metadata LIKE '%\"importance\"%'"
            )
            rows = await cursor.fetchall()
            updates: list[tuple[str, int]] = []

            for row in rows:
                metadata = self._safe_json_dict(row["metadata"])
                importance = clamp_float(metadata.get("importance"), default=0.5)
                access_count = safe_float(metadata.get("access_count"), 0.0)
                last_access_time = safe_float(metadata.get("last_access_time"), 0.0)

                recent_access_factor = (
                    1.0 if last_access_time >= access_window_start else 0.5
                )
                access_factor = min(1.0, access_count / max(1.0, max_access_count))
                effective_decay_rate = decay_rate * (
                    1 - 0.5 * access_factor * recent_access_factor
                )
                decay_factor = (1 - effective_decay_rate) ** days
                metadata["importance"] = max(
                    0.01,
                    round(importance * decay_factor, 4),
                )
                metadata["access_count"] = int(access_count * access_decay_multiplier)
                updates.append(
                    (json.dumps(metadata, ensure_ascii=False), int(row["id"]))
                )

            if not updates:
                return 0

            await self.db_connection.executemany(
                "UPDATE documents SET metadata = ? WHERE id = ?",
                updates,
            )

            await self.db_connection.commit()
            affected = len(updates)

            logger.info(
                f"[衰减] 批量衰减完成: 衰减率={decay_rate}, 天数={days}, "
                f"访问窗口={access_window_days:.1f}天, 影响记录={affected}"
            )

            self._invalidate_search_cache()
            return affected

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[衰减] 批量衰减失败: {e}", exc_info=True)
            return 0

    async def update_access_time(self, memory_id: int) -> bool:
        """
        更新最后访问时间

        Args:
            memory_id: 记忆ID

        Returns:
            bool: 是否更新成功
        """
        return await self._update_access_time_internal(memory_id)

    async def _update_access_time_internal(self, memory_id: int) -> bool:
        """内部方法:更新访问时间（直接更新documents表，不经过FAISS）"""
        import json

        current_time = time.time()

        try:
            if self.db_connection is None:
                return False

            # 直接更新 documents 表，不经过 FAISS
            # 1. 获取当前 metadata
            cursor = await self.db_connection.execute(
                "SELECT metadata FROM documents WHERE id = ?", (memory_id,)
            )
            row = await cursor.fetchone()

            if not row:
                return False

            # 2. 解析并更新 metadata
            metadata_str = row[0] if row and row[0] else "{}"
            try:
                metadata = (
                    json.loads(metadata_str)
                    if isinstance(metadata_str, str)
                    else metadata_str
                )
                if not isinstance(metadata, dict):
                    metadata = {}
            except (json.JSONDecodeError, TypeError):
                metadata = {}

            metadata["last_access_time"] = current_time
            try:
                access_count = int(metadata.get("access_count", 0) or 0)
            except (TypeError, ValueError):
                access_count = 0
            metadata["access_count"] = min(access_count + 1, 1_000_000)

            # 3. 写回 documents 表
            await self.db_connection.execute(
                "UPDATE documents SET metadata = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), memory_id),
            )
            await self.db_connection.commit()

            return True

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 记录错误但不影响查询流程
            logger.warning(
                f"更新访问时间失败 (memory_id={memory_id}): {e}",
                exc_info=True,
            )
            return False

    async def get_session_memories(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        获取会话的所有记忆（使用分批处理和数据库排序优化）

        Args:
            session_id: 会话ID(应传入完整的unified_msg_origin格式)
            limit: 限制数量

        Returns:
            List[Dict]: 记忆列表
        """
        # 【关键修改】不再提取UUID，直接使用完整的session_id进行匹配
        # 因为现在数据库中存储的就是完整的unified_msg_origin格式

        # 使用数据库层面的排序和分页，避免加载所有数据
        try:
            # 先获取总数判断是否需要分批
            total_count = await self.faiss_db.document_storage.count_documents(
                metadata_filters={"session_id": session_id}
            )

            if total_count == 0:
                return []

            # 如果总数小于等于limit，直接一次性获取
            if total_count <= limit:
                all_docs = await self.faiss_db.document_storage.get_documents(
                    metadata_filters={"session_id": session_id},
                    limit=limit,
                    offset=0,
                )
                # 通过线程池批量规范化 metadata（避免大量 json.loads 阻塞事件循环）
                all_docs = await asyncio.to_thread(
                    self._normalize_batch_metadata, all_docs
                )
                sorted_docs = sorted(
                    all_docs,
                    key=lambda d: safe_float(
                        d.get("metadata", {}).get("create_time"), 0.0
                    ),
                    reverse=True,
                )
            else:
                all_docs = []
                batch_size = 500
                offset = 0

                while offset < total_count:
                    batch = await self.faiss_db.document_storage.get_documents(
                        metadata_filters={"session_id": session_id},
                        limit=batch_size,
                        offset=offset,
                    )

                    if not batch:
                        break

                    batch = await asyncio.to_thread(
                        self._normalize_batch_metadata, batch
                    )
                    all_docs.extend(batch)
                    offset += batch_size

                sorted_docs = sorted(
                    all_docs,
                    key=lambda d: safe_float(
                        d.get("metadata", {}).get("create_time"), 0.0
                    ),
                    reverse=True,
                )[:limit]

            memories = []
            for doc in sorted_docs:
                memories.append(
                    {
                        "id": doc["id"],
                        "text": doc["text"],
                        "metadata": doc["metadata"],
                    }
                )

            return memories
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                f"[MemoryEngine] 获取会话记忆失败 (session_id={session_id})",
                exc_info=True,
            )
            return []

    async def batch_delete_memories(self, memory_ids: list[int]) -> int:
        """Batch delete multiple memories using bulk SQL operations."""
        if not memory_ids:
            return 0

        if self.db_connection is None:
            logger.error("[批量删除] 数据库连接未初始化")
            return 0

        self._invalidate_search_cache()
        total_deleted = 0
        sql_batch_size = 200

        for i in range(0, len(memory_ids), sql_batch_size):
            batch = memory_ids[i : i + sql_batch_size]
            placeholders = ",".join("?" * len(batch))
            op_id = await self._start_write_op(
                "batch_delete",
                {
                    "memory_ids": batch,
                    "batch_offset": i,
                    "batch_size": len(batch),
                },
            )
            batch_deleted = 0
            vector_delete_error: str | None = None
            failed_vector_doc_uuids: list[str] = []

            try:
                registry_cursor = await self.db_connection.execute(
                    f"SELECT memory_uid, memory_space_id, revision FROM memory_registry "
                    f"WHERE document_id IN ({placeholders})",
                    batch,
                )
                registry_rows = await registry_cursor.fetchall()
                batch_memory_uids = [str(row["memory_uid"]) for row in registry_rows]
                deleted_by_space: dict[str, list[str]] = {}
                for row in registry_rows:
                    deleted_by_space.setdefault(
                        str(row["memory_space_id"]), []
                    ).append(str(row["memory_uid"]))
                # 1. Batch delete from BM25 FTS
                await self.db_connection.execute(
                    f"DELETE FROM livingmemory_memories_fts WHERE doc_id IN ({placeholders})",
                    batch,
                )
                await self._advance_write_op(
                    op_id,
                    "bm25_deleted",
                    payload_patch={"memory_ids": batch},
                )

                # 2. Look up UUIDs and delete from FAISS vector DB
                cursor = await self.db_connection.execute(
                    f"SELECT id, doc_id FROM documents WHERE id IN ({placeholders})",
                    batch,
                )
                uuid_rows = await cursor.fetchall()
                found_ids = [int(row["id"]) for row in uuid_rows]
                if found_ids:
                    try:
                        deleted_vector_ids = (
                            await self.vector_retriever.delete_documents(found_ids)
                        )
                        if set(deleted_vector_ids) != set(found_ids):
                            raise RuntimeError(
                                "批量向量删除不完整: "
                                f"expected={found_ids}, deleted={deleted_vector_ids}"
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        vector_delete_error = str(exc)
                        failed_vector_doc_uuids = [
                            str(row["doc_id"])
                            for row in uuid_rows
                            if row["doc_id"]
                        ]
                        logger.warning(
                            "[批量删除] 向量索引删除失败，已继续删除主记录并标记待修复",
                            exc_info=True,
                        )
                await self._advance_write_op(
                    op_id,
                    "faiss_deleted",
                    payload_patch={
                        "memory_ids": batch,
                        "found_ids": found_ids,
                        "failed_vector_doc_uuids": failed_vector_doc_uuids,
                    },
                )

                # 3. Batch delete from documents table
                cursor = await self.db_connection.execute(
                    f"DELETE FROM documents WHERE id IN ({placeholders})",
                    batch,
                )
                await self.db_connection.commit()
                # FaissVecDB 与本引擎共享 documents 表；向量删除可能已移除
                # 对应行，因此以删除前查到的记录数作为准确结果。
                batch_deleted = len(found_ids)
                await self._advance_write_op(
                    op_id,
                    "documents_deleted",
                    payload_patch={
                        "memory_ids": batch,
                        "found_ids": found_ids,
                        "deleted_count": batch_deleted,
                    },
                )

                # 4. Batch delete graph artifacts and atoms
                await self._delete_graph_and_atoms_for_batch(batch)
                await self._advance_write_op(
                    op_id,
                    "graph_atoms_deleted",
                    payload_patch={"memory_ids": batch, "deleted_count": batch_deleted},
                )

                # 5. Remove logical identities that still point at deleted IDs.
                affected_by_space: dict[str, set[str]] = {}
                space_by_uid = {
                    str(row["memory_uid"]): str(row["memory_space_id"])
                    for row in registry_rows
                }
                for memory_uid in batch_memory_uids:
                    affected = await self._mark_dependent_topics_stale(
                        memory_uid,
                        reason="timeline_batch_deleted",
                    )
                    affected_by_space.setdefault(
                        space_by_uid[memory_uid], set()
                    ).update(affected)
                await self.memory_identity_store.delete_by_document_ids(batch)
                for memory_space_id, deleted_uids in deleted_by_space.items():
                    await self._queue_deleted_timeline_repair(
                        memory_space_id,
                        deleted_timeline_uids=deleted_uids,
                        affected_topic_uids=affected_by_space.get(
                            memory_space_id, set()
                        ),
                    )
                for row in registry_rows:
                    await self._queue_user_profile_projection(
                        operation=UserProfileProjectionOperation.DELETE,
                        timeline_uid=str(row["memory_uid"]),
                        timeline_revision=int(row["revision"]),
                        memory_space_id=str(row["memory_space_id"]),
                    )
                await self._advance_write_op(
                    op_id,
                    "identities_deleted",
                    payload_patch={"memory_ids": batch, "deleted_count": batch_deleted},
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._advance_write_op(
                    op_id,
                    "batch_delete_failed",
                    status="needs_repair",
                    error=str(e),
                    payload_patch={
                        "memory_ids": batch,
                        "deleted_count": batch_deleted,
                    },
                )
                logger.error(
                    f"[批量删除] 批次删除失败 (offset={i}, size={len(batch)})",
                    exc_info=True,
                )
                raise

            if vector_delete_error:
                await self._advance_write_op(
                    op_id,
                    "vector_delete_failed",
                    status="needs_repair",
                    error=vector_delete_error,
                    payload_patch={
                        "memory_ids": batch,
                        "deleted_count": batch_deleted,
                        "failed_vector_doc_uuids": failed_vector_doc_uuids,
                    },
                )
            else:
                await self._advance_write_op(
                    op_id,
                    "completed",
                    status="completed",
                    payload_patch={
                        "memory_ids": batch,
                        "deleted_count": batch_deleted,
                    },
                )
            total_deleted += batch_deleted

        if total_deleted:
            logger.info(f"[批量删除] 共删除 {total_deleted} 条记忆")
        return total_deleted

    async def cleanup_old_memories(
        self,
        days_threshold: int | None = None,
        importance_threshold: float | None = None,
    ) -> int:
        """
        清理旧记忆（使用分批处理避免内存问题）

        删除超过阈值且重要性低的记忆

        Args:
            days_threshold: 天数阈值,默认从配置读取
            importance_threshold: 重要性阈值,默认从配置读取

        Returns:
            int: 删除的记忆数量
        """
        # 使用配置或参数值
        days = (
            self.config.get("cleanup_days_threshold", 30)
            if days_threshold is None
            else days_threshold
        )
        importance = (
            self.config.get("cleanup_importance_threshold", 0.3)
            if importance_threshold is None
            else importance_threshold
        )
        try:
            days = int(days)
            importance = float(importance)
        except (TypeError, ValueError):
            logger.error(
                f"清理参数格式错误: days_threshold={days}, importance_threshold={importance}"
            )
            return 0

        if days < 0:
            logger.error(f"清理参数无效: days_threshold={days}（必须 >= 0）")
            return 0

        cutoff_time = time.time() - (days * 86400)

        # 分批扫描文档并删除，避免一次性加载所有数据到内存
        try:
            # 先获取总数
            total_count = await self.faiss_db.document_storage.count_documents(
                metadata_filters={}
            )

            if total_count == 0:
                return 0

            batch_size = 500
            offset = 0
            to_delete_ids: list[int] = []

            # First pass: scan candidates without deleting to avoid offset-shift skips.
            while offset < total_count:
                batch_docs = await self.faiss_db.document_storage.get_documents(
                    metadata_filters={}, limit=batch_size, offset=offset
                )

                if not batch_docs:
                    break

                batch_docs = await asyncio.to_thread(
                    self._normalize_batch_metadata, batch_docs
                )

                for doc in batch_docs:
                    metadata = doc["metadata"]

                    create_time = safe_float(metadata.get("create_time"), time.time())
                    last_access_time = safe_float(
                        metadata.get("last_access_time"), 0.0
                    )
                    age_anchor = max(create_time, last_access_time)
                    doc_importance = clamp_float(
                        metadata.get("importance"), default=0.5
                    )

                    if age_anchor < cutoff_time and doc_importance < importance:
                        to_delete_ids.append(doc["id"])

                offset += len(batch_docs)
                if len(batch_docs) < batch_size:
                    break

            if not to_delete_ids:
                return 0

            logger.info(f"[清理] 发现 {len(to_delete_ids)} 条候选记忆，开始批量删除")
            deleted_count = await self.batch_delete_memories(to_delete_ids)
            logger.info(f"[清理] 完成，已删除 {deleted_count} 条旧记忆")

            return deleted_count
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[清理] 清理旧记忆失败", exc_info=True)
            return 0

    async def _migrate_session_data_if_needed(self, unified_msg_origin: str) -> None:
        """
        运行时自动迁移：将旧格式的session_id更新为unified_msg_origin格式

        支持各种平台的旧格式（通用匹配策略）：
        - WebChat UUID: "ac8c2cef-959e-4146-ad22-c82d0230ad06"
        - WebChat带前缀: "webchat!astrbot!ac8c2cef-959e-4146-ad22-c82d0230ad06"
        - QQ号: "123456789"
        - 其他平台: 任意字符串

        目标格式: "platform:message_type:session_id"

        策略：
        1. 从unified_msg_origin解析出：platform、message_type、session_id
        2. 生成所有可能的旧格式匹配候选（递归拆分）
        3. 查找匹配任一候选且不含冒号的旧记录
        4. 批量更新为unified_msg_origin
        5. 使用unified_msg_origin本身作为迁移标记（避免重复）

        Args:
            unified_msg_origin: 完整的统一消息来源（格式：platform:type:session_id）
        """

        try:
            async with self._session_migration_lock:
                if unified_msg_origin in self._session_migration_checked:
                    return
                completed = await self._migrate_session_data_locked(
                    unified_msg_origin
                )
                if completed:
                    self._session_migration_checked.add(unified_msg_origin)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[自动迁移] 迁移失败: {e}", exc_info=True)

    async def _migrate_session_data_locked(
        self, unified_msg_origin: str
    ) -> bool:
        """Run one migration check while ``_session_migration_lock`` is held."""
        try:
            # 1. 解析 unified_msg_origin
            parts = unified_msg_origin.split(":", 2)
            if len(parts) != 3:
                logger.warning(
                    f"[自动迁移] unified_msg_origin 格式不正确: {unified_msg_origin}"
                )
                return True

            platform_id, message_type, full_session_id = parts

            # 2. 生成所有可能的旧格式匹配候选
            # 对于 "webchat!astrbot!ac8c2cef-..." 会生成:
            #   ["webchat!astrbot!ac8c2cef-...", "astrbot!ac8c2cef-...", "ac8c2cef-..."]
            # 对于 "123456789" 会生成: ["123456789"]
            candidates = [full_session_id]

            # 按感叹号递归拆分
            if "!" in full_session_id:
                parts_by_bang = full_session_id.split("!")
                for i in range(1, len(parts_by_bang)):
                    candidates.append("!".join(parts_by_bang[i:]))

            # 3. 检查是否已迁移（使用unified_msg_origin本身作为标记）
            migration_key = f"migrated_umo_{unified_msg_origin}"
            if self.db_connection is None:
                return False
            cursor = await self.db_connection.execute(
                "SELECT value FROM migration_status WHERE key = ?", (migration_key,)
            )
            row = await cursor.fetchone()
            if row and row[0] == "true":
                # 已迁移过，跳过
                return True

            logger.debug(
                "[自动迁移] 首次检查会话，候选匹配: %s",
                candidates,
            )

            # 4. 查找所有需要迁移的记录
            # 条件：session_id 匹配任一候选 且 不包含冒号（旧格式标识）
            placeholders = " OR ".join(
                ["json_extract(metadata, '$.session_id') = ?" for _ in candidates]
            )
            query = f"""
                SELECT id, metadata FROM documents
                WHERE ({placeholders})
                AND json_extract(metadata, '$.session_id') NOT LIKE '%:%'
            """

            cursor = await self.db_connection.execute(query, tuple(candidates))
            rows = list(await cursor.fetchall())

            if not rows:
                logger.info("[自动迁移] 未找到需要迁移的旧数据")
                # 即使没有旧数据也标记为已检查，避免重复查询
                await self.db_connection.execute(
                    "INSERT OR REPLACE INTO migration_status (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                    (migration_key, "true"),
                )
                await self.db_connection.commit()
                return True

            logger.info(f"[自动迁移] 找到 {len(list(rows))} 条旧数据需要迁移")

            # 5. 批量更新
            updated_count = 0
            for row in rows:
                doc_id = row[0]
                metadata_str = row[1]

                try:
                    metadata = json.loads(metadata_str) if metadata_str else {}
                except (json.JSONDecodeError, TypeError):
                    metadata = {}

                old_session_id = metadata.get("session_id", "unknown")

                # 更新为unified_msg_origin格式
                metadata["session_id"] = unified_msg_origin
                metadata["migrated_at"] = time.time()
                metadata["old_session_id"] = old_session_id  # 保留旧值便于追溯

                # 写回数据库
                await self.db_connection.execute(
                    "UPDATE documents SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), doc_id),
                )
                updated_count += 1

            # 6. 提交更新
            await self.db_connection.commit()

            # 7. 标记为已迁移
            await self.db_connection.execute(
                "INSERT OR REPLACE INTO migration_status (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                (migration_key, "true"),
            )
            await self.db_connection.commit()

            logger.info(
                f"[自动迁移] 完成！已更新 {updated_count} 条记录 -> {unified_msg_origin}"
            )
            return True

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[自动迁移] 迁移失败: {e}", exc_info=True)
            return False

    async def get_statistics(self) -> dict[str, Any]:
        """
        获取记忆统计信息（使用批量处理避免内存问题）

        Returns:
            Dict: 统计信息,包含:
                - total_memories: 总记忆数
                - sessions: 各会话的记忆数（按UUID分组）
                - status_breakdown: 各状态的记忆数
                - avg_importance: 平均重要性
                - oldest_memory: 最旧记忆时间
                - newest_memory: 最新记忆时间
        """
        try:
            # 使用 count_documents() 高效获取总数（不加载数据）
            total_count = await self.faiss_db.document_storage.count_documents(
                metadata_filters={}
            )

            stats = {}
            stats["total_memories"] = total_count

            # 初始化统计变量
            session_counts: dict[str, int] = {}
            status_breakdown = {"active": 0, "archived": 0, "deleted": 0}
            importance_sum = 0
            importance_count = 0
            importance_distribution = {
                "0-1": 0, "1-2": 0, "2-3": 0, "3-4": 0, "4-5": 0,
                "5-6": 0, "6-7": 0, "7-8": 0, "8-9": 0, "9-10": 0,
            }
            oldest_time = None
            newest_time = None

            # 分批处理，每次加载500条，避免内存问题
            batch_size = 500
            offset = 0

            while offset < total_count:
                # 获取一批文档
                batch_docs = await self.faiss_db.document_storage.get_documents(
                    metadata_filters={}, limit=batch_size, offset=offset
                )

                if not batch_docs:
                    break

                # 通过线程池批量规范化 metadata（避免大量 json.loads 阻塞事件循环）
                batch_docs = await asyncio.to_thread(
                    self._normalize_batch_metadata, batch_docs
                )

                for doc in batch_docs:
                    metadata = doc["metadata"]

                    # 统计会话（直接使用session_id分组）
                    session_id = metadata.get("session_id")
                    if session_id:
                        session_counts[session_id] = (
                            session_counts.get(session_id, 0) + 1
                        )

                    # 统计状态（默认 active）
                    status = metadata.get("status", "active")
                    if status in status_breakdown:
                        status_breakdown[status] += 1
                    else:
                        # 未知状态默认计入 active
                        status_breakdown["active"] += 1

                    # 统计重要性
                    importance = metadata.get("importance")
                    if importance is not None:
                        clamped = clamp_float(importance, default=0.5)
                        importance_sum += clamped
                        importance_count += 1
                        # 分桶统计 (0-10 归一化)
                        display_importance = clamped * 10 if clamped <= 1 else clamped
                        bucket_idx = min(9, max(0, int(display_importance)))
                        bucket_keys = [
                            "0-1", "1-2", "2-3", "3-4", "4-5",
                            "5-6", "6-7", "7-8", "8-9", "9-10",
                        ]
                        importance_distribution[bucket_keys[bucket_idx]] += 1

                    # 统计时间
                    create_time = metadata.get("create_time")
                    if create_time:
                        create_time = safe_float(create_time, 0.0)
                        if oldest_time is None or create_time < oldest_time:
                            oldest_time = create_time
                        if newest_time is None or create_time > newest_time:
                            newest_time = create_time

                # 移动到下一批
                offset += batch_size

            stats["sessions"] = session_counts
            stats["status_breakdown"] = status_breakdown
            stats["avg_importance"] = (
                importance_sum / importance_count if importance_count > 0 else 0.0
            )
            stats["importance_distribution"] = importance_distribution
            stats["oldest_memory"] = oldest_time
            stats["newest_memory"] = newest_time
            if self.graph_store is not None:
                stats.update(await self.graph_store.get_memory_entry_stats())
                stats["graph_memory_enabled"] = True
            else:
                stats["graph_memory_enabled"] = False

            return stats
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}", exc_info=True)
            return {
                "total_memories": 0,
                "sessions": {},
                "status_breakdown": {"active": 0, "archived": 0, "deleted": 0},
                "avg_importance": 0.0,
                "oldest_memory": None,
                "newest_memory": None,
                "graph_memory_enabled": bool(self.graph_store is not None),
            }

    async def preview_storage_maintenance(self) -> dict[str, Any]:
        """Return removable build/write artifacts and current SQLite free space."""
        if self.db_connection is None:
            raise RuntimeError("database connection is not initialized")
        topic = await self.topic_memory_store.preview_completed_build_artifact_cleanup()
        write_row = await (
            await self.db_connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0)
                FROM memory_write_ops
                WHERE status = 'completed' AND LENGTH(COALESCE(payload, '')) > 2
                """
            )
        ).fetchone()
        page_count_row = await (
            await self.db_connection.execute("PRAGMA page_count")
        ).fetchone()
        free_page_row = await (
            await self.db_connection.execute("PRAGMA freelist_count")
        ).fetchone()
        page_size_row = await (
            await self.db_connection.execute("PRAGMA page_size")
        ).fetchone()
        page_count = int(page_count_row[0] if page_count_row else 0)
        free_pages = int(free_page_row[0] if free_page_row else 0)
        page_size = int(page_size_row[0] if page_size_row else 0)
        return {
            "topic_build_artifacts": topic,
            "completed_write_ops": {
                "row_count": int(write_row[0] if write_row else 0),
                "payload_bytes": int(write_row[1] if write_row else 0),
            },
            "database": {
                "size_bytes": (
                    Path(self.db_path).stat().st_size
                    if Path(self.db_path).exists()
                    else 0
                ),
                "page_count": page_count,
                "free_page_count": free_pages,
                "free_bytes": free_pages * page_size,
                "free_ratio": (free_pages / page_count if page_count else 0.0),
            },
        }

    async def cleanup_completed_storage_artifacts(
        self,
        *,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Compact successful build/write logs without touching formal memories."""

        async with self._storage_maintenance_lock:
            return await self._cleanup_completed_storage_artifacts_locked(
                progress_callback=progress_callback
            )

    async def _cleanup_completed_storage_artifacts_locked(
        self,
        *,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Run artifact cleanup while holding the storage-maintenance lock."""

        async def emit(stage: str, current: int, total: int, step: str) -> None:
            if progress_callback is None:
                return
            value = progress_callback(stage, current, total, step)
            if hasattr(value, "__await__"):
                await value

        preview = await self.preview_storage_maintenance()
        run_total = int(
            preview["topic_build_artifacts"].get("eligible_run_count", 0)
        )
        total = max(1, run_total + 2)
        await emit("cleanup", 0, total, "正在准备清理已完成构建任务")

        async def topic_progress(current: int, _total: int, step: str) -> None:
            await emit("cleanup", current, total, step)

        topic_result = await self.topic_memory_store.cleanup_completed_build_artifacts(
            progress_callback=topic_progress
        )
        await emit(
            "cleanup",
            run_total + 1,
            total,
            "正在压缩已完成的 Timeline 写操作日志",
        )
        cursor = await self.db_connection.execute(
            """
            UPDATE memory_write_ops
            SET payload = '{}', updated_at = ?
            WHERE status = 'completed' AND LENGTH(COALESCE(payload, '')) > 2
            """,
            (time.time(),),
        )
        await self.db_connection.commit()
        await emit("cleanup", total, total, "已完成中间数据清理")
        return {
            "topic_build_artifacts": topic_result,
            "compacted_write_ops": int(cursor.rowcount or 0),
            "estimated_payload_bytes": (
                int(
                    preview["topic_build_artifacts"].get(
                        "estimated_payload_bytes", 0
                    )
                )
                + int(preview["completed_write_ops"].get("payload_bytes", 0))
            ),
        }

    async def maintain_storage(
        self,
        *,
        vacuum: bool = False,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Run SQLite storage maintenance and return size diagnostics."""

        async with self._storage_maintenance_lock:
            return await self._maintain_storage_locked(
                vacuum=vacuum,
                progress_callback=progress_callback,
            )

    async def _maintain_storage_locked(
        self,
        *,
        vacuum: bool = False,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Run SQLite maintenance while holding the maintenance lock."""
        try:
            async def emit(
                stage: str, current: int, total: int, step: str
            ) -> None:
                if progress_callback is None:
                    return
                value = progress_callback(stage, current, total, step)
                if hasattr(value, "__await__"):
                    await value

            db_path = Path(self.db_path)
            wal_path = Path(f"{self.db_path}-wal")
            before_size = db_path.stat().st_size if db_path.exists() else 0
            before_wal_size = wal_path.stat().st_size if wal_path.exists() else 0

            if self.db_connection is None:
                return {
                    "success": False,
                    "error": "database connection is not initialized",
                }

            await emit("optimizing", 0, 4, "正在优化全文检索索引")
            for fts_table in (
                "livingmemory_memories_fts",
                "livingmemory_graph_entries_fts",
                "memory_atoms_fts",
            ):
                try:
                    await self.db_connection.execute(
                        f"INSERT INTO {fts_table}({fts_table}) VALUES ('optimize')"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug(
                        f"[StorageMaintenance] 跳过 FTS optimize: {fts_table}",
                        exc_info=True,
                    )

            await self.db_connection.commit()
            await emit("checkpoint", 1, 4, "正在合并 SQLite WAL")
            checkpoint_cursor = await self.db_connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            )
            try:
                await checkpoint_cursor.fetchall()
            finally:
                await checkpoint_cursor.close()

            if vacuum:
                await emit("vacuum", 2, 4, "正在压缩数据库文件")
                # VACUUM runs on a dedicated connection. The shared runtime
                # connection may have unrelated readers, and SQLite rejects
                # VACUUM whenever its own connection has an active statement.
                async with aiosqlite.connect(
                    self.db_path, timeout=30.0
                ) as vacuum_db:
                    vacuum_cursor = await vacuum_db.execute("VACUUM")
                    await vacuum_cursor.close()
                    await vacuum_db.commit()
            await emit("verifying", 3, 4, "正在统计维护后的数据库大小")

            after_size = db_path.stat().st_size if db_path.exists() else 0
            after_wal_size = wal_path.stat().st_size if wal_path.exists() else 0
            result = {
                "success": True,
                "vacuum": vacuum,
                "db_size_before": before_size,
                "db_size_after": after_size,
                "wal_size_before": before_wal_size,
                "wal_size_after": after_wal_size,
                "bytes_reclaimed": max(
                    0,
                    before_size + before_wal_size - after_size - after_wal_size,
                ),
            }
            await emit("completed", 4, 4, "数据库存储维护已完成")
            return result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[StorageMaintenance] 执行存储维护失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @staticmethod
    def _normalize_batch_metadata(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize metadata from JSON strings to dicts for a batch of documents.

        Offloaded to thread pool in batch processing paths to avoid blocking
        the event loop with hundreds of json.loads calls.
        """
        for doc in docs:
            metadata = doc.get("metadata")
            if isinstance(metadata, str):
                try:
                    doc["metadata"] = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    doc["metadata"] = {}
            elif not isinstance(metadata, dict):
                doc["metadata"] = {}
        return docs
