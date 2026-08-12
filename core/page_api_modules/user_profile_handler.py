"""Administrator API for private user profiles and persona relationships."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from quart import request

from astrbot.api import logger

from ...storage.user_profile_store import UserProfileRevisionConflict
from ..user_profile_injection import UserProfileInjectionService

if TYPE_CHECKING:
    from .utils import PageApiUtils


class UserProfileHandler:
    def __init__(self, utils: "PageApiUtils") -> None:
        self.utils = utils

    @staticmethod
    def _components(memory_engine: Any) -> tuple[Any, Any]:
        store = getattr(memory_engine, "user_profile_store", None)
        manager = getattr(memory_engine, "user_profile_maintenance_manager", None)
        if store is None:
            raise RuntimeError("用户画像存储尚未初始化")
        return store, manager

    @staticmethod
    def _history_manager(memory_engine: Any) -> Any:
        return getattr(memory_engine, "user_profile_history_manager", None)

    @staticmethod
    def _task_public(task: dict[str, Any]) -> dict[str, Any]:
        result = dict(task)
        result.pop("persona_prompt", None)
        return result

    async def list_profiles(self, memory_engine: Any) -> dict[str, Any]:
        try:
            store, _manager = self._components(memory_engine)
            limit = max(1, min(200, int(request.args.get("limit") or 50)))
            offset = max(0, int(request.args.get("offset") or 0))
            items = await store.list_profiles(
                search=str(request.args.get("search") or ""),
                status=str(request.args.get("status") or "") or None,
                bot_account=str(request.args.get("bot_account") or "") or None,
                persona_id=str(request.args.get("persona_id") or "") or None,
                platform=str(request.args.get("platform") or "") or None,
                limit=limit,
                offset=offset,
            )
            return self.utils.ok(
                {
                    "items": items,
                    "count": len(items),
                    "limit": limit,
                    "offset": offset,
                    "has_more": len(items) == limit,
                }
            )
        except Exception as exc:
            return self.utils.error(str(exc))

    async def get_detail(self, memory_engine: Any) -> dict[str, Any]:
        try:
            store, _manager = self._components(memory_engine)
            scope_uid = str(request.args.get("profile_scope_uid") or "").strip()
            if not scope_uid:
                return self.utils.error("profile_scope_uid 不能为空")
            detail = await store.profile_detail(scope_uid)
            if detail is None:
                return self.utils.error("用户画像不存在")
            detail["tasks"] = [
                self._task_public(task) for task in detail.get("tasks", [])
            ]
            accounts = list(detail.get("accounts") or [])
            scope = detail["scope"]
            preview = None
            if accounts:
                actor_id = str(accounts[0].get("actor_id") or "")
                session_id = (
                    f"{scope['bot_account']}:private:"
                    f"{accounts[0].get('stable_user_id') or 'profile-preview'}"
                )
                rendered = await UserProfileInjectionService(
                    store,
                    getattr(memory_engine, "user_profile_config", {}),
                ).render_current_user(
                    session_id=session_id,
                    persona_id=str(scope["persona_id"]),
                    actor_id=actor_id,
                    query=str(request.args.get("query") or ""),
                )
                preview = rendered.to_tool_payload()
            detail["injection_preview"] = preview
            return self.utils.ok(detail)
        except Exception as exc:
            return self.utils.error(str(exc))

    async def set_enabled(self, memory_engine: Any, enabled: bool) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, manager = self._components(memory_engine)
            scope_uid = self._scope_uid(payload)
            result = await store.set_profile_enabled(scope_uid, enabled)
            history_manager = self._history_manager(memory_engine)
            if enabled and history_manager is not None:
                history = await history_manager.preview(scope_uid)
                result["history"] = history
                if history["missing_timeline_count"]:
                    await store.set_scope_state(scope_uid, has_gap=True)
                    result["has_gap"] = True
            if enabled and manager is not None and not result["has_gap"]:
                manager.schedule_scope(scope_uid)
            return self.utils.ok(result)
        except Exception as exc:
            return self.utils.error(str(exc))

    async def reset_profile(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, manager = self._components(memory_engine)
            scope_uid = self._scope_uid(payload)
            await self._verify_fingerprint(store, scope_uid, payload)
            objective = await store.reset_objective_profile(scope_uid)
            relationship = None
            if manager is not None:
                relationship = await manager.reset_relationship(
                    scope_uid, reason=self._reason(payload)
                )
            return self.utils.ok(
                {
                    "objective": objective,
                    "relationship_revision": (
                        relationship.revision if relationship is not None else None
                    ),
                    "fingerprint": await store.profile_fingerprint(scope_uid),
                }
            )
        except Exception as exc:
            return self._error(exc)

    async def delete_disable(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            scope_uid = self._scope_uid(payload)
            await self._verify_fingerprint(store, scope_uid, payload)
            return self.utils.ok(await store.delete_and_disable_profile(scope_uid))
        except Exception as exc:
            return self._error(exc)

    async def fact_action(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            result = await store.apply_fact_admin_action(
                self._scope_uid(payload),
                str(payload.get("profile_fact_uid") or ""),
                action=str(payload.get("action") or ""),
                expected_revision=int(payload.get("expected_revision")),
                reason=self._reason(payload),
            )
            return self.utils.ok(result)
        except Exception as exc:
            return self._error(exc)

    async def resolve_conflict(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            result = await store.resolve_profile_conflict(
                self._scope_uid(payload),
                str(payload.get("conflict_uid") or ""),
                resolution=str(payload.get("resolution") or ""),
                selected_fact_uid=(
                    str(payload.get("selected_fact_uid"))
                    if payload.get("selected_fact_uid")
                    else None
                ),
                expected_revision=int(payload.get("expected_revision")),
                reason=self._reason(payload),
            )
            return self.utils.ok(result)
        except Exception as exc:
            return self._error(exc)

    async def rebuild_preview(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            scope_uid = self._scope_uid(payload)
            detail = await store.profile_detail(scope_uid)
            if detail is None:
                raise ValueError("Unknown user-profile scope")
            projection_history = await store.list_projection_history(scope_uid)
            history_manager = self._history_manager(memory_engine)
            history = (
                await history_manager.preview(scope_uid)
                if history_manager is not None
                else {
                    "eligible_timeline_count": len(
                        {
                            str(event.get("timeline_uid") or "")
                            for event in projection_history
                        }
                    ),
                    "missing_timeline_count": 0,
                    "ambiguous_identity_count": 0,
                    "history_fingerprint": "",
                }
            )
            return self.utils.ok(
                {
                    "profile_scope_uid": scope_uid,
                    "fingerprint": detail["fingerprint"],
                    "timeline_event_count": len(projection_history),
                    "timeline_count": history["eligible_timeline_count"],
                    "missing_timeline_count": history["missing_timeline_count"],
                    "ambiguous_identity_count": history[
                        "ambiguous_identity_count"
                    ],
                    "history_fingerprint": history["history_fingerprint"],
                    "fact_count": len(detail.get("facts") or []),
                    "override_count": sum(
                        1 for item in detail.get("overrides") or [] if item.get("active")
                    ),
                    "open_conflict_count": sum(
                        1
                        for item in detail.get("conflicts") or []
                        if item.get("status") == "open"
                    ),
                    "shared_scope_count": len(
                        (detail.get("share_group") or {}).get("members") or []
                    ),
                }
            )
        except Exception as exc:
            return self.utils.error(str(exc))

    async def rebuild_start(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, manager = self._components(memory_engine)
            if manager is None:
                raise RuntimeError("用户画像维护器尚未初始化")
            scope_uid = self._scope_uid(payload)
            history_manager = self._history_manager(memory_engine)
            if history_manager is not None:
                await history_manager.validate_fingerprint(
                    scope_uid,
                    expected_history_fingerprint=str(
                        payload.get("history_fingerprint") or ""
                    ),
                )
            result = await store.prepare_profile_rebuild(
                scope_uid,
                clear_overrides=bool(payload.get("clear_overrides", False)),
                expected_fingerprint=str(payload.get("fingerprint") or ""),
            )
            if history_manager is not None:
                backfill = await history_manager.backfill(
                    scope_uid,
                    expected_history_fingerprint=str(
                        payload.get("history_fingerprint") or ""
                    ),
                )
                result["history"] = backfill
                result["event_count"] += int(backfill["inserted_event_count"])
            manager.schedule_scope(scope_uid)
            result["status"] = "scheduled" if result["event_count"] else "no_history"
            return self.utils.ok(result)
        except Exception as exc:
            return self._error(exc)

    async def identity_review_scan(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            scope_uid = self._scope_uid(payload)
            history_manager = self._history_manager(memory_engine)
            if history_manager is None:
                raise RuntimeError("用户画像历史解析器尚未初始化")
            diagnostics = await history_manager.preview(scope_uid)
            detail = await store.profile_detail(scope_uid)
            if detail is None:
                raise ValueError("Unknown user-profile scope")
            return self.utils.ok(
                {
                    "diagnostics": diagnostics,
                    "items": detail.get("identity_reviews") or [],
                }
            )
        except Exception as exc:
            return self._error(exc)

    async def identity_review_action(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            scope_uid = self._scope_uid(payload)
            action = str(payload.get("action") or "").strip()
            history_manager = self._history_manager(memory_engine)
            if history_manager is not None:
                await history_manager.preview(scope_uid)
            result = await store.resolve_timeline_identity_review(
                timeline_uid=str(payload.get("timeline_uid") or ""),
                timeline_revision=int(payload.get("timeline_revision") or 1),
                memory_space_id=str(payload.get("memory_space_id") or ""),
                action=action,
                expected_evidence_fingerprint=str(
                    payload.get("evidence_fingerprint") or ""
                ),
                profile_scope_uid=scope_uid if action == "bind" else None,
                actor_id=(
                    str(payload.get("actor_id") or "") if action == "bind" else None
                ),
                reason=self._reason(payload),
            )
            diagnostics = None
            if action in {"bind", "restore"} and history_manager is not None:
                diagnostics = await history_manager.preview(scope_uid)
                if action == "bind" or diagnostics.get("missing_timeline_count"):
                    await store.set_scope_state(scope_uid, has_gap=True)
            return self.utils.ok({"item": result, "diagnostics": diagnostics})
        except Exception as exc:
            return self._error(exc)

    async def list_tasks(self, memory_engine: Any) -> dict[str, Any]:
        try:
            store, _manager = self._components(memory_engine)
            scope_uid = str(request.args.get("profile_scope_uid") or "").strip()
            tasks = await store.list_profile_tasks(scope_uid, limit=100)
            return self.utils.ok(
                {"items": [self._task_public(task) for task in tasks]}
            )
        except Exception as exc:
            return self.utils.error(str(exc))

    async def get_task(self, memory_engine: Any) -> dict[str, Any]:
        try:
            store, _manager = self._components(memory_engine)
            task = await store.get_task(str(request.args.get("task_uid") or ""))
            return self.utils.ok(self._task_public(task)) if task else self.utils.error(
                "画像维护任务不存在"
            )
        except Exception as exc:
            return self.utils.error(str(exc))

    async def retry_task(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, manager = self._components(memory_engine)
            scope_uid = await store.retry_profile_task(
                str(payload.get("task_uid") or "")
            )
            if manager is not None:
                manager.schedule_scope(scope_uid)
            return self.utils.ok({"profile_scope_uid": scope_uid, "status": "scheduled"})
        except Exception as exc:
            return self.utils.error(str(exc))

    async def relationship_update(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            _store, manager = self._components(memory_engine)
            if manager is None:
                raise RuntimeError("用户画像维护器尚未初始化")
            state = await manager.update_relationship_manually(
                self._scope_uid(payload),
                changes=dict(payload.get("changes") or {}),
                reason=self._reason(payload),
                expected_revision=int(payload.get("expected_revision") or 0),
                sensitivity_override=(
                    payload.get("sensitivity_override")
                    if "sensitivity_override" in payload
                    else ...
                ),
                behavior_override=(
                    payload.get("behavior_override")
                    if "behavior_override" in payload
                    else ...
                ),
            )
            return self.utils.ok(asdict(state))
        except Exception as exc:
            return self._error(exc)

    async def relationship_freeze(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            _store, manager = self._components(memory_engine)
            if manager is None:
                raise RuntimeError("用户画像维护器尚未初始化")
            scope = await manager.set_relationship_frozen(
                self._scope_uid(payload), bool(payload.get("frozen"))
            )
            return self.utils.ok(asdict(scope) if scope else None)
        except Exception as exc:
            return self.utils.error(str(exc))

    async def relationship_reset(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, manager = self._components(memory_engine)
            if manager is None:
                raise RuntimeError("用户画像维护器尚未初始化")
            scope_uid = self._scope_uid(payload)
            await self._verify_fingerprint(store, scope_uid, payload)
            state = await manager.reset_relationship(
                scope_uid, reason=self._reason(payload)
            )
            return self.utils.ok(asdict(state) if state else None)
        except Exception as exc:
            return self._error(exc)

    async def relationship_rollback(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            _store, manager = self._components(memory_engine)
            if manager is None:
                raise RuntimeError("用户画像维护器尚未初始化")
            state = await manager.rollback_relationship(
                self._scope_uid(payload),
                int(payload.get("revision")),
                reason=self._reason(payload),
            )
            return self.utils.ok(asdict(state))
        except Exception as exc:
            return self.utils.error(str(exc))

    async def relationship_rebuild(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, manager = self._components(memory_engine)
            if manager is None:
                raise RuntimeError("用户画像维护器尚未初始化")
            scope_uid = self._scope_uid(payload)
            await self._verify_fingerprint(store, scope_uid, payload)
            state = await manager.rebuild_relationship_from_projection_history(
                scope_uid,
                use_all_history=bool(payload.get("use_all_history", True)),
                reason=self._reason(payload),
            )
            return self.utils.ok(asdict(state) if state else None)
        except Exception as exc:
            return self._error(exc)

    async def bind_preview(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            return self.utils.ok(
                await store.preview_account_binding(
                    target_actor_id=str(payload.get("target_actor_id") or ""),
                    actor_ids=list(payload.get("actor_ids") or []),
                )
            )
        except Exception as exc:
            return self.utils.error(str(exc))

    async def bind_accounts(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, manager = self._components(memory_engine)
            result = await store.bind_accounts(
                target_actor_id=str(payload.get("target_actor_id") or ""),
                actor_ids=list(payload.get("actor_ids") or []),
                expected_fingerprint=str(payload.get("fingerprint") or ""),
            )
            if manager is not None:
                for scope_uid in result["affected_scope_uids"]:
                    fingerprint = await store.profile_fingerprint(scope_uid)
                    await store.prepare_profile_rebuild(
                        scope_uid,
                        clear_overrides=False,
                        expected_fingerprint=fingerprint,
                    )
                    manager.schedule_scope(scope_uid)
            return self.utils.ok(result)
        except Exception as exc:
            return self._error(exc)

    async def unbind_preview(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            return self.utils.ok(
                await store.preview_account_unbind(str(payload.get("actor_id") or ""))
            )
        except Exception as exc:
            return self.utils.error(str(exc))

    async def unbind_account(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, manager = self._components(memory_engine)
            result = await store.unbind_account(
                str(payload.get("actor_id") or ""),
                expected_fingerprint=str(payload.get("fingerprint") or ""),
            )
            if manager is not None:
                for scope_uid in [
                    *result["created_scope_uids"],
                    *result["affected_old_scope_uids"],
                ]:
                    manager.schedule_scope(scope_uid)
            return self.utils.ok(result)
        except Exception as exc:
            return self._error(exc)

    async def share_preview(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            return self.utils.ok(
                await store.preview_share_group(
                    profile_scope_uids=list(payload.get("profile_scope_uids") or []),
                    share_group_uid=(
                        str(payload.get("share_group_uid"))
                        if payload.get("share_group_uid")
                        else None
                    ),
                )
            )
        except Exception as exc:
            return self.utils.error(str(exc))

    async def share_save(self, memory_engine: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            store, _manager = self._components(memory_engine)
            return self.utils.ok(
                await store.save_share_group(
                    name=str(payload.get("name") or ""),
                    profile_scope_uids=list(payload.get("profile_scope_uids") or []),
                    expected_fingerprint=str(payload.get("fingerprint") or ""),
                    share_group_uid=(
                        str(payload.get("share_group_uid"))
                        if payload.get("share_group_uid")
                        else None
                    ),
                )
            )
        except Exception as exc:
            return self._error(exc)

    @staticmethod
    def _scope_uid(payload: dict[str, Any]) -> str:
        value = str(payload.get("profile_scope_uid") or "").strip()
        if not value:
            raise ValueError("profile_scope_uid 不能为空")
        return value

    @staticmethod
    def _reason(payload: dict[str, Any]) -> str | None:
        value = str(payload.get("reason") or "").strip()
        return value[:2000] or None

    @staticmethod
    async def _verify_fingerprint(
        store: Any, scope_uid: str, payload: dict[str, Any]
    ) -> None:
        supplied = str(payload.get("fingerprint") or "")
        current = await store.profile_fingerprint(scope_uid)
        if not supplied or supplied != current:
            raise UserProfileRevisionConflict("操作预览已过期，请刷新后重试")

    def _error(self, exc: Exception) -> dict[str, Any]:
        if isinstance(exc, UserProfileRevisionConflict):
            return self.utils.error(f"stale_preview: {exc}")
        logger.warning("[PageAPI] 用户画像维护操作失败: %s", exc)
        return self.utils.error(str(exc))


__all__ = ["UserProfileHandler"]
