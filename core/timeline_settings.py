"""Runtime-adjustable Timeline memory settings shared by WebUI and consumers."""

from __future__ import annotations

from typing import Any


TIMELINE_SETTINGS_REVISION = 1


TIMELINE_SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    "recall_engine.top_k": {"default": 5, "type": "int", "min": 0, "max": 50, "category": "recall", "label": "自动召回数量", "description": "实际对话中最多注入的 Timeline 数量；0 表示关闭自动 Timeline 召回，但不影响召回测试。"},
    "recall_engine.max_k": {"default": 10, "type": "int", "min": 1, "max": 50, "category": "recall", "label": "主动检索最大数量", "description": "Agent 主动调用长期记忆检索工具时允许返回的最大 Timeline 数量。"},
    "recall_engine.importance_weight": {"default": 1.0, "type": "float", "min": 0.0, "max": 10.0, "step": 0.1, "category": "recall", "label": "重要性权重", "description": "Timeline 混合评分中记忆重要性的权重；越大越偏向重要记忆。"},
    "recall_engine.fallback_to_vector": {"default": True, "type": "bool", "category": "recall", "label": "失败时回退纯向量", "description": "混合检索失败或没有结果时尝试纯向量检索。"},
    "recall_engine.inject_with_recent_context": {"default": False, "type": "bool", "category": "recall", "label": "跨轮次上下文扩展", "description": "使用最近对话补充查询分支；当前消息始终为主查询。"},
    "recall_engine.assistant_context_mode": {"default": "exclude", "type": "select", "options": ["exclude", "low_weight", "normal"], "category": "recall", "label": "跨轮扩展中的 Bot 回复", "option_labels": {"exclude": "不查询", "low_weight": "低权重", "normal": "正常查询"}, "description": "控制 Bot 回复是否参与跨轮次扩展，仅在开启跨轮扩展时生效。"},
    "recall_engine.candidate_multiplier": {"default": 3, "type": "int", "min": 1, "max": 10, "category": "recall", "label": "候选倍率", "description": "先检索最终数量的若干倍候选，再执行相关度过滤和多样性选择。"},
    "recall_engine.min_relevance_score": {"default": 0.38, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "最低相关度", "description": "低于该综合相关度的 Timeline 不会进入自动召回结果。"},
    "recall_engine.relative_score_floor": {"default": 0.65, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "相对相关度下限", "description": "候选还需达到本轮最佳候选相关度的该比例。"},
    "recall_engine.mmr_lambda": {"default": 0.72, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "相关性与多样性平衡", "description": "越接近 1 越重视相关性，越低越倾向减少重复内容。"},
    "recall_engine.context_overlap_suppression": {"default": True, "type": "bool", "category": "recall", "label": "抑制当前上下文重复", "description": "来源消息仍位于当前原始上下文中时，不重复注入整条 Timeline。"},
    "reflection_engine.summary_trigger_rounds": {"default": 10, "type": "int", "min": 1, "max": 100, "category": "generation", "label": "总结触发轮次", "description": "累计达到该对话轮次后生成一条 Timeline 记忆。"},
    "fusion_strategy.rrf_k": {"default": 60, "type": "int", "min": 1, "max": 1000, "category": "recall", "label": "RRF 融合参数", "description": "值越小越强调排名靠前的关键词或向量结果，值越大融合越平滑。"},
    "filtering_settings.use_persona_filtering": {"default": True, "type": "bool", "category": "isolation", "label": "按人格隔离", "description": "开启后只召回与当前人格对应的 Timeline。"},
    "filtering_settings.use_session_filtering": {"default": True, "type": "bool", "category": "isolation", "label": "按会话隔离", "description": "开启后只召回当前会话中的 Timeline。"},
    "importance_decay.decay_rate": {"default": 0.01, "type": "float", "min": 0.0, "max": 1.0, "step": 0.001, "category": "lifecycle", "label": "每日重要性衰减率", "description": "每天降低 Timeline 重要性的比例；0 表示不执行重要性衰减。"},
    "importance_decay.access_decay_window_days": {"default": 30.0, "type": "float", "min": 0.0, "max": 3650.0, "step": 1.0, "category": "lifecycle", "label": "访问强化窗口（天）", "description": "在该窗口内被召回过的 Timeline 会获得一定衰减保护。"},
    "importance_decay.access_decay_max_count": {"default": 10, "type": "int", "min": 1, "max": 10000, "category": "lifecycle", "label": "访问强化次数上限", "description": "达到该访问次数后获得最大衰减保护。"},
    "importance_decay.access_count_decay_multiplier": {"default": 0.5, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "lifecycle", "label": "访问次数保留比例", "description": "每日维护后访问次数按该比例回落，避免旧热点永久不衰减。"},
    "forgetting_agent.auto_cleanup_enabled": {"default": True, "type": "bool", "category": "lifecycle", "label": "自动清理旧记忆", "description": "每日维护时清理时间久远且重要性低的 Timeline。"},
    "forgetting_agent.cleanup_days_threshold": {"default": 30, "type": "int", "min": 1, "max": 3650, "category": "lifecycle", "label": "自动清理天数阈值", "description": "超过该天数的 Timeline 才会进入低重要性清理判断。"},
    "forgetting_agent.cleanup_importance_threshold": {"default": 0.3, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "lifecycle", "label": "自动清理重要性阈值", "description": "重要性低于该值且超过时间阈值的 Timeline 可被自动清理。"},
    "recall_engine.search_cache_enabled": {"default": True, "type": "bool", "category": "performance", "label": "短期检索缓存", "description": "短时间内相同查询复用 Timeline 检索结果。"},
    "recall_engine.search_cache_ttl_seconds": {"default": 45.0, "type": "float", "min": 0.0, "max": 600.0, "step": 1.0, "category": "performance", "label": "检索缓存有效期（秒）", "description": "缓存保留时间；0 表示立即过期。"},
    "recall_engine.search_cache_max_size": {"default": 256, "type": "int", "min": 0, "max": 10000, "category": "performance", "label": "检索缓存最大条目", "description": "限制内存中的 Timeline 检索缓存数量。"},
}


def timeline_setting_defaults() -> dict[str, Any]:
    return {key: definition["default"] for key, definition in TIMELINE_SETTING_DEFINITIONS.items()}


def effective_timeline_settings(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    effective = timeline_setting_defaults()
    for key, value in (overrides or {}).items():
        if key in TIMELINE_SETTING_DEFINITIONS:
            effective[key] = validate_timeline_setting(key, value)
    return effective


def validate_timeline_setting(key: str, value: Any) -> Any:
    definition = TIMELINE_SETTING_DEFINITIONS.get(key)
    if definition is None:
        raise ValueError(f"Unknown Timeline setting: {key}")
    value_type = definition["type"]
    if value_type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be boolean")
        return value
    if value_type == "select":
        normalized = str(value)
        if normalized not in definition.get("options", []):
            raise ValueError(f"{key} has an unsupported value")
        return normalized
    if isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    try:
        normalized = int(value) if value_type == "int" else float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be numeric") from None
    minimum = definition.get("min")
    maximum = definition.get("max")
    if minimum is not None and normalized < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    if maximum is not None and normalized > maximum:
        raise ValueError(f"{key} must be <= {maximum}")
    return normalized


__all__ = [
    "TIMELINE_SETTING_DEFINITIONS",
    "TIMELINE_SETTINGS_REVISION",
    "effective_timeline_settings",
    "timeline_setting_defaults",
    "validate_timeline_setting",
]
