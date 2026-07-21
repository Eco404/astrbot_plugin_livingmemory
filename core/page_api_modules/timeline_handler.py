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


__all__ = ["TimelineHandler"]
