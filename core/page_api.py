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
    DatabaseHandler,
    GraphHandler,
    IdentityHandler,
    MemoryHandler,
    ModelHandler,
    PageApiUtils,
    RecallHandler,
    SessionHandler,
    SettingsHandler,
    StatsHandler,
    TimelineHandler,
    TopicHandler,
    UserProfileHandler,
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
        self.settings_handler = SettingsHandler(self.utils)
        self.graph_handler = GraphHandler(self.utils)
        self.identity_handler = IdentityHandler(self.utils)
        self.topic_handler = TopicHandler(self.utils)
        self.timeline_handler = TimelineHandler(self.utils)
        self.user_profile_handler = UserProfileHandler(self.utils)
        self.database_handler = DatabaseHandler(self.utils)

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
            f"{PAGE_API_PREFIX}/sessions/audit",
            self.audit_sessions,
            ["GET"],
            "LivingMemory Page session audit",
        )
        register(
            f"{PAGE_API_PREFIX}/sessions/maintenance/preview",
            self.preview_session_maintenance,
            ["POST"],
            "LivingMemory Page preview session maintenance",
        )
        register(
            f"{PAGE_API_PREFIX}/sessions/maintenance/start",
            self.start_session_maintenance,
            ["POST"],
            "LivingMemory Page start session maintenance",
        )
        register(
            f"{PAGE_API_PREFIX}/sessions/maintenance/task",
            self.get_session_maintenance_task,
            ["GET"],
            "LivingMemory Page session maintenance task",
        )
        register(
            f"{PAGE_API_PREFIX}/sessions/maintenance/tasks",
            self.list_session_maintenance_tasks,
            ["GET"],
            "LivingMemory Page session maintenance tasks",
        )
        register(
            f"{PAGE_API_PREFIX}/sessions/maintenance/tasks/delete",
            self.delete_session_maintenance_task,
            ["POST"],
            "LivingMemory Page delete session maintenance task",
        )
        register(
            f"{PAGE_API_PREFIX}/sessions/maintenance/tasks/clear",
            self.clear_session_maintenance_tasks,
            ["POST"],
            "LivingMemory Page clear finished session maintenance tasks",
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
            f"{PAGE_API_PREFIX}/memories/update/stage",
            self.stage_memory_update,
            ["POST"],
            "LivingMemory Page stage Timeline edit",
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
            f"{PAGE_API_PREFIX}/memories/export",
            self.export_memories,
            ["POST"],
            "LivingMemory Page export memories",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/import",
            self.import_memories,
            ["POST"],
            "LivingMemory Page import memories",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/batch-update",
            self.batch_update_memories,
            ["POST"],
            "LivingMemory Page batch update memories",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/settings",
            self.get_timeline_settings,
            ["GET"],
            "LivingMemory Page Timeline settings",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/settings/update",
            self.update_timeline_settings,
            ["POST"],
            "LivingMemory Page update Timeline settings",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/rebuild/preview",
            self.preview_timeline_rebuild,
            ["POST"],
            "LivingMemory Page preview Timeline reconstruction",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/rebuild/start",
            self.start_timeline_rebuild,
            ["POST"],
            "LivingMemory Page start Timeline reconstruction",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/rebuild/task",
            self.get_timeline_rebuild_task,
            ["GET"],
            "LivingMemory Page Timeline reconstruction task",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/rebuild/tasks",
            self.list_timeline_rebuild_tasks,
            ["GET"],
            "LivingMemory Page Timeline reconstruction tasks",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/rebuild/resume",
            self.resume_timeline_rebuild,
            ["POST"],
            "LivingMemory Page resume Timeline reconstruction",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/rebuild/cancel",
            self.cancel_timeline_rebuild,
            ["POST"],
            "LivingMemory Page cancel Timeline reconstruction",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/rebuild/tasks/delete",
            self.delete_timeline_rebuild_task,
            ["POST"],
            "LivingMemory Page delete Timeline reconstruction task",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/rebuild/tasks/clear",
            self.clear_timeline_rebuild_tasks,
            ["POST"],
            "LivingMemory Page clear Timeline reconstruction tasks",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/staged-edits",
            self.list_timeline_staged_edits,
            ["GET"],
            "LivingMemory Page staged Timeline edits",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/staged-edits/apply",
            self.apply_timeline_staged_edits,
            ["POST"],
            "LivingMemory Page apply staged Timeline edits",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/staged-edits/delete",
            self.delete_timeline_staged_edits,
            ["POST"],
            "LivingMemory Page delete staged Timeline edits",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/inactive",
            self.list_inactive_timelines,
            ["GET"],
            "LivingMemory Page inactive Timeline memories",
        )
        register(
            f"{PAGE_API_PREFIX}/timeline/inactive/restore",
            self.restore_inactive_timelines,
            ["POST"],
            "LivingMemory Page restore inactive Timeline memories",
        )
        register(
            f"{PAGE_API_PREFIX}/settings",
            self.get_settings,
            ["GET"],
            "LivingMemory Page unified settings",
        )
        register(
            f"{PAGE_API_PREFIX}/settings/update",
            self.update_settings,
            ["POST"],
            "LivingMemory Page update unified settings",
        )
        register(
            f"{PAGE_API_PREFIX}/recall/test",
            self.test_recall,
            ["POST"],
            "LivingMemory Page recall test",
        )
        register(
            f"{PAGE_API_PREFIX}/recall/traces",
            self.list_recall_traces,
            ["GET"],
            "LivingMemory Page recall trace list",
        )
        register(
            f"{PAGE_API_PREFIX}/recall/traces/detail",
            self.get_recall_trace,
            ["GET"],
            "LivingMemory Page recall trace detail",
        )
        register(
            f"{PAGE_API_PREFIX}/recall/traces/settings",
            self.update_recall_trace_settings,
            ["POST"],
            "LivingMemory Page recall trace settings",
        )
        register(
            f"{PAGE_API_PREFIX}/recall/traces/delete",
            self.delete_recall_trace,
            ["POST"],
            "LivingMemory Page delete recall trace",
        )
        register(
            f"{PAGE_API_PREFIX}/recall/traces/clear",
            self.clear_recall_traces,
            ["POST"],
            "LivingMemory Page clear recall traces",
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
            f"{PAGE_API_PREFIX}/database/health",
            self.check_database_health,
            ["GET"],
            "LivingMemory Page database health check",
        )
        register(
            f"{PAGE_API_PREFIX}/database/repair",
            self.repair_database_issues,
            ["POST"],
            "LivingMemory Page selected database repair",
        )
        register(
            f"{PAGE_API_PREFIX}/database/repair/progress",
            self.get_database_repair_progress,
            ["GET"],
            "LivingMemory Page database repair progress",
        )
        register(
            f"{PAGE_API_PREFIX}/database/storage",
            self.get_database_storage_preview,
            ["GET"],
            "LivingMemory Page database storage preview",
        )
        register(
            f"{PAGE_API_PREFIX}/database/storage/maintenance",
            self.start_database_storage_maintenance,
            ["POST"],
            "LivingMemory Page database storage maintenance",
        )
        register(
            f"{PAGE_API_PREFIX}/database/storage/progress",
            self.get_database_storage_progress,
            ["GET"],
            "LivingMemory Page database storage maintenance progress",
        )
        register(
            f"{PAGE_API_PREFIX}/database/storage/progress/clear",
            self.clear_database_storage_progress,
            ["POST"],
            "LivingMemory Page clear database storage maintenance progress",
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
            f"{PAGE_API_PREFIX}/topics/settings",
            self.get_topic_settings,
            ["GET"],
            "LivingMemory Page Topic settings",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/settings/update",
            self.update_topic_settings,
            ["POST"],
            "LivingMemory Page update Topic settings",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/maintenance/unindexed",
            self.list_unindexed_topic_timelines,
            ["GET"],
            "LivingMemory Page unindexed Timeline list",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/maintenance/preview",
            self.preview_topic_maintenance,
            ["POST"],
            "LivingMemory Page incremental Topic maintenance preview",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/reviews",
            self.list_topic_reviews,
            ["GET"],
            "LivingMemory Page Topic review queue",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/reviews/detail",
            self.get_topic_review_detail,
            ["GET"],
            "LivingMemory Page Topic review detail",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/reviews/resolve",
            self.resolve_topic_review,
            ["POST"],
            "LivingMemory Page resolve Topic review",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/governance/preview",
            self.preview_topic_governance,
            ["POST"],
            "LivingMemory Page preview Topic merge or split",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/governance/execute",
            self.execute_topic_governance,
            ["POST"],
            "LivingMemory Page execute Topic merge or split",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/relations/recompute",
            self.recompute_topic_relations,
            ["POST"],
            "LivingMemory Page recompute Topic relations",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/maintenance/revectorize",
            self.revectorize_topic_memories,
            ["POST"],
            "LivingMemory Page revectorize Topic memories",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/maintenance/clear",
            self.clear_topic_memories,
            ["POST"],
            "LivingMemory Page clear Topic memories",
        )
        register(
            f"{PAGE_API_PREFIX}/topics/archived/delete",
            self.delete_archived_topics,
            ["POST"],
            "LivingMemory Page permanently delete archived Topics",
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
            f"{PAGE_API_PREFIX}/topics/build/discard",
            self.discard_topic_build,
            ["POST"],
            "LivingMemory Page discard Topic build checkpoint",
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
            "LivingMemory Page supplemental identity profiles",
        )
        register(
            f"{PAGE_API_PREFIX}/identities/save",
            self.save_identity_profiles,
            ["POST"],
            "LivingMemory Page save supplemental identity profiles",
        )
        user_profile_routes = (
            ("/user-profiles", self.list_user_profiles, ["GET"]),
            (
                "/user-profiles/build-candidates",
                self.list_user_profile_build_candidates,
                ["GET"],
            ),
            ("/user-profiles/build", self.build_user_profile, ["POST"]),
            ("/user-profiles/detail", self.get_user_profile_detail, ["GET"]),
            ("/user-profiles/enable", self.enable_user_profile, ["POST"]),
            ("/user-profiles/disable", self.disable_user_profile, ["POST"]),
            ("/user-profiles/reset", self.reset_user_profile, ["POST"]),
            ("/user-profiles/delete-disable", self.delete_disable_user_profile, ["POST"]),
            ("/user-profiles/facts/action", self.user_profile_fact_action, ["POST"]),
            ("/user-profiles/conflicts/resolve", self.resolve_user_profile_conflict, ["POST"]),
            ("/user-profiles/rebuild/preview", self.preview_user_profile_rebuild, ["POST"]),
            ("/user-profiles/rebuild/start", self.start_user_profile_rebuild, ["POST"]),
            ("/user-profiles/identity-reviews/scan", self.scan_user_profile_identity_reviews, ["POST"]),
            ("/user-profiles/identity-reviews/action", self.review_user_profile_identity, ["POST"]),
            ("/user-profiles/tasks", self.list_user_profile_tasks, ["GET"]),
            ("/user-profiles/task", self.get_user_profile_task, ["GET"]),
            ("/user-profiles/tasks/retry", self.retry_user_profile_task, ["POST"]),
            ("/user-profiles/tasks/cancel", self.cancel_user_profile_task, ["POST"]),
            ("/user-profiles/tasks/delete", self.delete_user_profile_task, ["POST"]),
            ("/user-profiles/tasks/clear-completed", self.clear_completed_user_profile_tasks, ["POST"]),
            ("/user-profiles/rebuild/continue", self.continue_user_profile_rebuild, ["POST"]),
            ("/user-profiles/relationship/update", self.update_user_relationship, ["POST"]),
            ("/user-profiles/relationship/freeze", self.freeze_user_relationship, ["POST"]),
            ("/user-profiles/relationship/reset", self.reset_user_relationship, ["POST"]),
            ("/user-profiles/relationship/rollback", self.rollback_user_relationship, ["POST"]),
            ("/user-profiles/relationship/rebuild", self.rebuild_user_relationship, ["POST"]),
            ("/user-profiles/accounts/bind/preview", self.preview_user_profile_account_binding, ["POST"]),
            ("/user-profiles/accounts/bind", self.bind_user_profile_accounts, ["POST"]),
            ("/user-profiles/accounts/unbind/preview", self.preview_user_profile_account_unbind, ["POST"]),
            ("/user-profiles/accounts/unbind", self.unbind_user_profile_account, ["POST"]),
            ("/user-profiles/share-groups/preview", self.preview_user_profile_share_group, ["POST"]),
            ("/user-profiles/share-groups/save", self.save_user_profile_share_group, ["POST"]),
        )
        for suffix, handler, methods in user_profile_routes:
            register(
                f"{PAGE_API_PREFIX}{suffix}",
                handler,
                methods,
                f"LivingMemory Page {suffix.removeprefix('/')}",
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

    async def audit_sessions(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.session_handler.audit_sessions(
            ready["session_maintenance_manager"]
        )

    async def preview_session_maintenance(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.session_handler.preview_maintenance(
            ready["session_maintenance_manager"]
        )

    async def start_session_maintenance(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.session_handler.start_maintenance(
            ready["session_maintenance_manager"]
        )

    async def get_session_maintenance_task(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.session_handler.get_maintenance_task(
            ready["session_maintenance_manager"]
        )

    async def list_session_maintenance_tasks(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.session_handler.list_maintenance_tasks(
            ready["session_maintenance_manager"]
        )

    async def delete_session_maintenance_task(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.session_handler.delete_maintenance_task(
            ready["session_maintenance_manager"]
        )

    async def clear_session_maintenance_tasks(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.session_handler.clear_maintenance_tasks(
            ready["session_maintenance_manager"]
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

    async def stage_memory_update(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.stage_memory_update(
            ready["memory_engine"],
            ready["memory_processor"],
            ready["timeline_rebuild_manager"],
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

    async def export_memories(self):
        """导出全部或已选 Timeline 记忆。"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.export_memories(ready["memory_engine"])

    async def import_memories(self):
        """预览或导入 Timeline 记忆。"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.import_memories(
            ready["memory_engine"], ready["memory_processor"]
        )

    async def batch_update_memories(self):
        """批量更新记忆字段"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.batch_update_memories(ready["memory_engine"])

    async def get_timeline_settings(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.get_settings(self.plugin.initializer)

    async def update_timeline_settings(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.update_settings(self.plugin.initializer)

    async def preview_timeline_rebuild(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.preview_rebuild(
            ready["timeline_rebuild_manager"]
        )

    async def start_timeline_rebuild(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.start_rebuild(
            ready["timeline_rebuild_manager"]
        )

    async def get_timeline_rebuild_task(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.get_rebuild_task(
            ready["timeline_rebuild_manager"]
        )

    async def list_timeline_rebuild_tasks(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.list_rebuild_tasks(
            ready["timeline_rebuild_manager"]
        )

    async def resume_timeline_rebuild(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.resume_rebuild_task(
            ready["timeline_rebuild_manager"]
        )

    async def cancel_timeline_rebuild(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.cancel_rebuild_task(
            ready["timeline_rebuild_manager"]
        )

    async def delete_timeline_rebuild_task(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.delete_rebuild_task(
            ready["timeline_rebuild_manager"]
        )

    async def clear_timeline_rebuild_tasks(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.clear_rebuild_tasks(
            ready["timeline_rebuild_manager"]
        )

    async def list_timeline_staged_edits(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.list_staged_edits(
            ready["timeline_rebuild_manager"]
        )

    async def apply_timeline_staged_edits(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.apply_staged_edits(
            ready["timeline_rebuild_manager"]
        )

    async def delete_timeline_staged_edits(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.delete_staged_edits(
            ready["timeline_rebuild_manager"]
        )

    async def list_inactive_timelines(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.list_inactive(
            ready["timeline_rebuild_manager"]
        )

    async def restore_inactive_timelines(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.timeline_handler.restore_inactive(
            ready["timeline_rebuild_manager"]
        )

    async def get_settings(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.settings_handler.get_settings(
            ready["memory_engine"], self.plugin.initializer
        )

    async def update_settings(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.settings_handler.update_settings(
            ready["memory_engine"], self.plugin.initializer
        )

    async def test_recall(self):
        """测试记忆召回功能"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.recall_handler.test_recall(
            ready["memory_engine"],
            getattr(self.plugin.initializer, "config_manager", None),
            ready.get("conversation_manager"),
            ready.get("recall_trace_store"),
        )

    async def list_recall_traces(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.recall_handler.list_traces(ready.get("recall_trace_store"))

    async def get_recall_trace(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.recall_handler.get_trace(ready.get("recall_trace_store"))

    async def update_recall_trace_settings(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.recall_handler.update_trace_settings(
            ready.get("recall_trace_store")
        )

    async def delete_recall_trace(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.recall_handler.delete_trace(ready.get("recall_trace_store"))

    async def clear_recall_traces(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.recall_handler.clear_traces(ready.get("recall_trace_store"))

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

    async def check_database_health(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.database_handler.check_health(
            ready["memory_engine"], ready.get("conversation_manager")
        )

    async def repair_database_issues(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.database_handler.start_repair(
            ready["memory_engine"], ready.get("conversation_manager")
        )

    async def get_database_repair_progress(self):
        return await self.database_handler.get_repair_progress()

    async def get_database_storage_preview(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.database_handler.get_storage_preview(
            ready["memory_engine"]
        )

    async def start_database_storage_maintenance(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.database_handler.start_storage_maintenance(
            ready["memory_engine"]
        )

    async def get_database_storage_progress(self):
        return await self.database_handler.get_storage_maintenance_progress()

    async def clear_database_storage_progress(self):
        return await self.database_handler.clear_storage_maintenance_progress()

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

    async def get_topic_settings(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.get_settings(
            ready["memory_engine"], self.plugin.initializer
        )

    async def update_topic_settings(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.update_settings(
            ready["memory_engine"], self.plugin.initializer
        )

    async def list_unindexed_topic_timelines(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.list_unindexed_timelines(
            ready["memory_engine"]
        )

    async def preview_topic_maintenance(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.preview_incremental_maintenance(
            ready["memory_engine"]
        )

    async def list_topic_reviews(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.list_reviews(ready["memory_engine"])

    async def get_topic_review_detail(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.get_review_detail(ready["memory_engine"])

    async def resolve_topic_review(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.resolve_review(ready["memory_engine"])

    async def preview_topic_governance(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.preview_governance(ready["memory_engine"])

    async def execute_topic_governance(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.execute_governance(ready["memory_engine"])

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

    async def discard_topic_build(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.discard_build(ready["memory_engine"])

    async def clear_topic_memories(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.clear_topics(ready["memory_engine"])

    async def delete_archived_topics(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.delete_archived_topics(
            ready["memory_engine"]
        )

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
            return self.utils.error("补充人物资料存储尚未初始化")
        return await self.identity_handler.list_profiles(
            store,
            platform_manager=getattr(self.plugin.context, "platform_manager", None),
            conversation_manager=ready.get("conversation_manager"),
        )

    async def recompute_topic_relations(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.recompute_relations(ready["memory_engine"])

    async def revectorize_topic_memories(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.topic_handler.start_revectorization(
            ready["memory_engine"]
        )

    async def save_identity_profiles(self):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        store = ready.get("identity_profile_store")
        if store is None:
            return self.utils.error("补充人物资料存储尚未初始化")
        return await self.identity_handler.save_profiles(
            store,
            platform_manager=getattr(self.plugin.context, "platform_manager", None),
            conversation_manager=ready.get("conversation_manager"),
        )

    async def list_user_profiles(self):
        return await self._with_user_profiles("list_profiles")

    async def list_user_profile_build_candidates(self):
        return await self._with_user_profiles("list_build_candidates")

    async def build_user_profile(self):
        return await self._with_user_profiles("build_candidate")

    async def get_user_profile_detail(self):
        return await self._with_user_profiles("get_detail")

    async def enable_user_profile(self):
        return await self._with_user_profiles("set_enabled", True)

    async def disable_user_profile(self):
        return await self._with_user_profiles("set_enabled", False)

    async def reset_user_profile(self):
        return await self._with_user_profiles("reset_profile")

    async def delete_disable_user_profile(self):
        return await self._with_user_profiles("delete_disable")

    async def user_profile_fact_action(self):
        return await self._with_user_profiles("fact_action")

    async def resolve_user_profile_conflict(self):
        return await self._with_user_profiles("resolve_conflict")

    async def preview_user_profile_rebuild(self):
        return await self._with_user_profiles("rebuild_preview")

    async def start_user_profile_rebuild(self):
        return await self._with_user_profiles("rebuild_start")

    async def scan_user_profile_identity_reviews(self):
        return await self._with_user_profiles("identity_review_scan")

    async def review_user_profile_identity(self):
        return await self._with_user_profiles("identity_review_action")

    async def list_user_profile_tasks(self):
        return await self._with_user_profiles("list_tasks")

    async def get_user_profile_task(self):
        return await self._with_user_profiles("get_task")

    async def retry_user_profile_task(self):
        return await self._with_user_profiles("retry_task")

    async def cancel_user_profile_task(self):
        return await self._with_user_profiles("cancel_task")

    async def delete_user_profile_task(self):
        return await self._with_user_profiles("delete_task")

    async def clear_completed_user_profile_tasks(self):
        return await self._with_user_profiles("clear_completed_tasks")

    async def continue_user_profile_rebuild(self):
        return await self._with_user_profiles("continue_gap")

    async def update_user_relationship(self):
        return await self._with_user_profiles("relationship_update")

    async def freeze_user_relationship(self):
        return await self._with_user_profiles("relationship_freeze")

    async def reset_user_relationship(self):
        return await self._with_user_profiles("relationship_reset")

    async def rollback_user_relationship(self):
        return await self._with_user_profiles("relationship_rollback")

    async def rebuild_user_relationship(self):
        return await self._with_user_profiles("relationship_rebuild")

    async def preview_user_profile_account_binding(self):
        return await self._with_user_profiles("bind_preview")

    async def bind_user_profile_accounts(self):
        return await self._with_user_profiles("bind_accounts")

    async def preview_user_profile_account_unbind(self):
        return await self._with_user_profiles("unbind_preview")

    async def unbind_user_profile_account(self):
        return await self._with_user_profiles("unbind_account")

    async def preview_user_profile_share_group(self):
        return await self._with_user_profiles("share_preview")

    async def save_user_profile_share_group(self):
        return await self._with_user_profiles("share_save")

    async def _with_user_profiles(self, method: str, *args):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        handler = getattr(self.user_profile_handler, method)
        return await handler(ready["memory_engine"], *args)

    async def shutdown(self) -> None:
        """Stop page-owned background work before runtime components are closed."""
        await self.database_handler.shutdown()
        await self.topic_handler.shutdown()

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
            "session_maintenance_manager": getattr(
                self.plugin.initializer, "session_maintenance_manager", None
            ),
            "timeline_rebuild_manager": getattr(
                self.plugin.initializer, "timeline_rebuild_manager", None
            ),
            "recall_trace_store": getattr(
                self.plugin.initializer, "recall_trace_store", None
            ),
        }, None
