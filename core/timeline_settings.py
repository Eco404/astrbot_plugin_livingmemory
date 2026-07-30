"""Runtime-adjustable Timeline memory settings shared by WebUI and consumers."""

from __future__ import annotations

from typing import Any


TIMELINE_SETTINGS_REVISION = 7

SHARED_QUERY_SETTING_KEYS = frozenset(
    {
        "recall_engine.inject_with_recent_context",
        "recall_engine.recent_context_max_age_seconds",
        "recall_engine.assistant_context_mode",
        "recall_engine.recent_user_weight",
        "recall_engine.recent_assistant_weight",
    }
)


TIMELINE_SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    "recall_engine.top_k": {"default": 5, "type": "int", "min": 0, "max": 50, "category": "recall", "label": "自动召回数量", "description": "实际对话中最多注入的 Timeline 数量；0 表示关闭自动 Timeline 召回，但不影响召回测试。"},
    "recall_engine.max_k": {"default": 10, "type": "int", "min": 1, "max": 50, "category": "recall", "label": "主动检索最大数量", "description": "Agent 主动调用长期记忆检索工具时允许返回的最大 Timeline 数量。"},
    "recall_engine.importance_weight": {"default": 1.0, "type": "float", "min": 0.0, "max": 10.0, "step": 0.1, "category": "recall", "label": "重要性权重", "description": "Timeline 混合评分中记忆重要性的权重；越大越偏向重要记忆。"},
    "recall_engine.fallback_to_vector": {"default": True, "type": "bool", "category": "recall", "label": "失败时回退纯向量", "description": "混合检索失败或没有结果时尝试纯向量检索。"},
    "recall_engine.inject_with_recent_context": {"default": False, "type": "bool", "category": "recall", "label": "跨轮次上下文扩展", "description": "使用最近对话补充查询分支；当前消息始终为主查询。"},
    "recall_engine.recent_context_max_age_seconds": {"default": 7200, "type": "int", "min": 0, "max": 604800, "category": "recall", "label": "扩展上下文最大间隔（秒）", "description": "只使用该时间范围内的历史消息扩展召回，避免旧话题干扰；0 表示不限制时间。"},
    "recall_engine.assistant_context_mode": {"default": "exclude", "type": "select", "options": ["exclude", "low_weight", "normal"], "category": "recall", "label": "跨轮扩展中的 Bot 回复", "option_labels": {"exclude": "不查询", "low_weight": "低权重", "normal": "正常查询"}, "description": "控制 Bot 回复是否参与跨轮次扩展，仅在开启跨轮扩展时生效。"},
    "recall_engine.recent_user_weight": {"default": 0.45, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "recall", "label": "recent_user 查询权重", "description": "最近历史用户消息查询分支的权重；当前用户消息始终为 1.0。"},
    "recall_engine.recent_assistant_weight": {"default": 0.40, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "recall", "label": "recent_assistant 查询权重", "description": "Bot 历史回复在“正常查询”下的权重；“低权重”模式使用该值的一半。"},
    "recall_engine.candidate_multiplier": {"default": 3, "type": "int", "min": 1, "max": 10, "category": "recall", "label": "候选倍率", "description": "先检索最终数量的若干倍候选，再执行相关度过滤和多样性选择。"},
    "recall_engine.min_relevance_score": {"default": 0.38, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "最低相关度", "description": "低于该综合相关度的 Timeline 不会进入自动召回结果。"},
    "recall_engine.relative_score_floor": {"default": 0.65, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "相对相关度下限", "description": "候选还需达到本轮最佳候选相关度的该比例。"},
    "recall_engine.mmr_lambda": {"default": 0.72, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "recall", "label": "相关性与多样性平衡", "description": "越接近 1 越重视相关性，越低越倾向减少重复内容。"},
    "recall_engine.context_overlap_suppression": {"default": True, "type": "bool", "category": "recall", "label": "抑制当前上下文重复", "description": "来源消息仍位于当前原始上下文中时，不重复注入整条 Timeline。"},
    "reflection_engine.summary_trigger_rounds": {"default": 10, "type": "int", "min": 1, "max": 100, "category": "generation", "label": "总结触发轮次", "description": "累计达到该对话轮次后生成一条 Timeline 记忆。"},
    "reflection_engine.idle_summary_enabled": {"default": True, "type": "bool", "category": "generation", "label": "空闲后自动总结", "description": "对话长时间没有新消息时，自动总结尚未写入 Timeline 的内容。"},
    "reflection_engine.idle_summary_delay_minutes": {"default": 30.0, "type": "float", "min": 1.0, "max": 10080.0, "step": 1.0, "category": "generation", "label": "空闲触发时间（分钟）", "description": "会话最后一条消息经过该时间后进入空闲总结检查。"},
    "reflection_engine.idle_summary_min_rounds": {"default": 3, "type": "int", "min": 1, "max": 100, "category": "generation", "label": "空闲总结最少轮次", "description": "只有未总结对话达到该轮数时才执行空闲总结，避免插件刚启用时产生过碎的 Timeline。"},
    "reflection_engine.idle_summary_scan_interval_seconds": {"default": 60, "type": "int", "min": 30, "max": 3600, "category": "performance", "label": "空闲会话扫描间隔（秒）", "description": "后台检查空闲会话的时间间隔；只执行本地数据库查询，命中后才调用 LLM。"},
    "reflection_engine.source_retention_importance_threshold": {"default": 0.8, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "generation", "label": "来源快照重要性阈值", "description": "达到该基础重要性的 Timeline 会保留结构化来源消息，供原始聊天记录清理后的审计、重构和导出使用；0 表示全部保留。"},
    "session_manager.max_sessions": {"default": 100, "type": "int", "min": 1, "max": 10000, "category": "session", "label": "最大缓存会话数", "description": "内存中最多缓存的会话数量；淘汰缓存不会删除数据库数据。"},
    "session_manager.session_ttl": {"default": 3600, "type": "int", "min": 60, "max": 86400, "category": "session", "label": "会话缓存空闲时间（秒）", "description": "缓存超过该时间未被访问后失效；不会删除会话或原始消息。"},
    "session_manager.context_window_size": {"default": 50, "type": "int", "min": 1, "max": 1000, "category": "session", "label": "对话上下文窗口", "description": "插件读取近期对话时最多使用的消息条数。"},
    "session_manager.max_messages_per_session": {"default": 1000, "type": "int", "min": 100, "max": 10000, "category": "session", "label": "单会话消息上限", "description": "超过上限时只清理已经完成 Timeline 总结的最旧消息。"},
    "session_manager.cleanup_batch_size": {"default": 50, "type": "int", "min": 1, "max": 1000, "category": "session", "label": "历史消息清理批量", "description": "单会话超出消息上限时，每次至少尝试清理的旧消息数量。"},
    "session_manager.raw_message_retention_days": {"default": 0, "type": "int", "min": 0, "max": 3650, "category": "session", "label": "原始消息保留天数", "description": "仅作为会话审计和自动清理的期限；0 表示无限期保留。不会影响 Timeline 或 Topic。"},
    "session_manager.auto_delete_raw_sessions": {"default": False, "type": "bool", "category": "session", "label": "自动清理原始会话", "description": "默认关闭。开启后也只允许清理已总结且超过保留期的原始消息，不删除 Timeline 或 Topic。"},
    "recall_engine.injection_method": {"default": "extra_user_content", "type": "select", "options": ["extra_user_content", "user_message_before", "user_message_after", "fake_tool_call"], "category": "injection", "label": "记忆注入位置", "option_labels": {"extra_user_content": "附加到本轮用户内容", "user_message_before": "用户消息前", "user_message_after": "用户消息后", "fake_tool_call": "模拟工具调用"}, "description": "控制召回结果如何加入本轮 LLM 请求。"},
    "recall_engine.auto_remove_injected": {"default": True, "type": "bool", "category": "injection", "label": "清除旧注入片段", "description": "注入新记忆前清除历史中的旧注入标记，避免重复累积。"},
    "graph_memory.document_route_weight": {"default": 0.65, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "graph", "label": "文档路权重", "description": "Timeline 文档检索在图记忆双路融合中的基础权重。"},
    "graph_memory.graph_route_weight": {"default": 0.35, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "graph", "label": "图路权重", "description": "知识图谱检索在双路融合中的基础权重；两路权重会在使用时归一化。"},
    "graph_memory.cross_route_bonus": {"default": 0.08, "type": "float", "min": 0.0, "max": 0.5, "step": 0.01, "category": "graph", "label": "双路命中加分", "description": "同一记忆同时被文档路和图路命中时增加的分数。"},
    "graph_memory.expansion_limit": {"default": 24, "type": "int", "min": 1, "max": 200, "category": "graph", "label": "图邻居扩展上限", "description": "从图命中节点扩展到邻居记忆的最大候选数。"},
    "graph_memory.expansion_hops": {"default": 1, "type": "int", "min": 1, "max": 2, "category": "graph", "label": "图扩展跳数", "description": "一跳更精确，二跳可召回间接关系但会增加噪声和开销。"},
    "graph_memory.second_hop_weight": {"default": 0.4, "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "category": "graph", "label": "二跳候选权重", "description": "仅在图扩展为二跳时控制间接候选的权重。"},
    "graph_memory.dynamic_route_weighting": {"default": True, "type": "bool", "category": "graph", "label": "动态双路权重", "description": "根据查询中的关系、时间和定义意图自动微调双路占比。"},
    "graph_memory.max_topics_per_memory": {"default": 6, "type": "int", "min": 1, "max": 20, "category": "graph", "label": "单记忆最大主题节点", "description": "从一条 Timeline 中最多写入图谱的主题数量。"},
    "graph_memory.max_participants_per_memory": {"default": 8, "type": "int", "min": 1, "max": 30, "category": "graph", "label": "单记忆最大参与者节点", "description": "从一条 Timeline 中最多写入图谱的参与者数量。"},
    "graph_memory.max_facts_per_memory": {"default": 8, "type": "int", "min": 1, "max": 30, "category": "graph", "label": "单记忆最大事实节点", "description": "从一条 Timeline 中最多写入图谱的关键事实数量。"},
    "graph_memory.atom_maintenance_interval_hours": {"default": 24.0, "type": "float", "min": 1.0, "max": 168.0, "step": 1.0, "category": "lifecycle", "label": "原子维护间隔（小时）", "description": "记忆原子过期与遗忘状态的检查间隔。"},
    "graph_memory.atom_forget_delay_days": {"default": 7.0, "type": "float", "min": 1.0, "max": 90.0, "step": 1.0, "category": "lifecycle", "label": "原子遗忘延迟（天）", "description": "原子过期后等待多久从检索索引移除。"},
    "graph_memory.atom_purge_delay_days": {"default": 30.0, "type": "float", "min": 1.0, "max": 365.0, "step": 1.0, "category": "lifecycle", "label": "原子物理清理延迟（天）", "description": "原子进入遗忘状态后等待多久从数据库物理清理。"},
    "index_rebuild_settings.batch_size": {"default": 50, "type": "int", "min": 1, "max": 500, "category": "index", "label": "索引读取批量", "description": "重建 Timeline 向量索引时每批读取的记忆数量。"},
    "index_rebuild_settings.embedding_batch_size": {"default": 8, "type": "int", "min": 1, "max": 256, "category": "index", "label": "索引 Embedding 批量", "description": "一次 Embedding 请求包含的 Timeline 数量。"},
    "index_rebuild_settings.tasks_limit": {"default": 1, "type": "int", "min": 1, "max": 8, "category": "index", "label": "索引 Embedding 并发", "description": "索引重建时并行 Embedding 请求上限。"},
    "index_rebuild_settings.max_retries": {"default": 5, "type": "int", "min": 1, "max": 8, "category": "index", "label": "索引批次重试次数", "description": "单个索引 Embedding 批次失败后的最大尝试次数。"},
    "index_rebuild_settings.retry_base_delay": {"default": 30.0, "type": "float", "min": 0.0, "max": 60.0, "step": 1.0, "category": "index", "label": "索引重试基础等待（秒）", "description": "失败批次执行指数退避时的基础等待时间。"},
    "index_rebuild_settings.batch_delay": {"default": 5.0, "type": "float", "min": 0.0, "max": 10.0, "step": 0.5, "category": "index", "label": "索引读取批次间隔（秒）", "description": "两个数据库读取批次之间的额外等待。"},
    "index_rebuild_settings.request_delay": {"default": 5.0, "type": "float", "min": 0.0, "max": 60.0, "step": 0.5, "category": "index", "label": "Embedding 请求间隔（秒）", "description": "索引重建时相邻 Embedding 请求之间的等待。"},
    "index_rebuild_settings.max_failure_ratio": {"default": 0.02, "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "category": "index", "label": "索引允许失败比例", "description": "失败比例超过该值时不切换到新索引。"},
    "cloudflare_rerank.timeout_seconds": {"default": 30.0, "type": "float", "min": 1.0, "max": 300.0, "step": 1.0, "category": "model", "label": "Cloudflare Rerank 超时（秒）", "description": "仅影响插件内置 Cloudflare Rerank 客户端。"},
    "cloudflare_rerank.max_retries": {"default": 2, "type": "int", "min": 0, "max": 8, "category": "model", "label": "Cloudflare 临时错误重试", "description": "网络错误、HTTP 429 和 5xx 的自动重试次数。"},
    "cloudflare_rerank.retry_base_delay": {"default": 1.0, "type": "float", "min": 0.0, "max": 60.0, "step": 0.5, "category": "model", "label": "Cloudflare 重试等待（秒）", "description": "插件内置 Rerank 客户端重试的基础退避时间。"},
    "backup_settings.keep_days": {"default": 7, "type": "int", "min": 1, "max": 3650, "category": "maintenance", "label": "自动备份保留天数", "description": "超过该天数的自动备份会在维护时清理。"},
    "maintenance.auto_cleanup_completed_build_artifacts": {"default": True, "type": "bool", "category": "maintenance", "label": "自动清理构建中间数据", "description": "每日维护时清理已成功发布且没有待审查项的 Topic 草稿、候选与断点；不会压缩数据库文件，也不会删除正式 Timeline 或 Topic。"},
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
    "SHARED_QUERY_SETTING_KEYS",
    "effective_timeline_settings",
    "timeline_setting_defaults",
    "validate_timeline_setting",
]
