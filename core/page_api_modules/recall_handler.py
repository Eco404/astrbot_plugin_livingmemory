"""
召回测试处理模块
"""

import time
from typing import TYPE_CHECKING, Any

from quart import request

from astrbot.api import logger

from ..retrieval.recall_pipeline import RecallPipeline

if TYPE_CHECKING:
    from .utils import PageApiUtils


class RecallHandler:
    """召回测试处理器"""

    def __init__(self, utils: "PageApiUtils"):
        """
        初始化召回测试处理器

        Args:
            utils: PageApiUtils 工具实例
        """
        self.utils = utils

    async def test_recall(
        self,
        memory_engine,
        config_manager=None,
        conversation_manager=None,
    ) -> dict[str, Any]:
        """
        测试记忆召回功能

        Payload:
            - query: 查询文本（必需）
            - k: 返回结果数量（默认5，最大50）
            - session_id: 会话ID过滤（可选）

        Returns:
            包含召回结果和性能指标的字典
        """
        payload = await request.get_json(silent=True) or {}
        query_text = str(payload.get("query", "")).strip()
        if not query_text:
            return self.utils.error("查询内容不能为空")

        try:
            k = min(50, max(1, int(payload.get("k", 5))))
        except (TypeError, ValueError):
            return self.utils.error("k 必须是整数")

        session_id = self.utils.optional_text(payload.get("session_id"))

        expansion_enabled = self._config_value(
            config_manager, "recall_engine.inject_with_recent_context", False
        )
        if "use_recent_context" in payload:
            expansion_enabled = bool(payload.get("use_recent_context"))
        assistant_mode = self._config_value(
            config_manager, "recall_engine.assistant_context_mode", "exclude"
        )
        recent_messages: list[dict[str, Any]] = []
        if expansion_enabled and session_id and conversation_manager is not None:
            try:
                recent_messages = await conversation_manager.get_context(
                    session_id,
                    max_messages=4,
                    format_for_llm=False,
                )
            except Exception as exc:
                logger.warning(f"[PageAPI] 读取召回测试上下文失败: {exc}")

        try:
            start_time = time.time()
            outcome = await RecallPipeline(memory_engine, config_manager).search(
                current_query=query_text,
                final_k=k,
                session_id=session_id,
                persona_id=None,
                recent_messages=recent_messages,
                expansion_enabled=bool(expansion_enabled),
                assistant_mode=str(assistant_mode),
                track_access=False,
            )
            results = outcome.results
            topic_outcome = None
            fragment_outcome = None
            fragment_results = []
            topic_space_id = None
            topic_config = getattr(
                memory_engine.topic_recall_pipeline, "config", {}
            ) or {}
            if (
                getattr(memory_engine, "topic_memory_enabled", False) is True
                and bool(
                    self._config_value(
                        config_manager, "topic_memory.recall_enabled", True
                    )
                )
                and session_id
            ):
                try:
                    spaces = await memory_engine.topic_memory_store.find_memory_spaces_for_session(
                        session_id
                    )
                    if spaces:
                        topic_space_id = spaces[0]
                        topic_outcome = await memory_engine.topic_recall_pipeline.search(
                            branches=outcome.branches,
                            memory_space_id=topic_space_id,
                            final_k=min(
                                k,
                                int(topic_config.get("recall_top_k", 3)),
                            ),
                            track_access=False,
                        )
                        if topic_outcome.results:
                            supplement_k = min(
                                int(topic_config.get("timeline_supplement_k", 2)),
                                max(0, k - len(topic_outcome.results)),
                            )
                            fragment_outcome = await memory_engine.topic_recall_pipeline.search_fragment_supplements(
                                branches=outcome.branches,
                                topic_results=topic_outcome.results,
                                limit=supplement_k,
                            )
                            fragment_results = fragment_outcome.results
                            if fragment_outcome.available_count:
                                results = []
                            else:
                                results = memory_engine.topic_recall_pipeline.select_timeline_supplements(
                                    results,
                                    topic_outcome.results,
                                    supplement_k,
                                )
                except Exception:
                    logger.warning(
                        "[PageAPI] Topic 召回失败，召回测试回退 Timeline",
                        exc_info=True,
                    )
                    topic_outcome = None
                    topic_space_id = None
            elapsed_time = (time.time() - start_time) * 1000
        except Exception as exc:
            logger.error(f"[PageAPI] 召回测试失败: {exc}", exc_info=True)
            return self.utils.error(str(exc))

        formatted_results = []
        if topic_outcome is not None:
            for result in topic_outcome.results:
                formatted_results.append(
                    {
                        "memory_id": result.topic_uid,
                        "content": f"Topic: {result.topic.title}\n{result.topic.summary}",
                        "similarity_score": round(float(result.final_score), 4),
                        "score_percentage": round(float(result.final_score) * 100, 2),
                        "metadata": {
                            "memory_layer": "topic",
                            "title": result.topic.title,
                            "importance": result.topic.importance,
                            "status": result.topic.status.value,
                        },
                        "score_breakdown": {
                            "topic_relevance_score": round(
                                result.relevance_score, 6
                            ),
                            "topic_embedding_score": round(
                                result.embedding_score, 6
                            ),
                            "topic_keyword_score": round(result.keyword_score, 6),
                            "topic_base_relevance_score": round(
                                result.base_relevance_score
                                if result.base_relevance_score is not None
                                else result.relevance_score,
                                6,
                            ),
                            **(
                                {
                                    "topic_rerank_score": round(
                                        result.rerank_score, 6
                                    )
                                }
                                if result.rerank_score is not None
                                else {}
                            ),
                        },
                    }
                )
        for result in fragment_results:
            formatted_results.append(
                {
                    "memory_id": result.fragment_uid,
                    "content": result.content,
                    "similarity_score": round(float(result.final_score), 4),
                    "score_percentage": round(float(result.final_score) * 100, 2),
                    "metadata": {
                        "memory_layer": "topic_fragment",
                        "title": result.fragment.label,
                        "parent_topic_uid": result.topic_uid,
                        "importance": result.fragment.importance,
                        "narrative_perspective": "third_person",
                    },
                    "score_breakdown": {
                        "fragment_relevance_score": round(
                            result.relevance_score, 6
                        ),
                        "fragment_embedding_score": round(
                            result.embedding_score, 6
                        ),
                        "fragment_keyword_score": round(
                            result.keyword_score, 6
                        ),
                        "parent_topic_relevance": round(
                            result.parent_topic_relevance, 6
                        ),
                        **(
                            {"fragment_rerank_score": round(result.rerank_score, 6)}
                            if result.rerank_score is not None
                            else {}
                        ),
                    },
                }
            )
        for result in results:
            score_breakdown = {
                key: round(float(value), 6)
                for key, value in (
                    getattr(result, "score_breakdown", None) or {}
                ).items()
                if isinstance(value, (int, float))
            }
            metadata = {
                "session_id": result.metadata.get("session_id"),
                "persona_id": result.metadata.get("persona_id"),
                "importance": result.metadata.get("importance", 0.5),
                "memory_type": result.metadata.get("memory_type", "GENERAL"),
                "status": result.metadata.get("status", "active"),
                "create_time": result.metadata.get("create_time"),
                "memory_layer": (
                    "timeline_supplement"
                    if topic_outcome is not None and topic_outcome.results
                    else "timeline"
                ),
            }
            metadata.update(score_breakdown)
            formatted_results.append(
                {
                    "memory_id": result.doc_id,
                    "content": result.content,
                    "similarity_score": round(float(result.final_score), 4),
                    "score_percentage": round(float(result.final_score) * 100, 2),
                    "metadata": metadata,
                    "score_breakdown": score_breakdown,
                }
            )

        return self.utils.ok(
            {
                "results": formatted_results,
                "total": len(formatted_results),
                "query": query_text,
                "k": k,
                "session_id_filter": session_id,
                "elapsed_time_ms": round(elapsed_time, 2),
                "diagnostics": {
                    **outcome.diagnostics(),
                    "topic_space_id": topic_space_id,
                    "topic": topic_outcome.diagnostics()
                    if topic_outcome is not None
                    else None,
                    "topic_fragments": fragment_outcome.diagnostics()
                    if fragment_outcome is not None
                    else None,
                },
            }
        )

    @staticmethod
    def _config_value(config_manager, key: str, default: Any) -> Any:
        getter = getattr(config_manager, "get", None)
        return getter(key, default) if callable(getter) else default
