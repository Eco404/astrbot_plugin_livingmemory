"""Unified, function-oriented runtime settings API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from quart import request

from ..timeline_settings import (
    SHARED_QUERY_SETTING_KEYS,
    TIMELINE_SETTING_DEFINITIONS,
)
from ..topic_settings import TOPIC_SETTING_DEFINITIONS
from ..user_profile_settings import USER_PROFILE_SETTING_DEFINITIONS

if TYPE_CHECKING:
    from .utils import PageApiUtils


_CATEGORY_ORDER = (
    "recall", "timeline", "topic", "user_profile", "session", "graph",
    "lifecycle", "model", "index", "maintenance",
)


def _placement(owner: str, key: str, definition: dict[str, Any]) -> tuple[str, str]:
    category = str(definition.get("category") or "")
    if owner == "user_profile":
        return "user_profile", f"user_profile_{definition.get('group') or 'basic'}"
    if owner == "topic":
        if category == "recall":
            return "recall", "topic_recall"
        if key.startswith("related_topic_"):
            return "topic", "topic_relations"
        if key.startswith("incremental_") or key == "auto_debounce_seconds":
            return "topic", "topic_incremental"
        if category == "performance":
            return "topic", "topic_performance"
        return "topic", "topic_build"
    if category == "recall":
        return "recall", "timeline_recall"
    if category == "injection":
        return "recall", "injection"
    if category == "isolation":
        return "recall", "isolation"
    if category == "generation":
        return "timeline", "timeline_generation"
    if key == "reflection_engine.idle_summary_scan_interval_seconds":
        return "timeline", "timeline_scheduling"
    if category == "session":
        return "session", "session"
    if category == "graph":
        return "graph", "graph"
    if key.startswith("importance_decay."):
        return "lifecycle", "importance_lifecycle"
    if key.startswith("forgetting_agent."):
        return "lifecycle", "cleanup_lifecycle"
    if category == "lifecycle":
        return "lifecycle", "atom_lifecycle"
    if category == "model":
        return "model", "rerank_model"
    if category == "index":
        return "index", "timeline_index"
    if category == "maintenance":
        if key.startswith("maintenance."):
            return "maintenance", "database_maintenance"
        return "maintenance", "backup"
    if category == "performance" and key.startswith("recall_engine.search_cache_"):
        return "recall", "recall_cache"
    return "timeline", "timeline_scheduling"


_GROUP_LABELS = {
    "timeline_recall": "settings.group.timelineRecall",
    "topic_recall": "settings.group.topicRecall",
    "injection": "settings.group.injection",
    "isolation": "settings.group.isolation",
    "recall_cache": "settings.group.recallCache",
    "timeline_generation": "settings.group.timelineGeneration",
    "timeline_scheduling": "settings.group.timelineScheduling",
    "topic_build": "settings.group.topicBuild",
    "topic_relations": "settings.group.topicRelations",
    "topic_incremental": "settings.group.topicIncremental",
    "topic_performance": "settings.group.topicPerformance",
    "session": "settings.group.session",
    "graph": "settings.group.graph",
    "importance_lifecycle": "settings.group.importanceLifecycle",
    "cleanup_lifecycle": "settings.group.cleanupLifecycle",
    "atom_lifecycle": "settings.group.atomLifecycle",
    "rerank_model": "settings.group.rerankModel",
    "timeline_index": "settings.group.timelineIndex",
    "backup": "settings.group.backup",
    "database_maintenance": "settings.group.databaseMaintenance",
    "user_profile_basic": "settings.group.userProfileBasic",
    "user_profile_model_tasks": "settings.group.userProfileModelTasks",
    "user_profile_fact_admission": "settings.group.userProfileFactAdmission",
    "user_profile_inference": "settings.group.userProfileInference",
    "user_profile_conflicts": "settings.group.userProfileConflicts",
    "user_profile_lifecycle": "settings.group.userProfileLifecycle",
    "user_profile_injection": "settings.group.userProfileInjection",
    "user_profile_relationship": "settings.group.userProfileRelationship",
    "user_profile_recovery": "settings.group.userProfileRecovery",
}


class SettingsHandler:
    def __init__(self, utils: "PageApiUtils") -> None:
        self.utils = utils

    async def get_settings(self, memory_engine: Any, initializer: Any) -> dict[str, Any]:
        try:
            view = str(request.args.get("view") or "").strip().lower()
            if view not in {"", "timeline", "topic", "user_profile"}:
                return self.utils.error("view 只能是 timeline、topic 或 user_profile")
            return self.utils.ok(
                await self._payload(memory_engine, initializer, view=view or None)
            )
        except Exception as exc:
            return self.utils.error(str(exc))

    async def update_settings(self, memory_engine: Any, initializer: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        changes = payload.get("changes", {})
        reset_keys = payload.get("reset_keys", [])
        reset_all = bool(payload.get("reset_all", False))
        view = str(payload.get("view") or request.args.get("view") or "").strip().lower()
        if view not in {"", "timeline", "topic", "user_profile"}:
            return self.utils.error("view 只能是 timeline、topic 或 user_profile")
        if not isinstance(changes, dict):
            return self.utils.error("changes 必须是对象")
        if not isinstance(reset_keys, list):
            return self.utils.error("reset_keys 必须是数组")
        known = (
            set(TIMELINE_SETTING_DEFINITIONS)
            | set(TOPIC_SETTING_DEFINITIONS)
            | set(USER_PROFILE_SETTING_DEFINITIONS)
        )
        supplied = set(changes) | {str(key) for key in reset_keys}
        unknown = supplied - known
        if unknown:
            return self.utils.error("未知设置项: " + ", ".join(sorted(unknown)))
        allowed = self._view_keys(view or None)
        outside_view = supplied - allowed
        if outside_view:
            return self.utils.error("设置项不属于当前视图: " + ", ".join(sorted(outside_view)))
        topic_changes = {
            key: value for key, value in changes.items()
            if key in TOPIC_SETTING_DEFINITIONS
        }
        timeline_changes = {
            key: value for key, value in changes.items()
            if key in TIMELINE_SETTING_DEFINITIONS
        }
        profile_changes = {
            key: value for key, value in changes.items()
            if key in USER_PROFILE_SETTING_DEFINITIONS
        }
        topic_resets = [key for key in reset_keys if key in TOPIC_SETTING_DEFINITIONS]
        timeline_resets = [key for key in reset_keys if key in TIMELINE_SETTING_DEFINITIONS]
        profile_resets = [
            key for key in reset_keys if key in USER_PROFILE_SETTING_DEFINITIONS
        ]
        if reset_all and view == "topic":
            timeline_resets = sorted(set(timeline_resets) | set(SHARED_QUERY_SETTING_KEYS))
        try:
            if topic_changes or topic_resets or (reset_all and (not view or view == "topic")):
                await memory_engine.update_topic_runtime_settings(
                    topic_changes,
                    reset_keys=topic_resets,
                    reset_all=reset_all and (not view or view == "topic"),
                )
            if timeline_changes or timeline_resets or (reset_all and (not view or view == "timeline")):
                await initializer.update_timeline_runtime_settings(
                    timeline_changes,
                    reset_keys=timeline_resets,
                    reset_all=reset_all and (not view or view == "timeline"),
                )
            if profile_changes or profile_resets or (
                reset_all and (not view or view == "user_profile")
            ):
                await memory_engine.update_user_profile_runtime_settings(
                    profile_changes,
                    reset_keys=profile_resets,
                    reset_all=reset_all and (not view or view == "user_profile"),
                )
            return self.utils.ok(
                await self._payload(memory_engine, initializer, view=view or None)
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))

    @staticmethod
    def _view_keys(view: str | None) -> set[str]:
        if view == "timeline":
            return set(TIMELINE_SETTING_DEFINITIONS)
        if view == "topic":
            return set(TOPIC_SETTING_DEFINITIONS) | set(SHARED_QUERY_SETTING_KEYS)
        if view == "user_profile":
            return set(USER_PROFILE_SETTING_DEFINITIONS)
        return (
            set(TIMELINE_SETTING_DEFINITIONS)
            | set(TOPIC_SETTING_DEFINITIONS)
            | set(USER_PROFILE_SETTING_DEFINITIONS)
        )

    async def _payload(
        self,
        memory_engine: Any,
        initializer: Any,
        *,
        view: str | None,
    ) -> dict[str, Any]:
        timeline = await initializer.get_timeline_runtime_settings()
        topic = await memory_engine.get_topic_runtime_settings()
        user_profile = await memory_engine.get_user_profile_runtime_settings()
        build_active = bool(memory_engine.topic_build_manager.has_active_builds())
        allowed = self._view_keys(view)
        definitions: dict[str, dict[str, Any]] = {}
        effective: dict[str, Any] = {}
        overrides: dict[str, Any] = {}
        groups: dict[tuple[str, str], list[str]] = {}
        for owner, source in (
            ("timeline", TIMELINE_SETTING_DEFINITIONS),
            ("topic", TOPIC_SETTING_DEFINITIONS),
            ("user_profile", USER_PROFILE_SETTING_DEFINITIONS),
        ):
            source_payload = {
                "timeline": timeline,
                "topic": topic,
                "user_profile": user_profile,
            }[owner]
            for key, raw_definition in source.items():
                if key not in allowed:
                    continue
                definition = dict(raw_definition)
                category_id, group_id = _placement(owner, key, definition)
                views = [owner]
                if key in SHARED_QUERY_SETTING_KEYS:
                    views = ["timeline", "topic"]
                definition.update(
                    {
                        "settings_category": category_id,
                        "settings_group": group_id,
                        "settings_group_label": _GROUP_LABELS[group_id],
                        "views": views,
                        "locked": owner == "topic" and build_active,
                    }
                )
                definitions[key] = definition
                effective[key] = source_payload["effective"][key]
                if key in source_payload["overrides"]:
                    overrides[key] = source_payload["overrides"][key]
                groups.setdefault((category_id, group_id), []).append(key)
        categories = []
        for category_id in _CATEGORY_ORDER:
            category_groups = [
                {
                    "id": group_id,
                    "label": _GROUP_LABELS[group_id],
                    "keys": keys,
                }
                for (current_category, group_id), keys in groups.items()
                if current_category == category_id
            ]
            if category_groups:
                categories.append({"id": category_id, "groups": category_groups})
        return {
            "schema_revision": 1,
            "view": view or "all",
            "categories": categories,
            "definitions": definitions,
            "effective": effective,
            "overrides": overrides,
            "build_active": build_active,
        }


__all__ = ["SettingsHandler"]
