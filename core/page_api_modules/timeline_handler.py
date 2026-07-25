"""Timeline runtime settings endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from quart import request

from ..timeline_settings import TIMELINE_SETTING_DEFINITIONS

if TYPE_CHECKING:
    from .utils import PageApiUtils


class TimelineHandler:
    def __init__(self, utils: "PageApiUtils") -> None:
        self.utils = utils

    async def get_settings(self, initializer: Any) -> dict[str, Any]:
        try:
            return self.utils.ok(await initializer.get_timeline_runtime_settings())
        except Exception as exc:
            return self.utils.error(str(exc))

    async def update_settings(self, initializer: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        changes = payload.get("changes", {})
        reset_keys = payload.get("reset_keys", [])
        reset_all = bool(payload.get("reset_all", False))
        if not isinstance(changes, dict):
            return self.utils.error("changes 必须是对象")
        if not isinstance(reset_keys, list):
            return self.utils.error("reset_keys 必须是数组")
        unknown = (
            set(changes)
            | {str(key) for key in reset_keys}
        ) - set(TIMELINE_SETTING_DEFINITIONS)
        if unknown:
            return self.utils.error(
                "未知 Timeline 参数: " + ", ".join(sorted(unknown))
            )
        try:
            result = await initializer.update_timeline_runtime_settings(
                changes,
                reset_keys=[str(key) for key in reset_keys],
                reset_all=reset_all,
            )
            return self.utils.ok(result)
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))

    async def preview_rebuild(self, manager: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        memory_ids = payload.get("memory_ids")
        if memory_ids is not None and not isinstance(memory_ids, list):
            return self.utils.error("memory_ids 必须是数组")
        try:
            result = await manager.preview(
                memory_ids,
                memory_space_id=self.utils.optional_text(
                    payload.get("memory_space_id")
                ),
                limit=min(2000, max(1, int(payload.get("limit", 500)))),
            )
            return self.utils.ok(result)
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))

    async def start_rebuild(self, manager: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        memory_ids = payload.get("memory_ids", [])
        if not isinstance(memory_ids, list):
            return self.utils.error("memory_ids 必须是数组")
        try:
            return self.utils.ok(
                await manager.start_task(
                    memory_ids,
                    topic_mode=str(payload.get("topic_mode") or "local"),
                )
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))

    async def get_rebuild_task(self, manager: Any) -> dict[str, Any]:
        task_uid = self.utils.optional_text(request.args.get("task_uid"))
        if not task_uid:
            return self.utils.error("缺少 task_uid")
        task = await manager.get_task(task_uid)
        if not task:
            return self.utils.error("Timeline 重构任务不存在")
        return self.utils.ok(task)

    async def list_rebuild_tasks(self, manager: Any) -> dict[str, Any]:
        try:
            limit = min(100, max(1, int(request.args.get("limit", 30))))
            return self.utils.ok({"items": await manager.list_tasks(limit=limit)})
        except (TypeError, ValueError) as exc:
            return self.utils.error(str(exc))

    async def resume_rebuild_task(self, manager: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            return self.utils.ok(
                await manager.resume_task(str(payload.get("task_uid") or ""))
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))

    async def cancel_rebuild_task(self, manager: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            cancelled = await manager.cancel_task(
                str(payload.get("task_uid") or "")
            )
            return self.utils.ok({"cancelled": cancelled})
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))

    async def delete_rebuild_task(self, manager: Any) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            deleted = await manager.delete_task(
                str(payload.get("task_uid") or "")
            )
            return self.utils.ok({"deleted": deleted})
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))

    async def clear_rebuild_tasks(self, manager: Any) -> dict[str, Any]:
        try:
            return self.utils.ok(
                {"deleted_count": await manager.clear_finished_tasks()}
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))


__all__ = ["TimelineHandler"]
