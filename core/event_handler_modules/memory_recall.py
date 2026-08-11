"""
记忆召回模块
负责长期记忆的检索和注入
"""

import asyncio
import inspect
import time
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from ..embedding_signature import provider_identity
from ..fact_temporal import normalize_fact_temporal
from ..managers.timeline_topic_continuation import (
    PENDING_MESSAGE_EMBEDDING_FORMAT,
)
from ..models.conversation_models import stable_actor_id
from ..retrieval.unified_recall import (
    UnifiedRecallCoordinator,
    UnifiedRecallRequest,
)
from ..utils import (
    OperationContext,
    format_memories_for_fake_tool_call,
    format_memories_for_injection,
    get_persona_id,
)

if TYPE_CHECKING:
    from ..base.config_manager import ConfigManager
    from ..managers.conversation_manager import ConversationManager
    from ..managers.memory_engine import MemoryEngine
    from ..utils.injection_adapter import InjectionAdapter
    from .message_utils import MessageUtils


class MemoryRecall:
    """记忆召回类"""

    def __init__(
        self,
        context,
        config_manager: "ConfigManager",
        memory_engine: "MemoryEngine",
        conversation_manager: "ConversationManager",
        message_utils: "MessageUtils",
        injection_adapter: "InjectionAdapter",
        persona_resolver=None,
    ):
        """
        初始化记忆召回模块

        Args:
            context: AstrBot上下文
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            conversation_manager: 会话管理器
            message_utils: 消息处理工具
            injection_adapter: 注入适配器
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.conversation_manager = conversation_manager
        self.message_utils = message_utils
        self.injection_adapter = injection_adapter
        self.persona_resolver = persona_resolver or get_persona_id
        self.recall_coordinator = UnifiedRecallCoordinator(
            memory_engine, config_manager
        )
        # Compatibility for tests and callers that inspect query branches.
        self.recall_pipeline = self.recall_coordinator.timeline_pipeline

    async def handle_memory_recall(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """Query and inject long-term memory before LLM request"""
        trace_started = time.time()
        try:
            raw_session_id = event.unified_msg_origin
            session_scope = await self.memory_engine.resolve_session_scope(raw_session_id)
            session_id = session_scope[0] if session_scope else raw_session_id
            logger.debug(f"[DEBUG-Recall] 获取到 unified_msg_origin: {session_id}")

            # 检测异常session_id
            if session_id and (
                "Error:" in session_id or "error:" in session_id.lower()
            ):
                logger.warning(
                    f"[{session_id}] 检测到异常的session_id，这可能导致记忆功能异常。"
                )

            async with OperationContext("记忆召回", session_id):
                prompt_text = getattr(req, "prompt", "")
                extra_parts = getattr(req, "extra_user_content_parts", [])
                has_prompt_text = isinstance(prompt_text, str) and bool(
                    prompt_text.strip()
                )
                has_extra_parts = bool(extra_parts)

                if not has_prompt_text and not has_extra_parts:
                    logger.debug(f"[{session_id}] 请求中无可用用户内容，跳过记忆召回")
                    return

                normalized = self._normalize_text_only_context_parts(req, session_id)
                if normalized > 0:
                    logger.info(f"[{session_id}] 已归一化 {normalized} 条纯文本历史消息")

                # 自动删除旧的注入记忆
                if self.config_manager.get("recall_engine.auto_remove_injected", True):
                    removed = self._remove_injected_memories_from_context(
                        req, session_id
                    )
                    removed += self._remove_fake_tool_call_from_context(req, session_id)
                    if removed > 0:
                        logger.info(
                            f"[{session_id}] 已清理 {removed} 处历史记忆注入片段"
                        )

                # 先提取用户消息（消息存储和召回都需要）。组件提取保留
                # 图片、文件等非纯文本消息，避免它们从原始证据链中消失。
                raw_query = await self.message_utils.get_event_message_str(event)
                extracted_query = await self.message_utils.extract_message_content(
                    event, req
                )
                actual_query = raw_query or extracted_query

                request_query = (
                    prompt_text.strip() if isinstance(prompt_text, str) else ""
                )

                # Profile scope is persona-isolated and must be resolved before the
                # top_k early return, because message storage and profile creation
                # are independent from ordinary memory recall.
                persona_id = await self.persona_resolver(self.context, event)

                # 存储用户消息（仅私聊），无论是否启用召回都需要
                is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
                stored_user_message = None
                if not is_group and actual_query:
                    # 原始事件内容优先于 ProviderRequest.prompt，后者可能已被
                    # 其他插件改写，不适合作为可追溯的原始对话证据。
                    message_to_store = extracted_query or raw_query or request_query
                    stored_user_message = await self.conversation_manager.add_message_from_event(
                        event=event,
                        role="user",
                        content=message_to_store,
                        event_source="incoming_private_message",
                    )
                    await self.message_utils.enforce_message_limit(session_id)
                    if stored_user_message is not None:
                        sender_id = (
                            event.get_sender_id()
                            if hasattr(event, "get_sender_id")
                            else getattr(event, "sender_id", "")
                        )
                        platform = (
                            event.get_platform_name()
                            if hasattr(event, "get_platform_name")
                            else ""
                        )
                        display_name = (
                            event.get_sender_name()
                            if hasattr(event, "get_sender_name")
                            else None
                        )
                        if sender_id:
                            ensure_profile = getattr(
                                self.memory_engine,
                                "ensure_private_user_profile",
                                None,
                            )
                            if inspect.iscoroutinefunction(ensure_profile):
                                await ensure_profile(
                                    session_id=session_id,
                                    persona_id=persona_id,
                                    actor_id=stable_actor_id(
                                        platform, str(sender_id), "human"
                                    ),
                                    display_name=(
                                        str(display_name)
                                        if display_name is not None
                                        else None
                                    ),
                                )

                # 若 top_k <= 0，跳过记忆检索和注入，但上述清理和消息存储已执行
                top_k = self.config_manager.get("recall_engine.top_k", 5)
                if top_k <= 0:
                    logger.info(
                        f"[{session_id}] top_k={top_k} <= 0，跳过记忆检索和注入"
                    )
                    return

                if not actual_query:
                    logger.warning(f"[{session_id}] 原始用户消息为空，跳过记忆召回")
                    return

                # 获取过滤配置
                filtering_config = self.config_manager.filtering_settings
                use_persona_filtering = filtering_config.get(
                    "use_persona_filtering", True
                )
                use_session_filtering = filtering_config.get(
                    "use_session_filtering", True
                )

                # 获取 persona_id，与 AstrBot 主流程保持一致的三级优先级：
                # 1. session_service_config（最高）
                # 2. req.conversation.persona_id（会话级）
                # 3. 全局默认人格（最低）
                # 注意：on_llm_request 钩子在 _ensure_persona_and_skills 之前触发，
                # 因此不能直接依赖 req.system_prompt 已注入人格，需自行走完整优先级。
                recall_session_id = session_id if use_session_filtering else None
                recall_persona_id = persona_id if use_persona_filtering else None

                expansion_enabled = bool(
                    self.config_manager.get(
                        "recall_engine.inject_with_recent_context", False
                    )
                )
                recent_messages: list[dict] = []
                if expansion_enabled:
                    try:
                        # get_context 实际按时间升序返回。使用原始结构保留 role，
                        # RecallPipeline 会移除刚写入数据库的当前用户消息。
                        recent_messages = await self.conversation_manager.get_context(
                            session_id,
                            max_messages=5,
                            format_for_llm=False,
                        )
                    except Exception as e:
                        logger.warning(f"[{session_id}] 获取上下文扩展失败: {e}")

                visible_start, visible_end = await self._visible_context_range(
                    req,
                    session_id,
                    current_message_stored=bool(not is_group and actual_query),
                )
                assistant_mode = self.config_manager.get(
                    "recall_engine.assistant_context_mode", "exclude"
                )
                topic_enabled = self.recall_coordinator.topic_enabled()
                current_actor_ids: set[str] = set()
                if topic_enabled:
                    topic_config = getattr(
                        self.memory_engine.topic_recall_pipeline, "config", {}
                    ) or {}
                    sender_id = (
                        event.get_sender_id()
                        if hasattr(event, "get_sender_id")
                        else getattr(event, "sender_id", "")
                    )
                    platform = (
                        event.get_platform_name()
                        if hasattr(event, "get_platform_name")
                        else "unknown"
                    )
                    current_actor_ids = {
                        stable_actor_id(platform, str(sender_id), "human")
                    } if sender_id else set()
                    if (
                        is_group
                        and not bool(
                            topic_config.get(
                                "recall_group_current_sender_only", True
                            )
                        )
                    ):
                        current_actor_ids = await self._visible_group_actor_ids(
                            session_id=session_id,
                            visible_start=visible_start,
                            visible_end=visible_end,
                            current_actor_ids=current_actor_ids,
                            fallback_platform=platform,
                        )
                unified_outcome = await self.recall_coordinator.search(
                    UnifiedRecallRequest(
                        query=actual_query,
                        final_k=top_k,
                        session_id=session_id,
                        persona_id=persona_id,
                        recall_session_id=recall_session_id,
                        recall_persona_id=recall_persona_id,
                        session_scope=list(session_scope or [session_id]),
                        recent_messages=recent_messages,
                        expansion_enabled=expansion_enabled,
                        assistant_mode=assistant_mode,
                        visible_message_start_index=visible_start,
                        visible_message_end_index=visible_end,
                        current_actor_ids=current_actor_ids,
                        topic_enabled=topic_enabled,
                    )
                )
                recall_outcome = unified_outcome.timeline_outcome
                topic_outcome = unified_outcome.topic_outcome
                fragment_outcome = unified_outcome.fragment_outcome
                recalled_memories = unified_outcome.timeline_results
                topic_results = unified_outcome.topic_results
                fragment_results = unified_outcome.fragment_results
                if stored_user_message is None and is_group:
                    stored_user_message = await self._find_latest_stored_user_message(
                        session_id,
                        actual_query,
                    )
                await self._cache_current_query_vector(
                    stored_user_message,
                    recall_outcome,
                    topic_outcome,
                )
                branch_summary = ", ".join(
                    f"{item.name}:{item.weight:.2f}"
                    for item in recall_outcome.branches
                )
                logger.info(
                    f"[{session_id}] 结构化记忆召回完成: 查询分支=[{branch_summary}], "
                    f"候选={len(recall_outcome.candidates)}, "
                    f"入选={len(recalled_memories)}, "
                    f"阈值={recall_outcome.applied_threshold:.3f}, "
                    f"上下文重叠过滤={recall_outcome.overlap_suppressed}"
                )
                if topic_outcome is not None:
                    logger.info(
                        f"[{session_id}] Topic 召回完成: "
                        f"候选={len(topic_outcome.candidates)}, "
                        f"入选={len(topic_results)}, "
                        f"阈值={topic_outcome.applied_threshold:.3f}, "
                        f"上下文高覆盖过滤={topic_outcome.context_suppressed}, "
                        f"片段补充={len(fragment_results)}, "
                        f"兼容 Timeline 补充={len(recalled_memories)}"
                    )

                if topic_results or fragment_results or recalled_memories:
                    logger.info(
                        f"[{session_id}] 检索到 {len(topic_results)} 条 Topic、"
                        f"{len(fragment_results)} 条片段、"
                        f"{len(recalled_memories)} 条 Timeline"
                    )

                    # 格式化并注入记忆
                    memory_list = [
                        self._topic_memory_dict(item) for item in topic_results
                    ] + [
                        self._fragment_memory_dict(item)
                        for item in fragment_results
                    ] + [
                        self._timeline_memory_dict(
                            item, as_supplement=bool(topic_results)
                        )
                        for item in recalled_memories
                    ]

                    # 输出详细记忆信息
                    for i, mem in enumerate(memory_list, 1):
                        logger.debug(
                            f"[{session_id}] 记忆 #{i}: 得分={mem['score']:.3f}, "
                            f"层={mem['metadata'].get('memory_layer', 'timeline')}, "
                            f"内容={mem['content'][:100]}..."
                        )

                    # 根据配置选择注入方式（含 Provider 兼容降级）
                    configured_method = self.config_manager.get(
                        "recall_engine.injection_method", "extra_user_content"
                    )
                    provider = None
                    if configured_method in (
                        "fake_tool_call",
                        "fake_tool_call_deepseek_v4",
                    ):
                        try:
                            provider = self.context.get_using_provider(session_id)
                        except Exception as e:
                            logger.warning(
                                f"[{session_id}] 获取当前 Provider 失败，"
                                f"将按无 Provider 继续解析注入模式: {e}"
                            )
                    injection_method, fallback_reason = (
                        self.injection_adapter.resolve(provider, configured_method)
                    )
                    if fallback_reason:
                        logger.warning(
                            f"[{session_id}] 注入模式从 {configured_method} 降级为 "
                            f"{injection_method}: {fallback_reason}"
                        )

                    memory_str = format_memories_for_injection(memory_list)
                    injection_succeeded = False
                    injected_messages = None

                    if injection_method == "user_message_before":
                        req.prompt = memory_str + "\n\n" + (req.prompt or "")
                        injection_succeeded = True
                        logger.info(
                            f"[{session_id}] 成功向用户消息前注入 {len(memory_list)} 条记忆"
                        )
                    elif injection_method == "user_message_after":
                        req.prompt = (req.prompt or "") + "\n\n" + memory_str
                        injection_succeeded = True
                        logger.info(
                            f"[{session_id}] 成功向用户消息后注入 {len(memory_list)} 条记忆"
                        )
                    elif injection_method == "fake_tool_call":
                        fake_messages = format_memories_for_fake_tool_call(
                            memory_list,
                            query=actual_query,
                            k=self.config_manager.get("recall_engine.top_k", 5),
                            session_filtered=use_session_filtering,
                            persona_filtered=use_persona_filtering,
                        )
                        if fake_messages:
                            req.contexts.extend(fake_messages)
                            injected_messages = fake_messages
                            injection_succeeded = True
                            logger.info(
                                f"[{session_id}] 成功以伪造工具调用方式注入 "
                                f"{len(memory_list)} 条记忆"
                            )
                    else:
                        # extra_user_content（推荐）：追加到用户消息末尾，
                        # 不影响前缀缓存且 mark_as_temp 后不污染对话历史
                        req.extra_user_content_parts.append(
                            TextPart(text=memory_str).mark_as_temp()
                        )
                        injection_succeeded = True
                        logger.info(
                            f"[{session_id}] 成功向用户消息末尾注入 "
                            f"{len(memory_list)} 条记忆"
                        )
                    if injection_succeeded:
                        try:
                            await self.recall_coordinator.record_access(
                                unified_outcome
                            )
                        except Exception:
                            logger.warning(
                                f"[{session_id}] 记忆注入已成功，但访问统计更新失败",
                                exc_info=True,
                            )
                        await self._record_production_trace(
                            status="injected",
                            query_text=actual_query,
                            session_id=session_id,
                            persona_id=persona_id,
                            elapsed_ms=(time.time() - trace_started) * 1000,
                            request_data={
                                "query_text": actual_query,
                                "top_k": top_k,
                                "session_filter": recall_session_id,
                                "persona_filter": recall_persona_id,
                                "query_branches": [
                                    {
                                        "name": item.name,
                                        "role": item.role,
                                        "weight": item.weight,
                                        "text": item.text,
                                    }
                                    for item in recall_outcome.branches
                                ],
                            },
                            result_data={"items": memory_list},
                            diagnostics={
                                "timeline": recall_outcome.diagnostics(),
                                "topic": topic_outcome.diagnostics()
                                if topic_outcome is not None
                                else None,
                                "topic_fragments": fragment_outcome.diagnostics()
                                if fragment_outcome is not None
                                else None,
                            },
                            injection={
                                "configured_method": configured_method,
                                "actual_method": injection_method,
                                "fallback_reason": fallback_reason,
                                "content": memory_str,
                                "messages": injected_messages,
                            },
                        )
                else:
                    logger.info(f"[{session_id}] 未找到相关记忆")
                    await self._record_production_trace(
                        status="no_match",
                        query_text=actual_query,
                        session_id=session_id,
                        persona_id=persona_id,
                        elapsed_ms=(time.time() - trace_started) * 1000,
                        request_data={"query_text": actual_query, "top_k": top_k},
                        diagnostics={
                            "timeline": recall_outcome.diagnostics(),
                            "topic": topic_outcome.diagnostics()
                            if topic_outcome is not None
                            else None,
                        },
                    )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理 on_llm_request 钩子时发生错误: {e}", exc_info=True)
            await self._record_production_trace(
                status="failed",
                query_text=str(locals().get("actual_query") or ""),
                session_id=str(locals().get("session_id") or "") or None,
                persona_id=str(locals().get("persona_id") or "") or None,
                elapsed_ms=(time.time() - trace_started) * 1000,
                error=str(e),
            )

    async def _record_production_trace(self, **payload) -> None:
        """Best-effort diagnostics: recording must never affect a chat request."""
        store = getattr(self.memory_engine, "recall_trace_store", None)
        if store is None:
            return
        try:
            if not await store.production_enabled():
                return
            result_data = payload.get("result_data") or {}
            items = result_data.get("items", []) if isinstance(result_data, dict) else []
            await store.record(
                trace_type="production",
                mode="current",
                result_count=len(items) if isinstance(items, list) else 0,
                **payload,
            )
        except Exception:
            logger.warning("保存实际召回记录失败，已忽略", exc_info=True)

    @staticmethod
    def _topic_memory_dict(item) -> dict:
        metadata = item.topic.metadata if isinstance(item.topic.metadata, dict) else {}
        keywords = [
            str(value) for value in metadata.get("keywords", []) if str(value).strip()
        ]
        selected_atoms = [
            atom
            for atom in item.atoms[:4]
            if str(atom.get("content") or "").strip()
        ]
        facts = [str(atom.get("content") or "").strip() for atom in selected_atoms]
        fact_temporal = []
        for atom in selected_atoms:
            atom_metadata = (
                atom.get("metadata", {})
                if isinstance(atom.get("metadata"), dict)
                else {}
            )
            if atom_metadata.get("evidence_started_at") is not None:
                temporal = normalize_fact_temporal(atom_metadata)
            else:
                temporal = normalize_fact_temporal(
                    {},
                    fallback_started_at=atom.get("event_started_at")
                    or item.topic.started_at,
                    fallback_ended_at=atom.get("event_ended_at")
                    or item.topic.ended_at,
                    fallback_basis="topic_window",
                )
            fact_temporal.append(temporal)
        return {
            "id": item.topic_uid,
            "content": f"Topic: {item.content}".strip(),
            "score": item.final_score,
            "metadata": {
                "memory_layer": "topic",
                "title": item.topic.title,
                "importance": item.topic.importance,
                "confidence": item.topic.confidence,
                "status": item.topic.status.value,
                "topics": [item.topic.title, *keywords[:5]],
                "key_facts": facts,
                "key_fact_temporal": fact_temporal,
                "source_timeline_count": len(item.sources),
                "affect_match_score": item.affect_match_score,
                "affect_match_boost": item.affect_match_boost,
                "affect_event_count": len(item.selected_affect_events),
                "context_coverage": item.context_coverage,
                "started_at": item.topic.started_at,
                "ended_at": item.topic.ended_at,
            },
            "timestamp": None,
        }

    @staticmethod
    def _timeline_memory_dict(item, *, as_supplement: bool) -> dict:
        metadata = dict(item.metadata) if isinstance(item.metadata, dict) else {}
        source_window = (
            metadata.get("source_window", {})
            if isinstance(metadata.get("source_window"), dict)
            else {}
        )
        fallback_start = source_window.get("started_at") or metadata.get("create_time")
        fallback_end = source_window.get("ended_at") or fallback_start
        key_facts = metadata.get("key_facts", [])
        temporal_rows = metadata.get("key_fact_temporal", [])
        if isinstance(key_facts, list):
            metadata["key_fact_temporal"] = [
                normalize_fact_temporal(
                    temporal_rows[index]
                    if isinstance(temporal_rows, list)
                    and index < len(temporal_rows)
                    else {},
                    fallback_started_at=fallback_start,
                    fallback_ended_at=fallback_end,
                    fallback_basis="timeline_window",
                )
                for index in range(len(key_facts))
            ]
        metadata["memory_layer"] = "timeline_supplement" if as_supplement else "timeline"
        return {
            "id": getattr(item, "doc_id", None),
            "content": item.content,
            "score": item.final_score,
            "metadata": metadata,
            "timestamp": metadata.get("create_time"),
        }

    @staticmethod
    def _fragment_memory_dict(item) -> dict:
        facts_by_content = {
            str(fact.get("content") or "").strip(): fact
            for fact in item.fragment.facts
            if isinstance(fact, dict) and str(fact.get("content") or "").strip()
        }
        selected_facts = [str(value) for value in item.fact_contents if str(value)]
        fact_temporal = [
            normalize_fact_temporal(
                facts_by_content.get(content, {}),
                fallback_started_at=item.fragment.started_at,
                fallback_ended_at=item.fragment.ended_at,
                fallback_basis="fragment_window",
            )
            for content in selected_facts
        ]
        return {
            "id": item.fragment_uid,
            "content": item.content,
            "score": item.final_score,
            "metadata": {
                "memory_layer": "topic_fragment",
                "parent_topic_uid": item.topic_uid,
                "title": item.fragment.label,
                "importance": item.fragment.importance,
                "confidence": item.fragment.confidence,
                "fragment_body_suppressed": item.body_suppressed,
                "fragment_fact_count": len(item.fact_contents),
                "key_facts": selected_facts,
                "key_fact_temporal": fact_temporal,
                "affect_match_score": item.affect_match_score,
                "affect_match_boost": item.affect_match_boost,
                "affect_event_count": len(item.selected_affect_events),
                "source_timeline_count": len(item.fragment.timeline_uids),
                "context_coverage": item.context_coverage,
                "narrative_perspective": "first_person_assistant",
                "narrator_actor_id": item.fragment.metadata.get(
                    "conversation_roles", {}
                ).get("timeline_narrators", {}),
                "started_at": item.fragment.started_at,
                "ended_at": item.fragment.ended_at,
            },
            "timestamp": item.fragment.started_at,
        }

    async def _visible_group_actor_ids(
        self,
        *,
        session_id: str,
        visible_start: int | None,
        visible_end: int | None,
        current_actor_ids: set[str],
        fallback_platform: str,
    ) -> set[str]:
        """Resolve human speakers from the persisted range visible to this request.

        The current sender is supplied separately because group capture ordering is
        adapter-dependent and the current event may not have reached the store yet.
        Missing or unreadable range data deliberately falls back to that sender only.
        """
        actor_ids = set(current_actor_ids)
        if (
            visible_start is None
            or visible_end is None
            or int(visible_end) <= int(visible_start)
        ):
            return actor_ids
        try:
            messages = await self.conversation_manager.get_messages_range(
                session_id,
                start_index=max(0, int(visible_start)),
                end_index=max(0, int(visible_end)),
            )
        except Exception as exc:
            logger.debug(
                f"[{session_id}] 无法读取群聊可见参与者，退化为当前发言者: {exc}"
            )
            return actor_ids
        for message in messages:
            metadata = getattr(message, "metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            role = str(getattr(message, "role", "") or "").casefold()
            actor_type = str(metadata.get("actor_type") or "").casefold()
            if (
                role == "assistant"
                or actor_type == "assistant"
                or bool(metadata.get("is_bot_message"))
            ):
                continue
            sender_id = str(getattr(message, "sender_id", "") or "").strip()
            if not sender_id:
                continue
            platform = str(
                getattr(message, "platform", "") or fallback_platform or "unknown"
            )
            actor_ids.add(stable_actor_id(platform, sender_id, "human"))
        return actor_ids

    async def _visible_context_range(
        self,
        req: ProviderRequest,
        session_id: str,
        *,
        current_message_stored: bool,
    ) -> tuple[int | None, int | None]:
        """Estimate the persisted message-index range already visible to the LLM."""
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list):
            return None, None
        visible_count = sum(
            1
            for item in contexts
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
        )
        if visible_count <= 0:
            return None, None
        try:
            total = await self.conversation_manager.store.get_message_count(session_id)
            end_index = max(0, int(total) - (1 if current_message_stored else 0))
        except Exception as e:
            logger.debug(f"[{session_id}] 无法计算当前上下文消息范围: {e}")
            return None, None
        return max(0, end_index - visible_count), end_index

    def _remove_injected_memories_from_context(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """从请求上下文中移除临时注入的记忆片段"""
        import re

        from ..base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER

        removed = 0

        # 清理 system_prompt（兼容旧版本注入残留）
        if hasattr(req, "system_prompt") and req.system_prompt:
            if isinstance(req.system_prompt, str):
                original_prompt = req.system_prompt
                if (
                    MEMORY_INJECTION_HEADER in original_prompt
                    and MEMORY_INJECTION_FOOTER in original_prompt
                ):
                    # 使用正则清理记忆片段
                    pattern = re.compile(
                        re.escape(MEMORY_INJECTION_HEADER)
                        + r".*?"
                        + re.escape(MEMORY_INJECTION_FOOTER),
                        re.DOTALL,
                    )
                    cleaned_prompt = pattern.sub("", original_prompt)
                    cleaned_prompt = re.sub(r"\n{3,}", "\n\n", cleaned_prompt).strip()
                    req.system_prompt = cleaned_prompt
                    if cleaned_prompt != original_prompt:
                        removed += 1

        # 清理 extra_user_content_parts（通过 mark_as_temp/_no_save 标记）
        parts_before = len(getattr(req, "extra_user_content_parts", []))
        if parts_before > 0:
            req.extra_user_content_parts = [
                part
                for part in req.extra_user_content_parts
                if not self._is_livingmemory_temp_part(part)
            ]
            parts_after = len(req.extra_user_content_parts)
            removed += parts_before - parts_after

        return removed

    def _is_livingmemory_temp_part(self, part) -> bool:
        """判断是否为 LivingMemory 本轮临时注入的 extra_user_content part"""
        from ..base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER

        text = getattr(part, "text", "")
        return (
            getattr(part, "_no_save", False)
            and isinstance(text, str)
            and MEMORY_INJECTION_HEADER in text
            and MEMORY_INJECTION_FOOTER in text
        )

    async def _cache_current_query_vector(
        self, message, recall_outcome, topic_outcome
    ) -> None:
        """Best-effort reuse of the current query vector for topic continuation."""
        if (
            message is None
            or topic_outcome is None
            or not bool(
                self.config_manager.get(
                    "reflection_engine.topic_continuation_enabled", True
                )
            )
        ):
            return
        try:
            branches = list(getattr(recall_outcome, "branches", []) or [])
            vectors = list(getattr(topic_outcome, "query_vectors", []) or [])
            current_index = next(
                index
                for index, branch in enumerate(branches)
                if getattr(branch, "name", "") == "current"
            )
            if current_index >= len(vectors):
                return
            branch_text = str(getattr(branches[current_index], "text", "") or "")
            message_text = str(getattr(message, "content", "") or "")
            normalize = lambda value: " ".join(value.split())
            if normalize(branch_text) != normalize(message_text):
                return
            topic_pipeline = getattr(
                self.memory_engine, "topic_recall_pipeline", None
            )
            retriever = getattr(topic_pipeline, "retriever", None)
            refresh = getattr(retriever, "refresh_providers", None)
            if callable(refresh):
                refresh()
            provider = getattr(retriever, "embedding_provider", None)
            provider_id, model_id = provider_identity(provider)
            vector = [float(value) for value in vectors[current_index]]
            if not vector:
                return
            await self.conversation_manager.store.upsert_pending_message_feature(
                message_id=int(message.id),
                session_id=message.session_id,
                text=message_text,
                embedding=vector,
                provider_id=provider_id,
                model_id=model_id,
                input_format_version=PENDING_MESSAGE_EMBEDDING_FORMAT,
            )
        except (StopIteration, TypeError, ValueError, AttributeError) as exc:
            logger.debug("短期查询向量未缓存: %s", exc)
        except Exception as exc:
            logger.warning("缓存短期查询向量失败，不影响本轮召回: %s", exc)

    async def _find_latest_stored_user_message(self, session_id: str, text: str):
        """Locate the group message persisted by the independent capture hook."""
        try:
            total = await self.conversation_manager.store.get_message_count(session_id)
            if total <= 0:
                return None
            messages = await self.conversation_manager.get_messages_range(
                session_id=session_id,
                start_index=total - 1,
                end_index=total,
            )
            if not messages or messages[0].role != "user":
                return None
            normalize = lambda value: " ".join(str(value or "").split())
            return messages[0] if normalize(messages[0].content) == normalize(text) else None
        except Exception as exc:
            logger.debug("无法定位群聊当前消息的短期查询特征载体: %s", exc)
            return None

    def _normalize_text_only_context_parts(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """把历史中的纯文本 content parts 折叠回字符串，避免污染长期上下文格式"""
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list):
            return 0

        normalized = 0
        for msg in contexts:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list) or not content:
                continue

            text_parts = []
            text_only = True
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    text_only = False
                    break
                text_parts.append(str(part.get("text", "") or ""))

            if not text_only:
                continue

            msg["content"] = "".join(text_parts)
            normalized += 1

        if normalized:
            logger.debug(f"[{session_id}] 已归一化 {normalized} 条纯文本历史 content parts")
        return normalized

    def _remove_fake_tool_call_from_context(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """从请求上下文中移除伪造的工具调用记忆（fake_tool_call 注入方式）

        识别并移除以 FAKE_TOOL_CALL_ID_PREFIX 为 ID 前缀的
        assistant(tool_calls) + tool(result) 消息对。
        """
        from ..base.constants import FAKE_TOOL_CALL_ID_PREFIX

        if not hasattr(req, "contexts") or not req.contexts:
            return 0

        removed = 0
        indices_to_remove: set[int] = set()
        fake_call_ids: set[str] = set()

        try:
            # 单轮扫描：同时收集伪造 assistant(tool_calls) 和对应 tool(result) 消息
            for i, msg in enumerate(req.contexts):
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        tc_id = (
                            tc.get("id", "")
                            if isinstance(tc, dict)
                            else getattr(tc, "id", "")
                        )
                        if tc_id.startswith(FAKE_TOOL_CALL_ID_PREFIX):
                            fake_call_ids.add(tc_id)
                            indices_to_remove.add(i)
                elif role == "tool":
                    tc_id = msg.get("tool_call_id", "")
                    if tc_id in fake_call_ids:
                        indices_to_remove.add(i)

            # 从后往前删除，避免索引偏移
            for i in sorted(indices_to_remove, reverse=True):
                req.contexts.pop(i)
                removed += 1

        except Exception:
            pass

        return removed
