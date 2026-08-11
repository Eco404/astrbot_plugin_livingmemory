"""Transactional SQLite storage for private-chat user profiles."""

from __future__ import annotations

import json
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
                persona_signature TEXT NOT NULL DEFAULT '{}',
                persona_prompt TEXT NOT NULL DEFAULT '',
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
                observed = self._json_list(names_row["observed_names"] if names_row else None)
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
                if expected_revision is not None and current_revision != expected_revision:
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
    ) -> list[dict[str, Any]]:
        statuses = ["active"]
        if include_pending:
            statuses.append("pending")
        placeholders = ",".join("?" for _ in statuses)
        sensitive_clause = "" if include_sensitive else "AND f.sensitive = 0"
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
                    """,
                    [fact_namespace_uid, *statuses],
                )
            ).fetchall()
        return [self._fact_row(row) for row in rows]

    async def list_facts_for_maintenance(
        self, fact_namespace_uid: str
    ) -> list[dict[str, Any]]:
        """Return every current logical fact with its immutable display source."""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT f.*, s.raw_fact, s.actor_id, s.timeline_uid,
                           s.timeline_revision, s.fact_index, s.claim_type,
                           s.evidence_started_at, s.evidence_ended_at,
                           s.active AS representative_source_active
                    FROM user_profile_facts f
                    LEFT JOIN user_profile_fact_sources s
                      ON s.source_uid = f.representative_source_uid
                    WHERE f.fact_namespace_uid = ?
                    ORDER BY f.updated_at DESC, f.profile_fact_uid
                    """,
                    (fact_namespace_uid,),
                )
            ).fetchall()
        return [self._fact_row(row) for row in rows]

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
                if expected_revision is not None and current_revision != expected_revision:
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
                            conflict_uid, fact_namespace_uid, topic_key, fact_uids,
                            status, first_detected_at, last_evidence_at,
                            resolution, resolution_reason, created_at, updated_at,
                            metadata
                        ) VALUES (?, ?, ?, ?, 'open', ?, ?, NULL, ?, ?, ?, '{}')
                        """,
                        (
                            conflict_uid,
                            fact_namespace_uid,
                            str(conflict.get("topic_key") or "")[:500],
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
                if expected_revision is not None and current_revision != expected_revision:
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

    async def enqueue_projection_event(
        self, event: UserProfileProjectionEvent
    ) -> str:
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

    async def list_recoverable_tasks(self, *, limit: int = 64) -> list[dict[str, Any]]:
        now = time.time()
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT task_uid FROM user_profile_tasks
                    WHERE status IN (
                        'pending', 'running_facts', 'facts_completed',
                        'facts_failed', 'running_relationship'
                    ) AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY created_at ASC LIMIT ?
                    """,
                    (now, max(1, min(1000, int(limit)))),
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
                        provider_signature, persona_signature, persona_prompt,
                        retries, error, result_summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_uid,
                        task.profile_scope_uid,
                        self._enum(task.status),
                        self._json(task.settings_snapshot),
                        self._json(task.provider_signature),
                        self._json(task.persona_signature),
                        task.persona_prompt,
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
            "persona_signature",
            "result_summary",
        ):
            result[key] = self._json_object(result[key])
        result["items"] = []
        for item in items:
            converted = dict(item)
            converted["result"] = self._json_object(converted["result"])
            converted["event_payload"] = self._json_object(
                converted["event_payload"]
            )
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
        clear_persona_prompt: bool = False,
    ) -> None:
        completed = status in {"completed", "completed_partial", "failed", "cancelled"}
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE user_profile_tasks
                SET status = ?, error = ?,
                    result_summary = COALESCE(?, result_summary),
                    retries = COALESCE(?, retries), next_retry_at = ?,
                    persona_prompt = CASE WHEN ? THEN '' ELSE persona_prompt END,
                    updated_at = ?, completed_at = CASE WHEN ? THEN ? ELSE NULL END
                WHERE task_uid = ?
                """,
                (
                    status,
                    error,
                    self._json(result_summary) if result_summary is not None else None,
                    retries,
                    next_retry_at,
                    int(clear_persona_prompt),
                    time.time(),
                    int(completed),
                    time.time(),
                    task_uid,
                ),
            )
            await db.commit()

    async def list_profiles(
        self,
        *,
        search: str = "",
        status: str | None = None,
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
            where.append("u.status = ?")
            params.append(status)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        params.extend((max(1, min(500, int(limit))), max(0, int(offset))))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT s.*, u.status AS user_status, u.display_name_override,
                           a.actor_id, a.platform, a.stable_user_id,
                           a.last_observed_name,
                           COUNT(DISTINCT CASE WHEN f.status = 'active' THEN f.profile_fact_uid END)
                               AS active_fact_count,
                           COUNT(DISTINCT CASE WHEN f.status = 'pending' THEN f.profile_fact_uid END)
                               AS pending_fact_count,
                           COUNT(DISTINCT CASE WHEN f.status = 'conflict' THEN f.profile_fact_uid END)
                               AS conflict_fact_count,
                           r.revision AS relationship_revision,
                           r.updated_at AS relationship_updated_at
                    FROM user_profile_scopes s
                    JOIN user_profile_users u ON u.logical_user_uid = s.logical_user_uid
                    LEFT JOIN user_profile_accounts a
                      ON a.logical_user_uid = s.logical_user_uid
                    LEFT JOIN user_profile_facts f
                      ON f.fact_namespace_uid = s.fact_namespace_uid
                    LEFT JOIN user_relationship_states r
                      ON r.profile_scope_uid = s.profile_scope_uid
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
        facts = await self.list_serving_facts(
            scope.fact_namespace_uid, include_pending=True
        )
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
        return {
            "scope": asdict(scope),
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
            "relationship": (
                self._relationship_state_dict(relationship) if relationship else None
            ),
        }

    @staticmethod
    def _parse_human_actor_id(actor_id: str) -> tuple[str, str]:
        parts = str(actor_id or "").strip().split(":", 2)
        if len(parts) != 3 or parts[1] != "human" or not parts[0] or not parts[2]:
            return "", ""
        return parts[0], parts[2]

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
            relationship_sensitivity_override=row[
                "relationship_sensitivity_override"
            ],
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
