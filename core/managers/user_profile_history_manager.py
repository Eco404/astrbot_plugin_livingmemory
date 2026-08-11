"""Safe historical Timeline discovery and projection for private user profiles."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from typing import Any

import aiosqlite

from ...storage.user_profile_store import UserProfileRevisionConflict
from ..models.memory_identity import resolve_memory_space
from ..models.user_profile import (
    UserProfileProjectionEvent,
    UserProfileProjectionOperation,
)


class UserProfileHistoryChangedError(UserProfileRevisionConflict):
    """Raised when Timeline history changed after an administrator preview."""


class UserProfileHistoryManager:
    """Discover exact-scope private Timelines without guessing legacy identities."""

    def __init__(
        self,
        db_path: str,
        store: Any,
        *,
        actor_resolver: Callable[[dict[str, Any], str], tuple[str, str | None]],
        persona_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.db_path = db_path
        self.store = store
        self.actor_resolver = actor_resolver
        self.persona_resolver = persona_resolver

    async def preview(self, profile_scope_uid: str) -> dict[str, Any]:
        discovered, diagnostics = await self._discover(profile_scope_uid)
        history = await self.store.list_projection_history(profile_scope_uid)
        projected = {
            self._event_key(
                str(item.get("timeline_uid") or ""),
                int(item.get("timeline_revision") or 1),
                str(item.get("operation") or "upsert"),
                str(item.get("memory_space_id") or ""),
            )
            for item in history
        }
        missing = [item for item in discovered if item["event_key"] not in projected]
        return {
            **diagnostics,
            "eligible_timeline_count": len(discovered),
            "already_projected_count": len(discovered) - len(missing),
            "missing_timeline_count": len(missing),
            "projection_history_count": len(history),
            "history_fingerprint": self._fingerprint(discovered),
        }

    async def backfill(
        self,
        profile_scope_uid: str,
        *,
        expected_history_fingerprint: str,
    ) -> dict[str, Any]:
        discovered, diagnostics = await self._discover(profile_scope_uid)
        fingerprint = self._fingerprint(discovered)
        self._assert_fingerprint(fingerprint, expected_history_fingerprint)
        history = await self.store.list_projection_history(profile_scope_uid)
        projected = {
            self._event_key(
                str(item.get("timeline_uid") or ""),
                int(item.get("timeline_revision") or 1),
                str(item.get("operation") or "upsert"),
                str(item.get("memory_space_id") or ""),
            )
            for item in history
        }
        persona_snapshot = await self._resolve_persona_snapshot(
            str(discovered[0]["persona_id"]) if discovered else ""
        )
        if persona_snapshot:
            persona_snapshot["basis"] = "current_config"
        inserted = 0
        refreshed = 0
        for item in discovered:
            if item["event_key"] in projected:
                refreshed += 1
            else:
                inserted += 1
            payload = {
                "metadata": item["metadata"],
                "profile_actor_id": item["actor_id"],
                "profile_display_name": item["display_name"],
            }
            if persona_snapshot:
                payload["persona_snapshot"] = persona_snapshot
            await self.store.enqueue_projection_event(
                UserProfileProjectionEvent(
                    timeline_uid=item["timeline_uid"],
                    timeline_revision=item["timeline_revision"],
                    operation=item["operation"],
                    memory_space_id=item["memory_space_id"],
                    profile_scope_uid=profile_scope_uid,
                    payload=payload,
                )
            )
        return {
            **diagnostics,
            "eligible_timeline_count": len(discovered),
            "inserted_event_count": inserted,
            "refreshed_event_count": refreshed,
            "history_fingerprint": fingerprint,
        }

    async def validate_fingerprint(
        self,
        profile_scope_uid: str,
        *,
        expected_history_fingerprint: str,
    ) -> str:
        """Reject stale rebuild previews before any profile state is changed."""
        discovered, _diagnostics = await self._discover(profile_scope_uid)
        fingerprint = self._fingerprint(discovered)
        self._assert_fingerprint(fingerprint, expected_history_fingerprint)
        return fingerprint

    async def _discover(
        self, profile_scope_uid: str
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        detail = await self.store.profile_detail(profile_scope_uid)
        if detail is None:
            raise ValueError("Unknown user-profile scope")
        scope = dict(detail["scope"])
        actor_ids = {
            str(item.get("actor_id") or "")
            for item in detail.get("accounts") or []
            if item.get("actor_id")
        }
        if not actor_ids:
            raise ValueError("User-profile scope has no bound account")

        diagnostics = {
            "scanned_document_count": 0,
            "document_table_missing_count": 0,
            "invalid_metadata_count": 0,
            "non_timeline_count": 0,
            "out_of_scope_count": 0,
            "ambiguous_identity_count": 0,
        }
        discovered_by_uid: dict[str, dict[str, Any]] = {}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            table = await (
                await db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'documents'"
                )
            ).fetchone()
            if table is None:
                diagnostics["document_table_missing_count"] = 1
                return [], diagnostics
            cursor = await db.execute(
                "SELECT id, metadata FROM documents ORDER BY id ASC"
            )
            while True:
                rows = await cursor.fetchmany(512)
                if not rows:
                    break
                diagnostics["scanned_document_count"] += len(rows)
                for row in rows:
                    metadata = self._json_object(row["metadata"])
                    if not metadata:
                        diagnostics["invalid_metadata_count"] += 1
                        continue
                    if str(metadata.get("memory_layer") or "timeline") != "timeline":
                        diagnostics["non_timeline_count"] += 1
                        continue
                    space = resolve_memory_space(
                        metadata.get("session_id"), metadata.get("persona_id")
                    )
                    if (
                        space.chat_type != "private"
                        or space.bot_account != str(scope.get("bot_account") or "")
                        or space.persona_id != str(scope.get("persona_id") or "")
                    ):
                        diagnostics["out_of_scope_count"] += 1
                        continue
                    actor_id, display_name = self.actor_resolver(
                        metadata, space.target_id
                    )
                    if not actor_id:
                        diagnostics["ambiguous_identity_count"] += 1
                        continue
                    if actor_id not in actor_ids:
                        diagnostics["out_of_scope_count"] += 1
                        continue
                    timeline_uid = str(metadata.get("memory_uid") or "").strip()
                    if not timeline_uid:
                        diagnostics["invalid_metadata_count"] += 1
                        continue
                    try:
                        revision = max(1, int(metadata.get("revision") or 1))
                    except (TypeError, ValueError):
                        revision = 1
                    status = str(metadata.get("status") or "active").strip().lower()
                    operation = (
                        UserProfileProjectionOperation.UPSERT
                        if status == "active"
                        else UserProfileProjectionOperation.ARCHIVE
                    )
                    event_key = self._event_key(
                        timeline_uid,
                        revision,
                        str(operation.value),
                        space.memory_space_id,
                    )
                    item = {
                        "timeline_uid": timeline_uid,
                        "timeline_revision": revision,
                        "operation": operation,
                        "memory_space_id": space.memory_space_id,
                        "event_key": event_key,
                        "metadata": metadata,
                        "actor_id": actor_id,
                        "display_name": display_name,
                        "persona_id": space.persona_id,
                        "created_at": self._timestamp(metadata.get("create_time")),
                        "document_id": int(row["id"]),
                    }
                    current = discovered_by_uid.get(timeline_uid)
                    if current is None or revision > int(current["timeline_revision"]):
                        discovered_by_uid[timeline_uid] = item
        discovered = sorted(
            discovered_by_uid.values(),
            key=lambda item: (item["created_at"], item["document_id"]),
        )
        return discovered, diagnostics

    async def _resolve_persona_snapshot(self, persona_id: str) -> dict[str, Any]:
        if not persona_id or self.persona_resolver is None:
            return {}
        result = self.persona_resolver(persona_id)
        if inspect.isawaitable(result):
            result = await result
        return dict(result) if isinstance(result, dict) else {}

    @staticmethod
    def _event_key(
        timeline_uid: str, revision: int, operation: str, memory_space_id: str
    ) -> str:
        return "\x1f".join(
            (timeline_uid, str(max(1, int(revision))), operation, memory_space_id)
        )

    @classmethod
    def _fingerprint(cls, discovered: list[dict[str, Any]]) -> str:
        payload = []
        for item in discovered:
            metadata = json.dumps(
                item["metadata"],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            payload.append(
                [
                    str(item["event_key"]),
                    hashlib.sha256(metadata.encode("utf-8")).hexdigest(),
                ]
            )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _assert_fingerprint(actual: str, expected: str) -> None:
        if actual != str(expected or ""):
            raise UserProfileHistoryChangedError(
                "Timeline history changed after the user-profile rebuild preview"
            )

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    @staticmethod
    def _timestamp(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0


__all__ = ["UserProfileHistoryChangedError", "UserProfileHistoryManager"]
