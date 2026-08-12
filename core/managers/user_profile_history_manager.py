"""Safe historical Timeline discovery and projection for private user profiles."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosqlite

from ...storage.user_profile_store import UserProfileRevisionConflict
from ..models.memory_identity import resolve_memory_space
from ..models.platform_identity import canonical_platform
from ..models.user_profile import (
    UserProfileProjectionEvent,
    UserProfileProjectionOperation,
)


class UserProfileHistoryChangedError(UserProfileRevisionConflict):
    """Raised when Timeline history changed after an administrator preview."""


class UserProfileHistoryManager:
    """Resolve private Timeline identities into a durable, reviewable projection."""

    RESOLVER_VERSION = "private-timeline-identity-v1"
    _SQLITE_BATCH_SIZE = 500

    def __init__(
        self,
        db_path: str,
        store: Any,
        *,
        actor_resolver: Callable[[dict[str, Any], str], tuple[str, str | None]],
    ) -> None:
        self.db_path = db_path
        self.store = store
        self.actor_resolver = actor_resolver

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
                "identity_resolution": item["identity_resolution"],
            }
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
            "pending_review_count": 0,
            "ignored_identity_count": 0,
            "native_identity_count": 0,
            "legacy_auto_resolved_count": 0,
        }
        records: list[dict[str, Any]] = []
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
                    if space.chat_type != "private":
                        diagnostics["out_of_scope_count"] += 1
                        continue
                    if space.bot_account != str(
                        scope.get("bot_account") or ""
                    ) or space.persona_id != str(scope.get("persona_id") or ""):
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
                    actor_id, display_name = self.actor_resolver(
                        metadata, space.target_id
                    )
                    records.append(
                        {
                            "timeline_uid": timeline_uid,
                            "timeline_revision": revision,
                            "memory_space_id": space.memory_space_id,
                            "metadata": metadata,
                            "native_actor_id": actor_id,
                            "native_display_name": display_name,
                            "persona_id": space.persona_id,
                            "bot_account": space.bot_account,
                            "private_target_id": space.target_id,
                            "session_id": str(metadata.get("session_id") or ""),
                            "created_at": self._timestamp(metadata.get("create_time")),
                            "document_id": int(row["id"]),
                        }
                    )

        native_by_session: dict[str, set[str]] = {}
        native_names: dict[str, str | None] = {}
        for record in records:
            native_actor = str(record.get("native_actor_id") or "")
            if not native_actor:
                continue
            native_by_session.setdefault(str(record["session_id"]), set()).add(
                native_actor
            )
            native_names[native_actor] = record.get("native_display_name")
        conversation_evidence = await self._conversation_identity_evidence(
            {str(record["session_id"]) for record in records}
        )

        discovered_by_uid: dict[str, dict[str, Any]] = {}
        for record in records:
            metadata = record["metadata"]
            timeline_uid = str(record["timeline_uid"])
            revision = int(record["timeline_revision"])
            memory_space_id = str(record["memory_space_id"])
            native_actor = str(record.get("native_actor_id") or "")
            current = await self.store.get_timeline_identity_resolution(
                timeline_uid, revision, memory_space_id
            )
            if current and str(current.get("identity_basis") or "") == "admin_ignore":
                diagnostics["ignored_identity_count"] += 1
                continue

            if current and str(current.get("identity_basis") or "") == "admin_binding":
                resolution = current
            elif native_actor:
                native_scope = await self.store.get_scope_by_actor(
                    actor_id=native_actor,
                    bot_account=str(record["bot_account"]),
                    persona_id=str(record["persona_id"]),
                    include_disabled=True,
                )
                evidence_payload = {
                    "identity_evidence": ["native_role_binding"],
                    "native_actor_count": 1,
                    "decision_reason": "native_role_binding",
                }
                resolution = await self.store.record_timeline_identity_resolution(
                    timeline_uid=timeline_uid,
                    timeline_revision=revision,
                    memory_space_id=memory_space_id,
                    document_id=int(record["document_id"]),
                    session_id=str(record["session_id"]),
                    bot_account=str(record["bot_account"]),
                    persona_id=str(record["persona_id"]),
                    private_target_id=str(record["private_target_id"]),
                    profile_scope_uid=(
                        native_scope.profile_scope_uid if native_scope else None
                    ),
                    actor_id=native_actor,
                    status="resolved",
                    identity_basis="native_role_binding",
                    evidence_basis="message_grounded",
                    source_granularity="message",
                    resolver_version=self.RESOLVER_VERSION,
                    evidence_fingerprint=self._identity_fingerprint(
                        record,
                        native_by_session,
                        conversation_evidence,
                    ),
                    evidence=evidence_payload,
                )
                diagnostics["native_identity_count"] += 1
            else:
                automatic = await self._resolve_legacy_identity(
                    record,
                    native_by_session=native_by_session,
                    conversation_evidence=conversation_evidence,
                )
                resolution = await self.store.record_timeline_identity_resolution(
                    timeline_uid=timeline_uid,
                    timeline_revision=revision,
                    memory_space_id=memory_space_id,
                    document_id=int(record["document_id"]),
                    session_id=str(record["session_id"]),
                    bot_account=str(record["bot_account"]),
                    persona_id=str(record["persona_id"]),
                    private_target_id=str(record["private_target_id"]),
                    profile_scope_uid=automatic.get("profile_scope_uid"),
                    actor_id=automatic.get("actor_id"),
                    status=str(automatic["status"]),
                    identity_basis=str(automatic["identity_basis"]),
                    evidence_basis="timeline_summary_only",
                    source_granularity="timeline",
                    resolver_version=self.RESOLVER_VERSION,
                    evidence_fingerprint=self._identity_fingerprint(
                        record,
                        native_by_session,
                        conversation_evidence,
                        candidate_fingerprint=str(
                            automatic.get("candidate_fingerprint") or ""
                        ),
                    ),
                    evidence=dict(automatic["evidence"]),
                )
                if resolution["status"] == "resolved":
                    diagnostics["legacy_auto_resolved_count"] += 1

            if str(resolution.get("status") or "") == "ignored":
                diagnostics["ignored_identity_count"] += 1
                continue
            actor_id = str(resolution.get("actor_id") or "")
            if str(resolution.get("status") or "") != "resolved" or not actor_id:
                diagnostics["pending_review_count"] += 1
                diagnostics["ambiguous_identity_count"] += 1
                continue
            if actor_id not in actor_ids:
                diagnostics["out_of_scope_count"] += 1
                continue
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
                memory_space_id,
            )
            item = {
                **record,
                "operation": operation,
                "event_key": event_key,
                "actor_id": actor_id,
                "display_name": (
                    record.get("native_display_name")
                    or native_names.get(actor_id)
                    or next(
                        (
                            account.get("last_observed_name")
                            for account in detail.get("accounts") or []
                            if str(account.get("actor_id") or "") == actor_id
                        ),
                        None,
                    )
                ),
                "identity_resolution": {
                    key: resolution.get(key)
                    for key in (
                        "status",
                        "identity_basis",
                        "evidence_basis",
                        "source_granularity",
                        "resolver_version",
                        "evidence_fingerprint",
                    )
                },
            }
            existing = discovered_by_uid.get(timeline_uid)
            if existing is None or revision > int(existing["timeline_revision"]):
                discovered_by_uid[timeline_uid] = item
        discovered = sorted(
            discovered_by_uid.values(),
            key=lambda item: (item["created_at"], item["document_id"]),
        )
        return discovered, diagnostics

    async def _resolve_legacy_identity(
        self,
        record: dict[str, Any],
        *,
        native_by_session: dict[str, set[str]],
        conversation_evidence: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        session_id = str(record.get("session_id") or "")
        target_id = str(record.get("private_target_id") or "")
        native_actors = set(native_by_session.get(session_id, set()))
        conversation = conversation_evidence.get(session_id, {})
        message_actors = {
            str(value)
            for value in conversation.get("stable_actor_ids", set())
            if self._is_stable_actor(str(value))
        }
        human_senders = {
            str(value) for value in conversation.get("human_sender_ids", set()) if value
        }
        candidates = await self.store.list_profile_identity_candidates(
            bot_account=str(record.get("bot_account") or ""),
            persona_id=str(record.get("persona_id") or ""),
            stable_user_id=target_id,
        )
        evidence_kinds: list[str] = []
        conflicts: list[str] = []
        if not target_id:
            conflicts.append("private_target_missing")
        if native_actors:
            evidence_kinds.append("same_session_native_actor")
        if message_actors:
            evidence_kinds.append("conversation_stable_actor")
        if human_senders:
            evidence_kinds.append("conversation_human_sender")
        if len(native_actors) > 1:
            conflicts.append("multiple_native_session_actors")
        if len(message_actors) > 1:
            conflicts.append("multiple_conversation_actors")
        if native_actors and any(
            self._actor_user_id(value) != target_id for value in native_actors
        ):
            conflicts.append("native_actor_target_mismatch")
        if message_actors and any(
            self._actor_user_id(value) != target_id for value in message_actors
        ):
            conflicts.append("conversation_actor_target_mismatch")
        if human_senders and human_senders != {target_id}:
            conflicts.append("conversation_sender_target_mismatch")

        actor_evidence = native_actors | message_actors
        selected: dict[str, Any] | None = None
        if not conflicts and len(actor_evidence) == 1:
            evidenced_actor = next(iter(actor_evidence))
            matched = [
                item
                for item in candidates
                if str(item.get("actor_id") or "") == evidenced_actor
            ]
            if len(matched) == 1:
                selected = matched[0]
            else:
                conflicts.append("stable_actor_not_uniquely_bound")
        elif not conflicts and human_senders == {target_id}:
            platform = canonical_platform(conversation.get("platform"))
            if platform:
                evidence_kinds.append("conversation_platform")
                matched = [
                    item
                    for item in candidates
                    if canonical_platform(item.get("platform")) == platform
                ]
                if len(matched) == 1:
                    selected = matched[0]
                elif len(matched) > 1:
                    conflicts.append("multiple_same_platform_profile_accounts")
                else:
                    conflicts.append("no_same_platform_profile_account")
            else:
                conflicts.append("platform_evidence_missing")
        elif not conflicts:
            conflicts.append("session_identity_evidence_insufficient")

        evidence = {
            "identity_evidence": evidence_kinds,
            "native_actor_count": len(native_actors),
            "conversation_actor_count": len(message_actors),
            "human_sender_count": len(human_senders),
            "matching_profile_account_count": len(candidates),
            "conflicts": conflicts,
            "decision_reason": (
                "unique_legacy_private_session_identity"
                if selected is not None
                else (conflicts[0] if conflicts else "identity_unresolved")
            ),
        }
        candidate_fingerprint = self._candidate_fingerprint(candidates)
        if selected is None:
            return {
                "status": "pending_review",
                "identity_basis": "legacy_private_session_pending",
                "candidate_fingerprint": candidate_fingerprint,
                "evidence": evidence,
            }
        return {
            "status": "resolved",
            "identity_basis": "legacy_private_session",
            "profile_scope_uid": str(selected.get("profile_scope_uid") or ""),
            "actor_id": str(selected.get("actor_id") or ""),
            "candidate_fingerprint": candidate_fingerprint,
            "evidence": evidence,
        }

    async def _conversation_identity_evidence(
        self, session_ids: set[str]
    ) -> dict[str, dict[str, Any]]:
        path = Path(self.db_path).with_name("conversations.db")
        if not path.is_file() or not session_ids:
            return {}
        result = {
            session_id: {
                "platform": "",
                "human_sender_ids": set(),
                "stable_actor_ids": set(),
            }
            for session_id in session_ids
            if session_id
        }
        if not result:
            return {}
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            table_rows = await (
                await db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('sessions', 'messages')"
                )
            ).fetchall()
            tables = {str(row["name"]) for row in table_rows}
            table_columns: dict[str, set[str]] = {}
            for table_name in tables:
                columns = await (
                    await db.execute(f'PRAGMA table_info("{table_name}")')
                ).fetchall()
                table_columns[table_name] = {str(row["name"]) for row in columns}

            session_list = list(result)
            for start in range(0, len(session_list), self._SQLITE_BATCH_SIZE):
                params = session_list[start : start + self._SQLITE_BATCH_SIZE]
                placeholders = ",".join("?" for _ in params)
                session_columns = table_columns.get("sessions", set())
                if "session_id" in session_columns:
                    platform_column = (
                        "platform" if "platform" in session_columns else "'' AS platform"
                    )
                    rows = await (
                        await db.execute(
                            f"SELECT session_id, {platform_column} FROM sessions "
                            f"WHERE session_id IN ({placeholders})",
                            params,
                        )
                    ).fetchall()
                    for row in rows:
                        bucket = result.get(str(row["session_id"] or ""))
                        if bucket is not None:
                            bucket["platform"] = str(row["platform"] or "")

                message_columns = table_columns.get("messages", set())
                if {"session_id", "role"} <= message_columns and (
                    "sender_id" in message_columns or "metadata" in message_columns
                ):
                    sender_column = (
                        "sender_id"
                        if "sender_id" in message_columns
                        else "'' AS sender_id"
                    )
                    metadata_column = (
                        "metadata" if "metadata" in message_columns else "'{}' AS metadata"
                    )
                    rows = await (
                        await db.execute(
                            f"SELECT session_id, role, {sender_column}, {metadata_column} "
                            f"FROM messages WHERE session_id IN ({placeholders})",
                            params,
                        )
                    ).fetchall()
                    for row in rows:
                        session_id = str(row["session_id"] or "")
                        bucket = result.get(session_id)
                        role = str(row["role"] or "").strip().lower()
                        if bucket is None or role not in {"user", "human"}:
                            continue
                        sender_id = str(row["sender_id"] or "").strip()
                        if sender_id:
                            bucket["human_sender_ids"].add(sender_id)
                        metadata = self._json_object(row["metadata"])
                        actor_id = str(metadata.get("actor_id") or "")
                        if self._is_stable_actor(actor_id):
                            bucket["stable_actor_ids"].add(actor_id)
        return result

    @classmethod
    def _identity_fingerprint(
        cls,
        record: dict[str, Any],
        native_by_session: dict[str, set[str]],
        conversation_evidence: dict[str, dict[str, Any]],
        *,
        candidate_fingerprint: str = "",
    ) -> str:
        session_id = str(record.get("session_id") or "")
        conversation = conversation_evidence.get(session_id, {})
        payload = {
            "timeline_uid": record.get("timeline_uid"),
            "timeline_revision": record.get("timeline_revision"),
            "memory_space_id": record.get("memory_space_id"),
            "private_target_id": record.get("private_target_id"),
            "native_actor_ids": sorted(native_by_session.get(session_id, set())),
            "conversation_platform": canonical_platform(conversation.get("platform")),
            "human_sender_ids": sorted(conversation.get("human_sender_ids", set())),
            "conversation_actor_ids": sorted(
                conversation.get("stable_actor_ids", set())
            ),
            "candidate_fingerprint": str(candidate_fingerprint or ""),
            "resolver_version": cls.RESOLVER_VERSION,
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _candidate_fingerprint(candidates: list[dict[str, Any]]) -> str:
        payload = sorted(
            (
                str(item.get("actor_id") or ""),
                canonical_platform(item.get("platform")),
                str(item.get("profile_scope_uid") or ""),
                bool(item.get("enabled")),
            )
            for item in candidates
        )
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_stable_actor(actor_id: str) -> bool:
        parts = str(actor_id or "").strip().split(":", 2)
        return (
            len(parts) == 3
            and parts[0] not in {"", "unknown"}
            and parts[1] == "human"
            and parts[2] not in {"", "unknown"}
        )

    @staticmethod
    def _actor_user_id(actor_id: str) -> str:
        parts = str(actor_id or "").split(":", 2)
        return parts[2] if len(parts) == 3 else ""

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
                    str(item.get("actor_id") or ""),
                    str(
                        (item.get("identity_resolution") or {}).get(
                            "evidence_fingerprint"
                        )
                        or ""
                    ),
                    str(
                        (item.get("identity_resolution") or {}).get("identity_basis")
                        or ""
                    ),
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
