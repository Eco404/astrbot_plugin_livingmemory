"""Single source of truth for runtime-adjustable Topic settings."""

from __future__ import annotations

from typing import Any


TOPIC_SETTINGS_REVISION = 3


TOPIC_SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    "recall_top_k": {"default": 3, "type": "int", "min": 1, "max": 20, "category": "recall", "label": "Topic 最大召回数量", "effect": "recall"},
    "recall_candidate_multiplier": {"default": 4, "type": "int", "min": 1, "max": 10, "category": "recall", "label": "召回候选倍率", "effect": "recall"},
    "recall_scan_limit": {"default": 2000, "type": "int", "min": 100, "max": 5000, "category": "recall", "label": "单次扫描上限", "effect": "recall"},
    "recall_min_relevance": {"default": 0.32, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "最低相关度", "effect": "recall"},
    "recall_relative_floor": {"default": 0.70, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "相对相关度下限", "effect": "recall"},
    "recall_mmr_lambda": {"default": 0.78, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "相关性与多样性平衡", "effect": "recall"},
    "recall_use_rerank": {"default": True, "type": "bool", "category": "recall", "label": "召回使用 Rerank", "effect": "recall"},
    "recall_rerank_weight": {"default": 0.35, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "recall", "label": "召回 Rerank 权重", "effect": "recall"},
    "recall_context_overlap_threshold": {"default": 0.8, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "上下文来源覆盖阈值", "effect": "recall"},
    "timeline_supplement_k": {"default": 2, "type": "int", "min": 0, "max": 10, "category": "recall", "label": "Topic 片段补充数量", "effect": "recall"},
    "fragment_min_relevance": {"default": 0.28, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "Topic 片段最低相关度", "effect": "recall"},
    "fragment_relative_floor": {"default": 0.65, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "Topic 片段相对门槛", "effect": "recall"},
    "time_gap_hours": {"default": 6.0, "type": "float", "min": 1 / 60, "max": 720.0, "step": 0.25, "category": "build", "label": "候选时间簇间隔（小时）", "effect": "rebuild"},
    "candidate_similarity_threshold": {"default": 0.52, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "候选窗口相似度阈值", "effect": "rebuild"},
    "fragment_similarity_threshold": {"default": 0.78, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "片段归并阈值", "effect": "rebuild"},
    "rerank_candidate_floor": {"default": 0.63, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "Rerank 候选最低相似度", "effect": "rebuild"},
    "component_min_pair_similarity": {"default": 0.52, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "组件最低两两相似度", "effect": "rebuild"},
    "component_min_average_similarity": {"default": 0.65, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "组件最低平均相似度", "effect": "rebuild"},
    "component_size_cohesion_penalty": {"default": 0.005, "type": "float", "min": 0.0, "max": 0.05, "step": 0.001, "category": "build", "label": "大组件一致性增量", "effect": "rebuild"},
    "component_review_enabled": {"default": True, "type": "bool", "category": "build", "label": "LLM 复核大组件结构", "effect": "rebuild"},
    "component_review_min_fragments": {"default": 6, "type": "int", "min": 3, "max": 100, "category": "build", "label": "组件结构复核起始片段数", "effect": "rebuild"},
    "component_review_max_fragments": {"default": 48, "type": "int", "min": 6, "max": 200, "category": "build", "label": "单次组件结构复核上限", "effect": "rebuild"},
    "component_review_failure_fallback": {"default": True, "type": "bool", "category": "performance", "label": "组件复核失败时保留原组件", "effect": "next_build"},
    "rerank_threshold": {"default": 0.55, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "Rerank 归并阈值", "effect": "rebuild"},
    "rerank_reciprocal_rank_threshold": {"default": 0.60, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "Rerank 双向相对排名阈值", "effect": "rebuild"},
    "rerank_top_n": {"default": 5, "type": "int", "min": 1, "max": 100, "category": "build", "label": "Rerank 候选数量", "effect": "rebuild"},
    "related_topic_similarity_threshold": {"default": 0.60, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "相关子话题相似度阈值", "effect": "relations"},
    "related_topic_top_n": {"default": 3, "type": "int", "min": 1, "max": 20, "category": "build", "label": "每个 Topic 的相关候选数量", "effect": "relations"},
    "existing_topic_match_threshold": {"default": 0.55, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "已有 Topic 延续阈值", "effect": "rebuild", "deprecated": True},
    "candidate_batch_size": {"default": 100, "type": "int", "min": 1, "max": 1000, "category": "performance", "label": "候选扫描批量", "effect": "next_build"},
    "fragment_extraction_batch_size": {"default": 12, "type": "int", "min": 1, "max": 100, "category": "performance", "label": "片段提取批量", "effect": "rebuild"},
    "synthesis_batch_size": {"default": 12, "type": "int", "min": 2, "max": 50, "category": "performance", "label": "分层合成批量", "effect": "rebuild"},
    "embedding_batch_size": {"default": 8, "type": "int", "min": 1, "max": 256, "category": "performance", "label": "片段向量批量", "effect": "next_build"},
    "llm_concurrency": {"default": 1, "type": "int", "min": 1, "max": 64, "category": "performance", "label": "LLM 并发数", "effect": "next_build"},
    "rerank_concurrency": {"default": 1, "type": "int", "min": 1, "max": 32, "category": "performance", "label": "Rerank 并发数", "effect": "next_build"},
    "llm_max_retries": {"default": 3, "type": "int", "min": 1, "max": 8, "category": "performance", "label": "模型调用重试次数", "effect": "next_build"},
    "rerank_failure_fallback": {"default": True, "type": "bool", "category": "performance", "label": "Rerank 失败时回退 Embedding", "effect": "next_build"},
    "auto_debounce_seconds": {"default": 60.0, "type": "float", "min": 0.0, "max": 3600.0, "step": 1.0, "category": "performance", "label": "自动维护合并等待（秒）", "effect": "next_build"},
    "incremental_context_similarity": {"default": 0.58, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "增量局部上下文相似度", "effect": "rebuild"},
    "incremental_max_timelines": {"default": 120, "type": "int", "min": 10, "max": 1000, "category": "performance", "label": "增量局部重构 Timeline 上限", "effect": "next_build"},
}


def topic_setting_defaults() -> dict[str, Any]:
    return {key: item["default"] for key, item in TOPIC_SETTING_DEFINITIONS.items()}


def validate_topic_setting(key: str, value: Any) -> Any:
    definition = TOPIC_SETTING_DEFINITIONS.get(str(key))
    if definition is None:
        raise ValueError(f"Unknown Topic setting: {key}")
    kind = definition["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        return value
    if isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    try:
        normalized = int(value) if kind == "int" else float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be {kind}") from exc
    minimum = definition.get("min")
    maximum = definition.get("max")
    if minimum is not None and normalized < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    if maximum is not None and normalized > maximum:
        raise ValueError(f"{key} must be <= {maximum}")
    return normalized


def effective_topic_settings(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    result = topic_setting_defaults()
    for key, value in (overrides or {}).items():
        if key in TOPIC_SETTING_DEFINITIONS:
            result[key] = validate_topic_setting(key, value)
    return result


__all__ = [
    "TOPIC_SETTING_DEFINITIONS",
    "TOPIC_SETTINGS_REVISION",
    "effective_topic_settings",
    "topic_setting_defaults",
    "validate_topic_setting",
]
