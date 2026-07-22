"""Single source of truth for runtime-adjustable Topic settings."""

from __future__ import annotations

from typing import Any


TOPIC_SETTINGS_REVISION = 6


TOPIC_SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    "recall_top_k": {"default": 3, "type": "int", "min": 1, "max": 20, "category": "recall", "label": "Topic 最大召回数量", "effect": "recall"},
    "recall_candidate_multiplier": {"default": 4, "type": "int", "min": 1, "max": 10, "category": "recall", "label": "召回候选倍率", "effect": "recall"},
    "recall_scan_limit": {"default": 2000, "type": "int", "min": 100, "max": 5000, "category": "recall", "label": "单次扫描上限", "effect": "recall"},
    "recall_min_relevance": {"default": 0.32, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "最低相关度", "effect": "recall"},
    "recall_relative_floor": {"default": 0.70, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "相对相关度下限", "effect": "recall"},
    "recall_selection_relative_floor": {"default": 0.90, "type": "float", "min": 0.5, "max": 1.0, "step": 0.01, "category": "recall", "label": "动态结果保留比例", "effect": "recall"},
    "recall_mmr_lambda": {"default": 0.78, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "相关性与多样性平衡", "effect": "recall"},
    "recall_use_rerank": {"default": True, "type": "bool", "category": "recall", "label": "召回使用 Rerank", "effect": "recall"},
    "recall_rerank_weight": {"default": 0.35, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "recall", "label": "召回 Rerank 权重", "effect": "recall"},
    "recall_context_support_cap": {"default": 0.08, "type": "float", "min": 0.0, "max": 0.25, "step": 0.01, "category": "recall", "label": "跨轮上下文奖励上限", "effect": "recall"},
    "recall_context_overlap_threshold": {"default": 0.8, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "上下文来源覆盖阈值", "effect": "recall"},
    "timeline_supplement_k": {"default": 2, "type": "int", "min": 0, "max": 10, "category": "recall", "label": "Topic 片段补充数量", "effect": "recall"},
    "fragment_min_relevance": {"default": 0.28, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "Topic 片段最低相关度", "effect": "recall"},
    "fragment_relative_floor": {"default": 0.65, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "Topic 片段相对门槛", "effect": "recall"},
    "recall_actor_match_boost": {"default": 0.04, "type": "float", "min": 0.0, "max": 0.2, "step": 0.01, "category": "recall", "label": "当前人物匹配加分", "effect": "recall"},
    "recall_group_current_sender_only": {"default": True, "type": "bool", "category": "recall", "label": "群聊仅匹配当前发言者", "effect": "recall"},
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
    "related_topic_similarity_threshold": {"default": 0.60, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "Topic 关联相似度阈值", "effect": "relations"},
    "related_topic_top_n": {"default": 3, "type": "int", "min": 1, "max": 20, "category": "build", "label": "每个 Topic 的最大关联数量", "effect": "relations"},
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


