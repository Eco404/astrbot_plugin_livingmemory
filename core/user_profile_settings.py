"""Single source of truth for runtime-adjustable user-profile settings."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


USER_PROFILE_SETTINGS_REVISION = 8


def _setting(
    default: Any,
    value_type: str,
    group: str,
    label: str,
    description: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "default": default,
        "type": value_type,
        "category": "user_profile",
        "group": group,
        "label": label,
        "description": description,
        **extra,
    }


USER_PROFILE_SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    "user_profile.enabled": _setting(True, "bool", "basic", "启用客观用户画像", "维护可溯源的私聊用户事实。"),
    "user_profile.auto_enable_private_users": _setting(True, "bool", "basic", "私聊用户自动建档", "首次成功保存具有稳定账号 ID 的私聊用户消息时自动创建空画像。", visible_when={"key": "user_profile.enabled", "equals": True}),
    "user_profile.relationship_enabled": _setting(True, "bool", "basic", "启用人格关系状态", "维护当前人格对私聊用户的主观感受和关系连续性。"),
    "user_profile.injection_enabled": _setting(True, "bool", "basic", "启用私聊画像注入", "在当前私聊请求中注入对应用户画像和人格关系。"),
    "user_profile.provider_id": _setting("", "string", "model_tasks", "画像维护 LLM Provider", "留空时使用 Timeline 总结 Provider。", special="select_provider"),
    "user_profile.maintenance_concurrency": _setting(1, "int", "model_tasks", "用户维护并发", "不同用户可以并行维护，同一用户始终严格串行。", min=1, max=16),
    "user_profile.maintenance_batch_timeline_limit": _setting(8, "int", "model_tasks", "单批 Timeline 上限", "一次画像维护最多合并的连续 Timeline 变化数。", min=1, max=64),
    "user_profile.maintenance_batch_candidate_limit": _setting(16, "int", "model_tasks", "单批候选事实上限", "一次客观事实维护最多交给模型判断的候选事实数；与 Timeline 和提示字符上限共同决定实际批次大小。单条 Timeline 超限时仍会单独处理。", min=1, max=256),
    "user_profile.maintenance_prompt_max_chars": _setting(16000, "int", "model_tasks", "事实维护提示字符上限", "客观事实维护提示的目标字符上限。系统会优先保留本批候选，再按优先级裁剪已有事实和历史行为证据；单条 Timeline 本身超限时允许超过该目标以保证任务可推进。", min=4000, max=200000),
    "user_profile.maintenance_request_timeout_seconds": _setting(180, "int", "model_tasks", "单次模型调用总超时", "单次画像模型调用（包括 Provider/SDK 内部行为）的总时间上限。超时后由可见的持久维护任务按退避策略重试。", min=30, max=1800),
    "user_profile.fact_maintenance_context_limit": _setting(200, "int", "model_tasks", "已有事实上下文上限", "一次事实维护最多向模型提供的当前事实数；优先保留冲突、有效、待确认和失效待复核事实，不发送长期归档、排除或已取代事实。", min=20, max=2000),
    "user_profile.maintenance_max_retries": _setting(3, "int", "model_tasks", "维护任务自动重试", "一次模型调用失败后，持久维护任务自动重试的次数。每次调用只执行一轮 AstrBot Provider 请求，达到上限后保留任务和断层供人工重试。", min=0, max=10),
    "user_profile.contract_correction_retries": _setting(2, "int", "model_tasks", "模型契约纠错次数", "事实或关系模型返回内容通过网络请求但不符合 JSON 或来源引用契约时，携带允许标识符白名单重新请求的次数；不放宽确定性校验。", min=0, max=5),
    "user_profile.maintenance_retry_base_seconds": _setting(60, "int", "model_tasks", "重试基础等待", "画像维护失败后的指数退避基础秒数。", min=5, max=3600),
    "user_profile.maintenance_retry_max_seconds": _setting(3600, "int", "model_tasks", "重试最大等待", "画像维护失败后的最大冷却秒数。", min=60, max=86400),
    "user_profile.fact_accept_confidence": _setting(0.85, "float", "fact_admission", "事实接受置信度", "候选事实立即成为有效画像所需的最低维护置信度。", min=0.0, max=1.0, step=0.01),
    "user_profile.fact_min_profile_value": _setting(0.65, "float", "fact_admission", "事实画像价值", "候选事实对未来理解用户的长期价值下限；普通一次性事件应低于该值并忽略。", min=0.0, max=1.0, step=0.01),
    "user_profile.legacy_summary_candidate_confidence": _setting(0.45, "float", "fact_admission", "旧摘要候选置信度", "缺少消息级归属的旧版 Timeline 摘要事实进入待确认画像时使用的置信度上限。", min=0.1, max=0.69, step=0.01),
    "user_profile.pending_retention_days": _setting(180, "int", "fact_admission", "候选保留天数", "未确认 pending 事实超过该期限后归档。", min=1, max=3650),
    "user_profile.stale_retention_days": _setting(180, "int", "lifecycle", "失效事实保留天数", "事实进入 stale 后继续缺少新证据达到该期限时归档；来源历史仍保留。", min=1, max=3650),
    "user_profile.behavior_inference_min_timelines": _setting(3, "int", "inference", "行为推断最少 Timeline", "从行为归纳习惯或交流偏好所需的独立 Timeline 数。", min=2, max=20),
    "user_profile.behavior_inference_min_span_days": _setting(14, "int", "inference", "行为证据最小跨度", "普通行为推断证据必须覆盖的天数。", min=1, max=365),
    "user_profile.behavior_inference_min_confidence": _setting(0.85, "float", "inference", "行为推断置信度", "普通行为推断所需的最低综合置信度。", min=0.0, max=1.0, step=0.01),
    "user_profile.behavior_evidence_pool_limit": _setting(128, "int", "inference", "跨批行为证据上限", "每次维护最多提供给模型的历史未归纳行为证据数；证据保留期限沿用候选保留天数。", min=10, max=1000),
    "user_profile.behavior_candidate_cluster_limit": _setting(12, "int", "inference", "行为候选簇上限", "行为发现阶段一次最多保留的候选规律数；最终发布仍需通过独立 Timeline、跨度、置信度和画像价值硬阈值。", min=1, max=50),
    "user_profile.behavior_cluster_evidence_limit": _setting(24, "int", "inference", "单个行为簇证据上限", "单个候选规律在语义补证后最多保留的来源数，防止宽泛候选挤占判定上下文。", min=3, max=128),
    "user_profile.behavior_cluster_time_tolerance_minutes": _setting(120, "int", "inference", "行为补证时刻容差", "时间型行为候选仅共享一个语义锚点时，观察时刻与原簇允许相差的分钟数；只用于补充候选，不改变发布硬阈值。", min=15, max=720),
    "user_profile.behavior_temporal_candidate_limit": _setting(4, "int", "inference", "时间规律候选上限", "每次行为综合最多增加的确定性时间邻域候选数；候选只提高召回，仍需模型与全部发布硬阈值判定。", min=0, max=20),
    "user_profile.behavior_evidence_timezone": _setting("Asia/Shanghai", "string", "inference", "行为证据时区", "将消息证据时间转换为模型可读时间时使用的 IANA 时区，例如 Asia/Shanghai；事实正文与原始时间戳不会被改写。"),
    "user_profile.behavior_synthesis_min_new_evidence": _setting(3, "int", "inference", "触发归纳的新增证据", "增量维护中，至少积累多少条新的未归纳行为证据才调用模型；历史重建末批不受此限制。", min=1, max=100),
    "user_profile.behavior_synthesis_cooldown_hours": _setting(24, "int", "inference", "行为归纳冷却时间", "两次增量行为归纳之间的最短小时数，避免短时间内反复请求；历史重建末批不受此限制。", min=0, max=720),
    "user_profile.behavior_derived_claim_max_chars": _setting(120, "int", "inference", "行为归纳文本上限", "模型形成的单条习惯或交流偏好结论的最大字符数。", min=20, max=500),
    "user_profile.legacy_review_batch_candidate_limit": _setting(64, "int", "fact_admission", "旧摘要单批候选上限", "旧版摘要经过保守去重后，单批最多交给模型复核的候选组数；不占用消息级普通事实候选额度。", min=1, max=512),
    "user_profile.sensitive_behavior_inference_enabled": _setting(False, "bool", "inference", "允许敏感行为推断", "允许根据多条行为证据推断敏感画像；安全秘密始终禁止。"),
    "user_profile.sensitive_inference_min_timelines": _setting(3, "int", "inference", "敏感推断最少 Timeline", "敏感行为推断所需的独立 Timeline 数。", min=2, max=20, visible_when={"key": "user_profile.sensitive_behavior_inference_enabled", "equals": True}),
    "user_profile.sensitive_inference_min_span_days": _setting(14, "int", "inference", "敏感证据最小跨度", "敏感行为推断证据必须覆盖的天数。", min=1, max=3650, visible_when={"key": "user_profile.sensitive_behavior_inference_enabled", "equals": True}),
    "user_profile.sensitive_inference_min_confidence": _setting(0.90, "float", "inference", "敏感推断置信度", "敏感行为推断所需的最低综合置信度。", min=0.0, max=1.0, step=0.01, visible_when={"key": "user_profile.sensitive_behavior_inference_enabled", "equals": True}),
    "user_profile.conflict_recheck_min_new_timelines": _setting(2, "int", "conflicts", "冲突复核新增 Timeline", "自动复核冲突前所需的相关新 Timeline 数。", min=1, max=20),
    "user_profile.conflict_recheck_min_span_days": _setting(14, "int", "conflicts", "冲突复核证据跨度", "冲突后新证据必须覆盖的天数。", min=1, max=3650),
    "user_profile.conflict_resolution_margin": _setting(0.15, "float", "conflicts", "冲突裁决置信优势", "自动解除冲突时胜出事实相对另一方所需的置信优势。", min=0.0, max=1.0, step=0.01),
    "user_profile.preference_fixed_days": _setting(180, "int", "lifecycle", "偏好固定注入天数", "偏好进入分层固定核心的期限。", min=1, max=3650),
    "user_profile.preference_review_days": _setting(365, "int", "lifecycle", "偏好复核天数", "偏好缺少新证据后进入待确认的期限。", min=1, max=3650),
    "user_profile.communication_fixed_days": _setting(180, "int", "lifecycle", "交流偏好固定天数", "交流偏好进入分层固定核心的期限。", min=1, max=3650),
    "user_profile.communication_review_days": _setting(365, "int", "lifecycle", "交流偏好复核天数", "交流偏好缺少新证据后进入待确认的期限。", min=1, max=3650),
    "user_profile.habit_fixed_days": _setting(90, "int", "lifecycle", "习惯固定注入天数", "习惯进入分层固定核心的期限。", min=1, max=3650),
    "user_profile.habit_review_days": _setting(180, "int", "lifecycle", "习惯复核天数", "习惯缺少新证据后进入待确认的期限。", min=1, max=3650),
    "user_profile.current_state_fixed_days": _setting(30, "int", "lifecycle", "当前状态固定天数", "当前状态进入分层固定核心的期限。", min=1, max=3650),
    "user_profile.current_state_review_days": _setting(90, "int", "lifecycle", "当前状态复核天数", "当前状态缺少新证据后进入待确认的期限。", min=1, max=3650),
    "user_profile.undated_plan_review_days": _setting(30, "int", "lifecycle", "无日期计划复核天数", "没有明确日期的计划或承诺进入待确认的期限。", min=1, max=3650),
    "user_profile.dated_plan_grace_days": _setting(3, "int", "lifecycle", "有日期计划宽限天数", "有明确日期的计划结束后继续保留的天数。", min=0, max=365),
    "user_profile.injection_mode": _setting("layered", "select", "injection", "画像注入模式", "分层动态选择或固定精简快照。", options=["layered", "compact_snapshot"]),
    "user_profile.injection_max_chars": _setting(800, "int", "injection", "画像注入总字符", "客观画像和人格关系合计的注入字符硬上限。", min=300, max=2000),
    "user_profile.relationship_reserved_chars": _setting(350, "int", "injection", "关系状态预留字符", "总预算中优先为人格关系保留的字符数；事实与关系未使用的预算会双向回流。", min=0, max=1000),
    "user_profile.fact_injection_max_chars": _setting(200, "int", "injection", "单条事实字符上限", "单条原始事实注入时允许的最大字符数。", min=50, max=1000),
    "user_profile.relationship_narrative_max_chars": _setting(500, "int", "relationship", "主观叙述字符上限", "人格对用户的第一人称主观叙述存储上限。", min=100, max=2000),
    "user_profile.legacy_relationship_initial_dimension_cap": _setting(0.35, "float", "relationship", "旧摘要关系初始上限", "仅由旧版摘要重建关系时，任一关系维度可达到的初始上限。", min=0.0, max=1.0, step=0.01),
    "user_profile.legacy_relationship_soft_limit": _setting(0.04, "float", "relationship", "旧摘要关系变化限幅", "仅有旧版摘要支撑一次关系更新时，单个维度相对当前状态的最大变化。", min=0.0, max=0.25, step=0.01),
    "user_profile.relationship_aftereffect_min_days": _setting(1, "int", "relationship", "余韵最短天数", "关系维护模型建议短期余韵时允许的最短持续时间。", min=1, max=30),
    "user_profile.relationship_aftereffect_default_days": _setting(7, "int", "relationship", "余韵默认天数", "关系维护模型没有提供期限时使用的持续时间。", min=1, max=30),
    "user_profile.relationship_aftereffect_max_days": _setting(14, "int", "relationship", "余韵最长天数", "关系维护模型建议短期余韵时允许的最长持续时间。", min=1, max=365),
    "user_profile.relationship_sensitivity": _setting("balanced", "select", "relationship", "关系变化敏感度", "控制长期关系维度的软限幅速度。", options=["very_slow", "slow", "balanced", "fast", "very_fast"]),
    "user_profile.relationship_behavior_mode": _setting("natural", "select", "relationship", "关系行为模式", "控制关系状态可以在回复行为中发挥的空间。", options=["restrained", "natural", "high_autonomy", "unrestricted"]),
    "user_profile.relationship_full_revision_limit": _setting(100, "int", "relationship", "完整关系 revision 数", "每个 persona-user 保留完整前后状态的 revision 数量。", min=10, max=1000),
    "user_profile.relationship_rebuild_batch_limit": _setting(32, "int", "relationship", "关系历史重建批次", "从全部历史重建关系时，每次模型调用最多处理的有意义 Timeline 数；全量记录会按时间顺序分批处理。", min=1, max=256),
    "user_profile.startup_recovery_limit": _setting(64, "int", "recovery", "启动恢复任务上限", "插件启动时自动恢复的未完成用户任务数量。", min=1, max=1000),
    "user_profile.completed_task_retention_days": _setting(30, "int", "recovery", "完成任务保留天数", "成功任务的精简摘要在数据库中的保留期限。", min=1, max=3650),
    "user_profile.lifecycle_scan_interval_hours": _setting(24, "int", "recovery", "画像生命周期扫描间隔", "定期转换到期事实状态、清理完成任务并压缩可重建的历史派生数据。", min=1, max=168),
    "user_profile.projection_compaction_days": _setting(30, "int", "recovery", "投影历史压缩天数", "超过该期限后清理同一 Timeline 的旧投影 revision 和未引用的失效事实来源；当前投影及可追溯事实来源始终保留。", min=1, max=3650),
}


def user_profile_setting_defaults() -> dict[str, Any]:
    return {
        key: definition["default"]
        for key, definition in USER_PROFILE_SETTING_DEFINITIONS.items()
    }


def validate_user_profile_setting(key: str, value: Any) -> Any:
    definition = USER_PROFILE_SETTING_DEFINITIONS.get(key)
    if definition is None:
        raise ValueError(f"Unknown user-profile setting: {key}")
    value_type = definition["type"]
    if value_type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be boolean")
        return value
    if value_type == "string":
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        return value.strip()
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


def effective_user_profile_settings(
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective = user_profile_setting_defaults()
    for key, value in (overrides or {}).items():
        if key in USER_PROFILE_SETTING_DEFINITIONS:
            effective[key] = validate_user_profile_setting(key, value)
    validate_user_profile_settings(effective)
    return effective


def validate_user_profile_settings(values: dict[str, Any]) -> None:
    try:
        ZoneInfo(str(values["user_profile.behavior_evidence_timezone"]))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("行为证据时区必须是有效的 IANA 时区") from None
    if int(values["user_profile.relationship_reserved_chars"]) > int(
        values["user_profile.injection_max_chars"]
    ):
        raise ValueError("人格关系预留字符不能高于画像注入总字符")
    aftereffect = (
        int(values["user_profile.relationship_aftereffect_min_days"]),
        int(values["user_profile.relationship_aftereffect_default_days"]),
        int(values["user_profile.relationship_aftereffect_max_days"]),
    )
    if not aftereffect[0] <= aftereffect[1] <= aftereffect[2]:
        raise ValueError("关系余韵天数必须满足最小值 <= 默认值 <= 最大值")
    for prefix in ("preference", "communication", "habit", "current_state"):
        if int(values[f"user_profile.{prefix}_fixed_days"]) > int(
            values[f"user_profile.{prefix}_review_days"]
        ):
            raise ValueError(f"{prefix} 固定注入期限不能高于复核期限")
    for suffix in ("min_timelines", "min_span_days", "min_confidence"):
        if float(values[f"user_profile.sensitive_inference_{suffix}"]) < float(
            values[f"user_profile.behavior_inference_{suffix}"]
        ):
            raise ValueError("敏感行为推断门槛不能低于普通行为推断门槛")
    if int(values["user_profile.maintenance_retry_max_seconds"]) < int(
        values["user_profile.maintenance_retry_base_seconds"]
    ):
        raise ValueError("画像维护最大重试等待不能低于基础等待")


__all__ = [
    "USER_PROFILE_SETTINGS_REVISION",
    "USER_PROFILE_SETTING_DEFINITIONS",
    "effective_user_profile_settings",
    "user_profile_setting_defaults",
    "validate_user_profile_setting",
    "validate_user_profile_settings",
]
