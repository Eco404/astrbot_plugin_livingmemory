"""Transactional SQLite storage for private-chat user profiles."""

from __future__ import annotations

import json
import hashlib
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from enum import Enum
from typing import Any, Iterable

import aiosqlite

from ..core.models.user_profile import (
    UserProfileFact,
    UserProfileFactSource,
    UserProfileProjectionEvent,
    UserProfileScope,
    UserProfileTask,
    UserRelationshipState,
)


class UserProfileRevisionConflict(RuntimeError):
    """Raised when an obsolete relationship or profile snapshot is published."""


class UserProfileStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        async with self._connect() as db:
            await self.create_schema(db)
            columns = {
                str(row["name"])
                for row in await (
                    await db.execute("PRAGMA table_info(user_profile_scopes)")
                ).fetchall()
            }
            if "relationship_reset_after" not in columns:
                await db.execute(
                    "ALTER TABLE user_profile_scopes "
                    "ADD COLUMN relationship_reset_after REAL"
                )
            now = time.time()
            await db.execute(
                """
                UPDATE user_profile_tasks
                SET status = 'pending', updated_at = ?,
                    error = CASE
                        WHEN error IS NULL OR error = ''
                        THEN 'Plugin process stopped before maintenance completed'
                        ELSE error
                    END
                WHERE status IN (
                    'running_facts', 'facts_completed', 'facts_failed',
                    'running_relationship'
                )
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE user_profile_projection_events
                SET status = 'pending', updated_at = ?,
                    error = CASE
                        WHEN error IS NULL OR error = ''
                        THEN 'Plugin process stopped while projecting this Timeline'
                        ELSE error
                    END
                WHERE status = 'running'
                """,
                (now,),
            )
            await db.commit()

    @staticmethod
    async def create_schema(db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_profile_setting_overrides (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                settings_revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profile_users (
                logical_user_uid TEXT PRIMARY KEY,
                display_name_override TEXT,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'disabled', 'deleted')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS user_profile_accounts (
                actor_id TEXT PRIMARY KEY,
                logical_user_uid TEXT NOT NULL,
                platform TEXT NOT NULL,
                stable_user_id TEXT NOT NULL,
                observed_names TEXT NOT NULL DEFAULT '[]',
                last_observed_name TEXT,
                linked_manually INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(logical_user_uid) REFERENCES user_profile_users(logical_user_uid)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_user_profile_accounts_user
                ON user_profile_accounts(logical_user_uid, platform);

            CREATE TABLE IF NOT EXISTS user_profile_fact_namespaces (
                fact_namespace_uid TEXT PRIMARY KEY,
                current_revision INTEGER NOT NULL DEFAULT 0,
                share_group_uid TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profile_scopes (
                profile_scope_uid TEXT PRIMARY KEY,
                logical_user_uid TEXT NOT NULL,
                bot_account TEXT NOT NULL,
                persona_id TEXT NOT NULL,
                fact_namespace_uid TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                auto_enable_blocked INTEGER NOT NULL DEFAULT 0,
                projection_cursor INTEGER NOT NULL DEFAULT 0,
                reset_after REAL,
                has_gap INTEGER NOT NULL DEFAULT 0,
                relationship_frozen INTEGER NOT NULL DEFAULT 0,
                relationship_reset_after REAL,
                relationship_sensitivity_override TEXT,
                relationship_behavior_override TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(logical_user_uid, bot_account, persona_id),
                FOREIGN KEY(logical_user_uid) REFERENCES user_profile_users(logical_user_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(fact_namespace_uid)
                    REFERENCES user_profile_fact_namespaces(fact_namespace_uid)
            );
            CREATE INDEX IF NOT EXISTS idx_user_profile_scopes_lookup
                ON user_profile_scopes(bot_account, persona_id, logical_user_uid, enabled);

            CREATE TABLE IF NOT EXISTS user_profile_share_groups (
                share_group_uid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                fact_namespace_uid TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(fact_namespace_uid)
                    REFERENCES user_profile_fact_namespaces(fact_namespace_uid)
            );

            CREATE TABLE IF NOT EXISTS user_profile_share_members (
                share_group_uid TEXT NOT NULL,
                profile_scope_uid TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                PRIMARY KEY(share_group_uid, profile_scope_uid),
                FOREIGN KEY(share_group_uid) REFERENCES user_profile_share_groups(share_group_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(profile_scope_uid) REFERENCES user_profile_scopes(profile_scope_uid)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_profile_fact_sources (
                source_uid TEXT PRIMARY KEY,
                profile_fact_uid TEXT,
                timeline_uid TEXT NOT NULL,
                timeline_revision INTEGER NOT NULL CHECK(timeline_revision >= 1),
                fact_index INTEGER NOT NULL CHECK(fact_index >= 0),
                fact_fingerprint TEXT NOT NULL,
                raw_fact TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                claim_type TEXT NOT NULL DEFAULT 'speaker_self',
                attribution_confidence REAL NOT NULL DEFAULT 1.0,
                timeline_quality TEXT NOT NULL DEFAULT '{}',
                evidence_started_at REAL,
                evidence_ended_at REAL,
                source_account_actor_id TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                UNIQUE(timeline_uid, timeline_revision, fact_index, actor_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_profile_sources_fact
                ON user_profile_fact_sources(profile_fact_uid, active);
            CREATE INDEX IF NOT EXISTS idx_user_profile_sources_timeline
                ON user_profile_fact_sources(timeline_uid, timeline_revision, active);
            CREATE INDEX IF NOT EXISTS idx_user_profile_sources_actor
                ON user_profile_fact_sources(actor_id, active);

            CREATE TABLE IF NOT EXISTS user_profile_facts (
                profile_fact_uid TEXT PRIMARY KEY,
                fact_namespace_uid TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                representative_source_uid TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.85,
                importance REAL NOT NULL DEFAULT 0.5,
                inference_kind TEXT NOT NULL DEFAULT 'explicit',
                sensitive INTEGER NOT NULL DEFAULT 0,
                admin_confirmed INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                first_seen_at REAL,
                last_confirmed_at REAL,
                fixed_injection_until REAL,
                review_after REAL,
                superseded_by TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(fact_namespace_uid)
                    REFERENCES user_profile_fact_namespaces(fact_namespace_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(representative_source_uid)
                    REFERENCES user_profile_fact_sources(source_uid),
                FOREIGN KEY(superseded_by) REFERENCES user_profile_facts(profile_fact_uid)
            );
            CREATE INDEX IF NOT EXISTS idx_user_profile_facts_serving
                ON user_profile_facts(fact_namespace_uid, status, pinned, importance DESC);

            CREATE TABLE IF NOT EXISTS user_profile_conflicts (
                conflict_uid TEXT PRIMARY KEY,
                fact_namespace_uid TEXT NOT NULL,
                conflict_key TEXT NOT NULL,
                fact_uids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'open',
                first_detected_at REAL NOT NULL,
                last_evidence_at REAL,
                resolution_kind TEXT,
                resolution_reason TEXT,
                resolved_fact_uid TEXT,
                resolved_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                UNIQUE(fact_namespace_uid, conflict_key, status),
                FOREIGN KEY(fact_namespace_uid)
                    REFERENCES user_profile_fact_namespaces(fact_namespace_uid)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_user_profile_conflicts_status
                ON user_profile_conflicts(fact_namespace_uid, status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS user_profile_fact_overrides (
                override_uid TEXT PRIMARY KEY,
                fact_namespace_uid TEXT NOT NULL,
                profile_fact_uid TEXT,
                override_type TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                payload TEXT NOT NULL DEFAULT '{}',
                reason TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(fact_namespace_uid)
                    REFERENCES user_profile_fact_namespaces(fact_namespace_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(profile_fact_uid) REFERENCES user_profile_facts(profile_fact_uid)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_user_profile_overrides_fact
                ON user_profile_fact_overrides(profile_fact_uid, active);

            CREATE TABLE IF NOT EXISTS user_relationship_states (
                relationship_uid TEXT PRIMARY KEY,
                profile_scope_uid TEXT NOT NULL UNIQUE,
                revision INTEGER NOT NULL DEFAULT 0,
                familiarity REAL NOT NULL DEFAULT 0.0,
                trust REAL NOT NULL DEFAULT 0.0,
                warmth REAL NOT NULL DEFAULT 0.0,
                ease REAL NOT NULL DEFAULT 0.0,
                tension REAL NOT NULL DEFAULT 0.0,
                concern REAL NOT NULL DEFAULT 0.0,
                stance_tags TEXT NOT NULL DEFAULT '[]',
                subjective_summary TEXT NOT NULL DEFAULT '',
                recent_aftereffect TEXT NOT NULL DEFAULT '',
                aftereffect_expires_at REAL,
                persona_signature TEXT NOT NULL DEFAULT '{}',
                source_timeline_uids TEXT NOT NULL DEFAULT '[]',
                reset_after REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(profile_scope_uid) REFERENCES user_profile_scopes(profile_scope_uid)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_relationship_revisions (
                relationship_uid TEXT NOT NULL,
                revision INTEGER NOT NULL,
                before_state TEXT NOT NULL DEFAULT '{}',
                after_state TEXT NOT NULL DEFAULT '{}',
                source_timeline_uids TEXT NOT NULL DEFAULT '[]',
                operation TEXT NOT NULL DEFAULT 'automatic',
                reason TEXT,
                change_summary TEXT NOT NULL DEFAULT '',
                diagnostics TEXT NOT NULL DEFAULT '{}',
                persona_signature TEXT NOT NULL DEFAULT '{}',
                provider_signature TEXT NOT NULL DEFAULT '{}',
                full_snapshot INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                PRIMARY KEY(relationship_uid, revision),
                FOREIGN KEY(relationship_uid) REFERENCES user_relationship_states(relationship_uid)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_user_relationship_revisions_recent
                ON user_relationship_revisions(relationship_uid, revision DESC);

            CREATE TABLE IF NOT EXISTS user_profile_timeline_identities (
                timeline_uid TEXT NOT NULL,
                timeline_revision INTEGER NOT NULL CHECK(timeline_revision >= 1),
                memory_space_id TEXT NOT NULL,
                document_id INTEGER,
                session_id TEXT NOT NULL DEFAULT '',
                bot_account TEXT NOT NULL DEFAULT '',
                persona_id TEXT NOT NULL DEFAULT '',
                private_target_id TEXT NOT NULL DEFAULT '',
                profile_scope_uid TEXT,
                actor_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK(status IN ('resolved', 'pending_review', 'ignored')),
                identity_basis TEXT NOT NULL DEFAULT '',
                evidence_basis TEXT NOT NULL DEFAULT 'timeline_summary_only',
                source_granularity TEXT NOT NULL DEFAULT 'timeline',
                resolver_version TEXT NOT NULL DEFAULT '',
                evidence_fingerprint TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                review_reason TEXT,
                reviewed_by TEXT,
                reviewed_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(timeline_uid, timeline_revision, memory_space_id),
                FOREIGN KEY(profile_scope_uid) REFERENCES user_profile_scopes(profile_scope_uid)
                    ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_profile_timeline_identity_review
                ON user_profile_timeline_identities(
                    status, bot_account, persona_id, updated_at DESC
                );
            CREATE INDEX IF NOT EXISTS idx_user_profile_timeline_identity_scope
                ON user_profile_timeline_identities(profile_scope_uid, status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS user_profile_projection_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_uid TEXT NOT NULL UNIQUE,
                timeline_uid TEXT NOT NULL,
                timeline_revision INTEGER NOT NULL CHECK(timeline_revision >= 1),
                operation TEXT NOT NULL,
                memory_space_id TEXT NOT NULL,
                profile_scope_uid TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                payload TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(timeline_uid, timeline_revision, operation, memory_space_id),
                FOREIGN KEY(profile_scope_uid) REFERENCES user_profile_scopes(profile_scope_uid)
                    ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_profile_events_pending
                ON user_profile_projection_events(status, sequence);

            CREATE TABLE IF NOT EXISTS user_profile_tasks (
                task_uid TEXT PRIMARY KEY,
                profile_scope_uid TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                settings_snapshot TEXT NOT NULL DEFAULT '{}',
                provider_signature TEXT NOT NULL DEFAULT '{}',
                retries INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL,
                error TEXT,
                result_summary TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                FOREIGN KEY(profile_scope_uid) REFERENCES user_profile_scopes(profile_scope_uid)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_user_profile_tasks_queue
                ON user_profile_tasks(status, next_retry_at, created_at);
            CREATE INDEX IF NOT EXISTS idx_user_profile_tasks_scope
                ON user_profile_tasks(profile_scope_uid, created_at DESC);

            CREATE TABLE IF NOT EXISTS user_profile_task_items (
                task_uid TEXT NOT NULL,
                event_uid TEXT NOT NULL,
                item_order INTEGER NOT NULL,
                timeline_uid TEXT NOT NULL,
                timeline_revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(task_uid, event_uid),
                UNIQUE(task_uid, item_order),
                FOREIGN KEY(task_uid) REFERENCES user_profile_tasks(task_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(event_uid) REFERENCES user_profile_projection_events(event_uid)
                    ON DELETE CASCADE
            );
            """
        )

    async def get_setting_overrides(self) -> dict[str, Any]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT setting_key, setting_value FROM user_profile_setting_overrides"
                )
            ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[str(row["setting_key"])] = json.loads(row["setting_value"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    async def update_setting_overrides(
        self,
        changes: dict[str, Any],
        *,
        reset_keys: list[str] | None = None,
        reset_all: bool = False,
        settings_revision: int = 1,
    ) -> dict[str, Any]:
        now = time.time()
        async with self._connect() as db:
            try:
                if reset_all:
                    await db.execute("DELETE FROM user_profile_setting_overrides")
                elif reset_keys:
                    placeholders = ",".join("?" for _ in reset_keys)
                    await db.execute(
                        f"DELETE FROM user_profile_setting_overrides "
                        f"WHERE setting_key IN ({placeholders})",
                        list(reset_keys),
                    )
                for key, value in changes.items():
                    await db.execute(
                        """
                        INSERT INTO user_profile_setting_overrides (
                            setting_key, setting_value, settings_revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(setting_key) DO UPDATE SET
                            setting_value = excluded.setting_value,
                            settings_revision = excluded.settings_revision,
                            updated_at = excluded.updated_at
                        """,
                        (
                            str(key),
                            self._json(value),
                            max(1, int(settings_revision)),
                            now,
                            now,
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_setting_overrides()

    async def ensure_private_scope(
        self,
        *,
        actor_id: str,
        bot_account: str,
        persona_id: str,
        display_name: str | None = None,
        auto_enable: bool = True,
    ) -> UserProfileScope | None:
        platform, stable_user_id = self._parse_human_actor_id(actor_id)
        if not platform or not stable_user_id:
            return None
        now = time.time()
        logical_user_uid = f"logical-user-{uuid.uuid5(uuid.NAMESPACE_URL, actor_id)}"
        scope = UserProfileScope(
            logical_user_uid=logical_user_uid,
            bot_account=str(bot_account).strip(),
            persona_id=str(persona_id).strip(),
            enabled=bool(auto_enable),
            created_at=now,
            updated_at=now,
        )
        async with self._connect() as db:
            try:
                existing = await (
                    await db.execute(
                        "SELECT logical_user_uid FROM user_profile_accounts WHERE actor_id = ?",
                        (actor_id,),
                    )
                ).fetchone()
                if existing:
                    logical_user_uid = str(existing["logical_user_uid"])
                    scope = UserProfileScope(
                        logical_user_uid=logical_user_uid,
                        bot_account=str(bot_account).strip(),
                        persona_id=str(persona_id).strip(),
                        enabled=bool(auto_enable),
                        created_at=now,
                        updated_at=now,
                    )
                await db.execute(
                    """
                    INSERT INTO user_profile_users (
                        logical_user_uid, status, created_at, updated_at, metadata
                    ) VALUES (?, 'active', ?, ?, '{}')
                    ON CONFLICT(logical_user_uid) DO UPDATE SET updated_at = excluded.updated_at
                    """,
                    (logical_user_uid, now, now),
                )
                names_row = await (
                    await db.execute(
                        "SELECT observed_names FROM user_profile_accounts WHERE actor_id = ?",
                        (actor_id,),
                    )
                ).fetchone()
                observed = self._json_list(
                    names_row["observed_names"] if names_row else None
                )
                clean_name = str(display_name or "").strip()
                if clean_name and clean_name not in observed:
                    observed.append(clean_name)
                    observed = observed[-20:]
                await db.execute(
                    """
                    INSERT INTO user_profile_accounts (
                        actor_id, logical_user_uid, platform, stable_user_id,
                        observed_names, last_observed_name, linked_manually,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(actor_id) DO UPDATE SET
                        observed_names = excluded.observed_names,
                        last_observed_name = COALESCE(
                            excluded.last_observed_name,
                            user_profile_accounts.last_observed_name
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (
                        actor_id,
                        logical_user_uid,
                        platform,
                        stable_user_id,
                        self._json(observed),
                        clean_name or None,
                        now,
                        now,
                    ),
                )
                await db.execute(
                    """
                    INSERT OR IGNORE INTO user_profile_fact_namespaces (
                        fact_namespace_uid, current_revision, created_at, updated_at
                    ) VALUES (?, 0, ?, ?)
                    """,
                    (scope.fact_namespace_uid, now, now),
                )
                await db.execute(
                    """
                    INSERT INTO user_profile_scopes (
                        profile_scope_uid, logical_user_uid, bot_account, persona_id,
                        fact_namespace_uid, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(logical_user_uid, bot_account, persona_id) DO UPDATE SET
                        updated_at = excluded.updated_at
                    """,
                    (
                        scope.profile_scope_uid,
                        logical_user_uid,
                        scope.bot_account,
                        scope.persona_id,
                        scope.fact_namespace_uid,
                        int(scope.enabled),
                        now,
                        now,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_scope_by_actor(
            actor_id=actor_id,
            bot_account=scope.bot_account,
            persona_id=scope.persona_id,
            include_disabled=True,
        )

    async def get_scope(self, profile_scope_uid: str) -> UserProfileScope | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM user_profile_scopes WHERE profile_scope_uid = ?",
                    (profile_scope_uid,),
                )
            ).fetchone()
        return self._row_to_scope(row) if row else None

    async def get_scope_by_actor(
        self,
        *,
        actor_id: str,
        bot_account: str,
        persona_id: str,
        include_disabled: bool = False,
    ) -> UserProfileScope | None:
        enabled_clause = "" if include_disabled else "AND s.enabled = 1"
        async with self._connect() as db:
            row = await (
                await db.execute(
                    f"""
                    SELECT s.*
                    FROM user_profile_accounts a
                    JOIN user_profile_scopes s
                      ON s.logical_user_uid = a.logical_user_uid
                    WHERE a.actor_id = ? AND s.bot_account = ? AND s.persona_id = ?
                      {enabled_clause}
                    LIMIT 1
                    """,
                    (actor_id, str(bot_account).strip(), str(persona_id).strip()),
                )
            ).fetchone()
        return self._row_to_scope(row) if row else None

    async def list_profile_identity_candidates(
        self,
        *,
        bot_account: str,
        persona_id: str,
        stable_user_id: str,
    ) -> list[dict[str, Any]]:
        """Return same Bot/persona accounts matching an exact social account ID."""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT a.actor_id, a.platform, a.stable_user_id,
                           a.last_observed_name, a.logical_user_uid,
                           s.profile_scope_uid, s.enabled
                    FROM user_profile_accounts a
                    JOIN user_profile_scopes s
                      ON s.logical_user_uid = a.logical_user_uid
                    WHERE s.bot_account = ? AND s.persona_id = ?
                      AND a.stable_user_id = ?
                    ORDER BY a.platform, a.actor_id, s.profile_scope_uid
                    """,
                    (
                        str(bot_account).strip(),
                        str(persona_id).strip(),
                        str(stable_user_id).strip(),
                    ),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def find_projection_scopes(
        self,
        *,
        timeline_uid: str,
        memory_space_id: str | None = None,
    ) -> list[UserProfileScope]:
        """Resolve scopes previously associated with a Timeline for delete events."""
        where_space = "AND e.memory_space_id = ?" if memory_space_id else ""
        params: list[Any] = [timeline_uid]
        if memory_space_id:
            params.append(memory_space_id)
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT DISTINCT s.*
                    FROM user_profile_projection_events e
                    JOIN user_profile_scopes s
                      ON s.profile_scope_uid = e.profile_scope_uid
                    WHERE e.timeline_uid = ? {where_space}
                    ORDER BY s.created_at
                    """,
                    params,
                )
            ).fetchall()
        return [self._row_to_scope(row) for row in rows]

    async def set_scope_state(
        self,
        profile_scope_uid: str,
        *,
        enabled: bool | None = None,
        auto_enable_blocked: bool | None = None,
        has_gap: bool | None = None,
        projection_cursor: int | None = None,
        reset_after: float | None | object = ...,
        relationship_frozen: bool | None = None,
        relationship_reset_after: float | None | object = ...,
        sensitivity_override: str | None | object = ...,
        behavior_override: str | None | object = ...,
    ) -> UserProfileScope | None:
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("enabled", enabled),
            ("auto_enable_blocked", auto_enable_blocked),
            ("has_gap", has_gap),
            ("relationship_frozen", relationship_frozen),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(int(value))
        if projection_cursor is not None:
            assignments.append("projection_cursor = ?")
            values.append(max(0, int(projection_cursor)))
        if reset_after is not ...:
            assignments.append("reset_after = ?")
            values.append(reset_after)
        if relationship_reset_after is not ...:
            assignments.append("relationship_reset_after = ?")
            values.append(relationship_reset_after)
        for column, value in (
            ("relationship_sensitivity_override", sensitivity_override),
            ("relationship_behavior_override", behavior_override),
        ):
            if value is not ...:
                assignments.append(f"{column} = ?")
                values.append(value)
        if not assignments:
            return await self.get_scope(profile_scope_uid)
        assignments.append("updated_at = ?")
        values.extend((time.time(), profile_scope_uid))
        async with self._connect() as db:
            await db.execute(
                f"UPDATE user_profile_scopes SET {', '.join(assignments)} "
                "WHERE profile_scope_uid = ?",
                values,
            )
            await db.commit()
        return await self.get_scope(profile_scope_uid)

    async def save_fact_sources(
        self, sources: Iterable[UserProfileFactSource]
    ) -> list[str]:
        source_list = list(sources)
        if not source_list:
            return []
        async with self._connect() as db:
            try:
                for source in source_list:
                    await db.execute(
                        """
                        INSERT INTO user_profile_fact_sources (
                            source_uid, profile_fact_uid, timeline_uid, timeline_revision,
                            fact_index, fact_fingerprint, raw_fact, actor_id, claim_type,
                            attribution_confidence, timeline_quality,
                            evidence_started_at, evidence_ended_at,
                            source_account_actor_id, active, created_at, updated_at, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        ON CONFLICT(timeline_uid, timeline_revision, fact_index, actor_id)
                        DO UPDATE SET
                            raw_fact = excluded.raw_fact,
                            fact_fingerprint = excluded.fact_fingerprint,
                            claim_type = excluded.claim_type,
                            attribution_confidence = excluded.attribution_confidence,
                            timeline_quality = excluded.timeline_quality,
                            evidence_started_at = excluded.evidence_started_at,
                            evidence_ended_at = excluded.evidence_ended_at,
                            active = 1,
                            updated_at = excluded.updated_at,
                            metadata = excluded.metadata
                        """,
                        (
                            source.source_uid,
                            source.profile_fact_uid,
                            source.timeline_uid,
                            max(1, int(source.timeline_revision)),
                            max(0, int(source.fact_index)),
                            source.fact_fingerprint,
                            source.raw_fact,
                            source.actor_id,
                            source.claim_type,
                            self._score(source.attribution_confidence),
                            self._json(source.timeline_quality),
                            source.evidence_started_at,
                            source.evidence_ended_at,
                            source.source_account_actor_id,
                            source.created_at,
                            source.updated_at,
                            self._json(source.metadata),
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return [source.source_uid for source in source_list]

    async def publish_fact_changes(
        self,
        *,
        fact_namespace_uid: str,
        upserts: Iterable[UserProfileFact],
        source_assignments: dict[str, str] | None = None,
        expected_revision: int | None = None,
    ) -> int:
        facts = list(upserts)
        now = time.time()
        async with self._connect() as db:
            try:
                row = await (
                    await db.execute(
                        "SELECT current_revision FROM user_profile_fact_namespaces "
                        "WHERE fact_namespace_uid = ?",
                        (fact_namespace_uid,),
                    )
                ).fetchone()
                if row is None:
                    raise ValueError("Unknown user-profile fact namespace")
                current_revision = int(row["current_revision"])
                if (
                    expected_revision is not None
                    and current_revision != expected_revision
                ):
                    raise UserProfileRevisionConflict(
                        f"Expected profile revision {expected_revision}, got {current_revision}"
                    )
                for fact in facts:
                    await db.execute(
                        """
                        INSERT INTO user_profile_facts (
                            profile_fact_uid, fact_namespace_uid, category, status,
                            representative_source_uid, confidence, importance,
                            inference_kind, sensitive, admin_confirmed, pinned,
                            first_seen_at, last_confirmed_at, fixed_injection_until,
                            review_after, superseded_by, created_at, updated_at, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(profile_fact_uid) DO UPDATE SET
                            category = excluded.category,
                            status = excluded.status,
                            representative_source_uid = excluded.representative_source_uid,
                            confidence = excluded.confidence,
                            importance = excluded.importance,
                            inference_kind = excluded.inference_kind,
                            sensitive = excluded.sensitive,
                            admin_confirmed = excluded.admin_confirmed,
                            pinned = excluded.pinned,
                            last_confirmed_at = excluded.last_confirmed_at,
                            fixed_injection_until = excluded.fixed_injection_until,
                            review_after = excluded.review_after,
                            superseded_by = excluded.superseded_by,
                            updated_at = excluded.updated_at,
                            metadata = excluded.metadata
                        """,
                        (
                            fact.profile_fact_uid,
                            fact_namespace_uid,
                            self._enum(fact.category),
                            self._enum(fact.status),
                            fact.representative_source_uid,
                            self._score(fact.confidence),
                            self._score(fact.importance),
                            self._enum(fact.inference_kind),
                            int(fact.sensitive),
                            int(fact.admin_confirmed),
                            int(fact.pinned),
                            fact.first_seen_at,
                            fact.last_confirmed_at,
                            fact.fixed_injection_until,
                            fact.review_after,
                            fact.superseded_by,
                            fact.created_at,
                            now,
                            self._json(fact.metadata),
                        ),
                    )
                for source_uid, profile_fact_uid in (source_assignments or {}).items():
                    await db.execute(
                        "UPDATE user_profile_fact_sources SET profile_fact_uid = ?, "
                        "updated_at = ? WHERE source_uid = ?",
                        (profile_fact_uid, now, source_uid),
                    )
                new_revision = current_revision + 1
                await db.execute(
                    "UPDATE user_profile_fact_namespaces "
                    "SET current_revision = ?, updated_at = ? "
                    "WHERE fact_namespace_uid = ?",
                    (new_revision, now, fact_namespace_uid),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return new_revision

    async def list_serving_facts(
        self,
        fact_namespace_uid: str,
        *,
        include_sensitive: bool = True,
        include_pending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        statuses = ["active"]
        if include_pending:
            statuses.append("pending")
        placeholders = ",".join("?" for _ in statuses)
        sensitive_clause = "" if include_sensitive else "AND f.sensitive = 0"
        limit_sql = ""
        params: list[Any] = [fact_namespace_uid, *statuses]
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(1, min(10000, int(limit))))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT f.*, s.raw_fact, s.actor_id, s.timeline_uid,
                           s.timeline_revision, s.fact_index, s.claim_type,
                           s.evidence_started_at, s.evidence_ended_at
                    FROM user_profile_facts f
                    JOIN user_profile_fact_sources s
                      ON s.source_uid = f.representative_source_uid
                    WHERE f.fact_namespace_uid = ?
                      AND f.status IN ({placeholders})
                      AND s.active = 1
                      {sensitive_clause}
                    ORDER BY f.pinned DESC, f.importance DESC,
                             COALESCE(f.last_confirmed_at, f.updated_at) DESC
                    {limit_sql}
                    """,
                    params,
                )
            ).fetchall()
        return [self._fact_row(row) for row in rows]

    async def list_facts_for_maintenance(
        self,
        fact_namespace_uid: str,
        *,
        statuses: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded current facts with their immutable display sources."""
        selected = [str(item) for item in (statuses or ()) if str(item)]
        clauses = ["f.fact_namespace_uid = ?"]
        params: list[Any] = [fact_namespace_uid]
        if selected:
            clauses.append(f"f.status IN ({','.join('?' for _ in selected)})")
            params.extend(selected)
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(1, min(10000, int(limit))))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT f.*, s.raw_fact, s.actor_id, s.timeline_uid,
                           s.timeline_revision, s.fact_index, s.claim_type,
                           s.evidence_started_at, s.evidence_ended_at,
                           s.active AS representative_source_active
                    FROM user_profile_facts f
                    LEFT JOIN user_profile_fact_sources s
                      ON s.source_uid = f.representative_source_uid
                    WHERE {' AND '.join(clauses)}
                    ORDER BY CASE f.status
                                 WHEN 'conflict' THEN 0
                                 WHEN 'active' THEN 1
                                 WHEN 'pending' THEN 2
                                 WHEN 'stale' THEN 3
                                 ELSE 4
                             END,
                             f.pinned DESC, f.importance DESC,
                             f.updated_at DESC, f.profile_fact_uid
                    {limit_sql}
                    """,
                    params,
                )
            ).fetchall()
        return [self._fact_row(row) for row in rows]

    async def list_unassigned_behavior_evidence(
        self,
        profile_scope_uid: str,
        *,
        limit: int = 128,
        retention_days: int = 180,
    ) -> list[UserProfileFactSource]:
        cutoff = time.time() - max(1, int(retention_days)) * 86400.0
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT DISTINCT s.*
                    FROM user_profile_fact_sources s
                    JOIN user_profile_projection_events e
                      ON e.timeline_uid = s.timeline_uid
                     AND e.timeline_revision = s.timeline_revision
                    WHERE e.profile_scope_uid = ?
                      AND s.active = 1
                      AND s.profile_fact_uid IS NULL
                      AND json_extract(s.metadata, '$.profile_signal') IN (
                          'behavior_evidence', 'behavior_pattern'
                      )
                      AND COALESCE(s.evidence_ended_at, s.updated_at) >= ?
                    ORDER BY COALESCE(s.evidence_ended_at, s.updated_at) DESC,
                             s.source_uid
                    LIMIT ?
                    """,
                    (
                        profile_scope_uid,
                        cutoff,
                        max(1, min(1000, int(limit))),
                    ),
                )
            ).fetchall()
        return [self._row_to_fact_source(row) for row in rows]

    async def get_fact_namespace_revision(self, fact_namespace_uid: str) -> int:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT current_revision FROM user_profile_fact_namespaces "
                    "WHERE fact_namespace_uid = ?",
                    (fact_namespace_uid,),
                )
            ).fetchone()
        if row is None:
            raise ValueError("Unknown user-profile fact namespace")
        return int(row["current_revision"])

    async def apply_fact_projection_batch(
        self,
        *,
        fact_namespace_uid: str,
        projections: Iterable[dict[str, Any]],
        facts: Iterable[UserProfileFact] = (),
        source_assignments: dict[str, str] | None = None,
        conflicts: Iterable[dict[str, Any]] = (),
        expected_revision: int | None = None,
        checkpoint_task_uid: str | None = None,
    ) -> int:
        """Atomically replace Timeline contributions and publish a fact revision."""
        projection_list = list(projections)
        fact_list = list(facts)
        conflict_list = list(conflicts)
        assignments = dict(source_assignments or {})
        now = time.time()
        async with self._connect() as db:
            try:
                row = await (
                    await db.execute(
                        "SELECT current_revision FROM user_profile_fact_namespaces "
                        "WHERE fact_namespace_uid = ?",
                        (fact_namespace_uid,),
                    )
                ).fetchone()
                if row is None:
                    raise ValueError("Unknown user-profile fact namespace")
                current_revision = int(row["current_revision"])
                if (
                    expected_revision is not None
                    and current_revision != expected_revision
                ):
                    raise UserProfileRevisionConflict(
                        f"Expected profile revision {expected_revision}, got {current_revision}"
                    )

                affected_fact_uids: set[str] = set()
                for projection in projection_list:
                    timeline_uid = str(projection.get("timeline_uid") or "")
                    if not timeline_uid:
                        continue
                    affected_rows = await (
                        await db.execute(
                            """
                            SELECT DISTINCT s.profile_fact_uid
                            FROM user_profile_fact_sources s
                            JOIN user_profile_facts f
                              ON f.profile_fact_uid = s.profile_fact_uid
                            WHERE s.timeline_uid = ? AND s.active = 1
                              AND f.fact_namespace_uid = ?
                            """,
                            (timeline_uid, fact_namespace_uid),
                        )
                    ).fetchall()
                    affected_fact_uids.update(
                        str(item["profile_fact_uid"])
                        for item in affected_rows
                        if item["profile_fact_uid"]
                    )
                    await db.execute(
                        """
                        UPDATE user_profile_fact_sources
                        SET active = 0, updated_at = ?
                        WHERE timeline_uid = ? AND active = 1
                          AND (
                            profile_fact_uid IS NULL OR profile_fact_uid IN (
                                SELECT profile_fact_uid FROM user_profile_facts
                                WHERE fact_namespace_uid = ?
                            )
                          )
                        """,
                        (now, timeline_uid, fact_namespace_uid),
                    )
                    for source in projection.get("sources") or []:
                        if not isinstance(source, UserProfileFactSource):
                            continue
                        await db.execute(
                            """
                            INSERT INTO user_profile_fact_sources (
                                source_uid, profile_fact_uid, timeline_uid,
                                timeline_revision, fact_index, fact_fingerprint,
                                raw_fact, actor_id, claim_type,
                                attribution_confidence, timeline_quality,
                                evidence_started_at, evidence_ended_at,
                                source_account_actor_id, active,
                                created_at, updated_at, metadata
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                            ON CONFLICT(timeline_uid, timeline_revision, fact_index, actor_id)
                            DO UPDATE SET
                                profile_fact_uid = CASE
                                    WHEN EXISTS (
                                        SELECT 1 FROM user_profile_fact_overrides o
                                        WHERE o.profile_fact_uid =
                                              user_profile_fact_sources.profile_fact_uid
                                          AND o.active = 1
                                    )
                                    THEN user_profile_fact_sources.profile_fact_uid
                                    ELSE excluded.profile_fact_uid
                                END,
                                raw_fact = excluded.raw_fact,
                                fact_fingerprint = excluded.fact_fingerprint,
                                claim_type = excluded.claim_type,
                                attribution_confidence = excluded.attribution_confidence,
                                timeline_quality = excluded.timeline_quality,
                                evidence_started_at = excluded.evidence_started_at,
                                evidence_ended_at = excluded.evidence_ended_at,
                                source_account_actor_id = excluded.source_account_actor_id,
                                active = 1, updated_at = excluded.updated_at,
                                metadata = excluded.metadata
                            """,
                            (
                                source.source_uid,
                                source.profile_fact_uid,
                                source.timeline_uid,
                                max(1, int(source.timeline_revision)),
                                max(0, int(source.fact_index)),
                                source.fact_fingerprint,
                                source.raw_fact,
                                source.actor_id,
                                source.claim_type,
                                self._score(source.attribution_confidence),
                                self._json(source.timeline_quality),
                                source.evidence_started_at,
                                source.evidence_ended_at,
                                source.source_account_actor_id,
                                source.created_at,
                                now,
                                self._json(source.metadata),
                            ),
                        )

                for fact in fact_list:
                    await db.execute(
                        """
                        INSERT INTO user_profile_facts (
                            profile_fact_uid, fact_namespace_uid, category, status,
                            representative_source_uid, confidence, importance,
                            inference_kind, sensitive, admin_confirmed, pinned,
                            first_seen_at, last_confirmed_at, fixed_injection_until,
                            review_after, superseded_by, created_at, updated_at, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(profile_fact_uid) DO UPDATE SET
                            category = excluded.category,
                            status = excluded.status,
                            representative_source_uid = excluded.representative_source_uid,
                            confidence = excluded.confidence,
                            importance = excluded.importance,
                            inference_kind = excluded.inference_kind,
                            sensitive = excluded.sensitive,
                            admin_confirmed = excluded.admin_confirmed,
                            pinned = excluded.pinned,
                            first_seen_at = COALESCE(
                                user_profile_facts.first_seen_at, excluded.first_seen_at
                            ),
                            last_confirmed_at = excluded.last_confirmed_at,
                            fixed_injection_until = excluded.fixed_injection_until,
                            review_after = excluded.review_after,
                            superseded_by = excluded.superseded_by,
                            updated_at = excluded.updated_at,
                            metadata = excluded.metadata
                        """,
                        (
                            fact.profile_fact_uid,
                            fact_namespace_uid,
                            self._enum(fact.category),
                            self._enum(fact.status),
                            fact.representative_source_uid,
                            self._score(fact.confidence),
                            self._score(fact.importance),
                            self._enum(fact.inference_kind),
                            int(fact.sensitive),
                            int(fact.admin_confirmed),
                            int(fact.pinned),
                            fact.first_seen_at,
                            fact.last_confirmed_at,
                            fact.fixed_injection_until,
                            fact.review_after,
                            fact.superseded_by,
                            fact.created_at,
                            now,
                            self._json(fact.metadata),
                        ),
                    )
                    affected_fact_uids.add(fact.profile_fact_uid)

                for source_uid, profile_fact_uid in assignments.items():
                    cursor = await db.execute(
                        """
                        UPDATE user_profile_fact_sources
                        SET profile_fact_uid = ?, updated_at = ?
                        WHERE source_uid = ? AND active = 1
                        """,
                        (profile_fact_uid, now, source_uid),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(f"Unknown active profile source: {source_uid}")
                    affected_fact_uids.add(profile_fact_uid)

                for conflict in conflict_list:
                    conflict_uid = str(conflict.get("conflict_uid") or uuid.uuid4())
                    await db.execute(
                        """
                        INSERT INTO user_profile_conflicts (
                            conflict_uid, fact_namespace_uid, conflict_key, fact_uids,
                            status, first_detected_at, last_evidence_at,
                            resolution_kind, resolution_reason, created_at, updated_at,
                            metadata
                        ) VALUES (?, ?, ?, ?, 'open', ?, ?, NULL, ?, ?, ?, '{}')
                        """,
                        (
                            conflict_uid,
                            fact_namespace_uid,
                            str(
                                conflict.get("conflict_key")
                                or conflict.get("topic_key")
                                or ""
                            )[:500],
                            self._json(conflict.get("fact_uids") or []),
                            now,
                            now,
                            str(conflict.get("reason") or "")[:2000] or None,
                            now,
                            now,
                        ),
                    )

                for fact_uid in affected_fact_uids:
                    fact_row = await (
                        await db.execute(
                            "SELECT status, representative_source_uid "
                            "FROM user_profile_facts WHERE profile_fact_uid = ? "
                            "AND fact_namespace_uid = ?",
                            (fact_uid, fact_namespace_uid),
                        )
                    ).fetchone()
                    if fact_row is None:
                        continue
                    best = await (
                        await db.execute(
                            """
                            SELECT source_uid FROM user_profile_fact_sources
                            WHERE profile_fact_uid = ? AND active = 1
                            ORDER BY attribution_confidence DESC,
                                     COALESCE(evidence_ended_at, updated_at) DESC,
                                     timeline_revision DESC LIMIT 1
                            """,
                            (fact_uid,),
                        )
                    ).fetchone()
                    status = str(fact_row["status"])
                    if best is None and status in {"active", "pending", "stale"}:
                        await db.execute(
                            "UPDATE user_profile_facts SET status = 'archived', "
                            "updated_at = ? WHERE profile_fact_uid = ?",
                            (now, fact_uid),
                        )
                    elif best is not None:
                        representative_active = await (
                            await db.execute(
                                "SELECT active FROM user_profile_fact_sources "
                                "WHERE source_uid = ?",
                                (str(fact_row["representative_source_uid"]),),
                            )
                        ).fetchone()
                        if representative_active is None or not bool(
                            representative_active["active"]
                        ):
                            await db.execute(
                                "UPDATE user_profile_facts "
                                "SET representative_source_uid = ?, updated_at = ? "
                                "WHERE profile_fact_uid = ?",
                                (str(best["source_uid"]), now, fact_uid),
                            )

                open_conflicts = await (
                    await db.execute(
                        "SELECT conflict_uid, fact_uids FROM user_profile_conflicts "
                        "WHERE fact_namespace_uid = ? AND status = 'open'",
                        (fact_namespace_uid,),
                    )
                ).fetchall()
                for conflict_row in open_conflicts:
                    conflict_fact_uids = [
                        str(value)
                        for value in self._json_list(conflict_row["fact_uids"])
                    ]
                    if not set(conflict_fact_uids) & affected_fact_uids:
                        continue
                    status_rows = (
                        await (
                            await db.execute(
                                f"SELECT profile_fact_uid, status FROM user_profile_facts "
                                f"WHERE profile_fact_uid IN "
                                f"({','.join('?' for _ in conflict_fact_uids)})",
                                conflict_fact_uids,
                            )
                        ).fetchall()
                        if conflict_fact_uids
                        else []
                    )
                    still_conflicted = [
                        str(item["profile_fact_uid"])
                        for item in status_rows
                        if str(item["status"]) == "conflict"
                    ]
                    if len(still_conflicted) <= 1:
                        resolved_uid = still_conflicted[0] if still_conflicted else None
                        await db.execute(
                            """
                            UPDATE user_profile_conflicts
                            SET status = 'auto_resolved',
                                resolution_kind = 'new_evidence',
                                resolved_fact_uid = ?, resolved_at = ?, updated_at = ?
                            WHERE conflict_uid = ?
                            """,
                            (resolved_uid, now, now, str(conflict_row["conflict_uid"])),
                        )

                new_revision = current_revision + 1
                await db.execute(
                    "UPDATE user_profile_fact_namespaces "
                    "SET current_revision = ?, updated_at = ? "
                    "WHERE fact_namespace_uid = ?",
                    (new_revision, now, fact_namespace_uid),
                )
                if checkpoint_task_uid:
                    task_row = await (
                        await db.execute(
                            "SELECT result_summary FROM user_profile_tasks "
                            "WHERE task_uid = ?",
                            (checkpoint_task_uid,),
                        )
                    ).fetchone()
                    if task_row is None:
                        raise ValueError("Unknown user-profile checkpoint task")
                    summary = self._json_object(task_row["result_summary"])
                    summary.update(
                        {
                            "facts_checkpoint": True,
                            "fact_revision": new_revision,
                        }
                    )
                    await db.execute(
                        """
                        UPDATE user_profile_tasks
                        SET status = 'facts_completed', result_summary = ?,
                            error = NULL, updated_at = ?
                        WHERE task_uid = ?
                        """,
                        (self._json(summary), now, checkpoint_task_uid),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return new_revision

    async def get_relationship(
        self, profile_scope_uid: str
    ) -> UserRelationshipState | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM user_relationship_states WHERE profile_scope_uid = ?",
                    (profile_scope_uid,),
                )
            ).fetchone()
        return self._row_to_relationship(row) if row else None

    async def publish_relationship(
        self,
        state: UserRelationshipState,
        *,
        expected_revision: int | None = None,
        operation: str = "automatic",
        reason: str | None = None,
        change_summary: str = "",
        diagnostics: dict[str, Any] | None = None,
        provider_signature: dict[str, Any] | None = None,
        full_revision_limit: int = 100,
        checkpoint_task_uid: str | None = None,
    ) -> UserRelationshipState:
        now = time.time()
        async with self._connect() as db:
            try:
                existing = await (
                    await db.execute(
                        "SELECT * FROM user_relationship_states WHERE profile_scope_uid = ?",
                        (state.profile_scope_uid,),
                    )
                ).fetchone()
                current_revision = int(existing["revision"]) if existing else 0
                if (
                    expected_revision is not None
                    and current_revision != expected_revision
                ):
                    raise UserProfileRevisionConflict(
                        f"Expected relationship revision {expected_revision}, got {current_revision}"
                    )
                relationship_uid = (
                    str(existing["relationship_uid"])
                    if existing
                    else state.relationship_uid
                )
                new_revision = current_revision + 1
                before_state = self._relationship_row_dict(existing) if existing else {}
                dimensions = state.dimensions()
                await db.execute(
                    """
                    INSERT INTO user_relationship_states (
                        relationship_uid, profile_scope_uid, revision,
                        familiarity, trust, warmth, ease, tension, concern,
                        stance_tags, subjective_summary, recent_aftereffect,
                        aftereffect_expires_at, persona_signature,
                        source_timeline_uids, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_scope_uid) DO UPDATE SET
                        revision = excluded.revision,
                        familiarity = excluded.familiarity,
                        trust = excluded.trust,
                        warmth = excluded.warmth,
                        ease = excluded.ease,
                        tension = excluded.tension,
                        concern = excluded.concern,
                        stance_tags = excluded.stance_tags,
                        subjective_summary = excluded.subjective_summary,
                        recent_aftereffect = excluded.recent_aftereffect,
                        aftereffect_expires_at = excluded.aftereffect_expires_at,
                        persona_signature = excluded.persona_signature,
                        source_timeline_uids = excluded.source_timeline_uids,
                        updated_at = excluded.updated_at
                    """,
                    (
                        relationship_uid,
                        state.profile_scope_uid,
                        new_revision,
                        dimensions["familiarity"],
                        dimensions["trust"],
                        dimensions["warmth"],
                        dimensions["ease"],
                        dimensions["tension"],
                        dimensions["concern"],
                        self._json(state.stance_tags),
                        state.subjective_summary,
                        state.recent_aftereffect,
                        state.aftereffect_expires_at,
                        self._json(state.persona_signature),
                        self._json(state.source_timeline_uids),
                        state.created_at,
                        now,
                    ),
                )
                state.relationship_uid = relationship_uid
                state.revision = new_revision
                state.updated_at = now
                after_state = self._relationship_state_dict(state)
                await db.execute(
                    """
                    INSERT INTO user_relationship_revisions (
                        relationship_uid, revision, before_state, after_state,
                        source_timeline_uids, operation, reason, change_summary,
                        diagnostics, persona_signature, provider_signature,
                        full_snapshot, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        relationship_uid,
                        new_revision,
                        self._json(before_state),
                        self._json(after_state),
                        self._json(state.source_timeline_uids),
                        operation,
                        reason,
                        change_summary,
                        self._json(diagnostics or {}),
                        self._json(state.persona_signature),
                        self._json(provider_signature or {}),
                        now,
                    ),
                )
                cutoff = max(1, new_revision - max(10, int(full_revision_limit)) + 1)
                await db.execute(
                    """
                    UPDATE user_relationship_revisions
                    SET before_state = '{}', after_state = '{}', full_snapshot = 0
                    WHERE relationship_uid = ? AND revision < ? AND full_snapshot = 1
                    """,
                    (relationship_uid, cutoff),
                )
                if checkpoint_task_uid:
                    task_row = await (
                        await db.execute(
                            "SELECT result_summary FROM user_profile_tasks "
                            "WHERE task_uid = ?",
                            (checkpoint_task_uid,),
                        )
                    ).fetchone()
                    if task_row is None:
                        raise ValueError("Unknown relationship checkpoint task")
                    summary = self._json_object(task_row["result_summary"])
                    summary.update(
                        {
                            "relationship_checkpoint": True,
                            "relationship_revision": new_revision,
                        }
                    )
                    await db.execute(
                        """
                        UPDATE user_profile_tasks
                        SET status = 'running_relationship', result_summary = ?,
                            error = NULL, updated_at = ?
                        WHERE task_uid = ?
                        """,
                        (self._json(summary), now, checkpoint_task_uid),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return state

    async def list_relationship_revisions(
        self, relationship_uid: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM user_relationship_revisions
                    WHERE relationship_uid = ?
                    ORDER BY revision DESC LIMIT ?
                    """,
                    (relationship_uid, max(1, min(1000, int(limit)))),
                )
            ).fetchall()
        return [
            {
                **dict(row),
                "before_state": self._json_object(row["before_state"]),
                "after_state": self._json_object(row["after_state"]),
                "source_timeline_uids": self._json_list(row["source_timeline_uids"]),
                "diagnostics": self._json_object(row["diagnostics"]),
                "persona_signature": self._json_object(row["persona_signature"]),
                "provider_signature": self._json_object(row["provider_signature"]),
            }
            for row in rows
        ]

    async def get_relationship_revision(
        self, relationship_uid: str, revision: int
    ) -> dict[str, Any] | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    """
                    SELECT * FROM user_relationship_revisions
                    WHERE relationship_uid = ? AND revision = ?
                    """,
                    (relationship_uid, int(revision)),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "before_state": self._json_object(row["before_state"]),
            "after_state": self._json_object(row["after_state"]),
            "source_timeline_uids": self._json_list(row["source_timeline_uids"]),
            "diagnostics": self._json_object(row["diagnostics"]),
            "persona_signature": self._json_object(row["persona_signature"]),
            "provider_signature": self._json_object(row["provider_signature"]),
        }

    async def enqueue_projection_event(self, event: UserProfileProjectionEvent) -> str:
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO user_profile_projection_events (
                    event_uid, timeline_uid, timeline_revision, operation,
                    memory_space_id, profile_scope_uid, status, payload,
                    error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(timeline_uid, timeline_revision, operation, memory_space_id)
                DO UPDATE SET
                    profile_scope_uid = COALESCE(
                        excluded.profile_scope_uid,
                        user_profile_projection_events.profile_scope_uid
                    ),
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    event.event_uid,
                    event.timeline_uid,
                    max(1, int(event.timeline_revision)),
                    self._enum(event.operation),
                    event.memory_space_id,
                    event.profile_scope_uid,
                    event.status,
                    self._json(event.payload),
                    event.error,
                    event.created_at,
                    event.updated_at,
                ),
            )
            await db.commit()
            row = await (
                await db.execute(
                    """
                    SELECT event_uid FROM user_profile_projection_events
                    WHERE timeline_uid = ? AND timeline_revision = ?
                      AND operation = ? AND memory_space_id = ?
                    """,
                    (
                        event.timeline_uid,
                        max(1, int(event.timeline_revision)),
                        self._enum(event.operation),
                        event.memory_space_id,
                    ),
                )
            ).fetchone()
        return str(row["event_uid"])

    async def list_pending_projection_events(
        self, *, limit: int = 64
    ) -> list[dict[str, Any]]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM user_profile_projection_events
                    WHERE status = 'pending'
                    ORDER BY sequence ASC LIMIT ?
                    """,
                    (max(1, min(1000, int(limit))),),
                )
            ).fetchall()
        return [self._event_row(row) for row in rows]

    async def list_projection_events_for_scope(
        self,
        profile_scope_uid: str,
        *,
        statuses: Iterable[str] = ("pending",),
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        status_list = [str(item) for item in statuses]
        if not status_list:
            return []
        placeholders = ",".join("?" for _ in status_list)
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT * FROM user_profile_projection_events
                    WHERE profile_scope_uid = ?
                      AND status IN ({placeholders})
                    ORDER BY sequence ASC LIMIT ?
                    """,
                    [
                        profile_scope_uid,
                        *status_list,
                        max(1, min(1000, int(limit))),
                    ],
                )
            ).fetchall()
        return [self._event_row(row) for row in rows]

    async def count_pending_projection_events(
        self,
        profile_scope_uid: str,
        *,
        projection_mode: str | None = None,
    ) -> int:
        clauses = ["profile_scope_uid = ?", "status = 'pending'"]
        params: list[Any] = [profile_scope_uid]
        if projection_mode is not None:
            clauses.append("json_extract(payload, '$.projection_mode') = ?")
            params.append(str(projection_mode))
        async with self._connect() as db:
            row = await (
                await db.execute(
                    f"SELECT COUNT(*) AS total FROM user_profile_projection_events "
                    f"WHERE {' AND '.join(clauses)}",
                    params,
                )
            ).fetchone()
        return int(row["total"] if row else 0)

    async def list_projection_history(
        self,
        profile_scope_uid: str,
        *,
        limit: int | None = None,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        limit_sql = ""
        params: list[Any] = [profile_scope_uid, max(0, int(after_sequence))]
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(1, min(50000, int(limit))))
        async with self._connect() as db:
            cursor = await db.execute(
                f"""
                SELECT * FROM user_profile_projection_events
                WHERE profile_scope_uid = ? AND sequence > ?
                ORDER BY sequence ASC
                {limit_sql}
                """,
                params,
            )
            rows = []
            while True:
                batch = await cursor.fetchmany(1000)
                if not batch:
                    break
                rows.extend(batch)
        return [self._event_row(row) for row in rows]

    async def get_timeline_identity_resolution(
        self,
        timeline_uid: str,
        timeline_revision: int,
        memory_space_id: str,
    ) -> dict[str, Any] | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    """
                    SELECT * FROM user_profile_timeline_identities
                    WHERE timeline_uid = ? AND timeline_revision = ?
                      AND memory_space_id = ?
                    """,
                    (
                        str(timeline_uid),
                        max(1, int(timeline_revision)),
                        str(memory_space_id),
                    ),
                )
            ).fetchone()
        return self._timeline_identity_row(row) if row is not None else None

    async def record_timeline_identity_resolution(
        self,
        *,
        timeline_uid: str,
        timeline_revision: int,
        memory_space_id: str,
        document_id: int | None,
        session_id: str,
        bot_account: str,
        persona_id: str,
        private_target_id: str,
        profile_scope_uid: str | None,
        actor_id: str | None,
        status: str,
        identity_basis: str,
        evidence_basis: str,
        source_granularity: str,
        resolver_version: str,
        evidence_fingerprint: str,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist automatic identity state without overriding administrator decisions."""
        if status not in {"resolved", "pending_review"}:
            raise ValueError(
                "Automatic Timeline identity status must be resolved or pending_review"
            )
        key = (
            str(timeline_uid).strip(),
            max(1, int(timeline_revision)),
            str(memory_space_id).strip(),
        )
        if not key[0] or not key[2]:
            raise ValueError(
                "Timeline identity requires timeline_uid and memory_space_id"
            )
        now = time.time()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await (
                    await db.execute(
                        """
                        SELECT * FROM user_profile_timeline_identities
                        WHERE timeline_uid = ? AND timeline_revision = ?
                          AND memory_space_id = ?
                        """,
                        key,
                    )
                ).fetchone()
                if current is not None:
                    current_basis = str(current["identity_basis"] or "")
                    current_status = str(current["status"] or "")
                    if current_basis in {"admin_binding", "admin_ignore"}:
                        await db.rollback()
                        return self._timeline_identity_row(current)
                    if current_status == "resolved" and status == "pending_review":
                        await db.rollback()
                        return self._timeline_identity_row(current)
                    unchanged = (
                        current_status == status
                        and str(current["profile_scope_uid"] or "")
                        == str(profile_scope_uid or "")
                        and str(current["actor_id"] or "") == str(actor_id or "")
                        and current_basis == str(identity_basis)
                        and str(current["evidence_fingerprint"] or "")
                        == str(evidence_fingerprint)
                        and str(current["resolver_version"] or "")
                        == str(resolver_version)
                    )
                    if unchanged:
                        await db.rollback()
                        return self._timeline_identity_row(current)
                await db.execute(
                    """
                    INSERT INTO user_profile_timeline_identities (
                        timeline_uid, timeline_revision, memory_space_id,
                        document_id, session_id, bot_account, persona_id,
                        private_target_id, profile_scope_uid, actor_id, status,
                        identity_basis, evidence_basis, source_granularity,
                        resolver_version, evidence_fingerprint, evidence_json,
                        review_reason, reviewed_by, reviewed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              NULL, NULL, NULL, ?, ?)
                    ON CONFLICT(timeline_uid, timeline_revision, memory_space_id)
                    DO UPDATE SET
                        document_id = excluded.document_id,
                        session_id = excluded.session_id,
                        bot_account = excluded.bot_account,
                        persona_id = excluded.persona_id,
                        private_target_id = excluded.private_target_id,
                        profile_scope_uid = excluded.profile_scope_uid,
                        actor_id = excluded.actor_id,
                        status = excluded.status,
                        identity_basis = excluded.identity_basis,
                        evidence_basis = excluded.evidence_basis,
                        source_granularity = excluded.source_granularity,
                        resolver_version = excluded.resolver_version,
                        evidence_fingerprint = excluded.evidence_fingerprint,
                        evidence_json = excluded.evidence_json,
                        review_reason = NULL,
                        reviewed_by = NULL,
                        reviewed_at = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        *key,
                        int(document_id) if document_id is not None else None,
                        str(session_id or ""),
                        str(bot_account or ""),
                        str(persona_id or ""),
                        str(private_target_id or ""),
                        str(profile_scope_uid or "") or None,
                        str(actor_id or "") or None,
                        status,
                        str(identity_basis or ""),
                        str(evidence_basis or "timeline_summary_only"),
                        str(source_granularity or "timeline"),
                        str(resolver_version or ""),
                        str(evidence_fingerprint or ""),
                        self._json(evidence or {}),
                        now,
                        now,
                    ),
                )
                row = await (
                    await db.execute(
                        """
                        SELECT * FROM user_profile_timeline_identities
                        WHERE timeline_uid = ? AND timeline_revision = ?
                          AND memory_space_id = ?
                        """,
                        key,
                    )
                ).fetchone()
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        if row is None:
            raise RuntimeError("Timeline identity resolution was not persisted")
        return self._timeline_identity_row(row)

    async def list_timeline_identity_resolutions(
        self,
        *,
        statuses: Iterable[str] = ("pending_review",),
        bot_account: str | None = None,
        persona_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        selected = [
            str(value)
            for value in dict.fromkeys(statuses)
            if str(value) in {"resolved", "pending_review", "ignored"}
        ]
        if not selected:
            return []
        clauses = [f"status IN ({','.join('?' for _ in selected)})"]
        params: list[Any] = list(selected)
        if bot_account is not None:
            clauses.append("bot_account = ?")
            params.append(str(bot_account))
        if persona_id is not None:
            clauses.append("persona_id = ?")
            params.append(str(persona_id))
        params.extend((max(1, min(1000, int(limit))), max(0, int(offset))))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT * FROM user_profile_timeline_identities
                    WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC, timeline_uid ASC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                )
            ).fetchall()
        return [self._timeline_identity_row(row) for row in rows]

    async def resolve_timeline_identity_review(
        self,
        *,
        timeline_uid: str,
        timeline_revision: int,
        memory_space_id: str,
        action: str,
        expected_evidence_fingerprint: str,
        profile_scope_uid: str | None = None,
        actor_id: str | None = None,
        reason: str | None = None,
        reviewed_by: str = "administrator",
    ) -> dict[str, Any]:
        """Apply a reversible administrator decision to one unresolved Timeline."""
        if action not in {"bind", "ignore", "restore"}:
            raise ValueError("Unsupported Timeline identity review action")
        key = (
            str(timeline_uid).strip(),
            max(1, int(timeline_revision)),
            str(memory_space_id).strip(),
        )
        now = time.time()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await (
                    await db.execute(
                        """
                        SELECT * FROM user_profile_timeline_identities
                        WHERE timeline_uid = ? AND timeline_revision = ?
                          AND memory_space_id = ?
                        """,
                        key,
                    )
                ).fetchone()
                if current is None:
                    raise ValueError("Timeline identity review item no longer exists")
                supplied = str(expected_evidence_fingerprint or "")
                if not supplied or supplied != str(
                    current["evidence_fingerprint"] or ""
                ):
                    raise UserProfileRevisionConflict(
                        "Timeline identity evidence changed; refresh before reviewing"
                    )
                if action == "bind":
                    scope_uid = str(profile_scope_uid or "").strip()
                    selected_actor = str(actor_id or "").strip()
                    scope = await (
                        await db.execute(
                            "SELECT * FROM user_profile_scopes WHERE profile_scope_uid = ?",
                            (scope_uid,),
                        )
                    ).fetchone()
                    account = await (
                        await db.execute(
                            "SELECT * FROM user_profile_accounts WHERE actor_id = ?",
                            (selected_actor,),
                        )
                    ).fetchone()
                    if scope is None or account is None:
                        raise ValueError(
                            "Selected profile scope or account no longer exists"
                        )
                    if str(scope["logical_user_uid"]) != str(
                        account["logical_user_uid"]
                    ):
                        raise ValueError(
                            "Selected account is not bound to this profile"
                        )
                    if str(scope["bot_account"]) != str(current["bot_account"]) or str(
                        scope["persona_id"]
                    ) != str(current["persona_id"]):
                        raise ValueError(
                            "Timeline and profile Bot/persona scopes do not match"
                        )
                    status = "resolved"
                    basis = "admin_binding"
                    scope_value = scope_uid
                    actor_value = selected_actor
                elif action == "ignore":
                    status = "ignored"
                    basis = "admin_ignore"
                    scope_value = None
                    actor_value = None
                else:
                    status = "pending_review"
                    basis = "admin_reconsidered"
                    scope_value = None
                    actor_value = None
                await db.execute(
                    """
                    UPDATE user_profile_timeline_identities
                    SET status = ?, identity_basis = ?, profile_scope_uid = ?,
                        actor_id = ?, review_reason = ?, reviewed_by = ?,
                        reviewed_at = ?, updated_at = ?
                    WHERE timeline_uid = ? AND timeline_revision = ?
                      AND memory_space_id = ?
                    """,
                    (
                        status,
                        basis,
                        scope_value,
                        actor_value,
                        str(reason or "").strip()[:2000] or None,
                        str(reviewed_by or "administrator")[:200],
                        now,
                        now,
                        *key,
                    ),
                )
                row = await (
                    await db.execute(
                        """
                        SELECT * FROM user_profile_timeline_identities
                        WHERE timeline_uid = ? AND timeline_revision = ?
                          AND memory_space_id = ?
                        """,
                        key,
                    )
                ).fetchone()
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        if row is None:
            raise RuntimeError("Timeline identity review was not updated")
        return self._timeline_identity_row(row)

    async def list_recoverable_tasks(
        self, *, limit: int = 64, include_future: bool = False
    ) -> list[dict[str, Any]]:
        now = time.time()
        retry_clause = "" if include_future else "AND (next_retry_at IS NULL OR next_retry_at <= ?)"
        params: list[Any] = [] if include_future else [now]
        params.append(max(1, min(1000, int(limit))))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT task_uid FROM user_profile_tasks
                    WHERE status IN (
                        'pending', 'running_facts', 'facts_completed',
                        'facts_failed', 'running_relationship'
                    ) {retry_clause}
                    ORDER BY created_at ASC LIMIT ?
                    """,
                    params,
                )
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            task = await self.get_task(str(row["task_uid"]))
            if task is not None:
                result.append(task)
        return result

    async def update_projection_event(
        self,
        event_uid: str,
        *,
        status: str,
        profile_scope_uid: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE user_profile_projection_events
                SET status = ?, profile_scope_uid = COALESCE(?, profile_scope_uid),
                    error = ?, updated_at = ?
                WHERE event_uid = ?
                """,
                (status, profile_scope_uid, error, time.time(), event_uid),
            )
            await db.commit()

    async def update_task_items(
        self,
        task_uid: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE user_profile_task_items
                SET status = ?, result = COALESCE(?, result), updated_at = ?
                WHERE task_uid = ?
                """,
                (
                    status,
                    self._json(result) if result is not None else None,
                    now,
                    task_uid,
                ),
            )
            await db.commit()

    async def finish_task_events(
        self,
        task_uid: str,
        *,
        status: str,
        error: str | None = None,
    ) -> int:
        """Move every event in a task to one final or retryable status."""
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE user_profile_projection_events
                SET status = ?, error = ?, updated_at = ?
                WHERE event_uid IN (
                    SELECT event_uid FROM user_profile_task_items WHERE task_uid = ?
                )
                """,
                (status, error, now, task_uid),
            )
            row = await (
                await db.execute(
                    """
                    SELECT COALESCE(MAX(e.sequence), 0) AS max_sequence
                    FROM user_profile_projection_events e
                    JOIN user_profile_task_items i ON i.event_uid = e.event_uid
                    WHERE i.task_uid = ?
                    """,
                    (task_uid,),
                )
            ).fetchone()
            await db.commit()
        return int(row["max_sequence"] if row else 0)

    async def mark_memory_space_gap(self, memory_space_id: str) -> int:
        """Mark known private scopes for a failed Timeline projection."""
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE user_profile_scopes
                SET has_gap = 1, updated_at = ?
                WHERE profile_scope_uid IN (
                    SELECT DISTINCT profile_scope_uid
                    FROM user_profile_projection_events
                    WHERE memory_space_id = ? AND profile_scope_uid IS NOT NULL
                )
                """,
                (time.time(), memory_space_id),
            )
            await db.commit()
        return max(0, int(cursor.rowcount))

    async def create_task(
        self,
        task: UserProfileTask,
        events: Iterable[dict[str, Any]],
    ) -> str:
        event_list = list(events)
        async with self._connect() as db:
            try:
                await db.execute(
                    """
                    INSERT INTO user_profile_tasks (
                        task_uid, profile_scope_uid, status, settings_snapshot,
                        provider_signature, retries, error, result_summary,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_uid,
                        task.profile_scope_uid,
                        self._enum(task.status),
                        self._json(task.settings_snapshot),
                        self._json(task.provider_signature),
                        max(0, int(task.retries)),
                        task.error,
                        self._json(task.result_summary),
                        task.created_at,
                        task.updated_at,
                    ),
                )
                for index, event in enumerate(event_list):
                    await db.execute(
                        """
                        INSERT INTO user_profile_task_items (
                            task_uid, event_uid, item_order, timeline_uid,
                            timeline_revision, status, result, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', '{}', ?, ?)
                        """,
                        (
                            task.task_uid,
                            str(event["event_uid"]),
                            index,
                            str(event["timeline_uid"]),
                            max(1, int(event["timeline_revision"])),
                            task.created_at,
                            task.updated_at,
                        ),
                    )
                    await db.execute(
                        "UPDATE user_profile_projection_events "
                        "SET status = 'queued', updated_at = ? WHERE event_uid = ?",
                        (task.updated_at, str(event["event_uid"])),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return task.task_uid

    async def get_task(self, task_uid: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            task = await (
                await db.execute(
                    "SELECT * FROM user_profile_tasks WHERE task_uid = ?",
                    (task_uid,),
                )
            ).fetchone()
            if task is None:
                return None
            items = await (
                await db.execute(
                    """
                    SELECT i.*, e.sequence AS event_sequence,
                           e.operation AS event_operation,
                           e.memory_space_id AS event_memory_space_id,
                           e.profile_scope_uid AS event_profile_scope_uid,
                           e.payload AS event_payload
                    FROM user_profile_task_items i
                    JOIN user_profile_projection_events e
                      ON e.event_uid = i.event_uid
                    WHERE i.task_uid = ? ORDER BY i.item_order
                    """,
                    (task_uid,),
                )
            ).fetchall()
        result = dict(task)
        for key in (
            "settings_snapshot",
            "provider_signature",
            "result_summary",
        ):
            result[key] = self._json_object(result[key])
        result["items"] = []
        for item in items:
            converted = dict(item)
            converted["result"] = self._json_object(converted["result"])
            converted["event_payload"] = self._json_object(converted["event_payload"])
            result["items"].append(converted)
        return result

    async def update_task(
        self,
        task_uid: str,
        *,
        status: str,
        error: str | None = None,
        result_summary: dict[str, Any] | None = None,
        retries: int | None = None,
        next_retry_at: float | None = None,
    ) -> None:
        completed = status in {"completed", "completed_partial", "failed", "cancelled"}
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE user_profile_tasks
                SET status = ?, error = ?,
                    result_summary = COALESCE(?, result_summary),
                    retries = COALESCE(?, retries), next_retry_at = ?,
                    updated_at = ?, completed_at = CASE WHEN ? THEN ? ELSE NULL END
                WHERE task_uid = ?
                """,
                (
                    status,
                    error,
                    self._json(result_summary) if result_summary is not None else None,
                    retries,
                    next_retry_at,
                    time.time(),
                    int(completed),
                    time.time(),
                    task_uid,
                ),
            )
            await db.commit()

    async def run_profile_lifecycle_maintenance(
        self,
        *,
        completed_task_retention_days: int = 30,
        projection_compaction_days: int = 30,
        stale_retention_days: int = 180,
        now: float | None = None,
    ) -> dict[str, int]:
        """Advance due facts and remove old data that can be deterministically rebuilt."""
        current = float(now if now is not None else time.time())
        task_cutoff = current - max(1, int(completed_task_retention_days)) * 86400.0
        projection_cutoff = current - max(1, int(projection_compaction_days)) * 86400.0
        stale_cutoff = current - max(1, int(stale_retention_days)) * 86400.0
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                affected_rows = await (
                    await db.execute(
                        """
                        SELECT DISTINCT fact_namespace_uid
                        FROM user_profile_facts
                        WHERE pinned = 0 AND (
                            (status IN ('active', 'pending')
                             AND review_after IS NOT NULL AND review_after <= ?)
                            OR (status = 'stale' AND updated_at <= ?)
                        )
                        """,
                        (current, stale_cutoff),
                    )
                ).fetchall()
                affected_namespaces = [
                    str(row["fact_namespace_uid"]) for row in affected_rows
                ]
                stale = await db.execute(
                    """
                    UPDATE user_profile_facts
                    SET status = 'stale', updated_at = ?
                    WHERE status = 'active' AND pinned = 0
                      AND review_after IS NOT NULL AND review_after <= ?
                    """,
                    (current, current),
                )
                archived = await db.execute(
                    """
                    UPDATE user_profile_facts
                    SET status = 'archived', updated_at = ?
                    WHERE status = 'pending' AND pinned = 0
                      AND review_after IS NOT NULL AND review_after <= ?
                    """,
                    (current, current),
                )
                stale_archived = await db.execute(
                    """
                    UPDATE user_profile_facts
                    SET status = 'archived', updated_at = ?
                    WHERE status = 'stale' AND pinned = 0 AND updated_at <= ?
                    """,
                    (current, stale_cutoff),
                )
                revised_namespace_count = 0
                if affected_namespaces:
                    placeholders = ",".join("?" for _ in affected_namespaces)
                    revised_namespaces = await db.execute(
                        f"""
                        UPDATE user_profile_fact_namespaces
                        SET current_revision = current_revision + 1, updated_at = ?
                        WHERE fact_namespace_uid IN ({placeholders})
                        """,
                        [current, *affected_namespaces],
                    )
                    revised_namespace_count = max(
                        0, int(revised_namespaces.rowcount)
                    )
                deleted_tasks = await db.execute(
                    """
                    DELETE FROM user_profile_tasks
                    WHERE status IN ('completed', 'completed_partial', 'cancelled')
                      AND completed_at IS NOT NULL AND completed_at < ?
                    """,
                    (task_cutoff,),
                )
                compacted_events = await db.execute(
                    """
                    DELETE FROM user_profile_projection_events AS old
                    WHERE old.status = 'completed' AND old.updated_at < ?
                      AND EXISTS (
                          SELECT 1 FROM user_profile_projection_events AS newer
                          WHERE newer.profile_scope_uid = old.profile_scope_uid
                            AND newer.timeline_uid = old.timeline_uid
                            AND newer.sequence > old.sequence
                            AND newer.status = 'completed'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM user_profile_task_items AS item
                          WHERE item.event_uid = old.event_uid
                      )
                    """,
                    (projection_cutoff,),
                )
                compacted_sources = await db.execute(
                    """
                    DELETE FROM user_profile_fact_sources
                    WHERE active = 0 AND profile_fact_uid IS NULL AND updated_at < ?
                    """,
                    (projection_cutoff,),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "facts_stale": max(0, int(stale.rowcount)),
            "pending_archived": max(0, int(archived.rowcount)),
            "stale_archived": max(0, int(stale_archived.rowcount)),
            "fact_namespaces_revised": revised_namespace_count,
            "tasks_deleted": max(0, int(deleted_tasks.rowcount)),
            "projection_events_compacted": max(0, int(compacted_events.rowcount)),
            "fact_sources_compacted": max(0, int(compacted_sources.rowcount)),
        }

    async def profile_fingerprint(self, profile_scope_uid: str) -> str:
        """Return a stale-preview fingerprint for all administrator-visible state."""
        async with self._connect() as db:
            scope = await (
                await db.execute(
                    """
                    SELECT s.*, n.current_revision,
                           COALESCE(r.revision, 0) AS relationship_revision,
                           COALESCE(MAX(a.updated_at), 0) AS account_updated_at,
                           COALESCE(MAX(c.updated_at), 0) AS conflict_updated_at
                    FROM user_profile_scopes s
                    JOIN user_profile_fact_namespaces n
                      ON n.fact_namespace_uid = s.fact_namespace_uid
                    LEFT JOIN user_relationship_states r
                      ON r.profile_scope_uid = s.profile_scope_uid
                    LEFT JOIN user_profile_accounts a
                      ON a.logical_user_uid = s.logical_user_uid
                    LEFT JOIN user_profile_conflicts c
                      ON c.fact_namespace_uid = s.fact_namespace_uid
                    WHERE s.profile_scope_uid = ?
                    GROUP BY s.profile_scope_uid
                    """,
                    (profile_scope_uid,),
                )
            ).fetchone()
        if scope is None:
            raise ValueError("Unknown user-profile scope")
        payload = {
            key: scope[key]
            for key in (
                "profile_scope_uid",
                "logical_user_uid",
                "fact_namespace_uid",
                "enabled",
                "auto_enable_blocked",
                "projection_cursor",
                "has_gap",
                "relationship_frozen",
                "updated_at",
                "current_revision",
                "relationship_revision",
                "account_updated_at",
                "conflict_updated_at",
            )
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def profile_gap_status(self, profile_scope_uid: str) -> dict[str, Any]:
        scope = await self.get_scope(profile_scope_uid)
        if scope is None:
            raise ValueError("Unknown user-profile scope")
        async with self._connect() as db:
            row = await (
                await db.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) AS max_sequence,
                           COUNT(CASE WHEN status IN (
                               'pending', 'queued', 'running'
                           ) THEN 1 END) AS pending_count,
                           COUNT(CASE WHEN status IN (
                               'failed', 'cancelled'
                           ) THEN 1 END) AS resumable_count
                    FROM user_profile_projection_events
                    WHERE profile_scope_uid = ?
                    """,
                    (profile_scope_uid,),
                )
            ).fetchone()
        max_sequence = int(row["max_sequence"] if row else 0)
        pending_count = int(row["pending_count"] if row else 0)
        resumable_count = int(row["resumable_count"] if row else 0)
        has_gap = bool(
            scope.has_gap
            or pending_count
            or resumable_count
            or max_sequence > int(scope.projection_cursor)
        )
        return {
            "has_gap": has_gap,
            "pending_count": pending_count,
            "resumable_count": resumable_count,
            "projection_cursor": int(scope.projection_cursor),
            "max_sequence": max_sequence,
        }

    async def set_profile_enabled(
        self, profile_scope_uid: str, enabled: bool
    ) -> dict[str, Any]:
        scope = await self.get_scope(profile_scope_uid)
        if scope is None:
            raise ValueError("Unknown user-profile scope")
        gap = await self.profile_gap_status(profile_scope_uid)
        await self.set_scope_state(
            profile_scope_uid,
            enabled=bool(enabled),
            auto_enable_blocked=False if enabled else None,
            has_gap=bool(gap["has_gap"]) if enabled else True,
        )
        async with self._connect() as db:
            await db.execute(
                "UPDATE user_profile_users SET status = ?, updated_at = ? "
                "WHERE logical_user_uid = ?",
                (
                    "active" if enabled else "disabled",
                    time.time(),
                    scope.logical_user_uid,
                ),
            )
            await db.commit()
        gap = await self.profile_gap_status(profile_scope_uid)
        gap["enabled"] = bool(enabled)
        return gap

    @classmethod
    async def _detach_shared_namespace(
        cls, db: aiosqlite.Connection, scope_row: aiosqlite.Row, now: float
    ) -> str:
        namespace_uid = str(scope_row["fact_namespace_uid"])
        namespace = await (
            await db.execute(
                "SELECT share_group_uid FROM user_profile_fact_namespaces "
                "WHERE fact_namespace_uid = ?",
                (namespace_uid,),
            )
        ).fetchone()
        count_row = await (
            await db.execute(
                "SELECT COUNT(*) AS value FROM user_profile_scopes "
                "WHERE fact_namespace_uid = ?",
                (namespace_uid,),
            )
        ).fetchone()
        shared = (
            bool(namespace and namespace["share_group_uid"])
            or int(count_row["value"] if count_row else 0) > 1
        )
        if not shared:
            return namespace_uid
        new_uid = f"profile-facts-v1-{uuid.uuid4()}"
        await db.execute(
            "INSERT INTO user_profile_fact_namespaces "
            "(fact_namespace_uid, current_revision, created_at, updated_at) "
            "VALUES (?, 0, ?, ?)",
            (new_uid, now, now),
        )
        await db.execute(
            "UPDATE user_profile_scopes SET fact_namespace_uid = ?, updated_at = ? "
            "WHERE profile_scope_uid = ?",
            (new_uid, now, str(scope_row["profile_scope_uid"])),
        )
        await db.execute(
            "DELETE FROM user_profile_share_members WHERE profile_scope_uid = ?",
            (str(scope_row["profile_scope_uid"]),),
        )
        return new_uid

    @staticmethod
    async def _clear_fact_namespace(
        db: aiosqlite.Connection,
        fact_namespace_uid: str,
        *,
        clear_overrides: bool,
        now: float,
    ) -> None:
        if clear_overrides:
            await db.execute(
                "DELETE FROM user_profile_fact_overrides WHERE fact_namespace_uid = ?",
                (fact_namespace_uid,),
            )
        await db.execute(
            """
            UPDATE user_profile_fact_sources
            SET active = 0, profile_fact_uid = NULL, updated_at = ?
            WHERE profile_fact_uid IN (
                SELECT profile_fact_uid FROM user_profile_facts
                WHERE fact_namespace_uid = ?
            )
            """,
            (now, fact_namespace_uid),
        )
        await db.execute(
            "DELETE FROM user_profile_conflicts WHERE fact_namespace_uid = ?",
            (fact_namespace_uid,),
        )
        await db.execute(
            "DELETE FROM user_profile_facts WHERE fact_namespace_uid = ?",
            (fact_namespace_uid,),
        )
        await db.execute(
            "UPDATE user_profile_fact_namespaces "
            "SET current_revision = current_revision + 1, updated_at = ? "
            "WHERE fact_namespace_uid = ?",
            (now, fact_namespace_uid),
        )

    async def reset_objective_profile(
        self,
        profile_scope_uid: str,
        *,
        clear_overrides: bool = True,
        detach_shared: bool = True,
    ) -> dict[str, Any]:
        now = time.time()
        async with self._connect() as db:
            try:
                scope_row = await (
                    await db.execute(
                        "SELECT * FROM user_profile_scopes WHERE profile_scope_uid = ?",
                        (profile_scope_uid,),
                    )
                ).fetchone()
                if scope_row is None:
                    raise ValueError("Unknown user-profile scope")
                namespace_uid = str(scope_row["fact_namespace_uid"])
                if detach_shared:
                    namespace_uid = await self._detach_shared_namespace(
                        db, scope_row, now
                    )
                await self._clear_fact_namespace(
                    db,
                    namespace_uid,
                    clear_overrides=clear_overrides,
                    now=now,
                )
                cursor_row = await (
                    await db.execute(
                        "SELECT COALESCE(MAX(sequence), 0) AS value "
                        "FROM user_profile_projection_events "
                        "WHERE profile_scope_uid = ?",
                        (profile_scope_uid,),
                    )
                ).fetchone()
                cursor = int(cursor_row["value"] if cursor_row else 0)
                await db.execute(
                    """
                    UPDATE user_profile_scopes
                    SET projection_cursor = ?, reset_after = ?, has_gap = 0,
                        updated_at = ? WHERE profile_scope_uid = ?
                    """,
                    (cursor, now, now, profile_scope_uid),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "profile_scope_uid": profile_scope_uid,
            "fact_namespace_uid": namespace_uid,
            "projection_cursor": cursor,
            "reset_after": now,
        }

    async def delete_and_disable_profile(
        self, profile_scope_uid: str
    ) -> dict[str, Any]:
        reset = await self.reset_objective_profile(
            profile_scope_uid, clear_overrides=True, detach_shared=True
        )
        now = time.time()
        async with self._connect() as db:
            try:
                scope = await (
                    await db.execute(
                        "SELECT logical_user_uid FROM user_profile_scopes "
                        "WHERE profile_scope_uid = ?",
                        (profile_scope_uid,),
                    )
                ).fetchone()
                await db.execute(
                    "DELETE FROM user_relationship_states WHERE profile_scope_uid = ?",
                    (profile_scope_uid,),
                )
                await db.execute(
                    "DELETE FROM user_profile_tasks WHERE profile_scope_uid = ?",
                    (profile_scope_uid,),
                )
                await db.execute(
                    "UPDATE user_profile_projection_events "
                    "SET status = 'cancelled', error = 'Profile deleted and disabled', "
                    "updated_at = ? WHERE profile_scope_uid = ?",
                    (now, profile_scope_uid),
                )
                await db.execute(
                    """
                    UPDATE user_profile_scopes
                    SET enabled = 0, auto_enable_blocked = 1, has_gap = 0,
                        relationship_frozen = 0, relationship_reset_after = ?,
                        updated_at = ? WHERE profile_scope_uid = ?
                    """,
                    (now, now, profile_scope_uid),
                )
                if scope is not None:
                    await db.execute(
                        "UPDATE user_profile_users SET status = 'deleted', updated_at = ? "
                        "WHERE logical_user_uid = ?",
                        (now, str(scope["logical_user_uid"])),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {**reset, "enabled": False, "auto_enable_blocked": True}

    async def apply_fact_admin_action(
        self,
        profile_scope_uid: str,
        profile_fact_uid: str,
        *,
        action: str,
        expected_revision: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        allowed = {"pause", "resume", "pin", "unpin", "confirm", "exclude"}
        if action not in allowed:
            raise ValueError("Unsupported profile fact action")
        now = time.time()
        async with self._connect() as db:
            try:
                row = await (
                    await db.execute(
                        """
                        SELECT f.*, s.fact_namespace_uid, n.current_revision
                        FROM user_profile_facts f
                        JOIN user_profile_scopes s
                          ON s.fact_namespace_uid = f.fact_namespace_uid
                        JOIN user_profile_fact_namespaces n
                          ON n.fact_namespace_uid = f.fact_namespace_uid
                        WHERE s.profile_scope_uid = ? AND f.profile_fact_uid = ?
                        """,
                        (profile_scope_uid, profile_fact_uid),
                    )
                ).fetchone()
                if row is None:
                    raise ValueError("Unknown profile fact")
                current_revision = int(row["current_revision"])
                if current_revision != int(expected_revision):
                    raise UserProfileRevisionConflict(
                        f"Expected profile revision {expected_revision}, got {current_revision}"
                    )
                updates: dict[str, Any] = {}
                if action == "pause":
                    updates["status"] = "excluded"
                elif action == "resume":
                    updates["status"] = "active"
                elif action in {"pin", "unpin"}:
                    updates["pinned"] = 1 if action == "pin" else 0
                elif action == "confirm":
                    updates.update(status="active", admin_confirmed=1)
                elif action == "exclude":
                    updates["status"] = "excluded"
                assignments = [f"{key} = ?" for key in updates]
                values = list(updates.values())
                assignments.append("updated_at = ?")
                values.extend((now, profile_fact_uid))
                await db.execute(
                    f"UPDATE user_profile_facts SET {', '.join(assignments)} "
                    "WHERE profile_fact_uid = ?",
                    values,
                )
                await db.execute(
                    "UPDATE user_profile_fact_overrides SET active = 0, updated_at = ? "
                    "WHERE profile_fact_uid = ? AND override_type = ? AND active = 1",
                    (now, profile_fact_uid, action),
                )
                await db.execute(
                    """
                    INSERT INTO user_profile_fact_overrides (
                        override_uid, fact_namespace_uid, profile_fact_uid,
                        override_type, active, payload, reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        str(row["fact_namespace_uid"]),
                        profile_fact_uid,
                        action,
                        self._json(
                            {
                                "previous_status": str(row["status"]),
                                "previous_pinned": bool(row["pinned"]),
                            }
                        ),
                        str(reason or "")[:2000] or None,
                        now,
                        now,
                    ),
                )
                new_revision = current_revision + 1
                await db.execute(
                    "UPDATE user_profile_fact_namespaces "
                    "SET current_revision = ?, updated_at = ? "
                    "WHERE fact_namespace_uid = ?",
                    (new_revision, now, str(row["fact_namespace_uid"])),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "profile_fact_uid": profile_fact_uid,
            "action": action,
            "fact_revision": new_revision,
        }

    async def resolve_profile_conflict(
        self,
        profile_scope_uid: str,
        conflict_uid: str,
        *,
        resolution: str,
        selected_fact_uid: str | None,
        expected_revision: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if resolution not in {"select", "pause", "exclude"}:
            raise ValueError("Unsupported conflict resolution")
        now = time.time()
        async with self._connect() as db:
            try:
                row = await (
                    await db.execute(
                        """
                        SELECT c.*, n.current_revision
                        FROM user_profile_conflicts c
                        JOIN user_profile_scopes s
                          ON s.fact_namespace_uid = c.fact_namespace_uid
                        JOIN user_profile_fact_namespaces n
                          ON n.fact_namespace_uid = c.fact_namespace_uid
                        WHERE s.profile_scope_uid = ? AND c.conflict_uid = ?
                        """,
                        (profile_scope_uid, conflict_uid),
                    )
                ).fetchone()
                if row is None:
                    raise ValueError("Unknown profile conflict")
                current_revision = int(row["current_revision"])
                if current_revision != int(expected_revision):
                    raise UserProfileRevisionConflict(
                        f"Expected profile revision {expected_revision}, got {current_revision}"
                    )
                fact_uids = [str(value) for value in self._json_list(row["fact_uids"])]
                if resolution == "select":
                    if not selected_fact_uid or selected_fact_uid not in fact_uids:
                        raise ValueError(
                            "selected_fact_uid must belong to the conflict"
                        )
                    await db.execute(
                        "UPDATE user_profile_facts SET status = 'active', updated_at = ? "
                        "WHERE profile_fact_uid = ?",
                        (now, selected_fact_uid),
                    )
                    others = [uid for uid in fact_uids if uid != selected_fact_uid]
                    if others:
                        await db.execute(
                            f"UPDATE user_profile_facts SET status = 'superseded', "
                            f"superseded_by = ?, updated_at = ? WHERE profile_fact_uid IN "
                            f"({','.join('?' for _ in others)})",
                            [selected_fact_uid, now, *others],
                        )
                    conflict_status = "resolved"
                elif resolution == "exclude":
                    if fact_uids:
                        await db.execute(
                            f"UPDATE user_profile_facts SET status = 'excluded', "
                            f"updated_at = ? WHERE profile_fact_uid IN "
                            f"({','.join('?' for _ in fact_uids)})",
                            [now, *fact_uids],
                        )
                    selected_fact_uid = None
                    conflict_status = "resolved"
                else:
                    selected_fact_uid = None
                    conflict_status = "open"
                await db.execute(
                    """
                    UPDATE user_profile_conflicts
                    SET status = ?, resolution_kind = ?, resolution_reason = ?,
                        resolved_fact_uid = ?, resolved_at = ?, updated_at = ?
                    WHERE conflict_uid = ?
                    """,
                    (
                        conflict_status,
                        resolution,
                        str(reason or "")[:2000] or None,
                        selected_fact_uid,
                        now if conflict_status == "resolved" else None,
                        now,
                        conflict_uid,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO user_profile_fact_overrides (
                        override_uid, fact_namespace_uid, profile_fact_uid,
                        override_type, payload, reason, created_at, updated_at
                    ) VALUES (?, ?, ?, 'conflict_resolution', ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        str(row["fact_namespace_uid"]),
                        selected_fact_uid,
                        self._json(
                            {
                                "conflict_uid": conflict_uid,
                                "resolution": resolution,
                                "fact_uids": fact_uids,
                            }
                        ),
                        str(reason or "")[:2000] or None,
                        now,
                        now,
                    ),
                )
                new_revision = current_revision + 1
                await db.execute(
                    "UPDATE user_profile_fact_namespaces "
                    "SET current_revision = ?, updated_at = ? "
                    "WHERE fact_namespace_uid = ?",
                    (new_revision, now, str(row["fact_namespace_uid"])),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "conflict_uid": conflict_uid,
            "resolution": resolution,
            "fact_revision": new_revision,
        }

    async def prepare_profile_rebuild(
        self,
        profile_scope_uid: str,
        *,
        clear_overrides: bool,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        current_fingerprint = await self.profile_fingerprint(profile_scope_uid)
        if current_fingerprint != str(expected_fingerprint):
            raise UserProfileRevisionConflict("Profile rebuild preview is stale")
        if clear_overrides:
            await self.reset_objective_profile(
                profile_scope_uid,
                clear_overrides=True,
                detach_shared=True,
            )
        now = time.time()
        async with self._connect() as db:
            try:
                await db.execute(
                    "UPDATE user_profile_tasks SET status = 'cancelled', "
                    "error = 'Superseded by explicit profile rebuild', updated_at = ?, "
                    "completed_at = ? WHERE profile_scope_uid = ? AND status NOT IN "
                    "('completed', 'completed_partial', 'cancelled')",
                    (now, now, profile_scope_uid),
                )
                cursor = await db.execute(
                    """
                    UPDATE user_profile_projection_events
                    SET status = 'pending', error = NULL, updated_at = ?
                    WHERE profile_scope_uid = ?
                    """,
                    (now, profile_scope_uid),
                )
                await db.execute(
                    "UPDATE user_profile_scopes SET enabled = 1, has_gap = 1, "
                    "projection_cursor = 0, updated_at = ? WHERE profile_scope_uid = ?",
                    (now, profile_scope_uid),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "profile_scope_uid": profile_scope_uid,
            "event_count": max(0, int(cursor.rowcount)),
            "clear_overrides": bool(clear_overrides),
        }

    async def list_profile_tasks(
        self, profile_scope_uid: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT task_uid FROM user_profile_tasks "
                    "WHERE profile_scope_uid = ? ORDER BY created_at DESC LIMIT ?",
                    (profile_scope_uid, max(1, min(500, int(limit)))),
                )
            ).fetchall()
        result = []
        for row in rows:
            task = await self.get_task(str(row["task_uid"]))
            if task is not None:
                result.append(task)
        return result

    async def get_blocking_failed_task(
        self, profile_scope_uid: str
    ) -> dict[str, Any] | None:
        """Return the oldest unfinished task that still owns queued or failed events."""
        async with self._connect() as db:
            row = await (
                await db.execute(
                    """
                    SELECT DISTINCT t.task_uid
                    FROM user_profile_tasks t
                    JOIN user_profile_task_items i ON i.task_uid = t.task_uid
                    JOIN user_profile_projection_events e ON e.event_uid = i.event_uid
                    WHERE t.profile_scope_uid = ?
                      AND t.status IN (
                          'pending', 'running_facts', 'facts_completed',
                          'facts_failed', 'running_relationship', 'failed'
                      )
                      AND e.status IN ('queued', 'running', 'failed')
                    ORDER BY t.created_at ASC LIMIT 1
                    """,
                    (profile_scope_uid,),
                )
            ).fetchone()
        return await self.get_task(str(row["task_uid"])) if row else None

    async def retry_profile_task(self, task_uid: str) -> str:
        task = await self.get_task(task_uid)
        if task is None:
            raise ValueError("Unknown user-profile task")
        if str(task.get("status") or "") not in {
            "facts_failed",
            "facts_completed",
            "failed",
        }:
            raise ValueError("User-profile task is not retryable")
        now = time.time()
        async with self._connect() as db:
            try:
                await db.execute(
                    "UPDATE user_profile_tasks SET status = 'pending', error = NULL, "
                    "retries = 0, next_retry_at = NULL, updated_at = ? WHERE task_uid = ?",
                    (now, task_uid),
                )
                await db.execute(
                    "UPDATE user_profile_task_items SET status = 'pending', "
                    "updated_at = ? WHERE task_uid = ?",
                    (now, task_uid),
                )
                await db.execute(
                    "UPDATE user_profile_projection_events SET status = 'queued', "
                    "error = NULL, updated_at = ? WHERE event_uid IN "
                    "(SELECT event_uid FROM user_profile_task_items WHERE task_uid = ?)",
                    (now, task_uid),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return str(task["profile_scope_uid"])

    async def cancel_profile_task_group(self, task_uid: str) -> dict[str, Any]:
        """Cancel a task and any unfinished events from the same history build."""
        task = await self.get_task(task_uid)
        if task is None:
            raise ValueError("Unknown user-profile task")
        if str(task.get("status") or "") in {
            "completed",
            "completed_partial",
            "cancelled",
        }:
            raise ValueError("User-profile task is not cancellable")
        scope_uid = str(task["profile_scope_uid"])
        build_uid = str(
            (task.get("result_summary") or {}).get("build_operation_uid") or ""
        )
        now = time.time()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if build_uid:
                    task_rows = await (
                        await db.execute(
                            "SELECT task_uid FROM user_profile_tasks "
                            "WHERE profile_scope_uid = ? AND "
                            "json_extract(result_summary, '$.build_operation_uid') = ? "
                            "AND status NOT IN ('completed', 'completed_partial', 'cancelled')",
                            (scope_uid, build_uid),
                        )
                    ).fetchall()
                else:
                    task_rows = await (
                        await db.execute(
                            "SELECT task_uid FROM user_profile_tasks WHERE task_uid = ?",
                            (task_uid,),
                        )
                    ).fetchall()
                task_uids = [str(row["task_uid"]) for row in task_rows]
                if not task_uids:
                    raise ValueError("User-profile task is no longer cancellable")
                placeholders = ",".join("?" for _ in task_uids)
                await db.execute(
                    f"UPDATE user_profile_tasks SET status = 'cancelled', "
                    f"error = 'Cancelled by administrator', next_retry_at = NULL, "
                    f"result_summary = json_set(result_summary, "
                    f"'$.automatic_retry_pending', json('false')), "
                    f"updated_at = ?, completed_at = ? "
                    f"WHERE task_uid IN ({placeholders})",
                    [now, now, *task_uids],
                )
                await db.execute(
                    f"UPDATE user_profile_task_items SET status = CASE "
                    f"WHEN status IN ('facts_completed', 'relationship_completed') "
                    f"THEN status ELSE 'cancelled' END, updated_at = ? "
                    f"WHERE task_uid IN ({placeholders})",
                    [now, *task_uids],
                )
                if build_uid:
                    event_cursor = await db.execute(
                        "UPDATE user_profile_projection_events SET status = 'cancelled', "
                        "error = 'Cancelled by administrator', updated_at = ? "
                        "WHERE profile_scope_uid = ? AND "
                        "json_extract(payload, '$.build_operation_uid') = ? "
                        "AND status NOT IN ('completed', 'cancelled')",
                        (now, scope_uid, build_uid),
                    )
                else:
                    event_cursor = await db.execute(
                        f"UPDATE user_profile_projection_events SET status = 'cancelled', "
                        f"error = 'Cancelled by administrator', updated_at = ? "
                        f"WHERE event_uid IN (SELECT event_uid "
                        f"FROM user_profile_task_items WHERE task_uid IN ({placeholders})) "
                        f"AND status != 'completed'",
                        [now, *task_uids],
                    )
                await db.execute(
                    "UPDATE user_profile_scopes SET has_gap = 1, updated_at = ? "
                    "WHERE profile_scope_uid = ?",
                    (now, scope_uid),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "profile_scope_uid": scope_uid,
            "task_uids": task_uids,
            "cancelled_event_count": max(0, int(event_cursor.rowcount)),
            "build_operation_uid": build_uid or None,
        }

    async def continue_profile_gap(self, profile_scope_uid: str) -> dict[str, Any]:
        """Requeue the earliest interrupted build without replaying completed events."""
        now = time.time()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                active = await (
                    await db.execute(
                        "SELECT 1 FROM user_profile_tasks WHERE profile_scope_uid = ? "
                        "AND status NOT IN ('completed', 'completed_partial', 'failed', 'cancelled') "
                        "LIMIT 1",
                        (profile_scope_uid,),
                    )
                ).fetchone()
                if active is not None:
                    raise ValueError("User-profile maintenance is already running")
                first = await (
                    await db.execute(
                        "SELECT event_uid, payload FROM user_profile_projection_events "
                        "WHERE profile_scope_uid = ? AND status IN ('failed', 'cancelled') "
                        "ORDER BY sequence ASC LIMIT 1",
                        (profile_scope_uid,),
                    )
                ).fetchone()
                if first is None:
                    raise ValueError("No resumable user-profile gap")
                payload = self._json_object(first["payload"])
                build_uid = str(payload.get("build_operation_uid") or "")
                if build_uid:
                    event_rows = await (
                        await db.execute(
                            "SELECT event_uid FROM user_profile_projection_events "
                            "WHERE profile_scope_uid = ? AND status IN ('failed', 'cancelled') "
                            "AND json_extract(payload, '$.build_operation_uid') = ? "
                            "ORDER BY sequence ASC",
                            (profile_scope_uid, build_uid),
                        )
                    ).fetchall()
                else:
                    event_rows = [first]
                event_uids = [str(row["event_uid"]) for row in event_rows]
                event_placeholders = ",".join("?" for _ in event_uids)
                task_row = await (
                    await db.execute(
                        f"SELECT t.task_uid, t.result_summary "
                        f"FROM user_profile_tasks t JOIN user_profile_task_items i "
                        f"ON i.task_uid = t.task_uid "
                        f"WHERE i.event_uid IN ({event_placeholders}) "
                        f"AND t.status IN ('failed', 'cancelled') "
                        f"ORDER BY t.created_at ASC LIMIT 1",
                        event_uids,
                    )
                ).fetchone()
                resume_task_uid = str(task_row["task_uid"]) if task_row else ""
                await db.execute(
                    f"UPDATE user_profile_projection_events SET status = 'pending', "
                    f"error = NULL, updated_at = ? "
                    f"WHERE event_uid IN ({event_placeholders})",
                    [now, *event_uids],
                )
                if resume_task_uid:
                    task_summary = self._json_object(task_row["result_summary"])
                    task_summary["automatic_retry_pending"] = False
                    task_summary.pop("failed_stage", None)
                    task_summary.pop("request_elapsed_seconds", None)
                    await db.execute(
                        "UPDATE user_profile_tasks SET status = 'pending', error = NULL, "
                        "retries = 0, next_retry_at = NULL, result_summary = ?, "
                        "updated_at = ?, completed_at = NULL WHERE task_uid = ?",
                        (self._json(task_summary), now, resume_task_uid),
                    )
                    item_status = (
                        "facts_completed"
                        if task_summary.get("facts_checkpoint")
                        else "pending"
                    )
                    await db.execute(
                        "UPDATE user_profile_task_items SET status = ?, updated_at = ? "
                        "WHERE task_uid = ?",
                        (item_status, now, resume_task_uid),
                    )
                    await db.execute(
                        "UPDATE user_profile_projection_events SET status = 'queued' "
                        "WHERE event_uid IN (SELECT event_uid "
                        "FROM user_profile_task_items WHERE task_uid = ?)",
                        (resume_task_uid,),
                    )
                await db.execute(
                    "UPDATE user_profile_scopes SET has_gap = 1, updated_at = ? "
                    "WHERE profile_scope_uid = ?",
                    (now, profile_scope_uid),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "profile_scope_uid": profile_scope_uid,
            "event_count": len(event_uids),
            "resume_task_uid": resume_task_uid or None,
            "build_operation_uid": build_uid or None,
        }

    async def delete_completed_profile_task(self, task_uid: str) -> str:
        task = await self.get_task(task_uid)
        if task is None:
            raise ValueError("Unknown user-profile task")
        if str(task.get("status") or "") not in {"completed", "completed_partial"}:
            raise ValueError("Only completed user-profile tasks can be deleted")
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM user_profile_tasks WHERE task_uid = ?", (task_uid,)
            )
            await db.commit()
        return str(task["profile_scope_uid"])

    async def clear_completed_profile_tasks(self, profile_scope_uid: str) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM user_profile_tasks WHERE profile_scope_uid = ? "
                "AND status IN ('completed', 'completed_partial')",
                (profile_scope_uid,),
            )
            await db.commit()
        return max(0, int(cursor.rowcount))

    async def preview_account_binding(
        self, *, target_actor_id: str, actor_ids: Iterable[str]
    ) -> dict[str, Any]:
        requested = list(dict.fromkeys([target_actor_id, *[str(v) for v in actor_ids]]))
        requested = [value for value in requested if value]
        if len(requested) < 2:
            raise ValueError("At least two stable accounts are required")
        placeholders = ",".join("?" for _ in requested)
        async with self._connect() as db:
            accounts = await (
                await db.execute(
                    f"SELECT * FROM user_profile_accounts WHERE actor_id IN ({placeholders})",
                    requested,
                )
            ).fetchall()
            if len(accounts) != len(requested):
                raise ValueError("One or more profile accounts do not exist")
            target = next(
                (row for row in accounts if str(row["actor_id"]) == target_actor_id),
                None,
            )
            if target is None:
                raise ValueError("Target account does not exist")
            source_users = {
                str(row["logical_user_uid"])
                for row in accounts
                if str(row["logical_user_uid"]) != str(target["logical_user_uid"])
            }
            impacted_accounts = (
                await (
                    await db.execute(
                        f"SELECT * FROM user_profile_accounts WHERE logical_user_uid IN "
                        f"({','.join('?' for _ in source_users)})",
                        list(source_users),
                    )
                ).fetchall()
                if source_users
                else []
            )
            scope_users = [str(target["logical_user_uid"]), *sorted(source_users)]
            scopes = await (
                await db.execute(
                    f"""
                    SELECT s.*, n.current_revision, n.share_group_uid,
                           COALESCE(r.revision, 0) AS relationship_revision,
                           (SELECT COUNT(*) FROM user_profile_facts f
                            WHERE f.fact_namespace_uid = s.fact_namespace_uid) AS fact_count,
                           (SELECT COUNT(*) FROM user_profile_conflicts c
                            WHERE c.fact_namespace_uid = s.fact_namespace_uid
                              AND c.status = 'open') AS conflict_count
                    FROM user_profile_scopes s
                    JOIN user_profile_fact_namespaces n
                      ON n.fact_namespace_uid = s.fact_namespace_uid
                    LEFT JOIN user_relationship_states r
                      ON r.profile_scope_uid = s.profile_scope_uid
                    WHERE s.logical_user_uid IN ({','.join('?' for _ in scope_users)})
                    ORDER BY s.bot_account, s.persona_id
                    """,
                    scope_users,
                )
            ).fetchall()
        target_user_uid = str(target["logical_user_uid"])
        target_keys = {
            (str(row["bot_account"]), str(row["persona_id"]))
            for row in scopes
            if str(row["logical_user_uid"]) == target_user_uid
        }
        collisions = [
            {
                "profile_scope_uid": str(row["profile_scope_uid"]),
                "bot_account": str(row["bot_account"]),
                "persona_id": str(row["persona_id"]),
                "fact_count": int(row["fact_count"]),
                "relationship_revision": int(row["relationship_revision"]),
            }
            for row in scopes
            if str(row["logical_user_uid"]) in source_users
            and (str(row["bot_account"]), str(row["persona_id"])) in target_keys
        ]
        blocked = any(row["share_group_uid"] for row in scopes)
        fingerprint_payload = {
            "accounts": [
                (
                    str(row["actor_id"]),
                    str(row["logical_user_uid"]),
                    float(row["updated_at"]),
                )
                for row in accounts
            ],
            "scopes": [
                (
                    str(row["profile_scope_uid"]),
                    str(row["fact_namespace_uid"]),
                    int(row["current_revision"]),
                    int(row["relationship_revision"]),
                    float(row["updated_at"]),
                )
                for row in scopes
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "target_actor_id": target_actor_id,
            "target_logical_user_uid": target_user_uid,
            "requested_actor_ids": requested,
            "moved_account_count": len(impacted_accounts),
            "source_scope_count": sum(
                1 for row in scopes if str(row["logical_user_uid"]) in source_users
            ),
            "fact_count": sum(int(row["fact_count"]) for row in scopes),
            "open_conflict_count": sum(int(row["conflict_count"]) for row in scopes),
            "scope_collisions": collisions,
            "blocked_reason": (
                "Remove objective share-group membership before binding accounts"
                if blocked
                else None
            ),
            "fingerprint": fingerprint,
        }

    async def bind_accounts(
        self,
        *,
        target_actor_id: str,
        actor_ids: Iterable[str],
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        preview = await self.preview_account_binding(
            target_actor_id=target_actor_id, actor_ids=actor_ids
        )
        if preview["fingerprint"] != str(expected_fingerprint):
            raise UserProfileRevisionConflict("Account binding preview is stale")
        if preview["blocked_reason"]:
            raise ValueError(str(preview["blocked_reason"]))
        target_user_uid = str(preview["target_logical_user_uid"])
        requested = list(preview["requested_actor_ids"])
        now = time.time()
        async with self._connect() as db:
            try:
                rows = await (
                    await db.execute(
                        f"SELECT logical_user_uid FROM user_profile_accounts "
                        f"WHERE actor_id IN ({','.join('?' for _ in requested)})",
                        requested,
                    )
                ).fetchall()
                source_users = {
                    str(row["logical_user_uid"])
                    for row in rows
                    if str(row["logical_user_uid"]) != target_user_uid
                }
                affected_target_scopes: set[str] = set()
                for source_user_uid in source_users:
                    source_scopes = await (
                        await db.execute(
                            "SELECT * FROM user_profile_scopes "
                            "WHERE logical_user_uid = ? ORDER BY created_at",
                            (source_user_uid,),
                        )
                    ).fetchall()
                    for source_scope in source_scopes:
                        target_scope = await (
                            await db.execute(
                                "SELECT * FROM user_profile_scopes WHERE logical_user_uid = ? "
                                "AND bot_account = ? AND persona_id = ?",
                                (
                                    target_user_uid,
                                    str(source_scope["bot_account"]),
                                    str(source_scope["persona_id"]),
                                ),
                            )
                        ).fetchone()
                        if target_scope is None:
                            await db.execute(
                                "UPDATE user_profile_scopes SET logical_user_uid = ?, "
                                "has_gap = 1, updated_at = ? WHERE profile_scope_uid = ?",
                                (
                                    target_user_uid,
                                    now,
                                    str(source_scope["profile_scope_uid"]),
                                ),
                            )
                            affected_target_scopes.add(
                                str(source_scope["profile_scope_uid"])
                            )
                            continue
                        source_scope_uid = str(source_scope["profile_scope_uid"])
                        target_scope_uid = str(target_scope["profile_scope_uid"])
                        source_namespace = str(source_scope["fact_namespace_uid"])
                        target_namespace = str(target_scope["fact_namespace_uid"])
                        if source_namespace != target_namespace:
                            duplicate_conflicts = await (
                                await db.execute(
                                    """
                                    SELECT c.conflict_uid FROM user_profile_conflicts c
                                    WHERE c.fact_namespace_uid = ? AND EXISTS (
                                        SELECT 1 FROM user_profile_conflicts t
                                        WHERE t.fact_namespace_uid = ?
                                          AND t.conflict_key = c.conflict_key
                                          AND t.status = c.status
                                    )
                                    """,
                                    (source_namespace, target_namespace),
                                )
                            ).fetchall()
                            for conflict in duplicate_conflicts:
                                await db.execute(
                                    "UPDATE user_profile_conflicts SET conflict_key = "
                                    "conflict_key || ':' || ? WHERE conflict_uid = ?",
                                    (
                                        source_scope_uid[:8],
                                        str(conflict["conflict_uid"]),
                                    ),
                                )
                            await db.execute(
                                "UPDATE user_profile_facts SET fact_namespace_uid = ? "
                                "WHERE fact_namespace_uid = ?",
                                (target_namespace, source_namespace),
                            )
                            await db.execute(
                                "UPDATE user_profile_conflicts SET fact_namespace_uid = ? "
                                "WHERE fact_namespace_uid = ?",
                                (target_namespace, source_namespace),
                            )
                            await db.execute(
                                "UPDATE user_profile_fact_overrides SET fact_namespace_uid = ? "
                                "WHERE fact_namespace_uid = ?",
                                (target_namespace, source_namespace),
                            )
                            await db.execute(
                                "UPDATE user_profile_fact_namespaces SET "
                                "current_revision = current_revision + 1, updated_at = ? "
                                "WHERE fact_namespace_uid = ?",
                                (now, target_namespace),
                            )
                        await db.execute(
                            "UPDATE user_profile_projection_events SET profile_scope_uid = ?, "
                            "updated_at = ? WHERE profile_scope_uid = ?",
                            (target_scope_uid, now, source_scope_uid),
                        )
                        await db.execute(
                            "UPDATE user_profile_tasks SET status = 'cancelled', "
                            "error = 'Account binding merged this scope', completed_at = ?, "
                            "updated_at = ? WHERE profile_scope_uid = ? AND status NOT IN "
                            "('completed', 'completed_partial', 'failed', 'cancelled')",
                            (now, now, source_scope_uid),
                        )
                        await db.execute(
                            "UPDATE user_profile_scopes SET enabled = 0, "
                            "auto_enable_blocked = 1, has_gap = 1, updated_at = ? "
                            "WHERE profile_scope_uid = ?",
                            (now, source_scope_uid),
                        )
                        await db.execute(
                            "UPDATE user_profile_scopes SET has_gap = 1, updated_at = ? "
                            "WHERE profile_scope_uid = ?",
                            (now, target_scope_uid),
                        )
                        affected_target_scopes.add(target_scope_uid)
                    await db.execute(
                        "UPDATE user_profile_accounts SET logical_user_uid = ?, "
                        "linked_manually = 1, updated_at = ? WHERE logical_user_uid = ?",
                        (target_user_uid, now, source_user_uid),
                    )
                    await db.execute(
                        "UPDATE user_profile_users SET status = 'deleted', updated_at = ? "
                        "WHERE logical_user_uid = ?",
                        (now, source_user_uid),
                    )
                await db.execute(
                    "UPDATE user_profile_users SET status = 'active', updated_at = ? "
                    "WHERE logical_user_uid = ?",
                    (now, target_user_uid),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "logical_user_uid": target_user_uid,
            "affected_scope_uids": sorted(affected_target_scopes),
            "requires_rebuild": bool(affected_target_scopes),
        }

    async def preview_account_unbind(self, actor_id: str) -> dict[str, Any]:
        async with self._connect() as db:
            account = await (
                await db.execute(
                    "SELECT * FROM user_profile_accounts WHERE actor_id = ?",
                    (actor_id,),
                )
            ).fetchone()
            if account is None:
                raise ValueError("Unknown profile account")
            accounts = await (
                await db.execute(
                    "SELECT * FROM user_profile_accounts WHERE logical_user_uid = ?",
                    (str(account["logical_user_uid"]),),
                )
            ).fetchall()
            if len(accounts) < 2:
                raise ValueError("This account is not bound to another account")
            scopes = await (
                await db.execute(
                    """
                    SELECT s.*, n.current_revision, n.share_group_uid,
                           (SELECT COUNT(*) FROM user_profile_fact_sources src
                            JOIN user_profile_facts f
                              ON f.profile_fact_uid = src.profile_fact_uid
                            WHERE f.fact_namespace_uid = s.fact_namespace_uid
                              AND src.source_account_actor_id = ? AND src.active = 1)
                              AS source_count
                    FROM user_profile_scopes s
                    JOIN user_profile_fact_namespaces n
                      ON n.fact_namespace_uid = s.fact_namespace_uid
                    WHERE s.logical_user_uid = ?
                    """,
                    (actor_id, str(account["logical_user_uid"])),
                )
            ).fetchall()
        blocked = any(row["share_group_uid"] for row in scopes)
        payload = {
            "account": (
                actor_id,
                str(account["logical_user_uid"]),
                float(account["updated_at"]),
            ),
            "scopes": [
                (
                    str(row["profile_scope_uid"]),
                    str(row["fact_namespace_uid"]),
                    int(row["current_revision"]),
                    float(row["updated_at"]),
                )
                for row in scopes
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "actor_id": actor_id,
            "logical_user_uid": str(account["logical_user_uid"]),
            "remaining_account_count": len(accounts) - 1,
            "new_scope_count": len(scopes),
            "source_count": sum(int(row["source_count"]) for row in scopes),
            "blocked_reason": (
                "Remove objective share-group membership before unbinding this account"
                if blocked
                else None
            ),
            "fingerprint": fingerprint,
        }

    async def unbind_account(
        self, actor_id: str, *, expected_fingerprint: str
    ) -> dict[str, Any]:
        preview = await self.preview_account_unbind(actor_id)
        if preview["fingerprint"] != str(expected_fingerprint):
            raise UserProfileRevisionConflict("Account unbind preview is stale")
        if preview["blocked_reason"]:
            raise ValueError(str(preview["blocked_reason"]))
        old_user_uid = str(preview["logical_user_uid"])
        new_user_uid = str(uuid.uuid4())
        now = time.time()
        created_scope_uids: list[str] = []
        affected_old_scope_uids: list[str] = []
        async with self._connect() as db:
            try:
                await db.execute(
                    "INSERT INTO user_profile_users "
                    "(logical_user_uid, status, created_at, updated_at, metadata) "
                    "VALUES (?, 'active', ?, ?, '{}')",
                    (new_user_uid, now, now),
                )
                await db.execute(
                    "UPDATE user_profile_accounts SET logical_user_uid = ?, "
                    "linked_manually = 0, updated_at = ? WHERE actor_id = ?",
                    (new_user_uid, now, actor_id),
                )
                old_scopes = await (
                    await db.execute(
                        "SELECT * FROM user_profile_scopes WHERE logical_user_uid = ?",
                        (old_user_uid,),
                    )
                ).fetchall()
                for old_scope in old_scopes:
                    old_scope_uid = str(old_scope["profile_scope_uid"])
                    affected_old_scope_uids.append(old_scope_uid)
                    new_scope = UserProfileScope(
                        logical_user_uid=new_user_uid,
                        bot_account=str(old_scope["bot_account"]),
                        persona_id=str(old_scope["persona_id"]),
                        has_gap=True,
                    )
                    await db.execute(
                        "INSERT INTO user_profile_fact_namespaces "
                        "(fact_namespace_uid, current_revision, created_at, updated_at) "
                        "VALUES (?, 0, ?, ?)",
                        (new_scope.fact_namespace_uid, now, now),
                    )
                    await db.execute(
                        """
                        INSERT INTO user_profile_scopes (
                            profile_scope_uid, logical_user_uid, bot_account, persona_id,
                            fact_namespace_uid, enabled, has_gap, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
                        """,
                        (
                            new_scope.profile_scope_uid,
                            new_user_uid,
                            new_scope.bot_account,
                            new_scope.persona_id,
                            new_scope.fact_namespace_uid,
                            now,
                            now,
                        ),
                    )
                    created_scope_uids.append(new_scope.profile_scope_uid)
                    events = await (
                        await db.execute(
                            "SELECT event_uid, payload FROM user_profile_projection_events "
                            "WHERE profile_scope_uid = ?",
                            (old_scope_uid,),
                        )
                    ).fetchall()
                    for event in events:
                        payload = self._json_object(event["payload"])
                        if str(payload.get("profile_actor_id") or "") != actor_id:
                            continue
                        await db.execute(
                            "UPDATE user_profile_projection_events SET profile_scope_uid = ?, "
                            "status = 'pending', error = NULL, updated_at = ? "
                            "WHERE event_uid = ?",
                            (new_scope.profile_scope_uid, now, str(event["event_uid"])),
                        )
                    old_namespace = str(old_scope["fact_namespace_uid"])
                    await db.execute(
                        """
                        UPDATE user_profile_fact_sources
                        SET active = 0, updated_at = ?
                        WHERE source_account_actor_id = ? AND active = 1
                          AND profile_fact_uid IN (
                              SELECT profile_fact_uid FROM user_profile_facts
                              WHERE fact_namespace_uid = ?
                          )
                        """,
                        (now, actor_id, old_namespace),
                    )
                    await db.execute(
                        """
                        UPDATE user_profile_facts
                        SET status = 'archived', updated_at = ?
                        WHERE fact_namespace_uid = ? AND NOT EXISTS (
                            SELECT 1 FROM user_profile_fact_sources src
                            WHERE src.profile_fact_uid = user_profile_facts.profile_fact_uid
                              AND src.active = 1
                        )
                        """,
                        (now, old_namespace),
                    )
                    await db.execute(
                        "UPDATE user_profile_fact_namespaces SET "
                        "current_revision = current_revision + 1, updated_at = ? "
                        "WHERE fact_namespace_uid = ?",
                        (now, old_namespace),
                    )
                    await db.execute(
                        "UPDATE user_profile_projection_events SET status = 'pending', "
                        "error = NULL, updated_at = ? WHERE profile_scope_uid = ?",
                        (now, old_scope_uid),
                    )
                    await db.execute(
                        "UPDATE user_profile_tasks SET status = 'cancelled', "
                        "error = 'Account unbind requires reprojection', completed_at = ?, "
                        "updated_at = ? WHERE profile_scope_uid = ? AND status NOT IN "
                        "('completed', 'completed_partial', 'failed', 'cancelled')",
                        (now, now, old_scope_uid),
                    )
                    await db.execute(
                        "UPDATE user_profile_scopes SET has_gap = 1, updated_at = ? "
                        "WHERE profile_scope_uid = ?",
                        (now, old_scope_uid),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "actor_id": actor_id,
            "logical_user_uid": new_user_uid,
            "created_scope_uids": created_scope_uids,
            "affected_old_scope_uids": affected_old_scope_uids,
            "requires_rebuild": True,
        }

    async def preview_share_group(
        self,
        *,
        profile_scope_uids: Iterable[str],
        share_group_uid: str | None = None,
    ) -> dict[str, Any]:
        scope_uids = list(
            dict.fromkeys(str(value) for value in profile_scope_uids if value)
        )
        if len(scope_uids) < 2:
            raise ValueError("An objective share group requires at least two scopes")
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT s.*, n.current_revision, n.share_group_uid,
                           (SELECT COUNT(*) FROM user_profile_facts f
                            WHERE f.fact_namespace_uid = s.fact_namespace_uid) AS fact_count,
                           (SELECT COUNT(*) FROM user_profile_conflicts c
                            WHERE c.fact_namespace_uid = s.fact_namespace_uid
                              AND c.status = 'open') AS conflict_count
                    FROM user_profile_scopes s
                    JOIN user_profile_fact_namespaces n
                      ON n.fact_namespace_uid = s.fact_namespace_uid
                    WHERE s.profile_scope_uid IN ({','.join('?' for _ in scope_uids)})
                    """,
                    scope_uids,
                )
            ).fetchall()
            if len(rows) != len(scope_uids):
                raise ValueError("One or more user-profile scopes do not exist")
            logical_users = {str(row["logical_user_uid"]) for row in rows}
            if len(logical_users) != 1:
                raise ValueError("Objective sharing is limited to one logical user")
            blocked = [
                str(row["profile_scope_uid"])
                for row in rows
                if row["share_group_uid"]
                and str(row["share_group_uid"]) != str(share_group_uid or "")
            ]
            category_rows = await (
                await db.execute(
                    f"""
                    SELECT category, COUNT(DISTINCT raw_fact) AS variants
                    FROM user_profile_facts f
                    JOIN user_profile_fact_sources src
                      ON src.source_uid = f.representative_source_uid
                    WHERE f.fact_namespace_uid IN (
                        {','.join('?' for _ in rows)}
                    ) AND f.status IN ('active', 'conflict')
                    GROUP BY category HAVING variants > 1
                    """,
                    [str(row["fact_namespace_uid"]) for row in rows],
                )
            ).fetchall()
        state = [
            (
                str(row["profile_scope_uid"]),
                str(row["fact_namespace_uid"]),
                int(row["current_revision"]),
                str(row["share_group_uid"] or ""),
                float(row["updated_at"]),
            )
            for row in rows
        ]
        fingerprint = hashlib.sha256(
            json.dumps(state, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "share_group_uid": share_group_uid,
            "logical_user_uid": next(iter(logical_users)),
            "profile_scope_uids": scope_uids,
            "fact_count": sum(int(row["fact_count"]) for row in rows),
            "open_conflict_count": sum(int(row["conflict_count"]) for row in rows),
            "potential_conflict_categories": [
                {"category": str(row["category"]), "variants": int(row["variants"])}
                for row in category_rows
            ],
            "blocked_scope_uids": blocked,
            "fingerprint": fingerprint,
        }

    async def save_share_group(
        self,
        *,
        name: str,
        profile_scope_uids: Iterable[str],
        expected_fingerprint: str,
        share_group_uid: str | None = None,
    ) -> dict[str, Any]:
        preview = await self.preview_share_group(
            profile_scope_uids=profile_scope_uids,
            share_group_uid=share_group_uid,
        )
        if preview["fingerprint"] != str(expected_fingerprint):
            raise UserProfileRevisionConflict("Share-group preview is stale")
        if preview["blocked_scope_uids"]:
            raise ValueError("A selected scope already belongs to another share group")
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Share-group name is required")
        group_uid = str(share_group_uid or uuid.uuid4())
        selected = list(preview["profile_scope_uids"])
        now = time.time()
        detached_scope_uids: list[str] = []
        async with self._connect() as db:
            try:
                group = await (
                    await db.execute(
                        "SELECT * FROM user_profile_share_groups WHERE share_group_uid = ?",
                        (group_uid,),
                    )
                ).fetchone()
                if group is None:
                    group_namespace = f"profile-shared-facts-v1-{uuid.uuid4()}"
                    await db.execute(
                        "INSERT INTO user_profile_fact_namespaces "
                        "(fact_namespace_uid, current_revision, share_group_uid, "
                        "created_at, updated_at) VALUES (?, 0, ?, ?, ?)",
                        (group_namespace, group_uid, now, now),
                    )
                    await db.execute(
                        """
                        INSERT INTO user_profile_share_groups (
                            share_group_uid, name, fact_namespace_uid,
                            created_at, updated_at, metadata
                        ) VALUES (?, ?, ?, ?, ?, '{}')
                        """,
                        (group_uid, clean_name[:200], group_namespace, now, now),
                    )
                else:
                    group_namespace = str(group["fact_namespace_uid"])
                    await db.execute(
                        "UPDATE user_profile_share_groups SET name = ?, updated_at = ? "
                        "WHERE share_group_uid = ?",
                        (clean_name[:200], now, group_uid),
                    )
                    current_members = await (
                        await db.execute(
                            "SELECT profile_scope_uid FROM user_profile_share_members "
                            "WHERE share_group_uid = ?",
                            (group_uid,),
                        )
                    ).fetchall()
                    removed = [
                        str(row["profile_scope_uid"])
                        for row in current_members
                        if str(row["profile_scope_uid"]) not in selected
                    ]
                    for scope_uid in removed:
                        new_namespace = f"profile-facts-v1-{uuid.uuid4()}"
                        await db.execute(
                            "INSERT INTO user_profile_fact_namespaces "
                            "(fact_namespace_uid, current_revision, created_at, updated_at) "
                            "VALUES (?, 0, ?, ?)",
                            (new_namespace, now, now),
                        )
                        await db.execute(
                            "UPDATE user_profile_scopes SET fact_namespace_uid = ?, "
                            "has_gap = 1, updated_at = ? WHERE profile_scope_uid = ?",
                            (new_namespace, now, scope_uid),
                        )
                        detached_scope_uids.append(scope_uid)
                    if removed:
                        await db.execute(
                            f"DELETE FROM user_profile_share_members "
                            f"WHERE share_group_uid = ? AND profile_scope_uid IN "
                            f"({','.join('?' for _ in removed)})",
                            [group_uid, *removed],
                        )

                scopes = await (
                    await db.execute(
                        f"SELECT * FROM user_profile_scopes WHERE profile_scope_uid IN "
                        f"({','.join('?' for _ in selected)})",
                        selected,
                    )
                ).fetchall()
                for scope in scopes:
                    scope_uid = str(scope["profile_scope_uid"])
                    source_namespace = str(scope["fact_namespace_uid"])
                    if source_namespace != group_namespace:
                        duplicate_conflicts = await (
                            await db.execute(
                                """
                                SELECT c.conflict_uid FROM user_profile_conflicts c
                                WHERE c.fact_namespace_uid = ? AND EXISTS (
                                    SELECT 1 FROM user_profile_conflicts t
                                    WHERE t.fact_namespace_uid = ?
                                      AND t.conflict_key = c.conflict_key
                                      AND t.status = c.status
                                )
                                """,
                                (source_namespace, group_namespace),
                            )
                        ).fetchall()
                        for conflict in duplicate_conflicts:
                            await db.execute(
                                "UPDATE user_profile_conflicts SET conflict_key = "
                                "conflict_key || ':' || ? WHERE conflict_uid = ?",
                                (scope_uid[:8], str(conflict["conflict_uid"])),
                            )
                        await db.execute(
                            "UPDATE user_profile_facts SET fact_namespace_uid = ? "
                            "WHERE fact_namespace_uid = ?",
                            (group_namespace, source_namespace),
                        )
                        await db.execute(
                            "UPDATE user_profile_conflicts SET fact_namespace_uid = ? "
                            "WHERE fact_namespace_uid = ?",
                            (group_namespace, source_namespace),
                        )
                        await db.execute(
                            "UPDATE user_profile_fact_overrides SET fact_namespace_uid = ? "
                            "WHERE fact_namespace_uid = ?",
                            (group_namespace, source_namespace),
                        )
                    await db.execute(
                        "UPDATE user_profile_scopes SET fact_namespace_uid = ?, "
                        "has_gap = 1, updated_at = ? WHERE profile_scope_uid = ?",
                        (group_namespace, now, scope_uid),
                    )
                    await db.execute(
                        "INSERT INTO user_profile_share_members "
                        "(share_group_uid, profile_scope_uid, created_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(profile_scope_uid) DO UPDATE SET "
                        "share_group_uid = excluded.share_group_uid",
                        (group_uid, scope_uid, now),
                    )
                await db.execute(
                    "UPDATE user_profile_fact_namespaces SET share_group_uid = ?, "
                    "current_revision = current_revision + 1, updated_at = ? "
                    "WHERE fact_namespace_uid = ?",
                    (group_uid, now, group_namespace),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "share_group_uid": group_uid,
            "fact_namespace_uid": group_namespace,
            "profile_scope_uids": selected,
            "detached_scope_uids": detached_scope_uids,
            "requires_rebuild": bool(detached_scope_uids),
        }

    async def list_profiles(
        self,
        *,
        search: str = "",
        status: str | None = None,
        bot_account: str | None = None,
        persona_id: str | None = None,
        platform: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        clean_search = str(search).strip()
        if clean_search:
            where.append(
                "(a.actor_id LIKE ? ESCAPE '\\' OR a.last_observed_name LIKE ? ESCAPE '\\' "
                "OR u.display_name_override LIKE ? ESCAPE '\\')"
            )
            pattern = f"%{self._escape_like(clean_search)}%"
            params.extend((pattern, pattern, pattern))
        if status:
            if status == "enabled":
                where.append("s.enabled = 1")
            elif status == "disabled":
                where.append("s.enabled = 0 AND s.auto_enable_blocked = 0")
            elif status == "deleted":
                where.append("s.auto_enable_blocked = 1")
            else:
                where.append("u.status = ?")
                params.append(status)
        if bot_account:
            where.append("s.bot_account = ?")
            params.append(str(bot_account))
        if persona_id:
            where.append("s.persona_id = ?")
            params.append(str(persona_id))
        if platform:
            where.append(
                "EXISTS (SELECT 1 FROM user_profile_accounts af "
                "WHERE af.logical_user_uid = s.logical_user_uid AND af.platform = ?)"
            )
            params.append(str(platform))
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        params.extend((max(1, min(500, int(limit))), max(0, int(offset))))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT s.*, u.status AS user_status, u.display_name_override,
                           MIN(a.actor_id) AS actor_id, MIN(a.platform) AS platform,
                           MIN(a.stable_user_id) AS stable_user_id,
                           COALESCE(u.display_name_override,
                                    MAX(a.last_observed_name), MIN(a.actor_id)) AS display_name,
                           COUNT(DISTINCT a.actor_id) AS account_count,
                           COUNT(DISTINCT CASE WHEN f.status = 'active' THEN f.profile_fact_uid END)
                               AS active_fact_count,
                           COUNT(DISTINCT CASE WHEN f.status = 'pending' THEN f.profile_fact_uid END)
                               AS pending_fact_count,
                           COUNT(DISTINCT CASE WHEN f.status = 'conflict' THEN f.profile_fact_uid END)
                               AS conflict_fact_count,
                           COUNT(DISTINCT CASE WHEN f.status = 'stale' THEN f.profile_fact_uid END)
                               AS stale_fact_count,
                           r.revision AS relationship_revision,
                           r.updated_at AS relationship_updated_at,
                           n.current_revision AS fact_revision,
                           n.share_group_uid,
                           COUNT(DISTINCT CASE WHEN t.status NOT IN (
                               'completed', 'completed_partial', 'failed', 'cancelled'
                           ) THEN t.task_uid END) AS running_task_count
                    FROM user_profile_scopes s
                    JOIN user_profile_users u ON u.logical_user_uid = s.logical_user_uid
                    JOIN user_profile_fact_namespaces n
                      ON n.fact_namespace_uid = s.fact_namespace_uid
                    LEFT JOIN user_profile_accounts a
                      ON a.logical_user_uid = s.logical_user_uid
                    LEFT JOIN user_profile_facts f
                      ON f.fact_namespace_uid = s.fact_namespace_uid
                    LEFT JOIN user_relationship_states r
                      ON r.profile_scope_uid = s.profile_scope_uid
                    LEFT JOIN user_profile_tasks t
                      ON t.profile_scope_uid = s.profile_scope_uid
                    {where_sql}
                    GROUP BY s.profile_scope_uid
                    ORDER BY s.updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def profile_detail(self, profile_scope_uid: str) -> dict[str, Any] | None:
        scope = await self.get_scope(profile_scope_uid)
        if scope is None:
            return None
        facts = await self.list_facts_for_maintenance(scope.fact_namespace_uid)
        relationship = await self.get_relationship(profile_scope_uid)
        async with self._connect() as db:
            accounts = await (
                await db.execute(
                    "SELECT * FROM user_profile_accounts WHERE logical_user_uid = ? "
                    "ORDER BY platform, stable_user_id",
                    (scope.logical_user_uid,),
                )
            ).fetchall()
            conflicts = await (
                await db.execute(
                    "SELECT * FROM user_profile_conflicts "
                    "WHERE fact_namespace_uid = ? ORDER BY updated_at DESC",
                    (scope.fact_namespace_uid,),
                )
            ).fetchall()
            sources = await (
                await db.execute(
                    """
                    SELECT src.* FROM user_profile_fact_sources src
                    JOIN user_profile_facts f
                      ON f.profile_fact_uid = src.profile_fact_uid
                    WHERE f.fact_namespace_uid = ?
                    ORDER BY src.evidence_ended_at DESC, src.updated_at DESC
                    """,
                    (scope.fact_namespace_uid,),
                )
            ).fetchall()
            overrides = await (
                await db.execute(
                    "SELECT * FROM user_profile_fact_overrides "
                    "WHERE fact_namespace_uid = ? ORDER BY updated_at DESC",
                    (scope.fact_namespace_uid,),
                )
            ).fetchall()
            namespace = await (
                await db.execute(
                    "SELECT * FROM user_profile_fact_namespaces "
                    "WHERE fact_namespace_uid = ?",
                    (scope.fact_namespace_uid,),
                )
            ).fetchone()
            share_group = None
            if namespace is not None and namespace["share_group_uid"]:
                share_group = await (
                    await db.execute(
                        "SELECT * FROM user_profile_share_groups "
                        "WHERE share_group_uid = ?",
                        (str(namespace["share_group_uid"]),),
                    )
                ).fetchone()
            member_rows = []
            if share_group is not None:
                member_rows = await (
                    await db.execute(
                        """
                        SELECT s.profile_scope_uid, s.bot_account, s.persona_id
                        FROM user_profile_share_members m
                        JOIN user_profile_scopes s
                          ON s.profile_scope_uid = m.profile_scope_uid
                        WHERE m.share_group_uid = ? ORDER BY s.bot_account, s.persona_id
                        """,
                        (str(share_group["share_group_uid"]),),
                    )
                ).fetchall()
        sources_by_fact: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            converted = dict(source)
            converted["active"] = bool(converted["active"])
            converted["timeline_quality"] = self._json_object(
                converted["timeline_quality"]
            )
            converted["metadata"] = self._json_object(converted["metadata"])
            sources_by_fact.setdefault(str(source["profile_fact_uid"]), []).append(
                converted
            )
        for fact in facts:
            fact["sources"] = sources_by_fact.get(str(fact["profile_fact_uid"]), [])
        tasks = await self.list_profile_tasks(profile_scope_uid, limit=50)
        revisions = (
            await self.list_relationship_revisions(relationship.relationship_uid)
            if relationship
            else []
        )
        gap = await self.profile_gap_status(profile_scope_uid)
        identity_reviews = await self.list_timeline_identity_resolutions(
            statuses=("pending_review", "ignored"),
            bot_account=scope.bot_account,
            persona_id=scope.persona_id,
            limit=500,
        )
        fingerprint = await self.profile_fingerprint(profile_scope_uid)
        return {
            "scope": asdict(scope),
            "fingerprint": fingerprint,
            "fact_revision": int(namespace["current_revision"] if namespace else 0),
            "gap": gap,
            "accounts": [
                {**dict(row), "observed_names": self._json_list(row["observed_names"])}
                for row in accounts
            ],
            "facts": facts,
            "conflicts": [
                {
                    **dict(row),
                    "fact_uids": self._json_list(row["fact_uids"]),
                    "metadata": self._json_object(row["metadata"]),
                }
                for row in conflicts
            ],
            "overrides": [
                {
                    **dict(row),
                    "active": bool(row["active"]),
                    "payload": self._json_object(row["payload"]),
                }
                for row in overrides
            ],
            "relationship": (
                self._relationship_state_dict(relationship) if relationship else None
            ),
            "relationship_revisions": revisions,
            "share_group": (
                {
                    **dict(share_group),
                    "metadata": self._json_object(share_group["metadata"]),
                    "members": [dict(row) for row in member_rows],
                }
                if share_group is not None
                else None
            ),
            "tasks": tasks,
            "identity_reviews": identity_reviews,
        }

    @staticmethod
    def _parse_human_actor_id(actor_id: str) -> tuple[str, str]:
        parts = str(actor_id or "").strip().split(":", 2)
        if len(parts) != 3 or parts[1] != "human" or not parts[0] or not parts[2]:
            return "", ""
        return parts[0], parts[2]

    @classmethod
    def _timeline_identity_row(cls, row: Any) -> dict[str, Any]:
        converted = dict(row)
        converted["evidence"] = cls._json_object(converted.pop("evidence_json", "{}"))
        return converted

    @staticmethod
    def _row_to_scope(row: aiosqlite.Row) -> UserProfileScope:
        return UserProfileScope(
            profile_scope_uid=str(row["profile_scope_uid"]),
            logical_user_uid=str(row["logical_user_uid"]),
            bot_account=str(row["bot_account"]),
            persona_id=str(row["persona_id"]),
            fact_namespace_uid=str(row["fact_namespace_uid"]),
            enabled=bool(row["enabled"]),
            auto_enable_blocked=bool(row["auto_enable_blocked"]),
            projection_cursor=int(row["projection_cursor"]),
            reset_after=row["reset_after"],
            has_gap=bool(row["has_gap"]),
            relationship_frozen=bool(row["relationship_frozen"]),
            relationship_reset_after=row["relationship_reset_after"],
            relationship_sensitivity_override=row["relationship_sensitivity_override"],
            relationship_behavior_override=row["relationship_behavior_override"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @classmethod
    def _row_to_relationship(cls, row: aiosqlite.Row) -> UserRelationshipState:
        return UserRelationshipState(
            relationship_uid=str(row["relationship_uid"]),
            profile_scope_uid=str(row["profile_scope_uid"]),
            revision=int(row["revision"]),
            familiarity=float(row["familiarity"]),
            trust=float(row["trust"]),
            warmth=float(row["warmth"]),
            ease=float(row["ease"]),
            tension=float(row["tension"]),
            concern=float(row["concern"]),
            stance_tags=[str(item) for item in cls._json_list(row["stance_tags"])],
            subjective_summary=str(row["subjective_summary"] or ""),
            recent_aftereffect=str(row["recent_aftereffect"] or ""),
            aftereffect_expires_at=row["aftereffect_expires_at"],
            persona_signature=cls._json_object(row["persona_signature"]),
            source_timeline_uids=[
                str(item) for item in cls._json_list(row["source_timeline_uids"])
            ],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @classmethod
    def _relationship_row_dict(cls, row: aiosqlite.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        return cls._relationship_state_dict(cls._row_to_relationship(row))

    @staticmethod
    def _relationship_state_dict(state: UserRelationshipState) -> dict[str, Any]:
        result = asdict(state)
        result.update(state.dimensions())
        return result

    @classmethod
    def _fact_row(cls, row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        result["sensitive"] = bool(result["sensitive"])
        result["admin_confirmed"] = bool(result["admin_confirmed"])
        result["pinned"] = bool(result["pinned"])
        result["metadata"] = cls._json_object(result["metadata"])
        return result

    @classmethod
    def _row_to_fact_source(cls, row: aiosqlite.Row) -> UserProfileFactSource:
        return UserProfileFactSource(
            source_uid=str(row["source_uid"]),
            profile_fact_uid=(
                str(row["profile_fact_uid"]) if row["profile_fact_uid"] else None
            ),
            timeline_uid=str(row["timeline_uid"]),
            timeline_revision=int(row["timeline_revision"]),
            fact_index=int(row["fact_index"]),
            fact_fingerprint=str(row["fact_fingerprint"]),
            raw_fact=str(row["raw_fact"]),
            actor_id=str(row["actor_id"]),
            claim_type=str(row["claim_type"]),
            attribution_confidence=float(row["attribution_confidence"]),
            timeline_quality=cls._json_object(row["timeline_quality"]),
            evidence_started_at=row["evidence_started_at"],
            evidence_ended_at=row["evidence_ended_at"],
            source_account_actor_id=str(row["source_account_actor_id"] or ""),
            metadata=cls._json_object(row["metadata"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @classmethod
    def _event_row(cls, row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = cls._json_object(result["payload"])
        return result

    @staticmethod
    def _enum(value: Any) -> str:
        return str(value.value if isinstance(value, Enum) else value)

    @staticmethod
    def _score(value: Any) -> float:
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return list(value)
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["UserProfileRevisionConflict", "UserProfileStore"]
