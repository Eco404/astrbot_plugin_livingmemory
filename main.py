# -*- coding: utf-8 -*-
"""
main.py - LivingMemory 插件主文件
负责插件注册、初始化所有引擎、绑定事件钩子以及管理生命周期。
"""

import asyncio
import os
import json
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
from .core.engines.reflection_engine import ReflectionEngine
from .core.engines.forgetting_agent import ForgettingAgent
from .core.retrieval import SparseRetriever
from .core.utils import get_persona_id, format_memories_for_injection, get_now_datetime, retry_on_failure, OperationContext, safe_parse_metadata
from .core.config_validator import validate_config, merge_config_with_defaults

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
        import time
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
        import time
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
    "1.0.0",
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
        
        # 初始化状态标记
        self._initialization_complete = False
        self._initialization_task: Optional[asyncio.Task] = None
        
        # 会话管理器
        session_config = self.config.get("session_manager", {})
        self.session_manager = SessionManager(
            max_sessions=session_config.get("max_sessions", 1000),
            session_ttl=session_config.get("session_ttl", 3600)
        )

        # 启动异步初始化流程
        self._initialization_task = asyncio.create_task(self._initialize_plugin())

    async def _initialize_plugin(self):
        """
        执行插件的异步初始化。
        """
        logger.info("开始异步初始化 LivingMemory 插件...")
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

            # 标记初始化完成
            self._initialization_complete = True
            logger.info("LivingMemory 插件初始化成功！")

        except Exception as e:
            logger.critical(
                f"LivingMemory 插件初始化过程中发生严重错误: {e}", exc_info=True
            )
            self._initialization_complete = False

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
    async def lmem_status(self, event: AstrMessageEvent):
        """[管理员] 查看当前记忆库的状态。"""
        if not self.faiss_manager or not self.faiss_manager.db:
            yield event.plain_result("记忆库尚未初始化。")
            return

        count = await self.faiss_manager.db.count_documents()
        yield event.plain_result(f"📊 LivingMemory 记忆库状态：\n- 总记忆数: {count}")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("search")
    async def lmem_search(self, event: AstrMessageEvent, query: str, k: int = 3):
        """[管理员] 手动搜索记忆。"""
        if not self.recall_engine:
            yield event.plain_result("回忆引擎尚未初始化。")
            return

        results = await self.recall_engine.recall(self.context, query, k=k)
        if not results:
            yield event.plain_result(f"未能找到与 '{query}' 相关的记忆。")
            return

        response_parts = [f"为您找到 {len(results)} 条相关记忆："]
        tz = get_now_datetime(self.context).tzinfo  # 获取当前时区

        for res in results:
            metadata = res.data.get("metadata", {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}

            def format_timestamp(ts):
                if not ts:
                    return "未知"
                try:
                    dt_utc = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                    dt_local = dt_utc.astimezone(tz)
                    return dt_local.strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    return "未知"

            create_time_str = format_timestamp(metadata.get("create_time"))
            last_access_time_str = format_timestamp(metadata.get("last_access_time"))

            importance_score = metadata.get("importance", 0.0)
            event_type = metadata.get("event_type", "未知")

            card = (
                f"ID: {res.data['id']}\n"
                f"记 忆 度: {res.similarity:.2f}\n"
                f"重 要 性: {importance_score:.2f}\n"
                f"记忆类型: {event_type}\n\n"
                f"内容: {res.data['text']}\n\n"
                f"创建于: {create_time_str}\n"
                f"最后访问: {last_access_time_str}"
            )
            response_parts.append(card)

        response = "\n\n".join(response_parts)
        yield event.plain_result(response)

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("forget")
    async def lmem_forget(self, event: AstrMessageEvent, doc_id: int):
        """[管理员] 强制删除一条指定整数 ID 的记忆。"""
        if not self.faiss_manager:
            yield event.plain_result("记忆库尚未初始化。")
            return

        try:
            await self.faiss_manager.delete_memories([doc_id])
            yield event.plain_result(f"已成功删除 ID 为 {doc_id} 的记忆。")
        except Exception as e:
            yield event.plain_result(f"删除记忆时发生错误: {e}")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("run_forgetting_agent")
    async def run_forgetting_agent(self, event: AstrMessageEvent):
        """[管理员] 手动触发一次遗忘代理的清理任务。"""
        if not self.forgetting_agent:
            yield event.plain_result("遗忘代理尚未初始化。")
            return

        yield event.plain_result("正在后台手动触发遗忘代理任务...")
        try:
            logger.debug("1")
            await self.forgetting_agent._prune_memories()
            await self.context.send_message(
                event.unified_msg_origin, MessageChain().message("遗忘代理任务执行完毕。")
            )
        except Exception as e:
            logger.error(f"遗忘代理任务执行失败: {e}", exc_info=True)
            await self.context.send_message(
                event.unified_msg_origin, MessageChain().message(f"遗忘代理任务执行失败: {e}")
            )

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("sparse_rebuild")
    async def lmem_sparse_rebuild(self, event: AstrMessageEvent):
        """[管理员] 重建稀疏检索索引。"""
        if not self.sparse_retriever:
            yield event.plain_result("稀疏检索器未启用。")
            return

        yield event.plain_result("正在重建稀疏检索索引...")
        try:
            await self.sparse_retriever.rebuild_index()
            yield event.plain_result("稀疏检索索引重建完成。")
        except Exception as e:
            logger.error(f"重建稀疏索引失败: {e}", exc_info=True)
            yield event.plain_result(f"重建稀疏索引失败: {e}")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("search_mode")
    async def lmem_search_mode(self, event: AstrMessageEvent, mode: str):
        """[管理员] 设置检索模式。
        
        用法: /lmem search_mode <mode>
        
        模式:
          hybrid - 混合检索（默认）
          dense - 纯密集检索
          sparse - 纯稀疏检索
        """
        valid_modes = ["hybrid", "dense", "sparse"]
        if mode not in valid_modes:
            yield event.plain_result(f"无效的模式，请使用: {', '.join(valid_modes)}")
            return

        if not self.recall_engine:
            yield event.plain_result("回忆引擎尚未初始化。")
            return

        # 更新配置
        self.recall_engine.config["retrieval_mode"] = mode
        yield event.plain_result(f"检索模式已设置为: {mode}")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("sparse_test")
    async def lmem_sparse_test(self, event: AstrMessageEvent, query: str, k: int = 5):
        """[管理员] 测试稀疏检索功能。"""
        if not self.sparse_retriever:
            yield event.plain_result("稀疏检索器未启用。")
            return

        try:
            results = await self.sparse_retriever.search(query=query, limit=k)
            
            if not results:
                yield event.plain_result(f"未找到与 '{query}' 相关的记忆。")
                return

            response_parts = [f"🔍 稀疏检索结果 ({len(results)} 条):"]
            
            for i, res in enumerate(results, 1):
                response_parts.append(f"\n{i}. [ID: {res.doc_id}] Score: {res.score:.3f}")
                response_parts.append(f"   内容: {res.content[:100]}{'...' if len(res.content) > 100 else ''}")
                
                # 显示元数据
                metadata = res.metadata
                if metadata.get("event_type"):
                    response_parts.append(f"   类型: {metadata['event_type']}")
                if metadata.get("importance"):
                    response_parts.append(f"   重要性: {metadata['importance']:.2f}")

            yield event.plain_result("\n".join(response_parts))

        except Exception as e:
            logger.error(f"稀疏检索测试失败: {e}", exc_info=True)
            yield event.plain_result(f"稀疏检索测试失败: {e}")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("edit")
    async def lmem_edit(self, event: AstrMessageEvent, memory_id: str, field: str, value: str, reason: str = ""):
        """[管理员] 编辑记忆内容或元数据。
        
        用法: /lmem edit <id> <字段> <值> [原因]
        
        字段:
          content - 记忆内容
          importance - 重要性评分 (0.0-1.0)
          type - 事件类型 (FACT/PREFERENCE/GOAL/OPINION/RELATIONSHIP/OTHER)
          status - 状态 (active/archived/deleted)
        
        示例:
          /lmem edit 123 content 这是新的记忆内容 修正了错误信息
          /lmem edit 123 importance 0.9 提高重要性
          /lmem edit 123 type PREFERENCE 重新分类
          /lmem edit 123 status archived 项目已完成
        """
        if not self.faiss_manager:
            yield event.plain_result("记忆库尚未初始化。")
            return

        try:
            # 解析 memory_id 为整数或字符串
            try:
                memory_id_int = int(memory_id)
                memory_id_to_use = memory_id_int
            except ValueError:
                memory_id_to_use = memory_id

            # 解析字段和值
            updates = {}
            
            if field == "content":
                updates["content"] = value
            elif field == "importance":
                try:
                    updates["importance"] = float(value)
                    if not 0.0 <= updates["importance"] <= 1.0:
                        yield event.plain_result("❌ 重要性评分必须在 0.0 到 1.0 之间")
                        return
                except ValueError:
                    yield event.plain_result("❌ 重要性评分必须是数字")
                    return
            elif field == "type":
                valid_types = ["FACT", "PREFERENCE", "GOAL", "OPINION", "RELATIONSHIP", "OTHER"]
                if value not in valid_types:
                    yield event.plain_result(f"❌ 无效的事件类型，必须是: {', '.join(valid_types)}")
                    return
                updates["event_type"] = value
            elif field == "status":
                valid_statuses = ["active", "archived", "deleted"]
                if value not in valid_statuses:
                    yield event.plain_result(f"❌ 无效的状态，必须是: {', '.join(valid_statuses)}")
                    return
                updates["status"] = value
            else:
                yield event.plain_result(f"❌ 未知的字段 '{field}'，支持的字段: content, importance, type, status")
                return

            # 执行更新
            result = await self.faiss_manager.update_memory(
                memory_id=memory_id_to_use,
                update_reason=reason or f"更新{field}",
                **updates
            )

            if result["success"]:
                response_parts = [f"✅ {result['message']}"]
                
                if result["updated_fields"]:
                    response_parts.append("\n📋 已更新的字段:")
                    for f in result["updated_fields"]:
                        response_parts.append(f"  - {f}")
                
                # 如果更新了内容，显示预览
                if "content" in updates and len(updates["content"]) > 100:
                    response_parts.append(f"\n📝 内容预览: {updates['content'][:100]}...")
                
                yield event.plain_result("\n".join(response_parts))
            else:
                yield event.plain_result(f"❌ 更新失败: {result['message']}")

        except Exception as e:
            logger.error(f"编辑记忆时发生错误: {e}", exc_info=True)
            yield event.plain_result(f"编辑记忆时发生错误: {e}")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("update")
    async def lmem_update(self, event: AstrMessageEvent, memory_id: str):
        """[管理员] 交互式编辑记忆。
        
        用法: /lmem update <id>
        
        会引导你逐步选择要更新的字段。
        """
        if not self.faiss_manager:
            yield event.plain_result("记忆库尚未初始化。")
            return

        try:
            # 解析 memory_id
            try:
                memory_id_int = int(memory_id)
                docs = await self.faiss_manager.db.document_storage.get_documents(ids=[memory_id_int])
            except ValueError:
                docs = await self.faiss_manager.db.document_storage.get_documents(
                    metadata_filters={"memory_id": memory_id}
                )

            if not docs:
                yield event.plain_result(f"未找到ID为 {memory_id} 的记忆。")
                return

            doc = docs[0]
            metadata = (
                json.loads(doc["metadata"])
                if isinstance(doc["metadata"], str)
                else doc["metadata"]
            )

            # 显示当前记忆信息
            response = f"📝 记忆 {memory_id} 的当前信息:\n\n"
            response += f"内容: {doc['content'][:100]}{'...' if len(doc['content']) > 100 else ''}\n\n"
            response += f"重要性: {metadata.get('importance', 'N/A')}\n"
            response += f"类型: {metadata.get('event_type', 'N/A')}\n"
            response += f"状态: {metadata.get('status', 'active')}\n\n"
            response += "请回复要更新的字段编号:\n"
            response += "1. 内容\n"
            response += "2. 重要性\n"
            response += "3. 事件类型\n"
            response += "4. 状态\n"
            response += "0. 取消"

            yield event.plain_result(response)

            # 这里应该等待用户回复，但由于命令系统的限制，
            # 我们只能引导用户使用 /lmem edit 命令
            yield event.plain_result(f"\n请使用 /lmem edit {memory_id} <字段> <值> [原因] 来更新记忆")

        except Exception as e:
            logger.error(f"查看记忆时发生错误: {e}", exc_info=True)
            yield event.plain_result(f"查看记忆时发生错误: {e}")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("history")
    async def lmem_history(self, event: AstrMessageEvent, memory_id: str):
        """[管理员] 查看记忆的更新历史。"""
        if not self.faiss_manager or not self.faiss_manager.db:
            yield event.plain_result("记忆库尚未初始化。")
            return

        try:
            # 解析 memory_id
            try:
                memory_id_int = int(memory_id)
                docs = await self.faiss_manager.db.document_storage.get_documents(ids=[memory_id_int])
            except ValueError:
                docs = await self.faiss_manager.db.document_storage.get_documents(
                    metadata_filters={"memory_id": memory_id}
                )

            if not docs:
                yield event.plain_result(f"未找到ID为 {memory_id} 的记忆。")
                return

            doc = docs[0]
            metadata = (
                json.loads(doc["metadata"])
                if isinstance(doc["metadata"], str)
                else doc["metadata"]
            )

            response_parts = [f"📝 记忆 {memory_id} 的详细信息:"]
            response_parts.append(f"\n内容: {doc['content']}")
            
            # 基本信息
            response_parts.append(f"\n📊 基本信息:")
            response_parts.append(f"- 重要性: {metadata.get('importance', 'N/A')}")
            response_parts.append(f"- 类型: {metadata.get('event_type', 'N/A')}")
            response_parts.append(f"- 状态: {metadata.get('status', 'active')}")
            
            # 时间信息
            tz = get_now_datetime(self.context).tzinfo
            create_time = metadata.get('create_time')
            if create_time:
                dt = datetime.fromtimestamp(create_time, tz=timezone.utc)
                dt_local = dt.astimezone(tz)
                response_parts.append(f"- 创建时间: {dt_local.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # 更新历史
            update_history = metadata.get('update_history', [])
            if update_history:
                response_parts.append(f"\n🔄 更新历史 ({len(update_history)} 次):")
                for i, update in enumerate(update_history[-5:], 1):  # 只显示最近5次
                    timestamp = update.get('timestamp')
                    if timestamp:
                        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                        dt_local = dt.astimezone(tz)
                        time_str = dt_local.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        time_str = "未知"
                    
                    response_parts.append(f"\n{i}. {time_str}")
                    response_parts.append(f"   原因: {update.get('reason', 'N/A')}")
                    response_parts.append(f"   字段: {', '.join(update.get('fields', []))}")
            else:
                response_parts.append("\n🔄 暂无更新记录")

            yield event.plain_result("\n".join(response_parts))

        except Exception as e:
            logger.error(f"查看记忆历史时发生错误: {e}", exc_info=True)
            yield event.plain_result(f"查看记忆历史时发生错误: {e}")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("config")
    async def lmem_config(self, event: AstrMessageEvent, action: str = "show"):
        """[管理员] 查看或验证配置。
        
        用法: /lmem config [show|validate]
        
        动作:
          show - 显示当前配置
          validate - 验证配置有效性
        """
        if action == "show":
            try:
                # 显示主要配置项
                config_summary = []
                config_summary.append("📋 LivingMemory 配置摘要:")
                config_summary.append("")
                
                # 会话管理器配置
                sm_config = self.config.get("session_manager", {})
                config_summary.append(f"🗂️ 会话管理:")
                config_summary.append(f"  - 最大会话数: {sm_config.get('max_sessions', 1000)}")
                config_summary.append(f"  - 会话TTL: {sm_config.get('session_ttl', 3600)}秒")
                config_summary.append(f"  - 当前会话数: {self.session_manager.get_session_count()}")
                config_summary.append("")
                
                # 回忆引擎配置
                re_config = self.config.get("recall_engine", {})
                config_summary.append(f"🧠 回忆引擎:")
                config_summary.append(f"  - 检索模式: {re_config.get('retrieval_mode', 'hybrid')}")
                config_summary.append(f"  - 返回数量: {re_config.get('top_k', 5)}")
                config_summary.append(f"  - 召回策略: {re_config.get('recall_strategy', 'weighted')}")
                config_summary.append("")
                
                # 反思引擎配置
                rf_config = self.config.get("reflection_engine", {})
                config_summary.append(f"💭 反思引擎:")
                config_summary.append(f"  - 触发轮次: {rf_config.get('summary_trigger_rounds', 10)}")
                config_summary.append(f"  - 重要性阈值: {rf_config.get('importance_threshold', 0.5)}")
                config_summary.append("")
                
                # 遗忘代理配置
                fa_config = self.config.get("forgetting_agent", {})
                config_summary.append(f"🗑️ 遗忘代理:")
                config_summary.append(f"  - 启用状态: {'是' if fa_config.get('enabled', True) else '否'}")
                config_summary.append(f"  - 检查间隔: {fa_config.get('check_interval_hours', 24)}小时")
                config_summary.append(f"  - 保留天数: {fa_config.get('retention_days', 90)}天")
                
                yield event.plain_result("\n".join(config_summary))
                
            except Exception as e:
                yield event.plain_result(f"显示配置时发生错误: {e}")
                
        elif action == "validate":
            try:
                from .core.config_validator import validate_config
                # 重新验证当前配置
                validate_config(self.config)
                yield event.plain_result("✅ 配置验证通过，所有参数均有效")
                
            except Exception as e:
                yield event.plain_result(f"❌ 配置验证失败: {e}")
                
        else:
            yield event.plain_result("❌ 无效的动作，请使用 'show' 或 'validate'")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("fusion")
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
        if not self.recall_engine:
            yield event.plain_result("❌ 回忆引擎尚未初始化。")
            return
        
        if strategy == "show":
            # 显示当前融合配置
            fusion_config = self.config.get("fusion", {})
            current_strategy = fusion_config.get("strategy", "rrf")
            
            response = ["🔄 当前检索融合配置:"]
            response.append(f"策略: {current_strategy}")
            response.append("")
            
            if current_strategy in ["rrf", "hybrid_rrf"]:
                response.append(f"RRF参数k: {fusion_config.get('rrf_k', 60)}")
                if current_strategy == "hybrid_rrf":
                    response.append(f"多样性奖励: {fusion_config.get('diversity_bonus', 0.1)}")
            
            if current_strategy in ["weighted", "convex", "rank_fusion", "score_fusion"]:
                response.append(f"密集权重: {fusion_config.get('dense_weight', 0.7)}")
                response.append(f"稀疏权重: {fusion_config.get('sparse_weight', 0.3)}")
            
            if current_strategy == "convex":
                response.append(f"凸组合λ: {fusion_config.get('convex_lambda', 0.5)}")
            
            if current_strategy == "interleave":
                response.append(f"交替比例: {fusion_config.get('interleave_ratio', 0.5)}")
            
            if current_strategy == "rank_fusion":
                response.append(f"排序偏置: {fusion_config.get('rank_bias_factor', 0.1)}")
            
            response.append("")
            response.append("💡 各策略特点:")
            response.append("• rrf: 经典方法，平衡性好")
            response.append("• hybrid_rrf: 动态调整，适应查询类型")
            response.append("• weighted: 简单加权，可解释性强")
            response.append("• convex: 凸组合，数学严格")
            response.append("• interleave: 交替选择，保证多样性")
            response.append("• rank_fusion: 基于排序位置")
            response.append("• score_fusion: Borda Count投票")
            response.append("• cascade: 稀疏初筛+密集精排")
            response.append("• adaptive: 根据查询自适应")
            
            yield event.plain_result("\n".join(response))
            
        elif strategy in ["rrf", "hybrid_rrf", "weighted", "convex", "interleave", 
                         "rank_fusion", "score_fusion", "cascade", "adaptive"]:
            
            # 更新融合策略
            if "fusion" not in self.config:
                self.config["fusion"] = {}
            
            old_strategy = self.config["fusion"].get("strategy", "rrf")
            self.config["fusion"]["strategy"] = strategy
            
            # 处理参数
            if param and "=" in param:
                key, value = param.split("=", 1)
                key = key.strip()
                value = value.strip()
                
                try:
                    # 尝试转换为数字
                    if "." in value:
                        self.config["fusion"][key] = float(value)
                    else:
                        self.config["fusion"][key] = int(value)
                        
                    logger.info(f"更新融合参数 {key} = {value}")
                except ValueError:
                    yield event.plain_result(f"❌ 参数值无效: {value}")
                    return
            
            # 更新 RecallEngine 中的融合配置
            if hasattr(self.recall_engine, 'result_fusion'):
                self.recall_engine.result_fusion.strategy = strategy
                self.recall_engine.result_fusion.config = self.config["fusion"]
                
                # 更新融合器的参数
                fusion_obj = self.recall_engine.result_fusion
                fusion_obj.dense_weight = self.config["fusion"].get("dense_weight", 0.7)
                fusion_obj.sparse_weight = self.config["fusion"].get("sparse_weight", 0.3)
                fusion_obj.rrf_k = self.config["fusion"].get("rrf_k", 60)
                fusion_obj.convex_lambda = self.config["fusion"].get("convex_lambda", 0.5)
                fusion_obj.interleave_ratio = self.config["fusion"].get("interleave_ratio", 0.5)
                fusion_obj.rank_bias_factor = self.config["fusion"].get("rank_bias_factor", 0.1)
            
            yield event.plain_result(f"✅ 融合策略已从 '{old_strategy}' 更新为 '{strategy}'{f' (参数: {param})' if param else ''}")
            
        else:
            yield event.plain_result("❌ 不支持的融合策略。使用 /lmem fusion show 查看可用选项。")

    @permission_type(PermissionType.ADMIN)
    @lmem_group.command("test_fusion")
    async def lmem_test_fusion(self, event: AstrMessageEvent, query: str, k: int = 5):
        """[管理员] 测试不同融合策略的效果。
        
        用法: /lmem test_fusion <查询> [返回数量]
        
        这个命令会使用当前的融合策略进行搜索，并显示详细的融合过程信息。
        """
        if not self.recall_engine:
            yield event.plain_result("❌ 回忆引擎尚未初始化。")
            return
        
        try:
            yield event.plain_result(f"🔍 测试融合策略，查询: '{query}', 返回数量: {k}")
            
            # 执行搜索
            session_id = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            persona_id = await get_persona_id(self.context, event)
            
            results = await self.recall_engine.recall(
                self.context, query, session_id, persona_id, k
            )
            
            if not results:
                yield event.plain_result("📭 未找到相关记忆。")
                return
            
            # 获取融合配置
            fusion_config = self.config.get("fusion", {})
            current_strategy = fusion_config.get("strategy", "rrf")
            
            response = [f"🎯 融合测试结果 (策略: {current_strategy})"]
            response.append("=" * 50)
            
            for i, result in enumerate(results, 1):
                # 解析元数据
                metadata = safe_parse_metadata(result.data.get("metadata", {}))
                importance = metadata.get("importance", 0.0)
                event_type = metadata.get("event_type", "未知")
                
                response.append(f"\n{i}. [ID: {result.data['id']}] 分数: {result.similarity:.4f}")
                response.append(f"   重要性: {importance:.3f} | 类型: {event_type}")
                response.append(f"   内容: {result.data['text'][:100]}{'...' if len(result.data['text']) > 100 else ''}")
            
            response.append("\n" + "=" * 50)
            response.append(f"💡 当前融合配置:")
            response.append(f"   策略: {current_strategy}")
            if current_strategy in ["rrf", "hybrid_rrf"]:
                response.append(f"   RRF-k: {fusion_config.get('rrf_k', 60)}")
            if current_strategy in ["weighted", "convex"]:
                response.append(f"   密集权重: {fusion_config.get('dense_weight', 0.7)}")
                response.append(f"   稀疏权重: {fusion_config.get('sparse_weight', 0.3)}")
            
            yield event.plain_result("\n".join(response))
            
        except Exception as e:
            logger.error(f"融合策略测试失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 测试失败: {e}")

    async def terminate(self):
        """
        插件停止时的清理逻辑。
        """
        logger.info("LivingMemory 插件正在停止...")
        if self.forgetting_agent:
            await self.forgetting_agent.stop()
        if self.db:
            await self.db.close()
        logger.info("LivingMemory 插件已成功停止。")
