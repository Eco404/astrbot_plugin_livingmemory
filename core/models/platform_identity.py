"""Canonical platform identities shared by conversations and profile records."""

from __future__ import annotations

import re
from typing import Any


_PLATFORM_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "qq": (
        "qq",
        "aiocqhttp",
        "onebot",
        "onebot11",
        "qq_official",
        "qq_official_webhook",
    ),
    "telegram": ("telegram", "telegram_bot", "telegrambot"),
    "discord": ("discord", "discord_bot", "discordbot"),
    "wechat": ("wechat", "weixin", "wechatpadpro"),
    "wecom": ("wecom", "wechat_work", "wechatwork"),
    "feishu": ("feishu", "lark"),
    "dingtalk": ("dingtalk", "ding_talk"),
    "slack": ("slack",),
}


def normalize_platform_token(value: Any) -> str:
    """Normalize spelling while retaining enough structure for unknown adapters."""
    token = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold())
    return token.strip("_")


_PLATFORM_ALIAS_MAP = {
    normalize_platform_token(alias): canonical
    for canonical, aliases in _PLATFORM_ALIAS_GROUPS.items()
    for alias in aliases
}


def canonical_platform(value: Any) -> str:
    """Return a stable social-platform key for known AstrBot adapters.

    Unknown third-party adapter names remain usable instead of being guessed.
    Empty values deliberately stay empty because authoritative profiles use that
    value as the backwards-compatible cross-platform wildcard.
    """
    token = normalize_platform_token(value)
    if not token:
        return ""
    direct = _PLATFORM_ALIAS_MAP.get(token)
    if direct:
        return direct
    # AstrBot adapters sometimes append a transport/version suffix.
    for alias, canonical in sorted(
        _PLATFORM_ALIAS_MAP.items(), key=lambda item: -len(item[0])
    ):
        if token.startswith(f"{alias}_") or token.endswith(f"_{alias}"):
            return canonical
    return token


def platform_aliases(value: Any) -> tuple[str, ...]:
    canonical = canonical_platform(value)
    if not canonical:
        return ()
    return _PLATFORM_ALIAS_GROUPS.get(canonical, (canonical,))


__all__ = [
    "canonical_platform",
    "normalize_platform_token",
    "platform_aliases",
]
