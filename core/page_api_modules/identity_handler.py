"""Authoritative identity profile management for the plugin dashboard."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from quart import request

from astrbot.api import logger

from ..models.identity_profile import parse_authoritative_identity_profiles
from ..models.platform_identity import canonical_platform, platform_aliases

if TYPE_CHECKING:
    from ..models.identity_profile import AuthoritativeIdentityStore
    from .utils import PageApiUtils


class IdentityHandler:
    """List and atomically replace WebUI-managed participant profiles."""

    def __init__(self, utils: "PageApiUtils") -> None:
        self.utils = utils

    async def list_profiles(
        self,
        store: "AuthoritativeIdentityStore",
        *,
        platform_manager: Any = None,
        conversation_manager: Any = None,
    ) -> dict[str, Any]:
        payload = store.payload()
        payload["platform_options"] = await self._platform_catalog(
            store,
            platform_manager=platform_manager,
            conversation_manager=conversation_manager,
        )
        return self.utils.ok(payload)

    async def save_profiles(
        self,
        store: "AuthoritativeIdentityStore",
        *,
        topic_build_active: bool = False,
        platform_manager: Any = None,
        conversation_manager: Any = None,
        on_saved: Any = None,
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
            previous_profiles = list(store.payload().get("profiles", []))
            parsed_profiles = parse_authoritative_identity_profiles(profiles)
            normalized_profiles = [item.to_storage_dict() for item in parsed_profiles]
            deleted_profiles = self._deleted_profiles(
                previous_profiles, normalized_profiles
            )
            if deleted_profiles and payload.get("confirm_identity_deletions") is not True:
                return self.utils.error("删除人物资料前必须先确认影响范围")
            sync_mode = str(payload.get("sync_mode") or "queue").strip().lower()
            if sync_mode not in {"queue", "immediate"}:
                return self.utils.error("sync_mode 必须是 queue 或 immediate")
            store.replace_profiles(normalized_profiles)
            result = store.payload()
            if callable(on_saved):
                try:
                    parameters = inspect.signature(on_saved).parameters
                    kwargs = (
                        {"sync_mode": sync_mode}
                        if "sync_mode" in parameters
                        or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in parameters.values()
                        )
                        else {}
                    )
                    sync_result = on_saved(
                        previous_profiles,
                        list(result.get("profiles", [])),
                        **kwargs,
                    )
                    if inspect.isawaitable(sync_result):
                        sync_result = await sync_result
                    result["topic_sync"] = sync_result or {}
                except Exception as exc:
                    logger.error(
                        "[PageAPI] 人物资料已保存，但 Topic 同步排队失败",
                        exc_info=True,
                    )
                    result["topic_sync"] = {"queued": False, "error": str(exc)}
            result["platform_options"] = await self._platform_catalog(
                store,
                platform_manager=platform_manager,
                conversation_manager=conversation_manager,
            )
            return self.utils.ok(result)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[PageAPI] 保存权威人物资料失败: %s", exc)
            return self.utils.error(str(exc))

    async def preview_profile_changes(
        self,
        store: "AuthoritativeIdentityStore",
        *,
        impact_resolver: Any,
    ) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        profiles = payload.get("profiles")
        if not isinstance(profiles, list):
            return self.utils.error("profiles 必须是数组")
        try:
            normalized = [
                item.to_storage_dict()
                for item in parse_authoritative_identity_profiles(profiles)
            ]
            previous = list(store.payload().get("profiles", []))
            result = impact_resolver(previous, normalized)
            if inspect.isawaitable(result):
                result = await result
            return self.utils.ok(result or {})
        except (TypeError, ValueError) as exc:
            return self.utils.error(str(exc))

    @staticmethod
    def _deleted_profiles(
        previous_profiles: list[dict[str, Any]],
        current_profiles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        current_keys = {
            (
                canonical_platform(item.get("platform")),
                str(item.get("user_id") or "").strip().casefold(),
            )
            for item in current_profiles
            if isinstance(item, dict)
        }
        return [
            item
            for item in previous_profiles
            if isinstance(item, dict)
            and (
                canonical_platform(item.get("platform")),
                str(item.get("user_id") or "").strip().casefold(),
            )
            not in current_keys
        ]

    @staticmethod
    async def _platform_catalog(
        store: "AuthoritativeIdentityStore",
        *,
        platform_manager: Any = None,
        conversation_manager: Any = None,
    ) -> list[dict[str, Any]]:
        """Combine live adapters, observed history, and persisted profile values."""
        catalog: dict[str, dict[str, Any]] = {}

        def include(
            value: Any,
            *,
            source: str,
            adapter: Any = None,
            instance_id: Any = None,
            display_name: Any = None,
        ) -> None:
            canonical = canonical_platform(value or adapter)
            if not canonical:
                return
            item = catalog.setdefault(
                canonical,
                {
                    "value": canonical,
                    "display_name": str(display_name or canonical),
                    "aliases": [],
                    "instance_ids": [],
                    "sources": [],
                },
            )
            aliases = [str(value or "").strip(), str(adapter or "").strip()]
            aliases.extend(platform_aliases(canonical))
            for alias in aliases:
                if alias and alias not in item["aliases"]:
                    item["aliases"].append(alias)
            instance_id = str(instance_id or "").strip()
            if instance_id and instance_id not in item["instance_ids"]:
                item["instance_ids"].append(instance_id)
            if source not in item["sources"]:
                item["sources"].append(source)

        if platform_manager is not None:
            try:
                instances = platform_manager.get_insts()
                if hasattr(instances, "__await__"):
                    instances = await instances
                for instance in instances or []:
                    metadata = (
                        instance.meta()
                        if callable(getattr(instance, "meta", None))
                        else None
                    )
                    adapter = getattr(metadata, "name", "")
                    include(
                        adapter,
                        source="runtime",
                        adapter=adapter,
                        instance_id=getattr(metadata, "id", ""),
                        display_name=getattr(metadata, "adapter_display_name", "")
                        or canonical_platform(adapter),
                    )
            except Exception:
                logger.debug("[PageAPI] 无法读取 AstrBot 平台实例目录", exc_info=True)

        if conversation_manager is not None:
            try:
                sessions = await conversation_manager.store.get_recent_sessions(
                    limit=5000
                )
                for session in sessions:
                    include(session.platform, source="history", adapter=session.platform)
            except Exception:
                logger.debug("[PageAPI] 无法读取历史会话平台目录", exc_info=True)

        for profile in store.profiles:
            include(profile.platform, source="profile")
            for alias in profile.platform_aliases:
                include(alias, source="profile", adapter=alias)
            item = catalog.get(canonical_platform(profile.platform))
            if item is not None:
                for instance_id in profile.platform_instances:
                    if instance_id not in item["instance_ids"]:
                        item["instance_ids"].append(instance_id)

        return sorted(catalog.values(), key=lambda item: item["value"].casefold())


__all__ = ["IdentityHandler"]
