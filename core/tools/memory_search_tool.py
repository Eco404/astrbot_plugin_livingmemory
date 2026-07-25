"""供 Agent 主动调用的长期记忆回忆工具。"""

import asyncio
import inspect
import json
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..base.config_manager import ConfigManager
from ..models.conversation_models import stable_actor_id
from ..retrieval.unified_recall import (
    UnifiedRecallCoordinator,
    UnifiedRecallRequest,
)
from ..utils import get_persona_id


def _json_result(data: dict[str, Any]) -> str:
    """将工具结果稳定序列化为 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


@dataclass
class MemorySearchTool(FunctionTool[AstrAgentContext]):
    """长期记忆主动回忆工具。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: ConfigManager | None = None
    memory_engine: Any = None

    name: str = "recall_long_term_memory"
    description: str = (
        "Recall long-term memory when the current context is insufficient. "
        "Use concise, focused recall keywords instead of copying the full user message. "
        "Call this when the user asks you to recall prior facts, preferences, agreements, or older context, "
        "or when resolving ambiguous references requires checking memory. "
        "Prefer short topic phrases, named entities, preferences, commitments, or past events as recall keywords. "
        "If the first recall is not enough, refine the keywords and recall again."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Concise recall keywords for long-term memory. Prefer key entities, topics, preferences, commitments, or past events instead of copying the full user message.",
                },
                "k": {
                    "type": "integer",
                    "description": "Maximum number of memory items to return for one recall. Keep this small unless more evidence is needed.",
                    "default": 5,
                },
            },
            "required": ["query"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        query: str,
        k: int = 5,
    ) -> ToolExecResult:
        """执行长期记忆回忆。"""
        cleaned_query = (query or "").strip()
        if not cleaned_query:
            return _json_result(
                {
                    "query": "",
                    "count": 0,
                    "results": [],
                    "error": "query is empty",
                }
            )

        if (
            self.config_manager is None
            or self.memory_engine is None
            or self.context is None
        ):
            return _json_result(
                {
                    "query": cleaned_query,
                    "count": 0,
                    "results": [],
                    "error": "memory search tool is not initialized",
                }
            )

        try:
            event = context.context.event
            filtering_config = self.config_manager.filtering_settings
            use_persona_filtering = filtering_config.get("use_persona_filtering", True)
            use_session_filtering = filtering_config.get("use_session_filtering", True)

            raw_session_id = event.unified_msg_origin
            scope_resolver = getattr(
                self.memory_engine, "resolve_session_scope", None
            )
            scope_result = (
                scope_resolver(raw_session_id)
                if callable(scope_resolver)
                else None
            )
            session_scope = (
                await scope_result
                if inspect.isawaitable(scope_result)
                else [raw_session_id]
            )
            session_id = session_scope[0] if session_scope else raw_session_id
            coordinator = UnifiedRecallCoordinator(
                self.memory_engine, self.config_manager
            )
            topic_enabled = coordinator.topic_enabled()
            persona_id = (
                await get_persona_id(self.context, event)
                if use_persona_filtering or topic_enabled
                else None
            )

            recall_session_id = session_id if use_session_filtering else None
            recall_persona_id = persona_id if use_persona_filtering else None

            default_k = int(self.config_manager.get("recall_engine.top_k", 5))
            max_k = int(self.config_manager.get("recall_engine.max_k", 10))
            requested_k = default_k if k is None else k
            try:
                requested_k_int = int(requested_k)
            except (TypeError, ValueError):
                requested_k_int = default_k

            limited_k = max(1, min(requested_k_int, max_k))

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
            unified_outcome = await coordinator.search(
                UnifiedRecallRequest(
                    query=cleaned_query,
                    final_k=limited_k,
                    session_id=session_id,
                    persona_id=persona_id,
                    recall_session_id=recall_session_id,
                    recall_persona_id=recall_persona_id,
                    session_scope=list(session_scope or [session_id]),
                    current_actor_ids=current_actor_ids,
                    topic_enabled=topic_enabled,
                )
            )
            topic_outcome = unified_outcome.topic_outcome
            topic_results = unified_outcome.topic_results
            fragment_outcome = unified_outcome.fragment_outcome
            fragment_results = unified_outcome.fragment_results
            memories = unified_outcome.timeline_results
            serialized_results = [
                {
                    "id": item.topic_uid,
                    "content": f"Topic: {item.content}".strip(),
                    "score": item.final_score,
                    "importance": item.topic.importance,
                    "memory_layer": "topic",
                    "source_timeline_count": len(item.sources),
                    "affect_match_score": item.affect_match_score,
                    "affect_match_boost": item.affect_match_boost,
                    "affect_event_count": len(item.selected_affect_events),
                }
                for item in topic_results
            ]
            serialized_results.extend(
                {
                    "id": item.fragment_uid,
                    "content": item.content,
                    "score": item.final_score,
                    "importance": item.fragment.importance,
                    "memory_layer": "topic_fragment",
                    "parent_topic_uid": item.topic_uid,
                    "fragment_body_suppressed": item.body_suppressed,
                    "fragment_fact_count": len(item.fact_contents),
                    "source_timeline_count": len(item.fragment.timeline_uids),
                    "narrative_perspective": "first_person_assistant",
                }
                for item in fragment_results
            )
            for memory in memories:
                metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
                serialized_results.append(
                    {
                        "id": memory.doc_id,
                        "content": memory.content,
                        "score": memory.final_score,
                        "importance": metadata.get("importance"),
                        "session_id": metadata.get("session_id"),
                        "persona_id": metadata.get("persona_id"),
                        "create_time": metadata.get("create_time"),
                        "last_access_time": metadata.get("last_access_time"),
                        **(
                            {
                                "memory_layer": (
                                    "timeline_supplement"
                                    if topic_results
                                    else "timeline"
                                )
                            }
                            if topic_enabled
                            else {}
                        ),
                    }
                )

            if serialized_results:
                try:
                    await coordinator.record_access(unified_outcome)
                except Exception:
                    logger.warning(
                        "记忆工具已生成结果，但访问统计更新失败",
                        exc_info=True,
                    )

            return _json_result(
                {
                    "query": cleaned_query,
                    "applied_filters": {
                        "session_filtered": use_session_filtering,
                        "persona_filtered": use_persona_filtering,
                    },
                    "count": len(serialized_results),
                    "results": serialized_results,
                    "diagnostics": unified_outcome.diagnostics(),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"记忆工具检索失败: {e}", exc_info=True)
            return _json_result(
                {
                    "query": cleaned_query,
                    "count": 0,
                    "results": [],
                    "error": "internal_error",
                }
            )
