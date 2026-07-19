"""Stable logical identity helpers for timeline memories."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass


_MEMORY_SPACE_NAMESPACE = uuid.UUID("40e09d5a-1c40-4b20-8c8b-1c925f17a605")


@dataclass(frozen=True, slots=True)
class MemorySpace:
    """The isolation boundary used by future derived-memory layers."""

    memory_space_id: str
    bot_account: str
    chat_type: str
    target_id: str
    persona_id: str


def resolve_memory_space(
    session_id: str | None,
    persona_id: str | None,
) -> MemorySpace:
    """Build a deterministic, session-isolated memory-space identity.

    Stage one deliberately follows the plugin's current strict session/persona
    isolation. A later topic-memory feature may introduce an explicit broader
    scope, but must never infer one by silently changing this resolver.
    """

    normalized_session = str(session_id or "").strip()
    normalized_persona = str(persona_id or "").strip()
    parts = normalized_session.split(":", 2)
    bot_account = parts[0] if normalized_session else "global"
    message_type = parts[1] if len(parts) > 1 else ""
    target_id = parts[2] if len(parts) > 2 else normalized_session or "global"

    normalized_type = message_type.casefold()
    if "group" in normalized_type:
        chat_type = "group"
    elif "friend" in normalized_type or "private" in normalized_type:
        chat_type = "private"
    else:
        chat_type = "other" if normalized_session else "global"

    payload = json.dumps(
        {
            "version": 1,
            "bot_account": bot_account,
            "chat_type": chat_type,
            "target_id": target_id,
            "session_id": normalized_session,
            "persona_id": normalized_persona,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    memory_space_id = f"space-v1-{uuid.uuid5(_MEMORY_SPACE_NAMESPACE, payload)}"
    return MemorySpace(
        memory_space_id=memory_space_id,
        bot_account=bot_account,
        chat_type=chat_type,
        target_id=target_id,
        persona_id=normalized_persona,
    )


__all__ = ["MemorySpace", "resolve_memory_space"]
