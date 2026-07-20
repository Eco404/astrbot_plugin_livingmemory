"""
官方插件 Page API 适配层。

职责：
1. 为 AstrBot 官方插件页面注册原生 Web API。
2. 直接复用插件运行期组件，不再代理到旧 FastAPI WebUI。
3. 保留返回结构与旧前端尽量一致，降低页面迁移成本。
"""

from __future__ import annotations

from typing import Any

from .page_api_modules import (
    BackupHandler,
    GraphHandler,
    IdentityHandler,
    MemoryHandler,
    ModelHandler,
    PageApiUtils,
    RecallHandler,
    SessionHandler,
    StatsHandler,
    TopicHandler,
)

PLUGIN_NAME = "astrbot_plugin_livingmemory"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"


class PluginPageApi:
    """LivingMemory 官方插件页面 API（Facade）。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

        # 初始化工具类
        self.utils = PageApiUtils()

        # 初始化各个处理器
        self.stats_handler = StatsHandler(self.utils)
        self.memory_handler = MemoryHandler(self.utils)
        self.model_handler = ModelHandler(self.utils)
        self.recall_handler = RecallHandler(self.utils)
        self.session_handler = SessionHandler(self.utils)
        self.graph_handler = GraphHandler(self.utils)
        self.identity_handler = IdentityHandler(self.utils)
        self.topic_handler = TopicHandler(self.utils)

        # BackupHandler 需要 data_dir，延迟初始化
        self._backup_handler = None

    @property
    def backup_handler(self) -> BackupHandler:
        """延迟初始化 BackupHandler"""
        if self._backup_handler is None:
            data_dir = (
                self.plugin.initializer.data_dir if self.plugin.initializer else ""
            )
            self._backup_handler = BackupHandler(self.utils, data_dir)
        return self._backup_handler

    def register_routes(self) -> None:
        """注册官方插件页面所需的原生 API。"""
        register = self.plugin.context.register_web_api
        register(
            f"{PAGE_API_PREFIX}/stats",
            self.get_stats,
            ["GET"],
            "LivingMemory Page stats",
        )
        register(
            f"{PAGE_API_PREFIX}/sessions",
            self.list_sessions,
            ["GET"],
            "LivingMemory Page session catalog",
        )
        register(
            f"{PAGE_API_PREFIX}/memories",
            self.list_memories,
            ["GET"],
            "LivingMemory Page memories",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/detail",
            self.get_memory_detail,
            ["GET"],
            "LivingMemory Page memory detail",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/update",
            self.update_memory,
            ["POST"],
            "LivingMemory Page update memory",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/related",
            self.detect_related_memories,
            ["POST"],
            "LivingMemory Page detect related memories",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/update/start",
            self.start_structured_update_job,
            ["POST"],
            "LivingMemory Page start structured update job",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/update/progress",
            self.get_structured_update_progress,
            ["GET"],
            "LivingMemory Page structured update progress",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/batch-delete",
            self.batch_delete_memories,
            ["POST"],
            "LivingMemory Page batch delete memories",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/batch-update",
            self.batch_update_memories,
            ["POST"],
            "LivingMemory Page batch update memories",
        )
        register(
            f"{PAGE_API_PREFIX}/recall/test",
            self.test_recall,
            ["POST"],
            "LivingMemory Page recall test",
        )
        register(
            f"{PAGE_API_PREFIX}/graph/overview",
            self.get_graph_overview,
            ["GET"],
            "LivingMemory Page graph overview",
        )
        register(
            f"{PAGE_API_PREFIX}/graph/query",
            self.query_graph,
            ["POST"],
            "LivingMemory Page graph query",
        )
        register(
            f"{PAGE_API_PREFIX}/backups",
            self.list_backups,
            ["GET"],
            "LivingMemory Page backup list",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/overview",
            self.get_topic_overview,
            ["GET"],
            "LivingMemory Page Topic overview",
        )
        register(
            f"{PAGE_API_PREFIX}/topics",
            self.list_topics,
            ["GET"],
            "LivingMemory Page Topic list",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/detail",
            self.get_topic_detail,
            ["GET"],
            "LivingMemory Page Topic detail",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/build/start",
            self.start_topic_build,
            ["POST"],
            "LivingMemory Page start Topic build",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/build/progress",
            self.get_topic_build_progress,
            ["GET"],
            "LivingMemory Page Topic build progress",
        )
        register(
            f"{PAGE_API_PREFIX}/models",
            self.list_models,
            ["GET"],
            "LivingMemory Page model information",
        )
        register(
            f"{PAGE_API_PREFIX}/models/test",
            self.test_model_connection,
            ["POST"],
            "LivingMemory Page model connection test",
        )
        register(
            f"{PAGE_API_PREFIX}/identities",
            self.list_identity_profiles,
            ["GET"],
            "LivingMemory Page authoritative identity profiles",
        )
        register(
            f"{PAGE_API_PREFIX}/identities/save",
            self.save_identity_profiles,
            ["POST"],
            "LivingMemory Page save authoritative identity profiles",
        )

    # ==================== 路由处理方法 ====================
    # 所有方法都委托给相应的处理器

    async def get_stats(self):
        """获取插件统计信息"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.stats_handler.get_stats(ready["memory_engine"])

    async def list_sessions(self):
        """获取供 WebUI 分层筛选使用的轻量会话目录。"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.session_handler.list_sessions(
            ready["conversation_manager"],
        )

    async def list_memories(self):
        """获取记忆列表（带分页和过滤）"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.list_memories(ready["memory_engine"])

    async def get_memory_detail(self):
        """获取单个记忆的完整详情"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.get_memory_detail(ready["memory_engine"])

    async def update_memory(self):
        """更新单个记忆的字段"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.update_memory(
            ready["memory_engine"], ready["memory_processor"]
        )

    async def detect_related_memories(self):
        """检测所选会话或人格范围内的关联记忆。"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.detect_related_memories(
            ready["memory_engine"]
        )

    async def start_structured_update_job(self):
        """启动带进度跟踪的结构化记忆更新任务。"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.start_structured_update_job(
            ready["memory_engine"], ready["memory_processor"]
        )

    async def get_structured_update_progress(self):
        """查询结构化记忆更新任务进度。"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.get_structured_update_progress()

    async def batch_delete_memories(self):
        """批量删除记忆"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.batch_delete_memories(ready["memory_engine"])

    async def batch_update_memories(self):
        """批量更新记忆字段"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.batch_update_memories(ready["memory_engine"])

    async def test_recall(self):
        """测试记忆召回功能"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.recall_handler.test_recall(ready["memory_engine"])

    async def get_graph_overview(self):
        """获取图谱概览"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.graph_handler.get_graph_overview(ready["memory_engine"])

    async def query_graph(self):
        """查询图谱"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.graph_handler.query_graph(ready["memory_engine"])

    async def list_backups(self):
        """列出所有版本备份及其元数据"""
        return await self.backup_handler.list_backups()

    async def get_topic_overview(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.get_overview(ready["memory_engine"])

    async def list_topics(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.list_topics(ready["memory_engine"])

    async def get_topic_detail(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.get_topic_detail(ready["memory_engine"])

    async def start_topic_build(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.start_build(ready["memory_engine"])

    async def get_topic_build_progress(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.get_build_progress()

    async def list_models(self):
        """列出插件运行时实际使用的模型，包括默认回退来源。"""
        return await self.model_handler.list_models(self.plugin.initializer)

    async def test_model_connection(self):
        """测试指定模型角色当前解析到的 Provider。"""
        return await self.model_handler.test_connection(self.plugin.initializer)

    async def list_identity_profiles(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        store = ready.get("identity_profile_store")
        if store is None:
            return self.utils.error("人物资料存储尚未初始化")
        return await self.identity_handler.list_profiles(store)

    async def save_identity_profiles(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        store = ready.get("identity_profile_store")
        if store is None:
            return self.utils.error("人物资料存储尚未初始化")
        manager = ready["memory_engine"].topic_build_manager
        topic_build_active = bool(
            self.topic_handler.has_active_jobs() or manager.has_active_builds()
        )
        return await self.identity_handler.save_profiles(
            store,
            topic_build_active=topic_build_active,
        )

    # ==================== 辅助方法 ====================

    async def _ensure_plugin_ready(self) -> tuple[dict[str, Any] | None, dict | None]:
        """
        确保插件已就绪

        Returns:
            (ready_dict, error_dict) 元组
            - ready_dict: 包含 memory_engine 等组件的字典
            - error_dict: 错误响应字典（如果有错误）
        """
        ready, message = await self.plugin._ensure_plugin_ready()
        if not ready:
            return None, self.utils.error(message or "插件尚未就绪")

        memory_engine = self.plugin.initializer.memory_engine
        if memory_engine is None:
            return None, self.utils.error("记忆引擎未初始化")

        return {
            "memory_engine": memory_engine,
            "conversation_manager": self.plugin.initializer.conversation_manager,
            "memory_processor": getattr(
                self.plugin.initializer, "memory_processor", None
            ),
            "index_validator": self.plugin.initializer.index_validator,
            "identity_profile_store": getattr(
                self.plugin.initializer, "identity_profile_store", None
            ),
        }, None
