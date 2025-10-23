# -*- coding: utf-8 -*-
"""
main.py - LivingMemory 插件主文件
负责插件注册、初始化所有引擎、绑定事件钩子以及管理生命周期。
"""

import asyncio
import os
import json
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# AstrBot API
from astrbot.api.event import filter, AstrMessageEvent,MessageChain
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.provider import (
    LLMResponse,
    ProviderRequest,
    Provider,
)
from astrbot.core.provider.provider import EmbeddingProvider

from astrbot.api import logger
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB

# 插件内部模块
from .storage.faiss_manager import FaissManager
from .core.engines.recall_engine import RecallEngine
from .core.commands import require_handlers, handle_command_errors, deprecated
from .core.engines.reflection_engine import ReflectionEngine
from .core.engines.forgetting_agent import ForgettingAgent
from .core.retrieval import SparseRetriever
from .core.utils import get_persona_id, format_memories_for_injection, get_now_datetime, retry_on_failure, OperationContext, safe_parse_metadata
from .core.config_validator import validate_config, merge_config_with_defaults
from .core.handlers import MemoryHandler, SearchHandler, AdminHandler, FusionHandler
from .webui import WebUIServer

# 会话管理器类，替代全局字典
class SessionManager:
    def __init__(self, max_sessions: int = 1000, session_ttl: int = 3600):
        """
        Args:
            max_sessions: 最大会话数量
            session_ttl: 会话生存时间（秒）
        """
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._access_times: Dict[str, float] = {}
        self.max_sessions = max_sessions
        self.session_ttl = session_ttl
        
    def get_session(self, session_id: str) -> Dict[str, Any]:
        """获取会话数据，如果不存在则创建"""
        current_time = time.time()
        
        # 清理过期会话
        self._cleanup_expired_sessions(current_time)
        
        if session_id not in self._sessions:
            self._sessions[session_id] = {"history": [], "round_count": 0}
            
        self._access_times[session_id] = current_time
        return self._sessions[session_id]
        
    def _cleanup_expired_sessions(self, current_time: float):
        """清理过期的会话"""
        expired_sessions = []
        for session_id, last_access in self._access_times.items():
            if current_time - last_access > self.session_ttl:
                expired_sessions.append(session_id)
                
        for session_id in expired_sessions:
            self._sessions.pop(session_id, None)
            self._access_times.pop(session_id, None)
            
        # 如果会话数量超过限制，删除最旧的会话
        if len(self._sessions) > self.max_sessions:
            # 按访问时间排序，删除最旧的
            sorted_sessions = sorted(self._access_times.items(), key=lambda x: x[1])
            sessions_to_remove = sorted_sessions[:len(self._sessions) - self.max_sessions]
            
            for session_id, _ in sessions_to_remove:
                self._sessions.pop(session_id, None)
                self._access_times.pop(session_id, None)
                
    def reset_session(self, session_id: str):
        """重置指定会话"""
        if session_id in self._sessions:
            self._sessions[session_id] = {"history": [], "round_count": 0}
            self._access_times[session_id] = time.time()
            
    def get_session_count(self) -> int:
        """获取当前会话数量"""
        return len(self._sessions)


