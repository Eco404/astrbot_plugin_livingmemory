"""Single source of truth for runtime-adjustable Topic settings."""

from __future__ import annotations

from typing import Any


TOPIC_SETTINGS_REVISION = 11


TOPIC_SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    "recall_top_k": {"default": 3, "type": "int", "min": 1, "max": 20, "category": "recall", "label": "Topic 最大召回数量", "effect": "recall"},
    "recall_candidate_multiplier": {"default": 4, "type": "int", "min": 1, "max": 10, "category": "recall", "label": "召回候选倍率", "effect": "recall"},
    "recall_scan_limit": {"default": 2000, "type": "int", "min": 100, "max": 5000, "category": "recall", "label": "旧版扫描上限", "effect": "recall", "deprecated": True},
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
    "recall_affect_enabled": {"default": True, "type": "bool", "category": "recall", "label": "使用情感上下文", "effect": "recall"},
    "recall_affect_boost_cap": {"default": 0.04, "type": "float", "min": 0.0, "max": 0.12, "step": 0.01, "category": "recall", "label": "情感匹配加分上限", "effect": "recall"},
    "recall_affect_event_limit": {"default": 1, "type": "int", "min": 0, "max": 3, "category": "recall", "label": "情感上下文数量", "effect": "recall"},
    "recall_affect_min_confidence": {"default": 0.65, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "recall", "label": "情感事件最低置信度", "effect": "recall"},
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
    "existing_topic_match_threshold": {"default": 0.55, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "全量构建 Topic 延续阈值", "effect": "rebuild"},
    "candidate_batch_size": {"default": 100, "type": "int", "min": 1, "max": 1000, "category": "performance", "label": "候选扫描批量", "effect": "next_build"},
    "fragment_extraction_batch_size": {"default": 12, "type": "int", "min": 1, "max": 100, "category": "performance", "label": "片段提取批量", "effect": "rebuild"},
    "fragment_validation_retries": {"default": 2, "type": "int", "min": 0, "max": 8, "category": "performance", "label": "片段格式校正重试次数", "effect": "next_build"},
    "synthesis_batch_size": {"default": 12, "type": "int", "min": 2, "max": 50, "category": "performance", "label": "分层合成批量", "effect": "rebuild"},
    "embedding_batch_size": {"default": 8, "type": "int", "min": 1, "max": 256, "category": "performance", "label": "片段向量批量", "effect": "next_build"},
    "llm_concurrency": {"default": 1, "type": "int", "min": 1, "max": 64, "category": "performance", "label": "LLM 并发数", "effect": "next_build"},
    "rerank_concurrency": {"default": 1, "type": "int", "min": 1, "max": 32, "category": "performance", "label": "Rerank 并发数", "effect": "next_build"},
    "llm_max_retries": {"default": 3, "type": "int", "min": 1, "max": 8, "category": "performance", "label": "模型调用重试次数", "effect": "next_build"},
    "rerank_failure_fallback": {"default": True, "type": "bool", "category": "performance", "label": "Rerank 失败时回退 Embedding", "effect": "next_build"},
    "auto_debounce_seconds": {"default": 60.0, "type": "float", "min": 0.0, "max": 3600.0, "step": 1.0, "category": "performance", "label": "自动维护合并等待（秒）", "effect": "next_build"},
    "incremental_context_similarity": {"default": 0.58, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "旧版增量上下文阈值", "effect": "rebuild", "deprecated": True},
    "incremental_max_timelines": {"default": 120, "type": "int", "min": 1, "max": 1000, "category": "performance", "label": "单批增量 Timeline 上限", "effect": "next_build"},
    "incremental_auto_max_timelines": {"default": 240, "type": "int", "min": 1, "max": 5000, "category": "performance", "label": "自动增量总量上限", "effect": "next_build"},
    "incremental_topic_candidate_k": {"default": 8, "type": "int", "min": 2, "max": 64, "category": "build", "label": "增量匹配 Topic 候选数", "effect": "next_build"},
    "incremental_topic_match_threshold": {"default": 0.55, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "增量 Topic 延续阈值", "effect": "next_build"},
    "incremental_topic_review_threshold": {"default": 0.72, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "build", "label": "增量人工审查阈值", "effect": "next_build"},
    "incremental_topic_match_margin": {"default": 0.04, "type": "float", "min": 0.0, "max": 0.5, "step": 0.01, "category": "build", "label": "增量匹配歧义间隔", "effect": "next_build"},
    "incremental_topic_time_proximity_days": {"default": 7.0, "type": "float", "min": 0.25, "max": 90.0, "step": 0.25, "category": "build", "label": "增量事件时间邻近半衰期（天）", "effect": "next_build"},
    "incremental_event_anchor_margin": {"default": 0.03, "type": "float", "min": 0.0, "max": 0.2, "step": 0.01, "category": "build", "label": "同事件锚点最小优势", "effect": "next_build"},
    "incremental_event_anchor_min_count": {"default": 2, "type": "int", "min": 2, "max": 10, "category": "build", "label": "同事件最少锚点数", "effect": "next_build"},
    "incremental_event_continuity_bonus": {"default": 0.10, "type": "float", "min": 0.0, "max": 0.15, "step": 0.01, "category": "build", "label": "同事件连续性加分上限", "effect": "next_build"},
    "incremental_event_rescue_band": {"default": 0.10, "type": "float", "min": 0.0, "max": 0.2, "step": 0.01, "category": "build", "label": "同事件片段救回区间", "effect": "next_build"},
}


