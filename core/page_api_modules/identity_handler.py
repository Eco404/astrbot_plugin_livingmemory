"""Authoritative identity profile management for the plugin dashboard."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from quart import request

from astrbot.api import logger

if TYPE_CHECKING:
    from ..models.identity_profile import AuthoritativeIdentityStore
    from .utils import PageApiUtils


class IdentityHandler:
    """List and atomically replace WebUI-managed participant profiles."""

    def __init__(self, utils: "PageApiUtils") -> None:
        self.utils = utils

    async def list_profiles(
        self, store: "AuthoritativeIdentityStore"
    ) -> dict[str, Any]:
        return self.utils.ok(store.payload())

    async def save_profiles(
        self,
        store: "AuthoritativeIdentityStore",
        *,
        topic_build_active: bool = False,
    ) -> dict[str, Any]:
        if topic_build_active:
            return self.utils.error(
                "Topic 构建正在运行，请在任务完成后再修改人物资料"
            )
        payload = await request.get_json(silent=True) or {}
        profiles = payload.get("profiles")
        if not isinstance(profiles, list):
            return self.utils.error("profiles 必须是数组")
        try:
            store.replace_profiles(profiles)
            return self.utils.ok(store.payload())
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[PageAPI] 保存权威人物资料失败: %s", exc)
            return self.utils.error(str(exc))


__all__ = ["IdentityHandler"]