@register(
    "LivingMemory",
    "lxfight",
    "一个拥有动态生命周期的智能长期记忆插件。",
    "1.3.2",
    "https://github.com/lxfight/astrbot_plugin_livingmemory",
)
class LivingMemoryPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.context = context
        
        # 验证和标准化配置
        try:
            merged_config = merge_config_with_defaults(config)
            self.config_obj = validate_config(merged_config)
            self.config = self.config_obj.model_dump()  # 保持向后兼容
            logger.info("插件配置验证成功")
        except Exception as e:
            logger.error(f"配置验证失败，使用默认配置: {e}")
            from .core.config_validator import get_default_config
            self.config = get_default_config()
            self.config_obj = validate_config(self.config)

        # 初始化状态
        self.embedding_provider: Optional[EmbeddingProvider] = None
        self.llm_provider: Optional[Provider] = None
        self.db: Optional[FaissVecDB] = None
        self.faiss_manager: Optional[FaissManager] = None
        self.sparse_retriever: Optional[SparseRetriever] = None
        self.recall_engine: Optional[RecallEngine] = None
        self.reflection_engine: Optional[ReflectionEngine] = None
        self.forgetting_agent: Optional[ForgettingAgent] = None
        
        # 初始化业务逻辑处理器
        self.memory_handler: Optional[MemoryHandler] = None
        self.search_handler: Optional[SearchHandler] = None
        self.admin_handler: Optional[AdminHandler] = None
        self.fusion_handler: Optional[FusionHandler] = None
        
        # 初始化状态标记
        self._initialization_complete = False
        self._initialization_task: Optional[asyncio.Task] = None
        
        # 会话管理器
        session_config = self.config.get("session_manager", {})
        self.session_manager = SessionManager(
            max_sessions=session_config.get("max_sessions", 1000),
            session_ttl=session_config.get("session_ttl", 3600)
        )
        # 启动初始化任务
        self._initialization_task = asyncio.create_task(self._wait_for_astrbot_and_initialize())

        # WebUI 服务句柄
        self.webui_server: Optional[WebUIServer] = None

    async def _wait_for_astrbot_and_initialize(self):
        """
        等待AstrBot完全启动后进行插件初始化。
        通过检查是否有可用的LLM provider来判断AstrBot是否完全启动。
        在插件重载时，由于AstrBot仍在运行，providers应该立即可用。
        """
        logger.info("等待AstrBot完全启动...")

        while True:
            # 检查是否有可用的LLM provider，这表明AstrBot已完全初始化
            if self.context.get_using_provider() is not None:
                try:
                    await self._initialize_plugin()
                    break
                except Exception as e:
                    logger.error(f"插件初始化失败: {e}", exc_info=True)
                    break

            await asyncio.sleep(1)

    async def _initialize_plugin(self):
        """
        执行插件的异步初始化。
        """
        logger.info("开始初始化 LivingMemory 插件...")
        try:
            # 1. 初始化 Provider
            self._initialize_providers()
            if not self.embedding_provider or not self.llm_provider:
                logger.error("Provider 初始化失败，插件无法正常工作。")
                return

            # 2. 初始化数据库和管理器
            data_dir = StarTools.get_data_dir()
            db_path = os.path.join(data_dir, "livingmemory.db")
            index_path = os.path.join(data_dir, "livingmemory.index")
            self.db = FaissVecDB(db_path, index_path, self.embedding_provider)
            await self.db.initialize()
            logger.info(f"数据库已初始化。数据目录: {data_dir}")

            self.faiss_manager = FaissManager(self.db)

            # 2.5. 初始化稀疏检索器
            sparse_config = self.config.get("sparse_retriever", {})
            if sparse_config.get("enabled", True):
                self.sparse_retriever = SparseRetriever(db_path, sparse_config)
                await self.sparse_retriever.initialize()
            else:
                self.sparse_retriever = None

            # 3. 初始化三大核心引擎
            self.recall_engine = RecallEngine(
                self.config.get("recall_engine", {}), 
                self.faiss_manager,
                self.sparse_retriever
            )
            self.reflection_engine = ReflectionEngine(
                self.config.get("reflection_engine", {}),
                self.llm_provider,
                self.faiss_manager,
            )
            self.forgetting_agent = ForgettingAgent(
                self.context,
                self.config.get("forgetting_agent", {}),
                self.faiss_manager,
            )

            # 4. 启动后台任务
            await self.forgetting_agent.start()

            # 初始化业务逻辑处理器
            self.memory_handler = MemoryHandler(self.context, self.config, self.faiss_manager)
            self.search_handler = SearchHandler(self.context, self.config, self.recall_engine, self.sparse_retriever)
            self.admin_handler = AdminHandler(self.context, self.config, self.faiss_manager, self.forgetting_agent, self.session_manager, self.recall_engine)
            self.fusion_handler = FusionHandler(self.context, self.config, self.recall_engine)

            # 启动 WebUI（如启用）
            await self._start_webui()

            # 标记初始化完成
            self._initialization_complete = True
            logger.info("LivingMemory 插件初始化成功！")

        except Exception as e:
            logger.critical(
                f"LivingMemory 插件初始化过程中发生严重错误: {e}", exc_info=True
            )
            self._initialization_complete = False

    async def _start_webui(self):
        """
        根据配置启动 WebUI 控制台。
        """
        webui_config = self.config.get("webui_settings", {}) if isinstance(self.config, dict) else {}
        if not webui_config.get("enabled"):
            return
        if self.webui_server:
            return
        if not self.faiss_manager:
            logger.warning("WebUI 控制台启动失败：记忆管理器尚未初始化")
            return
        if not webui_config.get("access_password"):
            logger.error("WebUI 控制台已启用但未配置入口密码，已跳过启动")
            return

        try:
            self.webui_server = WebUIServer(
                webui_config,
                self.faiss_manager,
                self.session_manager,
                self.recall_engine,
                self.reflection_engine,
                self.forgetting_agent,
                self.sparse_retriever,
            )
            await self.webui_server.start()
        except Exception as e:
            logger.error(f"启动 WebUI 控制台失败: {e}", exc_info=True)
            self.webui_server = None

    async def _stop_webui(self):
        """
        停止 WebUI 控制台。
        """
        if not self.webui_server:
            return
        try:
            await self.webui_server.stop()
        except Exception as e:
            logger.warning(f"停止 WebUI 控制台时出现异常: {e}", exc_info=True)
        finally:
            self.webui_server = None

    async def _wait_for_initialization(self, timeout: float = 30.0) -> bool:
        """
        等待插件初始化完成。

        Args:
            timeout: 超时时间（秒）

        Returns:
            bool: 是否初始化成功
        """
        if self._initialization_complete:
            return True

        if self._initialization_task:
            try:
                await asyncio.wait_for(self._initialization_task, timeout=timeout)
                return self._initialization_complete
            except asyncio.TimeoutError:
                logger.error(f"插件初始化超时（{timeout}秒）")
                return False
            except Exception as e:
                logger.error(f"等待插件初始化时发生错误: {e}")
                return False

        return False

    def _get_webui_url(self) -> Optional[str]:
        """
        获取 WebUI 访问地址。

        Returns:
            str: WebUI URL，如果未启用则返回 None
        """
        webui_config = self.config.get("webui_settings", {})
        if not webui_config.get("enabled") or not self.webui_server:
            return None

        host = webui_config.get("host", "127.0.0.1")
        port = webui_config.get("port", 8080)

        if host in ["0.0.0.0", ""]:
            return f"http://127.0.0.1:{port}"
        else:
            return f"http://{host}:{port}"

    def _build_deprecation_message(self, feature_name: str, webui_features: list) -> str:
        """
        构建废弃命令的统一引导消息。

        Args:
            feature_name: 功能名称
            webui_features: WebUI 功能列表

        Returns:
            str: 格式化的消息
        """
        webui_url = self._get_webui_url()

        if webui_url:
            features_text = "\n".join([f"  • {feature}" for feature in webui_features])
            message = (
                "⚠️ 此命令已废弃\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"请使用 WebUI {feature_name}。\n\n"
                f"🌐 访问地址: {webui_url}\n\n"
                f"💡 WebUI {feature_name}功能：\n"
                f"{features_text}\n"
            )
        else:
            message = (
                "⚠️ 此命令已废弃\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"请启用并使用 WebUI {feature_name}。\n\n"
                "使用 /lmem webui 查看如何启用 WebUI。"
            )

        return message

    def _initialize_providers(self):
        """
        初始化 Embedding 和 LLM provider。
        """
        # 初始化 Embedding Provider
        emb_id = self.config.get("provider_settings", {}).get("embedding_provider_id")
        if emb_id:
            self.embedding_provider = self.context.get_provider_by_id(emb_id)
            if self.embedding_provider:
                logger.info(f"成功从配置加载 Embedding Provider: {emb_id}")

        if not self.embedding_provider:
            # 检查是否有可用的embedding provider
            embedding_providers = self.context.provider_manager.embedding_provider_insts
            if embedding_providers:
                self.embedding_provider = embedding_providers[0]
                logger.info(
                    f"未指定 Embedding Provider，使用默认的: {self.embedding_provider.provider_config.get('id')}"
                )
            else:
                # 如果没有可用的embedding provider，则无法继续
                self.embedding_provider = None
                logger.error("没有可用的 Embedding Provider，插件将无法使用。")

        # 初始化 LLM Provider
        llm_id = self.config.get("provider_settings", {}).get("llm_provider_id")
        if llm_id:
            self.llm_provider = self.context.get_provider_by_id(llm_id)
            if self.llm_provider:
                logger.info(f"成功从配置加载 LLM Provider: {llm_id}")
        else:
            self.llm_provider = self.context.get_using_provider()
            logger.info("使用 AstrBot 当前默认的 LLM Provider。")

    @filter.on_llm_request()
    async def handle_memory_recall(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        [事件钩子] 在 LLM 请求前，查询并注入长期记忆。
        """
        # 等待初始化完成
        if not await self._wait_for_initialization():
            logger.warning("插件未完成初始化，跳过记忆召回。")
            return
            
        if not self.recall_engine:
            logger.debug("回忆引擎尚未初始化，跳过记忆召回。")
            return

        try:
            session_id = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin
                )
            )
            
            async with OperationContext("记忆召回", session_id):
                # 根据配置决定是否进行过滤
                filtering_config = self.config.get("filtering_settings", {})
                use_persona_filtering = filtering_config.get("use_persona_filtering", True)
                use_session_filtering = filtering_config.get("use_session_filtering", True)

                persona_id = await get_persona_id(self.context, event)

                recall_session_id = session_id if use_session_filtering else None
                recall_persona_id = persona_id if use_persona_filtering else None

                # 使用 RecallEngine 进行智能回忆，带重试机制
                recalled_memories = await retry_on_failure(
                    self.recall_engine.recall,
                    self.context, req.prompt, recall_session_id, recall_persona_id,
                    max_retries=1,  # 记忆召回失败影响较小，只重试1次
                    backoff_factor=0.5,
                    exceptions=(Exception,)
                )

                if recalled_memories:
                    # 格式化并注入记忆
                    memory_str = format_memories_for_injection(recalled_memories)
                    req.system_prompt = memory_str + "\n" + req.system_prompt
                    logger.info(
                        f"[{session_id}] 成功向 System Prompt 注入 {len(recalled_memories)} 条记忆。"
                    )

                # 管理会话历史
                session_data = self.session_manager.get_session(session_id)
                session_data["history"].append(
                    {"role": "user", "content": req.prompt}
                )

        except Exception as e:
            logger.error(f"处理 on_llm_request 钩子时发生错误: {e}", exc_info=True)

    @filter.on_llm_response()
    async def handle_memory_reflection(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        """
        [事件钩子] 在 LLM 响应后，检查是否需要进行反思和记忆存储。
        """
        # 等待初始化完成
        if not await self._wait_for_initialization():
            logger.warning("插件未完成初始化，跳过记忆反思。")
            return
            
        if not self.reflection_engine or resp.role != "assistant":
            logger.debug("反思引擎尚未初始化或响应不是助手角色，跳过反思。")
            return

        try:
            session_id = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin
                )
            )
            if not session_id:
                return

            # 添加助手响应到历史并增加轮次计数
            current_session = self.session_manager.get_session(session_id)
            current_session["history"].append(
                {"role": "assistant", "content": resp.completion_text}
            )
            current_session["round_count"] += 1

            # 检查是否满足总结条件
            trigger_rounds = self.config.get("reflection_engine", {}).get(
                "summary_trigger_rounds", 10
            )
            logger.debug(
                f"[{session_id}] 当前轮次: {current_session['round_count']}, 触发轮次: {trigger_rounds}"
            )
            if current_session["round_count"] >= trigger_rounds:
                logger.info(
                    f"[{session_id}] 对话达到 {trigger_rounds} 轮，启动反思任务。"
                )

                history_to_reflect = list(current_session["history"])
                # 重置会话
                self.session_manager.reset_session(session_id)

                persona_id = await get_persona_id(self.context, event)

                # 获取人格提示词
                persona_prompt = None
                filtering_config = self.config.get("filtering_settings", {})
                if filtering_config.get("use_persona_filtering", True) and persona_id:
                    list_personas = self.context.provider_manager.personas
                    # 获取当前人格的提示词
                    for persona_obj in list_personas:
                        if persona_obj.get("name") == persona_id:
                            persona_prompt = persona_obj.get("prompt")
                            break

                # 创建后台任务进行反思和存储
                logger.debug(
                    f"正在处理反思任务，session_id: {session_id}, persona_id: {persona_id}"
                )
                
                async def reflection_task():
                    async with OperationContext("记忆反思", session_id):
                        try:
                            # 使用重试机制执行反思
                            await retry_on_failure(
                                self.reflection_engine.reflect_and_store,
                                conversation_history=history_to_reflect,
                                session_id=session_id,
                                persona_id=persona_id,
                                persona_prompt=persona_prompt,
                                max_retries=2,  # 重试2次
                                backoff_factor=1.0,
                                exceptions=(Exception,)  # 捕获所有异常重试
                            )
                        except Exception as e:
                            logger.error(f"[{session_id}] 反思任务最终失败: {e}", exc_info=True)
                
                asyncio.create_task(reflection_task())

        except Exception as e:
            logger.error(f"处理 on_llm_response 钩子时发生错误: {e}", exc_info=True)

    # --- 命令处理 ---
    @filter.command_group("lmem")
    def lmem_group(self):
        """长期记忆管理命令组 /lmem"""
        pass

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("status")
    @handle_command_errors
    @require_handlers("admin_handler")
    async def lmem_status(self, event: AstrMessageEvent):
        """[管理员] 查看当前记忆库的状态。"""
        result = await self.admin_handler.get_memory_status()
        yield event.plain_result(self.admin_handler.format_status_for_display(result))

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("search")
    @handle_command_errors
    @require_handlers("search_handler")
    async def lmem_search(self, event: AstrMessageEvent, query: str, k: int = 3):
        """[管理员] 手动搜索记忆。"""
        result = await self.search_handler.search_memories(query, k)
        yield event.plain_result(self.search_handler.format_search_results_for_display(result))

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("forget")
    @handle_command_errors
    @require_handlers("admin_handler")
    async def lmem_forget(self, event: AstrMessageEvent, doc_id: int):
        """[管理员] 强制删除一条指定整数 ID 的记忆。"""
        result = await self.admin_handler.delete_memory(doc_id)
        yield event.plain_result(result["message"])

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("run_forgetting_agent")
    @deprecated("请使用 WebUI 系统管理页面", version="1.4.0")
    @handle_command_errors
    async def run_forgetting_agent(self, event: AstrMessageEvent):
        """[管理员] 手动触发一次遗忘代理的清理任务（已废弃）。

        此命令已废弃，请使用 WebUI 的系统管理页面。
        使用 /lmem webui 查看访问地址。
        """
        if not await self._wait_for_initialization():
            yield event.plain_result("插件尚未完成初始化，请稍后再试。")
            return

        message = self._build_deprecation_message(
            "系统管理页面",
            [
                "实时显示清理进度",
                "查看上次运行时间和结果",
                "配置遗忘策略参数",
                "可视化衰减曲线"
            ]
        )
        yield event.plain_result(message)

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("sparse_rebuild")
    @deprecated("请使用 WebUI 系统管理页面", version="1.4.0")
    @handle_command_errors
    async def lmem_sparse_rebuild(self, event: AstrMessageEvent):
        """[管理员] 重建稀疏检索索引（已废弃）。

        此命令已废弃，请使用 WebUI 的系统管理页面。
        使用 /lmem webui 查看访问地址。
        """
        if not await self._wait_for_initialization():
            yield event.plain_result("插件尚未完成初始化，请稍后再试。")
            return

        message = self._build_deprecation_message(
            "系统管理页面",
            [
                "实时显示重建进度",
                "查看索引状态和文档数",
                "查看最后更新时间",
                "批量索引管理操作"
            ]
        )
        yield event.plain_result(message)

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("search_mode")
    @deprecated("请使用 WebUI 配置页面", version="1.4.0")
    @handle_command_errors
    async def lmem_search_mode(self, event: AstrMessageEvent):
        """[管理员] 设置检索模式（已废弃）。

        此命令已废弃，请使用 WebUI 的配置页面。
        使用 /lmem webui 查看访问地址。
        """
        if not await self._wait_for_initialization():
            yield event.plain_result("插件尚未完成初始化，请稍后再试。")
            return

        message = self._build_deprecation_message(
            "配置页面",
            [
                "可视化选择检索模式",
                "调整 Top-K 参数",
                "配置召回策略",
                "实时查看配置效果"
            ]
        )
        yield event.plain_result(message)

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("sparse_test")
    @deprecated("请使用 WebUI 调试工具页面", version="1.4.0")
    @handle_command_errors
    async def lmem_sparse_test(self, event: AstrMessageEvent):
        """[管理员] 测试稀疏检索功能（已废弃）。

        此命令已废弃，请使用 WebUI 的调试工具页面。
        使用 /lmem webui 查看访问地址。
        """
        if not await self._wait_for_initialization():
            yield event.plain_result("插件尚未完成初始化，请稍后再试。")
            return

        message = self._build_deprecation_message(
            "调试工具页面",
            [
                "多模式并排对比",
                "性能指标分析",
                "结果差异高亮",
                "可视化性能图表"
            ]
        )
        yield event.plain_result(message)

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("edit")
    @deprecated("请使用 WebUI 进行记忆编辑操作", version="1.4.0")
    @handle_command_errors
    async def lmem_edit(self, event: AstrMessageEvent):
        """[管理员] 编辑记忆内容或元数据（已废弃）。

        此命令已废弃，请使用 WebUI 的记忆编辑功能。
        使用 /lmem webui 查看访问地址。
        """
        if not await self._wait_for_initialization():
            yield event.plain_result("插件尚未完成初始化，请稍后再试。")
            return

        message = self._build_deprecation_message(
            "记忆编辑页面",
            [
                "可视化表单，支持实时验证",
                "查看完整的更新历史记录",
                "批量编辑多条记忆",
                "支持更丰富的字段编辑"
            ]
        )
        yield event.plain_result(message)

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("info")
    @deprecated("请使用 WebUI 查看记忆详情", version="1.4.0")
    @handle_command_errors
    async def lmem_info(self, event: AstrMessageEvent):
        """[管理员] 查看记忆详细信息（已废弃）。

        此命令已废弃，请使用 WebUI 的记忆详情页。
        使用 /lmem webui 查看访问地址。
        """
        if not await self._wait_for_initialization():
            yield event.plain_result("插件尚未完成初始化，请稍后再试。")
            return

        message = self._build_deprecation_message(
            "记忆详情页",
            [
                "可视化展示记忆完整信息",
                "查看更新历史和时间线",
                "直接编辑记忆内容",
                "查看关联记忆和社区信息"
            ]
        )
        yield event.plain_result(message)

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("update")
    @deprecated("/lmem info", version="1.4.0")
    @handle_command_errors
    @require_handlers("memory_handler")
    async def lmem_update(self, event: AstrMessageEvent, memory_id: str):
        """[管理员] 查看记忆详细信息并提供编辑指引。（已废弃，请使用 /lmem info）

        用法: /lmem update <id>

        显示记忆的完整信息，并指引如何使用编辑命令。
        """
        # 内部调用新命令
        async for result in self.lmem_info(event, memory_id, full=False):
            yield result

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("history")
    @deprecated("/lmem info <id> --full", version="1.4.0")
    @handle_command_errors
    @require_handlers("memory_handler")
    async def lmem_history(self, event: AstrMessageEvent, memory_id: str):
        """[管理员] 查看记忆的更新历史。（已废弃，请使用 /lmem info <id> --full）"""
        # 内部调用新命令
        async for result in self.lmem_info(event, memory_id, full=True):
            yield result

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("config")
    @handle_command_errors
    @require_handlers("admin_handler")
    async def lmem_config(self, event: AstrMessageEvent, action: str = "show"):
        """[管理员] 查看或验证配置。

        用法: /lmem config [show|validate]

        动作:
          show - 显示当前配置
          validate - 验证配置有效性
        """
        result = await self.admin_handler.get_config_summary(action)
        if action == "show":
            yield event.plain_result(self.admin_handler.format_config_summary_for_display(result))
        else:
            yield event.plain_result(result["message"])

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("fusion")
    @handle_command_errors
    @require_handlers("fusion_handler")
    async def lmem_fusion(self, event: AstrMessageEvent, strategy: str = "show", param: str = ""):
        """[管理员] 管理检索融合策略。

        用法: /lmem fusion [strategy] [param=value]

        策略:
          show - 显示当前融合配置
          rrf - Reciprocal Rank Fusion (经典RRF)
          hybrid_rrf - 混合RRF (动态调整参数)
          weighted - 加权融合
          convex - 凸组合融合
          interleave - 交替融合
          rank_fusion - 基于排序的融合
          score_fusion - 基于分数的融合 (Borda Count)
          cascade - 级联融合
          adaptive - 自适应融合

        示例:
          /lmem fusion show
          /lmem fusion hybrid_rrf
          /lmem fusion convex lambda=0.6
          /lmem fusion weighted dense_weight=0.8
        """
        if strategy == "show":
            result = await self.fusion_handler.manage_fusion_strategy("show")
            yield event.plain_result(self.fusion_handler.format_fusion_config_for_display(result))
        else:
            result = await self.fusion_handler.manage_fusion_strategy(strategy, param)
            yield event.plain_result(result["message"])

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("test_fusion")
    @handle_command_errors
    @require_handlers("fusion_handler")
    async def lmem_test_fusion(self, event: AstrMessageEvent, query: str, k: int = 5):
        """[管理员] 测试不同融合策略的效果。

        用法: /lmem test_fusion <查询> [返回数量]

        这个命令会使用当前的融合策略进行搜索，并显示详细的融合过程信息。
        """
        yield event.plain_result(f"🔍 测试融合策略，查询: '{query}', 返回数量: {k}")
        result = await self.fusion_handler.test_fusion_strategy(query, k)
        yield event.plain_result(self.fusion_handler.format_fusion_test_for_display(result))

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("webui")
    @handle_command_errors
    async def lmem_webui(self, event: AstrMessageEvent):
        """[管理员] 显示 WebUI 访问信息。

        用法: /lmem webui

        显示 WebUI 控制台的访问地址、状态和功能说明。
        """
        # 等待初始化完成
        if not await self._wait_for_initialization():
            yield event.plain_result("插件尚未完成初始化，请稍后再试。")
            return

        webui_config = self.config.get("webui_settings", {})

        if not webui_config.get("enabled"):
            message = (
                "⚠️ WebUI 控制台未启用\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "请在配置文件中启用 WebUI：\n\n"
                "webui_settings:\n"
                "  enabled: true\n"
                "  access_password: \"test-token\"\n"
                "  host: \"127.0.0.1\"\n"
                "  port: 8080\n\n"
                "配置完成后重新加载插件即可使用。"
            )
            yield event.plain_result(message)
            return

        if not self.webui_server:
            yield event.plain_result("⚠️ WebUI 控制台启动失败，请检查配置和日志。")
            return

        host = webui_config.get("host", "127.0.0.1")
        port = webui_config.get("port", 8080)

        # 构建访问地址
        if host in ["0.0.0.0", ""]:
            access_url = f"http://127.0.0.1:{port}"
        else:
            access_url = f"http://{host}:{port}"

        message = (
            "🌐 LivingMemory WebUI 控制台\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 访问地址: {access_url}\n"
            "🔐 登录密码: 请查看配置文件中的 webui_settings.access_password\n\n"
            "💡 WebUI 功能说明：\n"
            "  • 📝 记忆管理 - 浏览、搜索、编辑、删除记忆\n"
            "  • 📊 统计分析 - 查看记忆分布和系统状态\n"
            "  • ⚙️ 配置管理 - 调整检索策略和融合算法\n"
            "  • 🛠️ 调试工具 - 测试检索效果和策略对比\n"
            "  • 🗂️ 批量操作 - 批量编辑、归档、导出记忆\n"
            "  • 🔧 系统管理 - 触发遗忘代理、重建索引\n\n"
            "📖 提示：使用 WebUI 可以更直观地管理记忆系统。"
        )

        yield event.plain_result(message)

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("help")
    @handle_command_errors
    async def lmem_help(self, event: AstrMessageEvent):
        """[管理员] 显示帮助信息。

        用法: /lmem help

        显示核心命令列表和 WebUI 使用指引。
        """
        message = (
            "📚 LivingMemory 命令帮助\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔧 核心命令：\n"
            "  /lmem status\n"
            "    查看记忆库状态（总数、类型分布等）\n\n"
            "  /lmem search <query> [k]\n"
            "    搜索记忆，k 为返回数量（默认3条）\n"
            "    示例: /lmem search 用户喜好 5\n\n"
            "  /lmem forget <id>\n"
            "    删除指定ID的记忆（紧急删除）\n"
            "    示例: /lmem forget 123\n\n"
            "  /lmem webui\n"
            "    显示 WebUI 访问信息和功能说明\n\n"
            "  /lmem help\n"
            "    显示本帮助信息\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🌐 高级功能请使用 WebUI 控制台\n\n"
            "使用 /lmem webui 查看 WebUI 访问地址。\n"
            "WebUI 提供以下高级功能：\n"
            "  • 记忆编辑和批量管理\n"
            "  • 配置检索策略和融合算法\n"
            "  • 测试和调试检索效果\n"
            "  • 系统维护和索引管理\n"
            "  • 统计分析和可视化\n\n"
            "💡 提示：命令行适合快速查询，WebUI 适合深度管理。"
        )

        yield event.plain_result(message)

    async def terminate(self):
        """
        插件停止时的清理逻辑。
        """
        logger.info("LivingMemory 插件正在停止...")
        await self._stop_webui()
        if self.forgetting_agent:
            await self.forgetting_agent.stop()
        if self.db:
            await self.db.close()
        logger.info("LivingMemory 插件已成功停止。")
