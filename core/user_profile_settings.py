"""Single source of truth for runtime-adjustable user-profile settings."""

from __future__ import annotations

from typing import Any


USER_PROFILE_SETTINGS_REVISION = 3


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
    "user_profile.maintenance_max_retries": _setting(3, "int", "model_tasks", "模型请求重试", "Provider/API 请求重试上限，不计入每批业务调用数量。", min=0, max=10),
    "user_profile.maintenance_retry_base_seconds": _setting(60, "int", "model_tasks", "重试基础等待", "画像维护失败后的指数退避基础秒数。", min=5, max=3600),
    "user_profile.maintenance_retry_max_seconds": _setting(3600, "int", "model_tasks", "重试最大等待", "画像维护失败后的最大冷却秒数。", min=60, max=86400),
    "user_profile.fact_accept_confidence": _setting(0.85, "float", "fact_admission", "事实接受置信度", "候选事实立即成为有效画像所需的最低维护置信度。", min=0.0, max=1.0, step=0.01),
    "user_profile.fact_min_profile_value": _setting(0.65, "float", "fact_admission", "事实画像价值", "候选事实对未来理解用户的长期价值下限；普通一次性事件应低于该值并忽略。", min=0.0, max=1.0, step=0.01),
    "user_profile.legacy_summary_candidate_confidence": _setting(0.45, "float", "fact_admission", "旧摘要候选置信度", "缺少消息级归属的旧版 Timeline 摘要事实进入待确认画像时使用的置信度上限。", min=0.1, max=0.69, step=0.01),
    "user_profile.pending_retention_days": _setting(180, "int", "fact_admission", "候选保留天数", "未确认 pending 事实超过该期限后归档。", min=1, max=3650),
    "user_profile.behavior_inference_min_timelines": _setting(3, "int", "inference", "行为推断最少 Timeline", "从行为归纳习惯或交流偏好所需的独立 Timeline 数。", min=2, max=20),
    "user_profile.behavior_inference_min_span_days": _setting(14, "int", "inference", "行为证据最小跨度", "普通行为推断证据必须覆盖的天数。", min=1, max=365),
    "user_profile.behavior_inference_min_confidence": _setting(0.85, "float", "inference", "行为推断置信度", "普通行为推断所需的最低综合置信度。", min=0.0, max=1.0, step=0.01),
    "user_profile.behavior_evidence_pool_limit": _setting(128, "int", "inference", "跨批行为证据上限", "每次维护最多提供给模型的历史未归纳行为证据数；证据保留期限沿用候选保留天数。", min=10, max=1000),
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
    "user_profile.relationship_reserved_chars": _setting(300, "int", "injection", "关系状态预留字符", "总预算中优先为人格关系保留的字符数；未使用时回流给事实。", min=0, max=1000),
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
    "user_profile.startup_recovery_limit": _setting(64, "int", "recovery", "启动恢复任务上限", "插件启动时自动恢复的未完成用户任务数量。", min=1, max=1000),
    "user_profile.completed_task_retention_days": _setting(30, "int", "recovery", "完成任务保留天数", "成功任务的精简摘要在数据库中的保留期限。", min=1, max=3650),
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