TOPIC_SETTING_DESCRIPTIONS: dict[str, str] = {
    "recall_top_k": "一次召回最多保留的 Topic 数量；过滤后结果可以少于该值。",
    "recall_candidate_multiplier": "扩大初始候选池后再过滤和去重；越大覆盖更广，但计算量更高。",
    "recall_scan_limit": "一次召回最多读取的活跃 Topic 数量，用于限制大库扫描开销。",
    "recall_min_relevance": "候选进入结果所需的最低综合相关度；调低会增加弱相关召回。",
    "recall_relative_floor": "相对本轮最佳候选的保留比例，用于过滤明显落后的结果。",
    "recall_selection_relative_floor": "候选通过基本门槛后，最终结果仍需达到本轮最佳当前相关度的该比例；用于让模糊查询自然少返回，而不是勉强填满数量。",
    "recall_mmr_lambda": "越接近 1 越重视相关性，越低越重视结果之间的多样性。",
    "recall_use_rerank": "可用时使用 Rerank 对 Topic 候选重新排序；关闭后只使用基础相关度。",
    "recall_rerank_weight": "Rerank 相对名次对最终排序的影响强度；实际加分还会按本轮分数可信度衰减，不混合原始绝对分，设为 0 时不调用 Rerank。",
    "recall_context_support_cap": "最近用户消息和 Bot 回复对已通过当前消息门槛的候选所能增加的最大排序奖励；上下文不能单独使候选入选。",
    "recall_context_overlap_threshold": "Topic 来源被当前上下文覆盖到该比例时，抑制重复注入。",
    "timeline_supplement_k": "每个召回 Topic 最多补充的正式片段数量；0 表示只注入 Topic 主体。",
    "fragment_min_relevance": "正式片段作为 Topic 补充时必须达到的最低相关度。",
    "fragment_relative_floor": "相对最佳正式片段的保留比例，用于去掉明显较弱的补充片段。",
    "recall_actor_match_boost": "召回 Topic 的人物索引命中当前人物时增加的分数；只加分，不会作为硬过滤条件。",
    "recall_group_current_sender_only": "开启时群聊只用当前发言者匹配 Topic 人物索引；关闭时使用本次 LLM 可见上下文中实际发过言的人类参与者。无法确定范围时退化为当前发言者。",
    "time_gap_hours": "相邻 Timeline 超过该间隔后优先划入不同时间簇。",
    "candidate_similarity_threshold": "扫描阶段把 Timeline 放入同一候选邻域所需的最低相似度。",
    "fragment_similarity_threshold": "正式片段之间进入归并候选所需的最低 Embedding 相似度。",
    "rerank_candidate_floor": "只有达到该基础相似度的片段对才会提交给 Rerank。",
    "component_min_pair_similarity": "同一 Topic 组件中任意两个片段允许的最低相似度。",
    "component_min_average_similarity": "片段加入组件后必须保持的最低平均相似度。",
    "component_size_cohesion_penalty": "组件越大时额外提高的一致性要求，用于抑制链式过度归并。",
    "component_review_enabled": "使用 LLM 复核较大组件是否应拆成多个独立 Topic。",
    "component_review_min_fragments": "组件达到该片段数后才触发 LLM 结构复核。",
    "component_review_max_fragments": "一次交给 LLM 复核的最大片段数，避免请求上下文过大。",
    "component_review_failure_fallback": "组件复核失败时保留原组件继续构建，而不是终止任务。",
    "rerank_threshold": "片段对经 Rerank 后允许归入同一 Topic 的最低分数。",
    "rerank_reciprocal_rank_threshold": "限制双向相对排名，避免只有单侧认为相关的片段被强行归并。",
    "rerank_top_n": "每个片段最多送入 Rerank 比较的候选数量。",
    "related_topic_similarity_threshold": "构建相关话题图时考虑候选边的最低语义相似度。",
    "related_topic_top_n": "每个 Topic 在相关话题图中允许保留的最大连接数。",
    "existing_topic_match_threshold": "旧版已有 Topic 延续参数，当前管线不再使用。",
    "candidate_batch_size": "候选扫描每批处理的 Timeline 数量，主要影响内存和进度刷新频率。",
    "fragment_extraction_batch_size": "一次 LLM 片段提取请求包含的 Timeline 数量。",
    "synthesis_batch_size": "一次 LLM 分层合成最多处理的正式片段数量。",
    "embedding_batch_size": "一次 Embedding 请求包含的片段文本数量；限流时可适当调小。",
    "llm_concurrency": "Topic 构建期间同时进行的 LLM 请求上限。",
    "rerank_concurrency": "片段匹配期间同时进行的 Rerank 请求上限。",
    "llm_max_retries": "单次 LLM 调用失败后允许尝试的总次数。",
    "rerank_failure_fallback": "Rerank 不可用或失败时回退到 Embedding 结果继续构建。",
    "auto_debounce_seconds": "自动维护触发后等待并合并连续变化的时间。",
    "incremental_context_similarity": "增量构建补入既有 Timeline 上下文所需的最低相似度。",
    "incremental_max_timelines": "一次增量局部重构允许包含的 Timeline 上限，超出后采用保守路径。",
}

for _setting_key, _description in TOPIC_SETTING_DESCRIPTIONS.items():
    TOPIC_SETTING_DEFINITIONS[_setting_key]["description"] = _description


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
    "TOPIC_SETTING_DESCRIPTIONS",
    "TOPIC_SETTINGS_REVISION",
    "effective_topic_settings",
    "topic_setting_defaults",
    "validate_topic_setting",
]
