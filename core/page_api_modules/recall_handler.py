"""
召回测试处理模块
"""

import time
from typing import TYPE_CHECKING, Any

from quart import request

from astrbot.api import logger

from ..retrieval.recall_pipeline import RecallPipeline, RecallPipelineResult

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
        trace_store=None,
    ) -> dict[str, Any]:
        """
        测试记忆召回功能

        Payload:
            - query: 查询文本（必需）
            - k: 返回结果数量（默认5，最大50）
            - session_id: 会话ID过滤（可选，Topic 专测必需）
            - mode: current | timeline | topic（默认 current）

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
        mode = str(payload.get("mode", "current") or "current").strip().lower()
        if mode not in {"current", "timeline", "topic"}:
            return self.utils.error("mode 必须是 current、timeline 或 topic")
        if mode == "topic" and not session_id:
            return self.utils.error("Topic 专项召回需要选择会话 ID，以确定记忆空间")

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
            pipeline = RecallPipeline(memory_engine, config_manager)
            production_recall_enabled = int(
                self._config_value(config_manager, "recall_engine.top_k", 5)
            ) > 0
            if mode == "topic" or (mode == "current" and not production_recall_enabled):
                branches = pipeline.build_query_branches(
                    query_text,
                    recent_messages,
                    expansion_enabled=bool(expansion_enabled),
                    assistant_mode=str(assistant_mode),
                )
                outcome = RecallPipelineResult([], branches, [], 0, 0, 0.0)
            else:
                # The test endpoint supplies k directly. In Timeline-only mode
                # this deliberately ignores the production auto-recall switch
                # (recall_engine.top_k == 0).
                outcome = await pipeline.search(
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
            topic_pipeline = getattr(memory_engine, "topic_recall_pipeline", None)
            topic_config = getattr(topic_pipeline, "config", {}) or {}
            topic_requested = mode == "topic" or (
                mode == "current"
                and production_recall_enabled
                and getattr(memory_engine, "topic_memory_enabled", False) is True
                and bool(self._config_value(config_manager, "topic_memory.recall_enabled", True))
            )
            if topic_requested and session_id:
                try:
                    if topic_pipeline is None:
                        raise RuntimeError("Topic 召回管线尚未初始化")
                    spaces = await memory_engine.topic_memory_store.find_memory_spaces_for_session(
                        session_id
                    )
                    if spaces:
                        topic_space_id = spaces[0]
                        topic_outcome = await topic_pipeline.search(
                            branches=outcome.branches,
                            memory_space_id=topic_space_id,
                            final_k=(
                                k
                                if mode == "topic"
                                else min(k, int(topic_config.get("recall_top_k", 3)))
                            ),
                        )
                        if topic_outcome.results and mode == "current":
                            supplement_k = min(
                                int(topic_config.get("timeline_supplement_k", 2)),
                                max(0, k - len(topic_outcome.results)),
                            )
                            fragment_outcome = await topic_pipeline.search_fragment_supplements(
                                branches=outcome.branches,
                                topic_results=topic_outcome.results,
                                limit=supplement_k,
                                query_vectors=getattr(topic_outcome, "query_vectors", None),
                            )
                            fragment_results = fragment_outcome.results
                            suppress_timeline_for_parent_duplicates = bool(
                                fragment_outcome.available_count > 0
                                and int(getattr(fragment_outcome, "duplicate_parent_count", 0))
                                == fragment_outcome.available_count
                            )
                            if fragment_results or suppress_timeline_for_parent_duplicates:
                                results = []
                            else:
                                results = topic_pipeline.select_timeline_supplements(
                                    results,
                                    topic_outcome.results,
                                    supplement_k,
                                )
                except Exception as exc:
                    if mode == "topic":
                        raise RuntimeError(f"Topic 召回阶段失败: {exc}") from exc
                    logger.warning(
                        "[PageAPI] Topic 召回失败，召回测试回退 Timeline",
                        exc_info=True,
                    )
                    topic_outcome = None
                    topic_space_id = None
            elapsed_time = (time.time() - start_time) * 1000
        except Exception as exc:
            logger.error(f"[PageAPI] 召回测试失败: {exc}", exc_info=True)
            if trace_store is not None:
                try:
                    await trace_store.record(
                        trace_type="test",
                        status="failed",
                        query_text=query_text,
                        mode=mode,
                        session_id=session_id,
                        elapsed_ms=(time.time() - start_time) * 1000
                        if "start_time" in locals()
                        else 0,
                        request_data=payload,
                        error=str(exc),
                    )
                except Exception:
                    logger.warning("[PageAPI] 保存失败的召回测试历史时出错", exc_info=True)
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
                            "topic_current_relevance": round(
                                result.current_relevance
                                if getattr(result, "current_relevance", None) is not None
                                else result.relevance_score,
                                6,
                            ),
                            "topic_context_support": round(
                                float(getattr(result, "context_support", 0.0)), 6
                            ),
                            "topic_ranking_score": round(
                                result.ranking_score
                                if getattr(result, "ranking_score", None) is not None
                                else result.final_score,
                                6,
                            ),
                            "topic_actor_match_boost": round(
                                float(getattr(result, "actor_match_boost", 0.0)), 6
                            ),
                            **(
                                {
                                    "topic_rerank_score": round(
                                        result.rerank_score, 6
                                    ),
                                    "topic_rerank_rank": getattr(
                                        result, "rerank_rank", None
                                    ),
                                    "topic_rerank_percentile": round(
                                        float(
                                            getattr(
                                                result,
                                                "rerank_percentile",
                                                0.0,
                                            )
                                            or 0.0
                                        ),
                                        6,
                                    ),
                                    "topic_rerank_rank_boost": round(
                                        float(
                                            getattr(
                                                result,
                                                "rerank_rank_boost",
                                                0.0,
                                            )
                                        ),
                                        6,
                                    ),
                                    "topic_rerank_confidence": round(
                                        float(
                                            getattr(
                                                result,
                                                "rerank_confidence",
                                                0.0,
                                            )
                                        ),
                                        6,
                                    ),
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
                        "fragment_body_suppressed": result.body_suppressed,
                        "fragment_fact_count": len(result.fact_contents),
                        "narrative_perspective": "first_person_assistant",
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
                        "fragment_current_relevance": round(
                            result.current_relevance
                            if getattr(result, "current_relevance", None) is not None
                            else result.relevance_score,
                            6,
                        ),
                        "fragment_context_support": round(
                            float(getattr(result, "context_support", 0.0)), 6
                        ),
                        "fragment_ranking_score": round(
                            result.ranking_score
                            if getattr(result, "ranking_score", None) is not None
                            else result.final_score,
                            6,
                        ),
                        **(
                            {
                                "fragment_rerank_score": round(
                                    result.rerank_score, 6
                                ),
                                "fragment_rerank_rank": getattr(
                                    result, "rerank_rank", None
                                ),
                                "fragment_rerank_percentile": round(
                                    float(
                                        getattr(
                                            result,
                                            "rerank_percentile",
                                            0.0,
                                        )
                                        or 0.0
                                    ),
                                    6,
                                ),
                                "fragment_rerank_rank_boost": round(
                                    float(
                                        getattr(
                                            result,
                                            "rerank_rank_boost",
                                            0.0,
                                        )
                                    ),
                                    6,
                                ),
                                "fragment_rerank_confidence": round(
                                    float(
                                        getattr(
                                            result,
                                            "rerank_confidence",
                                            0.0,
                                        )
                                    ),
                                    6,
                                ),
                            }
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

        response_data = {
                "results": formatted_results,
                "total": len(formatted_results),
                "query": query_text,
                "k": k,
                "session_id_filter": session_id,
                "mode": mode,
                "elapsed_time_ms": round(elapsed_time, 2),
                "diagnostics": {
                    **outcome.diagnostics(),
                    "mode": mode,
                    "topic_space_id": topic_space_id,
                    "topic": topic_outcome.diagnostics()
                    if topic_outcome is not None
                    else None,
                    "topic_fragments": fragment_outcome.diagnostics()
                    if fragment_outcome is not None
                    else None,
                },
            }
        if trace_store is not None:
            try:
                response_data["trace_uid"] = await trace_store.record(
                    trace_type="test",
                    status="completed",
                    query_text=query_text,
                    mode=mode,
                    session_id=session_id,
                    result_count=len(formatted_results),
                    elapsed_ms=elapsed_time,
                    request_data=payload,
                    result_data=response_data,
                    diagnostics=response_data["diagnostics"],
                )
            except Exception:
                logger.warning("[PageAPI] 保存召回测试历史时出错", exc_info=True)
        return self.utils.ok(response_data)

    async def list_traces(self, trace_store) -> dict[str, Any]:
        if trace_store is None:
            return self.utils.error("召回记录存储尚未初始化")
        trace_type = str(request.args.get("type", "production")).strip().lower()
        try:
            limit = max(1, min(int(request.args.get("limit", 50)), 200))
            items = await trace_store.list_records(trace_type, limit=limit)
            enabled = await trace_store.production_enabled()
            return self.utils.ok(
                {"items": items, "production_enabled": enabled, "type": trace_type}
            )
        except (TypeError, ValueError) as exc:
            return self.utils.error(str(exc))

    async def get_trace(self, trace_store) -> dict[str, Any]:
        if trace_store is None:
            return self.utils.error("召回记录存储尚未初始化")
        trace_uid = self.utils.optional_text(request.args.get("trace_uid"))
        if not trace_uid:
            return self.utils.error("缺少 trace_uid")
        item = await trace_store.get_record(trace_uid)
        return self.utils.ok(item) if item else self.utils.error("召回记录不存在")

    async def update_trace_settings(self, trace_store) -> dict[str, Any]:
        if trace_store is None:
            return self.utils.error("召回记录存储尚未初始化")
        payload = await request.get_json(silent=True) or {}
        if "production_enabled" not in payload:
            return self.utils.error("缺少 production_enabled")
        enabled = await trace_store.set_production_enabled(
            bool(payload.get("production_enabled"))
        )
        return self.utils.ok({"production_enabled": enabled})

    async def delete_trace(self, trace_store) -> dict[str, Any]:
        if trace_store is None:
            return self.utils.error("召回记录存储尚未初始化")
        payload = await request.get_json(silent=True) or {}
        trace_uid = self.utils.optional_text(payload.get("trace_uid"))
        trace_type = self.utils.optional_text(payload.get("type"))
        if not trace_uid:
            return self.utils.error("缺少 trace_uid")
        try:
            deleted = await trace_store.delete_record(
                trace_uid, trace_type=trace_type
            )
            return self.utils.ok({"deleted": deleted, "trace_uid": trace_uid})
        except ValueError as exc:
            return self.utils.error(str(exc))

    async def clear_traces(self, trace_store) -> dict[str, Any]:
        if trace_store is None:
            return self.utils.error("召回记录存储尚未初始化")
        payload = await request.get_json(silent=True) or {}
        trace_type = str(payload.get("type", "production")).strip().lower()
        try:
            count = await trace_store.clear_records(trace_type)
            return self.utils.ok({"deleted_count": count, "type": trace_type})
        except ValueError as exc:
            return self.utils.error(str(exc))

    @staticmethod
    def _config_value(config_manager, key: str, default: Any) -> Any:
        getter = getattr(config_manager, "get", None)
        return getter(key, default) if callable(getter) else default
