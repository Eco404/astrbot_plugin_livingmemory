"""Durable, structured source-message snapshots for Timeline memories."""

from __future__ import annotations

from typing import Any

from .models.conversation_models import Message


def serialize_source_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Keep only fields needed for review and deterministic reconstruction."""
    serialized: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):
            data = message
        elif hasattr(message, "to_dict"):
            data = message.to_dict()
        else:
            data = {
                key: getattr(message, key, None)
                for key in (
                    "id",
                    "session_id",
                    "role",
                    "content",
                    "sender_id",
                    "sender_name",
                    "group_id",
                    "platform",
                    "timestamp",
                    "metadata",
                )
            }
        content = Message.content_to_text(data.get("content"))
        if not content:
            continue
        raw_metadata = data.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        try:
            message_id = int(data.get("id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        try:
            timestamp = float(data.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        serialized.append(
            {
                "id": message_id,
                "session_id": str(data.get("session_id") or ""),
                "role": str(data.get("role") or "user"),
                "content": content,
                "sender_id": str(data.get("sender_id") or "unknown"),
                "sender_name": data.get("sender_name"),
                "group_id": data.get("group_id"),
                "platform": data.get("platform"),
                "timestamp": timestamp,
                "metadata": {
                    "is_bot_message": bool(metadata.get("is_bot_message", False)),
                    "persona_name": metadata.get("persona_name"),
                    "proactive_message": bool(
                        metadata.get("proactive_message", False)
                    ),
                },
            }
        )
    return serialized


def restore_source_messages(source: list[dict[str, Any]]) -> list[Message]:
    """Restore snapshot rows to the model consumed by MemoryProcessor."""
    return [Message.from_dict(item) for item in source]


__all__ = ["restore_source_messages", "serialize_source_messages"]
