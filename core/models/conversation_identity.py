"""Deterministic identities for raw conversation messages.

AstrBot reuses the incoming event when emitting an assistant response.  Some
adapters (and synthetic events) therefore expose the human sender through both
``get_sender_id`` and ``message_obj.self_id``.  This module keeps event parsing
out of the storage manager and records enough provenance to audit ambiguous
rows without guessing from display names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .platform_identity import canonical_platform


_QQ_INSTANCE_RE = re.compile(r"^qq(?P<account>\d+)$", re.IGNORECASE)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _safe_call(obj: Any, name: str) -> Any:
    resolver = getattr(obj, name, None)
    if not callable(resolver):
        return None
    try:
        return resolver()
    except Exception:
        return None


def _raw_get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


@dataclass(frozen=True)
class ConversationActorIdentity:
    """Resolved actor and the evidence used to resolve it."""

    sender_id: str
    sender_name: str
    actor_type: str
    platform: str
    canonical_platform: str
    platform_instance_id: str | None = None
    persona_id: str | None = None
    persona_name: str | None = None
    identity_source: str = "event_sender"
    identity_confidence: float = 1.0
    event_source: str = "incoming_message"
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "canonical_platform": self.canonical_platform,
            "identity_source": self.identity_source,
            "identity_confidence": self.identity_confidence,
            "event_source": self.event_source,
        }
        if self.platform_instance_id:
            result["platform_instance_id"] = self.platform_instance_id
        if self.persona_id:
            result["persona_id"] = self.persona_id
        if self.persona_name:
            result["persona_name"] = self.persona_name
        if self.warnings:
            result["identity_warnings"] = list(self.warnings)
        return result


class ConversationIdentityResolver:
    """Resolve event identities using IDs and roles, never nickname inference."""

    def __init__(self, normalize_name, resolve_human_name=None):
        self._normalize_name = normalize_name
        self._human_name_resolver = resolve_human_name

    def resolve(
        self,
        event: Any,
        role: str,
        *,
        persona_id: str | None = None,
        persona_name: str | None = None,
        event_source: str | None = None,
    ) -> ConversationActorIdentity:
        session_id = _text(getattr(event, "unified_msg_origin", None)) or "unknown"
        raw_platform = _text(_safe_call(event, "get_platform_name")) or "unknown"
        instance_id, instance_account, peer_id = self._parse_origin(session_id)

        raw_platform_match = _QQ_INSTANCE_RE.match(raw_platform)
        warnings: list[str] = []
        if raw_platform_match:
            # A platform field such as QQ20000001 is an adapter instance, not
            # a social-platform identifier.  Store a stable logical platform.
            instance_id = instance_id or raw_platform
            instance_account = instance_account or raw_platform_match.group("account")
            platform = "qq"
            warnings.append("platform_value_is_instance_id")
        else:
            platform = raw_platform
        platform_key = canonical_platform(platform) or "unknown"

        human_sender_id = _text(_safe_call(event, "get_sender_id"))
        if not human_sender_id:
            human_sender_id = _text(getattr(event, "sender_id", None))
        human_sender_id = human_sender_id or peer_id or session_id

        if role != "assistant":
            sender_name = self._resolve_human_name(event, human_sender_id)
            return ConversationActorIdentity(
                sender_id=human_sender_id,
                sender_name=sender_name,
                actor_type="human",
                platform=platform,
                canonical_platform=platform_key,
                platform_instance_id=instance_id,
                identity_source="event_sender",
                identity_confidence=1.0 if human_sender_id != session_id else 0.5,
                event_source=event_source or "incoming_message",
                warnings=tuple(warnings),
            )

        message_obj = getattr(event, "message_obj", None)
        event_self_id = _text(_safe_call(event, "get_self_id"))
        object_self_id = _text(_raw_get(message_obj, "self_id"))
        bot_id, identity_source, confidence = self._select_assistant_id(
            human_sender_id=human_sender_id,
            event_self_id=event_self_id,
            object_self_id=object_self_id,
            instance_account=instance_account,
            warnings=warnings,
        )
        if not bot_id:
            bot_id = f"persona:{persona_id or 'default'}"
            identity_source = "persona_fallback"
            confidence = 0.45
            warnings.append("assistant_account_id_unavailable")

        normalized_persona_name = self._normalize_name(persona_name)
        bot_display_name = normalized_persona_name or self._resolve_bot_name(event)
        if not bot_display_name:
            bot_display_name = bot_id
            warnings.append("assistant_display_name_fell_back_to_id")

        return ConversationActorIdentity(
            sender_id=bot_id,
            sender_name=bot_display_name,
            actor_type="assistant",
            platform=platform,
            canonical_platform=platform_key,
            platform_instance_id=instance_id,
            persona_id=_text(persona_id),
            persona_name=normalized_persona_name,
            identity_source=identity_source,
            identity_confidence=confidence,
            event_source=event_source or "llm_response",
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _resolve_human_name(self, event: Any, sender_id: str) -> str:
        if self._human_name_resolver is not None:
            resolved = self._normalize_name(
                self._human_name_resolver(event, sender_id)
            )
            if resolved:
                return resolved
        name = self._normalize_name(_safe_call(event, "get_sender_name"))
        if name:
            return name
        raw_sender = _raw_get(getattr(event, "message_obj", None), "sender")
        for key in ("nickname", "card", "display_name", "username"):
            name = self._normalize_name(_raw_get(raw_sender, key))
            if name:
                return name
        return sender_id

    def _resolve_bot_name(self, event: Any) -> str | None:
        for name in ("get_self_name", "get_bot_name"):
            value = self._normalize_name(_safe_call(event, name))
            if value:
                return value
        message_obj = getattr(event, "message_obj", None)
        for name in ("self_name", "bot_name"):
            value = self._normalize_name(_raw_get(message_obj, name))
            if value:
                return value
        return None

    @staticmethod
    def _select_assistant_id(
        *,
        human_sender_id: str,
        event_self_id: str | None,
        object_self_id: str | None,
        instance_account: str | None,
        warnings: list[str],
    ) -> tuple[str | None, str, float]:
        for candidate, source, confidence in (
            (event_self_id, "event_self_id", 1.0),
            (object_self_id, "message_object_self_id", 0.95),
        ):
            if not candidate:
                continue
            if candidate == human_sender_id and instance_account != human_sender_id:
                warnings.append(f"{source}_collides_with_human_sender")
                continue
            return candidate, source, confidence
        if instance_account:
            return instance_account, "platform_instance_id", 0.9
        return None, "unresolved", 0.0

    @staticmethod
    def _parse_origin(session_id: str) -> tuple[str | None, str | None, str | None]:
        parts = session_id.split(":")
        instance_id = parts[0] if parts else None
        peer_id = parts[2] if len(parts) >= 3 else None
        match = _QQ_INSTANCE_RE.match(instance_id or "")
        if match:
            return instance_id, match.group("account"), peer_id
        return None, None, peer_id


def audit_message_identity(message: Any) -> list[str]:
    """Return deterministic identity anomalies for historical audit UIs."""
    issues: list[str] = []
    metadata = getattr(message, "metadata", {}) or {}
    role = str(getattr(message, "role", ""))
    sender_id = _text(getattr(message, "sender_id", None))
    sender_name = _text(getattr(message, "sender_name", None))
    platform = _text(getattr(message, "platform", None)) or ""
    if _QQ_INSTANCE_RE.match(platform):
        issues.append("platform_value_is_instance_id")
    if role == "assistant" and sender_name == sender_id:
        issues.append("assistant_display_name_is_account_id")
    session_parts = str(getattr(message, "session_id", "") or "").split(":")
    session_peer = session_parts[2] if len(session_parts) >= 3 else None
    if role == "assistant" and sender_id and sender_id == session_peer:
        issues.append("assistant_sender_matches_session_peer")
    expected_type = "assistant" if role == "assistant" else "human"
    if metadata.get("actor_type") not in (None, expected_type):
        issues.append("actor_type_conflicts_with_role")
    expected_prefix = f"{canonical_platform(platform) or 'unknown'}:{expected_type}:"
    actor_id = str(metadata.get("actor_id") or "")
    if actor_id and not actor_id.startswith(expected_prefix):
        issues.append("actor_id_conflicts_with_message_fields")
    return issues


__all__ = [
    "ConversationActorIdentity",
    "ConversationIdentityResolver",
    "audit_message_identity",
]