TOPIC_SETTING_DESCRIPTIONS: dict[str, str] = {
    "recall_top_k": "一次召回最多保留的 Topic 数量；过滤后结果可以少于该值。",
    "recall_candidate_multiplier": "扩大初始候选池后再过滤和去重；越大覆盖更广，但计算量更高。",
    "recall_scan_limit": "仅供无派生向量索引的兼容路径使用；正式运行时召回不再按重要性截断 Topic。",
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
    "recall_affect_enabled": "在语义召回已合格后，用可溯源情感事件做小幅排序辅助，并在相关查询中注入简短情感上下文。",
    "recall_affect_boost_cap": "显式情绪查询与记忆情感画像匹配时的最大加分；不会帮助候选越过语义最低门槛。",
    "recall_affect_event_limit": "每条 Topic 或正式片段最多注入的情感事件数；0 表示保留情感索引但不注入文本。",
    "recall_affect_min_confidence": "低于该置信度的情感事件不会注入；模型推断事件的置信度会被额外限制。",
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
    "existing_topic_match_threshold": "全量构建未清空旧 Topic 时，判定新组件是否延续已有 Topic 的最低得分。",
    "candidate_batch_size": "候选扫描每批处理的 Timeline 数量，主要影响内存和进度刷新频率。",
    "fragment_extraction_batch_size": "一次 LLM 片段提取请求包含的 Timeline 数量。",
    "fragment_validation_retries": "片段提取结果不符合结构或来源契约时，额外要求模型定向校正的次数；0 表示直接使用确定性回退。",
    "synthesis_batch_size": "一次 LLM 分层合成最多处理的正式片段数量。",
    "embedding_batch_size": "一次 Embedding 请求包含的片段文本数量；限流时可适当调小。",
    "llm_concurrency": "Topic 构建期间同时进行的 LLM 请求上限。",
    "rerank_concurrency": "片段匹配期间同时进行的 Rerank 请求上限。",
    "llm_max_retries": "单次 LLM 调用允许的总尝试次数；Provider 支持请求级重试参数时由 Provider 单层执行，避免与构建器重复重试。",
    "rerank_failure_fallback": "Rerank 不可用或失败时回退到 Embedding 结果继续构建。",
    "auto_debounce_seconds": "自动维护触发后等待并合并连续变化的时间。",
    "incremental_context_similarity": "旧版兼容参数；delta-first 增量管线不再回读相似旧 Timeline。",
    "incremental_max_timelines": "每个增量批次最多处理的新增或变更 Timeline 数量；更大的任务会自动拆成多个有边界批次。",
    "incremental_auto_max_timelines": "自动维护一次允许处理的 Timeline 总数；超过时不调用模型并等待用户在维护面板确认，手动确认的任务仍按单批上限拆分。",
    "incremental_topic_candidate_k": "每个新增组件只与向量最接近的若干已有 Topic 比较，并额外包含直接受影响的 Topic。",
    "incremental_topic_match_threshold": "新增组件更新已有 Topic 所需的最低综合得分；低于该值时创建新 Topic。",
    "incremental_topic_review_threshold": "只有两个以上候选均达到该强相关阈值且仍难区分时才进入人工审查；普通的相邻大类会直接新建 Topic。",
    "incremental_topic_match_margin": "最佳与次佳已有 Topic 的分差低于该值时不自动合并；强候选进入人工审查，边缘候选直接新建 Topic。",
    "incremental_topic_time_proximity_days": "增量匹配时，事件时间邻近度按该半衰期衰减且最多只提供 0.05 弱加分；不会覆盖明显的语义冲突。",
    "incremental_event_anchor_margin": "同一来源窗口内，一个片段对已有 Topic 的领先优势达到该值后才可作为事件锚点。",
    "incremental_event_anchor_min_count": "同一 Timeline 与时间簇中，至少需要多少个明确片段指向同一 Topic，才允许辅助判断其他模糊片段。",
    "incremental_event_continuity_bonus": "同事件锚点对语义已接近合格线的兄弟片段最多提供的加分；代码同时限制其不超过 0.15。",
    "incremental_event_rescue_band": "兄弟片段原始得分距离增量匹配阈值不超过该范围时，才允许使用事件连续性补强。",
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
