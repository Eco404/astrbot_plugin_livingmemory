"""Transactional SQLite storage for derived topic memories."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import aiosqlite

from ..core.models.topic_memory import (
    TopicActorLink,
    TopicActorRef,
    TopicAtomActorLink,
    TopicAtomSource,
    TopicLinkStatus,
    TopicMaintenanceMode,
    TopicMaintenanceRun,
    TopicMaintenanceStatus,
    TopicMemory,
    TopicMemoryAtom,
    TopicMemoryStatus,
    TopicFragmentLink,
    TopicRelation,
    TopicTimelineLink,
    TimelineTopicCandidate,
    TopicCandidateGroup,
    TopicFragmentDraft,
)


class TopicRevisionConflict(RuntimeError):
    """Raised when a maintenance worker writes an obsolete topic snapshot."""


class TopicSourceValidationError(ValueError):
    """Raised when topic provenance crosses a memory-space boundary."""


class TopicMemoryStore:
    """Persist generated topics, their own atoms, and Timeline provenance."""

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
            await self.create_tables(db)
            now = time.time()
            await db.execute(
                """
                UPDATE topic_maintenance_runs
                SET status = 'pending', completed_at = NULL, updated_at = ?,
                    error = CASE
                        WHEN error IS NULL OR error = ''
                        THEN 'Plugin process stopped before the build completed'
                        ELSE error
                    END
                WHERE status = 'running'
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE topic_build_group_jobs
                SET status = 'failed', completed_at = ?, updated_at = ?,
                    error = CASE
                        WHEN error IS NULL OR error = ''
                        THEN 'Plugin process stopped during this group'
                        ELSE error
                    END
                WHERE status = 'running'
                """,
                (now, now),
            )
            await db.commit()

    @staticmethod
    async def create_tables(db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_memories (
                topic_uid TEXT PRIMARY KEY,
                memory_space_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                status TEXT NOT NULL DEFAULT 'active',
                base_importance REAL NOT NULL DEFAULT 0.5,
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.7,
                started_at REAL,
                ended_at REAL,
                last_accessed_at REAL,
                access_count INTEGER NOT NULL DEFAULT 0,
                decay_anchor_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_memories_space_status
            ON topic_memories(memory_space_id, status, updated_at DESC)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_memories_space_importance
            ON topic_memories(memory_space_id, importance DESC)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_memory_atoms (
                atom_uid TEXT PRIMARY KEY,
                topic_uid TEXT NOT NULL,
                atom_type TEXT NOT NULL DEFAULT 'unknown',
                content TEXT NOT NULL,
                canonical_content TEXT NOT NULL DEFAULT '',
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.7,
                status TEXT NOT NULL DEFAULT 'active',
                event_started_at REAL,
                event_ended_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(topic_uid) REFERENCES topic_memories(topic_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_atoms_topic_status
            ON topic_memory_atoms(topic_uid, status)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_timeline_links (
                topic_uid TEXT NOT NULL,
                timeline_uid TEXT NOT NULL,
                time_cluster_key TEXT NOT NULL,
                contribution_weight REAL NOT NULL DEFAULT 1.0,
                semantic_similarity REAL NOT NULL DEFAULT 1.0,
                temporal_affinity REAL NOT NULL DEFAULT 1.0,
                source_timeline_revision INTEGER NOT NULL,
                topic_revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(topic_uid, timeline_uid),
                FOREIGN KEY(topic_uid) REFERENCES topic_memories(topic_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(timeline_uid) REFERENCES memory_registry(memory_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_links_timeline_status
            ON topic_timeline_links(timeline_uid, status)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_links_cluster
            ON topic_timeline_links(topic_uid, time_cluster_key, status)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_atom_sources (
                source_uid TEXT PRIMARY KEY,
                topic_atom_uid TEXT NOT NULL,
                timeline_uid TEXT NOT NULL,
                source_atom_id INTEGER,
                source_atom_fingerprint TEXT,
                source_timeline_revision INTEGER NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'atom',
                contribution_weight REAL NOT NULL DEFAULT 1.0,
                created_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(topic_atom_uid) REFERENCES topic_memory_atoms(atom_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(timeline_uid) REFERENCES memory_registry(memory_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_atom_sources_timeline
            ON topic_atom_sources(timeline_uid, source_timeline_revision)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_atom_sources_atom
            ON topic_atom_sources(topic_atom_uid)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_relations (
                relation_uid TEXT PRIMARY KEY,
                memory_space_id TEXT NOT NULL,
                left_topic_uid TEXT NOT NULL,
                right_topic_uid TEXT NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'related_subtopic',
                confidence REAL NOT NULL DEFAULT 0.5,
                semantic_similarity REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'active',
                build_run_uid TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                UNIQUE(left_topic_uid, right_topic_uid, relation_type),
                CHECK(left_topic_uid <> right_topic_uid),
                FOREIGN KEY(left_topic_uid) REFERENCES topic_memories(topic_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(right_topic_uid) REFERENCES topic_memories(topic_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_relations_space_status
            ON topic_relations(memory_space_id, status, semantic_similarity DESC)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_relations_left
            ON topic_relations(left_topic_uid, status)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_relations_right
            ON topic_relations(right_topic_uid, status)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_maintenance_runs (
                run_uid TEXT PRIMARY KEY,
                memory_space_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                stage TEXT NOT NULL DEFAULT 'candidate_scan',
                cursor_memory_uid TEXT,
                current_group_index INTEGER NOT NULL DEFAULT 0,
                total_groups INTEGER NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                processed_items INTEGER NOT NULL DEFAULT 0,
                created_topics INTEGER NOT NULL DEFAULT 0,
                updated_topics INTEGER NOT NULL DEFAULT 0,
                failed_items INTEGER NOT NULL DEFAULT 0,
                started_at REAL,
                completed_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT,
                config TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_runs_space_status
            ON topic_maintenance_runs(memory_space_id, status, created_at DESC)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_maintenance_items (
                run_uid TEXT NOT NULL,
                timeline_uid TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'processed',
                time_cluster_key TEXT NOT NULL,
                candidate_payload TEXT NOT NULL,
                error TEXT,
                processed_at REAL NOT NULL,
                PRIMARY KEY(run_uid, timeline_uid),
                FOREIGN KEY(run_uid) REFERENCES topic_maintenance_runs(run_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(timeline_uid) REFERENCES memory_registry(memory_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_items_run_cluster
            ON topic_maintenance_items(run_uid, time_cluster_key, status)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_candidate_groups (
                group_uid TEXT PRIMARY KEY,
                run_uid TEXT NOT NULL,
                group_index INTEGER NOT NULL,
                memory_space_id TEXT NOT NULL,
                label TEXT NOT NULL,
                timeline_uids TEXT NOT NULL,
                time_cluster_keys TEXT NOT NULL,
                cohesion REAL NOT NULL DEFAULT 0.0,
                started_at REAL,
                ended_at REAL,
                shared_signals TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'preview',
                created_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                UNIQUE(run_uid, group_index),
                FOREIGN KEY(run_uid) REFERENCES topic_maintenance_runs(run_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_candidate_groups_run
            ON topic_candidate_groups(run_uid, group_index)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_build_group_jobs (
                run_uid TEXT NOT NULL,
                group_uid TEXT NOT NULL,
                group_index INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                input_hash TEXT,
                prompt_hash TEXT,
                provider_id TEXT,
                model_id TEXT,
                started_at REAL,
                completed_at REAL,
                error TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY(run_uid, group_uid),
                FOREIGN KEY(run_uid) REFERENCES topic_maintenance_runs(run_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(group_uid) REFERENCES topic_candidate_groups(group_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_build_jobs_run_status
            ON topic_build_group_jobs(run_uid, status, group_index)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_fragment_drafts (
                fragment_uid TEXT PRIMARY KEY,
                run_uid TEXT NOT NULL,
                candidate_group_uid TEXT NOT NULL,
                memory_space_id TEXT NOT NULL,
                label TEXT NOT NULL,
                summary TEXT NOT NULL,
                timeline_uids TEXT NOT NULL,
                source_revisions TEXT NOT NULL,
                facts TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '[]',
                time_cluster_keys TEXT NOT NULL DEFAULT '[]',
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.7,
                embedding TEXT NOT NULL DEFAULT '[]',
                started_at REAL,
                ended_at REAL,
                status TEXT NOT NULL DEFAULT 'draft',
                prompt_hash TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                provider_id TEXT,
                model_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(run_uid) REFERENCES topic_maintenance_runs(run_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(candidate_group_uid)
                    REFERENCES topic_candidate_groups(group_uid) ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_fragments_run_status
            ON topic_fragment_drafts(run_uid, status, candidate_group_uid)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_fragments_space
            ON topic_fragment_drafts(memory_space_id, status, updated_at DESC)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_fragments (
                fragment_uid TEXT PRIMARY KEY,
                run_uid TEXT NOT NULL,
                candidate_group_uid TEXT NOT NULL,
                memory_space_id TEXT NOT NULL,
                label TEXT NOT NULL,
                summary TEXT NOT NULL,
                timeline_uids TEXT NOT NULL,
                source_revisions TEXT NOT NULL,
                facts TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '[]',
                time_cluster_keys TEXT NOT NULL DEFAULT '[]',
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.7,
                embedding TEXT NOT NULL DEFAULT '[]',
                started_at REAL,
                ended_at REAL,
                status TEXT NOT NULL DEFAULT 'active',
                prompt_hash TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                provider_id TEXT,
                model_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_formal_topic_fragments_space
            ON topic_fragments(memory_space_id, status, updated_at DESC)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_fragment_links (
                topic_uid TEXT NOT NULL,
                fragment_uid TEXT NOT NULL,
                topic_revision INTEGER NOT NULL,
                contribution_weight REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(topic_uid, fragment_uid),
                FOREIGN KEY(topic_uid) REFERENCES topic_memories(topic_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(fragment_uid) REFERENCES topic_fragments(fragment_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_fragment_links_fragment
            ON topic_fragment_links(fragment_uid, status)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_fragment_links_topic_revision
            ON topic_fragment_links(topic_uid, topic_revision, status)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_actor_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_uid TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                display_name_snapshot TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                resolution_status TEXT NOT NULL DEFAULT 'resolved',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(topic_uid, actor_id, relation_type),
                FOREIGN KEY(topic_uid) REFERENCES topic_memories(topic_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_actor_topic
            ON topic_actor_links(topic_uid)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_actor_actor
            ON topic_actor_links(actor_id, relation_type)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_atom_actor_links (
                topic_atom_uid TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                fragment_uid TEXT NOT NULL,
                timeline_uid TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 1.0,
                metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (
                    topic_atom_uid, actor_id, relation_type,
                    fragment_uid, timeline_uid
                ),
                FOREIGN KEY(topic_atom_uid) REFERENCES topic_memory_atoms(atom_uid)
                    ON DELETE CASCADE,
                FOREIGN KEY(fragment_uid) REFERENCES topic_fragments(fragment_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_atom_actor_actor
            ON topic_atom_actor_links(actor_id, relation_type)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_atom_actor_fragment
            ON topic_atom_actor_links(fragment_uid, topic_atom_uid)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_build_decisions (
                decision_uid TEXT PRIMARY KEY,
                run_uid TEXT NOT NULL,
                topic_uid TEXT,
                action TEXT NOT NULL,
                fragment_uids TEXT NOT NULL,
                candidate_scores TEXT NOT NULL DEFAULT '{}',
                llm_output TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(run_uid) REFERENCES topic_maintenance_runs(run_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_build_decisions_run
            ON topic_build_decisions(run_uid, created_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_build_checkpoints (
                run_uid TEXT NOT NULL,
                checkpoint_key TEXT NOT NULL,
                stage TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(run_uid, checkpoint_key),
                FOREIGN KEY(run_uid) REFERENCES topic_maintenance_runs(run_uid)
                    ON DELETE CASCADE
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_build_checkpoints_run_stage
            ON topic_build_checkpoints(run_uid, stage, updated_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_setting_overrides (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                settings_revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS timeline_setting_overrides (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                settings_revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )

    async def get_topic_setting_overrides(self) -> dict[str, Any]:
        """Return only explicit runtime overrides; defaults never persist here."""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT setting_key, setting_value FROM topic_setting_overrides"
                )
            ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[str(row["setting_key"])] = json.loads(row["setting_value"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    async def get_timeline_setting_overrides(self) -> dict[str, Any]:
        """Return explicit Timeline runtime overrides and import markers."""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT setting_key, setting_value FROM timeline_setting_overrides"
                )
            ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[str(row["setting_key"])] = json.loads(row["setting_value"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    async def update_timeline_setting_overrides(
        self,
        changes: dict[str, Any],
        *,
        reset_keys: list[str] | None = None,
        reset_all: bool = False,
        settings_revision: int = 1,
    ) -> dict[str, Any]:
        """Atomically update sparse Timeline overrides."""
        now = time.time()
        async with self._connect() as db:
            try:
                if reset_all:
                    await db.execute(
                        "DELETE FROM timeline_setting_overrides "
                        "WHERE setting_key NOT LIKE '__%'"
                    )
                elif reset_keys:
                    placeholders = ",".join("?" * len(reset_keys))
                    await db.execute(
                        f"DELETE FROM timeline_setting_overrides "
                        f"WHERE setting_key IN ({placeholders})",
                        list(reset_keys),
                    )
                for key, value in changes.items():
                    await db.execute(
                        """
                        INSERT INTO timeline_setting_overrides (
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
                            self._to_json(value),
                            max(1, int(settings_revision)),
                            now,
                            now,
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_timeline_setting_overrides()

    async def update_topic_setting_overrides(
        self,
        changes: dict[str, Any],
        *,
        reset_keys: list[str] | None = None,
        reset_all: bool = False,
        settings_revision: int = 1,
    ) -> dict[str, Any]:
        """Atomically write sparse overrides and/or remove selected overrides."""
        now = time.time()
        async with self._connect() as db:
            try:
                if reset_all:
                    await db.execute(
                        "DELETE FROM topic_setting_overrides "
                        "WHERE setting_key NOT LIKE '__%'"
                    )
                elif reset_keys:
                    placeholders = ",".join("?" * len(reset_keys))
                    await db.execute(
                        f"DELETE FROM topic_setting_overrides "
                        f"WHERE setting_key IN ({placeholders})",
                        list(reset_keys),
                    )
                for key, value in changes.items():
                    await db.execute(
                        """
                        INSERT INTO topic_setting_overrides (
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
                            self._to_json(value),
                            max(1, int(settings_revision)),
                            now,
                            now,
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_topic_setting_overrides()

    async def save_topic_snapshot(
        self,
        topic: TopicMemory,
        *,
        atoms: list[TopicMemoryAtom],
        links: list[TopicTimelineLink],
        atom_sources: list[TopicAtomSource],
        actor_links: list[TopicActorLink] | None = None,
        atom_actor_links: list[TopicAtomActorLink] | None = None,
        fragments: list[TopicFragmentDraft] | None = None,
        expected_revision: int | None = None,
    ) -> TopicMemory:
        """Atomically replace one generated Topic snapshot and its provenance."""
        self._validate_topic(topic)
        self._validate_snapshot_members(topic.topic_uid, atoms, links, atom_sources)
        actor_links = list(actor_links or [])
        atom_actor_links = list(atom_actor_links or [])
        self._validate_actor_links(topic.topic_uid, atoms, actor_links, atom_actor_links)
        async with self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                saved = await self._save_topic_snapshot_tx(
                    db,
                    topic,
                    atoms=atoms,
                    links=links,
                    atom_sources=atom_sources,
                    actor_links=actor_links,
                    atom_actor_links=atom_actor_links,
                    fragments=fragments,
                    expected_revision=expected_revision,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return saved

    async def publish_topic_build(
        self,
        *,
        run_uid: str,
        memory_space_id: str,
        mode: TopicMaintenanceMode | str,
        snapshots: list[dict[str, Any]],
        relations: list[TopicRelation],
        affected_topic_uids: set[str] | None = None,
        reset_topics: bool = False,
    ) -> dict[str, Any]:
        """Publish one completed build as a single visible database change."""
        run_uid = str(run_uid or "").strip()
        memory_space_id = str(memory_space_id or "").strip()
        mode = TopicMaintenanceMode(mode)
        if not run_uid or not memory_space_id:
            raise ValueError("run_uid and memory_space_id are required")
        normalized_snapshots: list[dict[str, Any]] = []
        for snapshot in snapshots:
            topic = snapshot["topic"]
            atoms = list(snapshot.get("atoms") or [])
            links = list(snapshot.get("links") or [])
            atom_sources = list(snapshot.get("atom_sources") or [])
            actor_links = list(snapshot.get("actor_links") or [])
            atom_actor_links = list(snapshot.get("atom_actor_links") or [])
            self._validate_topic(topic)
            if topic.memory_space_id != memory_space_id:
                raise TopicSourceValidationError(
                    "Topic snapshot belongs to another memory space"
                )
            self._validate_snapshot_members(
                topic.topic_uid, atoms, links, atom_sources
            )
            self._validate_actor_links(
                topic.topic_uid, atoms, actor_links, atom_actor_links
            )
            normalized_snapshots.append(
                {
                    **snapshot,
                    "atoms": atoms,
                    "links": links,
                    "atom_sources": atom_sources,
                    "actor_links": actor_links,
                    "atom_actor_links": atom_actor_links,
                    "fragments": list(snapshot.get("fragments") or []),
                }
            )
        normalized_relations = self._normalize_topic_relations(
            memory_space_id, relations
        )
        affected = set(affected_topic_uids or set())
        active_uids = {
            str(snapshot["topic"].topic_uid) for snapshot in normalized_snapshots
        }
        saved_topics: list[TopicMemory] = []
        reset_result = None
        archived_topics = 0
        now = time.time()

        async with self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                run_row = await (
                    await db.execute(
                        "SELECT memory_space_id FROM topic_maintenance_runs "
                        "WHERE run_uid = ?",
                        (run_uid,),
                    )
                ).fetchone()
                if run_row is None or str(run_row["memory_space_id"]) != memory_space_id:
                    raise ValueError("Topic build run is missing or belongs to another space")

                if reset_topics:
                    topic_count = await (
                        await db.execute(
                            "SELECT COUNT(*) FROM topic_memories WHERE memory_space_id = ?",
                            (memory_space_id,),
                        )
                    ).fetchone()
                    run_count = await (
                        await db.execute(
                            "SELECT COUNT(*) FROM topic_maintenance_runs "
                            "WHERE memory_space_id = ? AND run_uid != ?",
                            (memory_space_id, run_uid),
                        )
                    ).fetchone()
                    await db.execute(
                        "DELETE FROM topic_memories WHERE memory_space_id = ?",
                        (memory_space_id,),
                    )
                    await db.execute(
                        "DELETE FROM topic_maintenance_runs "
                        "WHERE memory_space_id = ? AND run_uid != ?",
                        (memory_space_id, run_uid),
                    )
                    await db.execute(
                        "DELETE FROM topic_fragments WHERE memory_space_id = ?",
                        (memory_space_id,),
                    )
                    reset_result = {
                        "deleted_topics": int(topic_count[0] if topic_count else 0),
                        "deleted_runs": int(run_count[0] if run_count else 0),
                    }

                for snapshot in normalized_snapshots:
                    saved = await self._save_topic_snapshot_tx(
                        db,
                        snapshot["topic"],
                        atoms=snapshot["atoms"],
                        links=snapshot["links"],
                        atom_sources=snapshot["atom_sources"],
                        actor_links=snapshot["actor_links"],
                        atom_actor_links=snapshot["atom_actor_links"],
                        fragments=snapshot["fragments"],
                        expected_revision=snapshot.get("expected_revision"),
                    )
                    saved_topics.append(saved)
                    decision = dict(snapshot.get("decision") or {})
                    if decision:
                        await self._record_build_decision_tx(
                            db,
                            decision_uid=str(decision["decision_uid"]),
                            run_uid=run_uid,
                            topic_uid=saved.topic_uid,
                            action=str(decision["action"]),
                            fragment_uids=list(decision.get("fragment_uids") or []),
                            candidate_scores=dict(
                                decision.get("candidate_scores") or {}
                            ),
                            llm_output=dict(decision.get("llm_output") or {}),
                            metadata={
                                **dict(decision.get("metadata") or {}),
                                "topic_revision": saved.revision,
                            },
                        )

                if mode is TopicMaintenanceMode.FULL and not reset_topics:
                    archived_topics = await self._archive_topics_not_in_tx(
                        db, memory_space_id, active_uids, now
                    )
                elif affected:
                    archived_topics = await self._archive_topic_uids_not_in_tx(
                        db, memory_space_id, affected, active_uids, now
                    )
                await self._replace_topic_relations_tx(
                    db, memory_space_id, normalized_relations
                )
                created_topics = sum(1 for topic in saved_topics if topic.revision == 1)
                updated_topics = len(saved_topics) - created_topics
                await db.execute(
                    """
                    UPDATE topic_maintenance_runs
                    SET status = 'completed', stage = 'completed',
                        current_group_index = ?, total_groups = ?,
                        created_topics = ?, updated_topics = ?,
                        completed_at = ?, updated_at = ?, error = ''
                    WHERE run_uid = ?
                    """,
                    (
                        len(saved_topics),
                        len(saved_topics),
                        created_topics,
                        updated_topics,
                        now,
                        now,
                        run_uid,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "topics": saved_topics,
            "relation_count": len(normalized_relations),
            "archived_topics": archived_topics,
            "reset": reset_result,
        }

    async def _save_topic_snapshot_tx(
        self,
        db: aiosqlite.Connection,
        topic: TopicMemory,
        *,
        atoms: list[TopicMemoryAtom],
        links: list[TopicTimelineLink],
        atom_sources: list[TopicAtomSource],
        actor_links: list[TopicActorLink],
        atom_actor_links: list[TopicAtomActorLink],
        fragments: list[TopicFragmentDraft] | None,
        expected_revision: int | None,
    ) -> TopicMemory:
        timeline_uids = {link.timeline_uid for link in links}
        timeline_uids.update(source.timeline_uid for source in atom_sources)
        existing = await (
            await db.execute(
                "SELECT revision, created_at FROM topic_memories WHERE topic_uid = ?",
                (topic.topic_uid,),
            )
        ).fetchone()
        if existing:
            effective_expected = (
                topic.revision if expected_revision is None else expected_revision
            )
            if int(effective_expected) != int(existing["revision"]):
                raise TopicRevisionConflict(
                    f"Topic {topic.topic_uid} revision changed: "
                    f"expected {effective_expected}, current {existing['revision']}"
                )
            revision = int(existing["revision"]) + 1
            created_at = float(existing["created_at"])
        else:
            if expected_revision not in (None, 0) or topic.revision not in (0, 1):
                raise TopicRevisionConflict(f"Topic {topic.topic_uid} does not exist")
            revision = 1
            created_at = float(topic.created_at)

        registry = await self._load_timeline_registry(db, timeline_uids)
        self._validate_timeline_scope(topic.memory_space_id, timeline_uids, registry)
        now = time.time()
        await db.execute(
            """
            INSERT INTO topic_memories (
                topic_uid, memory_space_id, title, summary, revision, status,
                base_importance, importance, confidence, started_at, ended_at,
                last_accessed_at, access_count, decay_anchor_at,
                created_at, updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_uid) DO UPDATE SET
                memory_space_id = excluded.memory_space_id,
                title = excluded.title,
                summary = excluded.summary,
                revision = excluded.revision,
                status = excluded.status,
                base_importance = excluded.base_importance,
                importance = excluded.importance,
                confidence = excluded.confidence,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                last_accessed_at = excluded.last_accessed_at,
                access_count = excluded.access_count,
                decay_anchor_at = excluded.decay_anchor_at,
                updated_at = excluded.updated_at,
                metadata = excluded.metadata
            """,
            (
                topic.topic_uid,
                topic.memory_space_id,
                topic.title.strip(),
                topic.summary.strip(),
                revision,
                self._enum_value(topic.status),
                float(topic.base_importance),
                float(topic.importance),
                float(topic.confidence),
                topic.started_at,
                topic.ended_at,
                topic.last_accessed_at,
                max(0, int(topic.access_count)),
                topic.decay_anchor_at,
                created_at,
                now,
                self._to_json(topic.metadata),
            ),
        )
        await db.execute(
            "DELETE FROM topic_memory_atoms WHERE topic_uid = ?", (topic.topic_uid,)
        )
        await db.execute(
            "DELETE FROM topic_timeline_links WHERE topic_uid = ?", (topic.topic_uid,)
        )
        await db.execute(
            "DELETE FROM topic_actor_links WHERE topic_uid = ?", (topic.topic_uid,)
        )
        previous_fragment_uids: set[str] = set()
        if fragments is not None:
            previous_fragment_rows = await (
                await db.execute(
                    "SELECT fragment_uid FROM topic_fragment_links "
                    "WHERE topic_uid = ? AND status = 'active'",
                    (topic.topic_uid,),
                )
            ).fetchall()
            previous_fragment_uids = {
                str(row["fragment_uid"]) for row in previous_fragment_rows
            }
            await db.execute(
                "DELETE FROM topic_fragment_links WHERE topic_uid = ?",
                (topic.topic_uid,),
            )
        for atom in atoms:
            await self._insert_atom(db, atom, now)
        for link in links:
            await self._insert_link(
                db, link, revision, int(registry[link.timeline_uid]["revision"]), now
            )
        for source in atom_sources:
            await self._insert_atom_source(
                db, source, int(registry[source.timeline_uid]["revision"])
            )
        if fragments is not None:
            fragment_uids = [fragment.fragment_uid for fragment in fragments]
            await self._insert_fragment_links(db, topic, revision, fragments, now)
            stale_uids = previous_fragment_uids - set(fragment_uids)
            if stale_uids:
                placeholders = ",".join("?" * len(stale_uids))
                await db.execute(
                    f"""
                    UPDATE topic_fragments SET status = 'archived', updated_at = ?
                    WHERE fragment_uid IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM topic_fragment_links l
                          WHERE l.fragment_uid = topic_fragments.fragment_uid
                            AND l.status = 'active'
                      )
                    """,
                    [now, *sorted(stale_uids)],
                )
        for actor_link in actor_links:
            await self._insert_actor_link(db, actor_link, now)
        for atom_actor_link in atom_actor_links:
            await self._insert_atom_actor_link(db, atom_actor_link)
        return replace(
            topic,
            revision=revision,
            created_at=created_at,
            updated_at=now,
        )

    async def get_topic(self, topic_uid: str) -> TopicMemory | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM topic_memories WHERE topic_uid = ?", (topic_uid,)
            )
            row = await cursor.fetchone()
            actor_rows = []
            if row:
                actor_rows = await (
                    await db.execute(
                        "SELECT * FROM topic_actor_links WHERE topic_uid = ?",
                        (topic_uid,),
                    )
                ).fetchall()
        if not row:
            return None
        topic = self._row_to_topic(row)
        topic.participants, topic.mentioned_actors = self._aggregate_actor_refs(
            actor_rows
        )
        return topic

    async def list_topics(
        self,
        memory_space_id: str,
        *,
        status: TopicMemoryStatus | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TopicMemory]:
        where = "memory_space_id = ?"
        params: list[Any] = [memory_space_id]
        if status is not None:
            where += " AND status = ?"
            params.append(self._enum_value(status))
        params.extend([max(1, min(int(limit), 1000)), max(0, int(offset))])
        async with self._connect() as db:
            cursor = await db.execute(
                f"""
                SELECT * FROM topic_memories
                WHERE {where}
                ORDER BY importance DESC, updated_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [self._row_to_topic(row) for row in rows]

    async def list_topic_recall_payloads(
        self,
        memory_space_id: str,
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Load active Topics and their compact recall provenance in bulk."""
        safe_limit = max(1, min(int(limit), 5000))
        async with self._connect() as db:
            topic_rows = await (
                await db.execute(
                    """
                    SELECT * FROM topic_memories
                    WHERE memory_space_id = ? AND status = 'active'
                    ORDER BY importance DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (memory_space_id, safe_limit),
                )
            ).fetchall()
            topic_uids = [str(row["topic_uid"]) for row in topic_rows]
            if not topic_uids:
                return []
            placeholders = ",".join("?" * len(topic_uids))
            atom_rows = await (
                await db.execute(
                    f"""
                    SELECT topic_uid, atom_uid, atom_type, content, importance,
                           confidence, event_started_at, event_ended_at
                    FROM topic_memory_atoms
                    WHERE topic_uid IN ({placeholders}) AND status = 'active'
                    ORDER BY importance DESC, created_at, atom_uid
                    """,
                    topic_uids,
                )
            ).fetchall()
            source_rows = await (
                await db.execute(
                    f"""
                    SELECT l.topic_uid, l.timeline_uid, l.time_cluster_key,
                           l.contribution_weight, l.semantic_similarity,
                           l.temporal_affinity, s.session_id, s.start_index,
                           s.end_index, s.started_at, s.ended_at
                    FROM topic_timeline_links l
                    LEFT JOIN memory_source_spans s
                      ON s.memory_uid = l.timeline_uid
                    WHERE l.topic_uid IN ({placeholders})
                      AND l.status = 'active'
                    ORDER BY l.contribution_weight DESC,
                             l.semantic_similarity DESC, l.timeline_uid
                    """,
                    topic_uids,
                )
            ).fetchall()
            actor_rows = await (
                await db.execute(
                    f"""
                    SELECT topic_uid, actor_id, actor_type, relation_type,
                           display_name_snapshot, confidence,
                           resolution_status, metadata
                    FROM topic_actor_links
                    WHERE topic_uid IN ({placeholders})
                    ORDER BY topic_uid, relation_type, actor_id
                    """,
                    topic_uids,
                )
            ).fetchall()
        atoms_by_topic: dict[str, list[dict[str, Any]]] = {
            uid: [] for uid in topic_uids
        }
        sources_by_topic: dict[str, list[dict[str, Any]]] = {
            uid: [] for uid in topic_uids
        }
        actors_by_topic: dict[str, list[dict[str, Any]]] = {
            uid: [] for uid in topic_uids
        }
        for row in atom_rows:
            atoms_by_topic[str(row["topic_uid"])].append(dict(row))
        for row in source_rows:
            sources_by_topic[str(row["topic_uid"])].append(dict(row))
        for row in actor_rows:
            item = dict(row)
            item["metadata"] = self._from_json(item.get("metadata"))
            actors_by_topic[str(row["topic_uid"])].append(item)
        return [
            {
                "topic": self._row_to_topic(row),
                "atoms": atoms_by_topic[str(row["topic_uid"])],
                "sources": sources_by_topic[str(row["topic_uid"])],
                "actors": actors_by_topic[str(row["topic_uid"])],
            }
            for row in topic_rows
        ]

    async def list_active_fragments_for_topics(
        self,
        topic_uids: list[str],
    ) -> list[dict[str, Any]]:
        """Load formal, current-revision fragment snapshots for recalled Topics."""
        normalized = sorted(
            {str(uid).strip() for uid in topic_uids if str(uid).strip()}
        )
        if not normalized:
            return []
        placeholders = ",".join("?" * len(normalized))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT f.*, l.topic_uid, l.topic_revision,
                           l.contribution_weight AS link_contribution_weight,
                           l.metadata AS link_metadata
                    FROM topic_fragment_links l
                    JOIN topic_memories t ON t.topic_uid = l.topic_uid
                    JOIN topic_fragments f
                      ON f.fragment_uid = l.fragment_uid
                    WHERE l.topic_uid IN ({placeholders})
                      AND t.status = 'active'
                      AND l.status = 'active'
                      AND f.status = 'active'
                      AND l.topic_revision = t.revision
                    ORDER BY l.topic_uid, f.started_at, f.created_at,
                             f.fragment_uid
                    """,
                    normalized,
                )
            ).fetchall()
            fragments_by_uid = {
                str(row["fragment_uid"]): self._row_to_fragment(row)
                for row in rows
            }
            fragment_timeline_uids = sorted(
                {
                    uid
                    for fragment in fragments_by_uid.values()
                    for uid in fragment.timeline_uids
                }
            )
            spans_by_timeline: dict[str, dict[str, Any]] = {}
            if fragment_timeline_uids:
                span_placeholders = ",".join("?" * len(fragment_timeline_uids))
                span_rows = await (
                    await db.execute(
                        f"""
                        SELECT memory_uid, session_id, start_index, end_index,
                               started_at, ended_at
                        FROM memory_source_spans
                        WHERE memory_uid IN ({span_placeholders})
                        """,
                        fragment_timeline_uids,
                    )
                ).fetchall()
                spans_by_timeline = {
                    str(row["memory_uid"]): dict(row) for row in span_rows
                }
        result: list[dict[str, Any]] = []
        for row in rows:
            fragment = fragments_by_uid[str(row["fragment_uid"])]
            result.append(
                {
                    "topic_uid": str(row["topic_uid"]),
                    "topic_revision": int(row["topic_revision"]),
                    "contribution_weight": float(
                        row["link_contribution_weight"]
                    ),
                    "link_metadata": self._from_json(row["link_metadata"]),
                    "fragment": fragment,
                    "sources": [
                        spans_by_timeline[uid]
                        for uid in fragment.timeline_uids
                        if uid in spans_by_timeline
                    ],
                }
            )
        return result

    async def record_topic_access(self, topic_uids: list[str]) -> int:
        """Update reinforcement fields only for Topics actually injected."""
        normalized = sorted({str(uid).strip() for uid in topic_uids if str(uid).strip()})
        if not normalized:
            return 0
        placeholders = ",".join("?" * len(normalized))
        now = time.time()
        async with self._connect() as db:
            cursor = await db.execute(
                f"""
                UPDATE topic_memories
                SET access_count = access_count + 1, last_accessed_at = ?
                WHERE topic_uid IN ({placeholders}) AND status = 'active'
                """,
                [now, *normalized],
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def get_topics_for_timeline(self, timeline_uid: str) -> list[dict[str, Any]]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT t.*, l.time_cluster_key, l.contribution_weight,
                       l.semantic_similarity, l.temporal_affinity,
                       l.source_timeline_revision, l.topic_revision,
                       l.status AS link_status
                FROM topic_timeline_links l
                JOIN topic_memories t ON t.topic_uid = l.topic_uid
                WHERE l.timeline_uid = ?
                ORDER BY l.contribution_weight DESC, t.updated_at DESC
                """,
                (timeline_uid,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def find_incremental_topic_scope(
        self,
        memory_space_id: str,
        context_timeline_uids: list[str],
        seed_timeline_uids: list[str],
    ) -> dict[str, list[str]]:
        """Select focused existing Topics safe to reconstruct as one local scope."""
        context = sorted({str(uid) for uid in context_timeline_uids if str(uid)})
        seeds = sorted({str(uid) for uid in seed_timeline_uids if str(uid)})
        if not context:
            return {"topic_uids": [], "timeline_uids": []}
        context_json = self._to_json(context)
        seed_json = self._to_json(seeds)
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    WITH support AS (
                        SELECT t.topic_uid,
                               COUNT(*) AS source_count,
                               SUM(CASE WHEN l.timeline_uid IN
                                   (SELECT value FROM json_each(?))
                                   THEN 1 ELSE 0 END) AS context_count,
                               SUM(CASE WHEN l.timeline_uid IN
                                   (SELECT value FROM json_each(?))
                                   THEN 1 ELSE 0 END) AS seed_count
                        FROM topic_memories t
                        JOIN topic_timeline_links l ON l.topic_uid = t.topic_uid
                        WHERE t.memory_space_id = ?
                          AND t.status = 'active' AND l.status = 'active'
                        GROUP BY t.topic_uid
                    )
                    SELECT topic_uid FROM support
                    WHERE seed_count > 0
                       OR (context_count > 0 AND (
                           source_count <= 4 OR context_count >= 2
                           OR CAST(context_count AS REAL) / source_count >= 0.25
                       ))
                    ORDER BY topic_uid
                    """,
                    (context_json, seed_json, memory_space_id),
                )
            ).fetchall()
            topic_uids = [str(row["topic_uid"]) for row in rows]
            timeline_uids: list[str] = []
            if topic_uids:
                placeholders = ",".join("?" * len(topic_uids))
                source_rows = await (
                    await db.execute(
                        f"""
                        SELECT DISTINCT timeline_uid
                        FROM topic_timeline_links
                        WHERE status = 'active'
                          AND topic_uid IN ({placeholders})
                        ORDER BY timeline_uid
                        """,
                        topic_uids,
                    )
                ).fetchall()
                timeline_uids = [str(row["timeline_uid"]) for row in source_rows]
        return {"topic_uids": topic_uids, "timeline_uids": timeline_uids}

    async def get_topic_provenance(self, topic_uid: str) -> dict[str, Any]:
        async with self._connect() as db:
            topic_row = await (
                await db.execute(
                    "SELECT revision, metadata FROM topic_memories WHERE topic_uid = ?",
                    (topic_uid,),
                )
            ).fetchone()
            links = await (
                await db.execute(
                    """
                    SELECT * FROM topic_timeline_links
                    WHERE topic_uid = ?
                    ORDER BY time_cluster_key, timeline_uid
                    """,
                    (topic_uid,),
                )
            ).fetchall()
            atoms = await (
                await db.execute(
                    """
                    SELECT * FROM topic_memory_atoms
                    WHERE topic_uid = ? ORDER BY created_at, atom_uid
                    """,
                    (topic_uid,),
                )
            ).fetchall()
            sources = await (
                await db.execute(
                    """
                    SELECT s.* FROM topic_atom_sources s
                    JOIN topic_memory_atoms a ON a.atom_uid = s.topic_atom_uid
                    WHERE a.topic_uid = ?
                    ORDER BY s.topic_atom_uid, s.created_at
                    """,
                    (topic_uid,),
                )
            ).fetchall()
            actor_rows = await (
                await db.execute(
                    """
                    SELECT * FROM topic_actor_links
                    WHERE topic_uid = ?
                    ORDER BY relation_type, actor_id
                    """,
                    (topic_uid,),
                )
            ).fetchall()
            atom_actor_rows = await (
                await db.execute(
                    """
                    SELECT l.* FROM topic_atom_actor_links l
                    JOIN topic_memory_atoms a
                      ON a.atom_uid = l.topic_atom_uid
                    WHERE a.topic_uid = ?
                    ORDER BY l.topic_atom_uid, l.relation_type, l.actor_id
                    """,
                    (topic_uid,),
                )
            ).fetchall()
            topic_metadata = self._from_json(topic_row["metadata"]) if topic_row else {}
            fragment_link_rows = await (
                await db.execute(
                    """
                    SELECT * FROM topic_fragment_links
                    WHERE topic_uid = ? AND status = 'active'
                      AND topic_revision = ?
                    ORDER BY contribution_weight DESC, fragment_uid
                    """,
                    (topic_uid, int(topic_row["revision"]) if topic_row else 0),
                )
            ).fetchall()
            fragment_uids = {
                str(row["fragment_uid"]) for row in fragment_link_rows
            }
            if not fragment_uids:
                fragment_uids = {
                    str(value) for value in topic_metadata.get("fragment_uids", [])
                }
            fragment_rows: list[aiosqlite.Row] = []
            if fragment_uids:
                placeholders = ",".join("?" * len(fragment_uids))
                fragment_rows = await (
                    await db.execute(
                        f"""
                        SELECT * FROM topic_fragments
                        WHERE fragment_uid IN ({placeholders})
                        ORDER BY started_at, fragment_uid
                        """,
                        sorted(fragment_uids),
                    )
                ).fetchall()
        link_items = [dict(row) for row in links]
        atom_items = [dict(row) for row in atoms]
        source_items = [dict(row) for row in sources]
        actor_items = [dict(row) for row in actor_rows]
        atom_actor_items = [dict(row) for row in atom_actor_rows]
        for item in [
            *link_items,
            *atom_items,
            *source_items,
            *actor_items,
            *atom_actor_items,
        ]:
            if "metadata" in item:
                item["metadata"] = self._from_json(item["metadata"])
        return {
            "links": link_items,
            "atoms": atom_items,
            "atom_sources": source_items,
            "actor_links": actor_items,
            "atom_actor_links": atom_actor_items,
            "fragments": [
                self._fragment_to_dict(self._row_to_fragment(row))
                for row in fragment_rows
            ],
            "fragment_links": [
                {
                    **dict(row),
                    "metadata": self._from_json(row["metadata"]),
                }
                for row in fragment_link_rows
            ],
        }

    async def get_topic_support_metrics(self, topic_uid: str) -> dict[str, float | int]:
        """Return cluster-aware evidence metrics without treating rows as votes."""
        async with self._connect() as db:
            row = await (
                await db.execute(
                    """
                    SELECT COUNT(*) AS timeline_count,
                           COUNT(DISTINCT time_cluster_key) AS time_cluster_count,
                           COALESCE(SUM(contribution_weight), 0.0) AS contribution_weight,
                           COALESCE(AVG(semantic_similarity), 0.0) AS semantic_similarity,
                           COALESCE(AVG(temporal_affinity), 0.0) AS temporal_affinity
                    FROM topic_timeline_links
                    WHERE topic_uid = ? AND status = 'active'
                    """,
                    (topic_uid,),
                )
            ).fetchone()
        return dict(row) if row else {
            "timeline_count": 0,
            "time_cluster_count": 0,
            "contribution_weight": 0.0,
            "semantic_similarity": 0.0,
            "temporal_affinity": 0.0,
        }

    async def replace_topic_relations(
        self,
        memory_space_id: str,
        relations: list[TopicRelation],
    ) -> int:
        """Atomically replace the derived related-subtopic graph for one space."""
        memory_space_id = str(memory_space_id or "").strip()
        if not memory_space_id:
            raise ValueError("memory_space_id is required")
        normalized = self._normalize_topic_relations(memory_space_id, relations)
        async with self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._replace_topic_relations_tx(
                    db, memory_space_id, normalized
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return len(normalized)

    def _normalize_topic_relations(
        self,
        memory_space_id: str,
        relations: list[TopicRelation],
    ) -> list[TopicRelation]:
        normalized: list[TopicRelation] = []
        for relation in relations:
            left, right = sorted(
                (str(relation.left_topic_uid), str(relation.right_topic_uid))
            )
            if not left or not right or left == right:
                raise ValueError("Topic relation requires two distinct Topic UIDs")
            if relation.memory_space_id != memory_space_id:
                raise TopicSourceValidationError(
                    "Topic relation belongs to another memory space"
                )
            self._validate_score("relation confidence", relation.confidence)
            self._validate_score(
                "relation semantic_similarity", relation.semantic_similarity
            )
            normalized.append(
                replace(
                    relation,
                    left_topic_uid=left,
                    right_topic_uid=right,
                )
            )
        return normalized

    async def _replace_topic_relations_tx(
        self,
        db: aiosqlite.Connection,
        memory_space_id: str,
        relations: list[TopicRelation],
    ) -> None:
        topic_uids = {
            uid
            for relation in relations
            for uid in (relation.left_topic_uid, relation.right_topic_uid)
        }
        if topic_uids:
            placeholders = ",".join("?" * len(topic_uids))
            rows = await (
                await db.execute(
                    f"SELECT topic_uid, memory_space_id FROM topic_memories "
                    f"WHERE topic_uid IN ({placeholders})",
                    sorted(topic_uids),
                )
            ).fetchall()
            scopes = {
                str(row["topic_uid"]): str(row["memory_space_id"]) for row in rows
            }
            if set(scopes) != topic_uids or any(
                scope != memory_space_id for scope in scopes.values()
            ):
                raise TopicSourceValidationError(
                    "Topic relation crosses a memory-space boundary"
                )
        await db.execute(
            "DELETE FROM topic_relations WHERE memory_space_id = ?",
            (memory_space_id,),
        )
        for relation in relations:
            await db.execute(
                """
                INSERT INTO topic_relations (
                    relation_uid, memory_space_id, left_topic_uid,
                    right_topic_uid, relation_type, confidence,
                    semantic_similarity, status, build_run_uid,
                    created_at, updated_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relation.relation_uid,
                    memory_space_id,
                    relation.left_topic_uid,
                    relation.right_topic_uid,
                    self._enum_value(relation.relation_type),
                    float(relation.confidence),
                    float(relation.semantic_similarity),
                    relation.status,
                    relation.build_run_uid,
                    float(relation.created_at),
                    float(relation.updated_at),
                    self._to_json(relation.metadata),
                ),
            )

    async def list_topic_relations(self, topic_uid: str) -> list[dict[str, Any]]:
        """Return active neighboring Topics without flattening their contents."""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT r.*,
                           CASE WHEN r.left_topic_uid = ?
                                THEN r.right_topic_uid ELSE r.left_topic_uid END
                                AS related_topic_uid,
                           t.title AS related_title,
                           t.summary AS related_summary,
                           t.status AS related_status
                    FROM topic_relations r
                    JOIN topic_memories t ON t.topic_uid = CASE
                        WHEN r.left_topic_uid = ?
                        THEN r.right_topic_uid ELSE r.left_topic_uid END
                    WHERE (r.left_topic_uid = ? OR r.right_topic_uid = ?)
                      AND r.status = 'active' AND t.status = 'active'
                    ORDER BY r.semantic_similarity DESC, t.importance DESC
                    """,
                    (topic_uid, topic_uid, topic_uid, topic_uid),
                )
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = self._from_json(item.get("metadata"))
            result.append(item)
        return result

    async def get_overview(self, memory_space_id: str | None = None) -> dict[str, Any]:
        where = "WHERE memory_space_id = ?" if memory_space_id else ""
        params = [memory_space_id] if memory_space_id else []
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT status, COUNT(*) AS count
                    FROM topic_memories {where} GROUP BY status
                    """,
                    params,
                )
            ).fetchall()
            atom_count = await (
                await db.execute(
                    f"""
                    SELECT COUNT(*) FROM topic_memory_atoms a
                    JOIN topic_memories t ON t.topic_uid = a.topic_uid
                    {where}
                    """,
                    params,
                )
            ).fetchone()
            link_count = await (
                await db.execute(
                    f"""
                    SELECT COUNT(*) FROM topic_timeline_links l
                    JOIN topic_memories t ON t.topic_uid = l.topic_uid
                    {where}
                    """,
                    params,
                )
            ).fetchone()
            relation_count = await (
                await db.execute(
                    f"SELECT COUNT(*) FROM topic_relations {where}",
                    params,
                )
            ).fetchone()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "topic_count": sum(counts.values()),
            "status_counts": counts,
            "atom_count": int(atom_count[0] if atom_count else 0),
            "timeline_link_count": int(link_count[0] if link_count else 0),
            "relation_count": int(relation_count[0] if relation_count else 0),
        }

    async def list_memory_spaces(self, *, limit: int = 200) -> list[dict[str, Any]]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT r.memory_space_id,
                           COUNT(DISTINCT r.memory_uid) AS timeline_count,
                           COUNT(DISTINCT t.topic_uid) AS topic_count,
                           MAX(r.updated_at) AS updated_at
                    FROM memory_registry r
                    LEFT JOIN topic_memories t
                      ON t.memory_space_id = r.memory_space_id
                    WHERE r.memory_layer = 'timeline' AND r.status = 'active'
                    GROUP BY r.memory_space_id
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (max(1, min(int(limit), 1000)),),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def find_memory_spaces_for_session(
        self, session_id: str, *, limit: int = 10
    ) -> list[str]:
        """Resolve WebUI/tool session input to existing Topic memory spaces."""
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT r.memory_space_id, MAX(r.updated_at) AS last_updated
                    FROM memory_registry r
                    JOIN memory_source_spans s ON s.memory_uid = r.memory_uid
                    JOIN topic_memories t
                      ON t.memory_space_id = r.memory_space_id
                     AND t.status = 'active'
                    WHERE r.memory_layer = 'timeline' AND r.status = 'active'
                      AND s.session_id = ?
                    GROUP BY r.memory_space_id
                    ORDER BY last_updated DESC
                    LIMIT ?
                    """,
                    (session_id, max(1, min(int(limit), 100))),
                )
            ).fetchall()
        return [str(row["memory_space_id"]) for row in rows]

    async def list_maintenance_runs(
        self, memory_space_id: str | None = None, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        where = "WHERE memory_space_id = ?" if memory_space_id else ""
        params: list[Any] = [memory_space_id] if memory_space_id else []
        params.append(max(1, min(int(limit), 100)))
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT * FROM topic_maintenance_runs {where}
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    params,
                )
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["config"] = self._from_json(item.get("config"))
            item["metadata"] = self._from_json(item.get("metadata"))
            result.append(item)
        return result

    async def mark_timeline_stale(self, timeline_uid: str) -> list[str]:
        """Invalidate only Topics derived from a rebuilt Timeline memory."""
        now = time.time()
        async with self._connect() as db:
            try:
                rows = await (
                    await db.execute(
                        "SELECT topic_uid FROM topic_timeline_links WHERE timeline_uid = ?",
                        (timeline_uid,),
                    )
                ).fetchall()
                topic_uids = [str(row[0]) for row in rows]
                await db.execute(
                    """
                    UPDATE topic_timeline_links
                    SET status = 'stale', updated_at = ?
                    WHERE timeline_uid = ?
                    """,
                    (now, timeline_uid),
                )
                if topic_uids:
                    placeholders = ",".join("?" * len(topic_uids))
                    await db.execute(
                        f"""
                        UPDATE topic_memories
                        SET status = 'stale', updated_at = ?
                        WHERE topic_uid IN ({placeholders})
                        """,
                        [now, *topic_uids],
                    )
                await db.commit()
                return topic_uids
            except Exception:
                await db.rollback()
                raise

    async def mark_identity_topics_pending(
        self,
        *,
        actor_ids: set[str],
        actor_suffixes: set[str],
        display_names: set[str],
    ) -> dict[str, list[str]]:
        """Mark actor-affected Topics for queued rebuild without hiding old snapshots."""
        actor_ids = {str(value).strip() for value in actor_ids if str(value).strip()}
        actor_suffixes = {
            str(value).strip() for value in actor_suffixes if str(value).strip()
        }
        normalized_names = {
            str(value).strip().casefold()
            for value in display_names
            if str(value).strip()
        }
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT t.topic_uid, t.memory_space_id, t.metadata,
                           a.actor_id, a.display_name_snapshot,
                           a.resolution_status
                    FROM topic_memories t
                    LEFT JOIN topic_actor_links a ON a.topic_uid = t.topic_uid
                    WHERE t.status = 'active'
                    ORDER BY t.topic_uid
                    """
                )
            ).fetchall()
            matched: dict[str, tuple[str, dict[str, Any]]] = {}
            topics_with_actors: set[str] = set()
            all_topics: dict[str, tuple[str, dict[str, Any]]] = {}
            for row in rows:
                topic_uid = str(row["topic_uid"])
                memory_space_id = str(row["memory_space_id"])
                metadata = self._from_json(row["metadata"])
                all_topics[topic_uid] = (memory_space_id, metadata)
                actor_id = str(row["actor_id"] or "").strip()
                if actor_id:
                    topics_with_actors.add(topic_uid)
                display_name = str(row["display_name_snapshot"] or "").strip()
                suffix_match = any(
                    actor_id.endswith(f":human:{suffix}")
                    for suffix in actor_suffixes
                )
                unresolved_name_match = (
                    str(row["resolution_status"] or "") == "unresolved"
                    and display_name.casefold() in normalized_names
                )
                if actor_id in actor_ids or suffix_match or unresolved_name_match:
                    matched[topic_uid] = (memory_space_id, metadata)
            # Legacy Topics have no authoritative actor rows. Rebuild them once so
            # identity changes cannot leave permanently unindexed snapshots.
            for topic_uid, value in all_topics.items():
                if topic_uid not in topics_with_actors:
                    matched.setdefault(topic_uid, value)
            now = time.time()
            for topic_uid, (_, metadata) in matched.items():
                metadata["identity_sync_pending"] = True
                metadata["identity_sync_requested_at"] = now
                await db.execute(
                    """
                    UPDATE topic_memories SET metadata = ?, updated_at = ?
                    WHERE topic_uid = ?
                    """,
                    (self._to_json(metadata), now, topic_uid),
                )
            result: dict[str, list[str]] = {}
            for topic_uid, (memory_space_id, _) in matched.items():
                timeline_rows = await (
                    await db.execute(
                        """
                        SELECT timeline_uid FROM topic_timeline_links
                        WHERE topic_uid = ? AND status = 'active'
                        ORDER BY timeline_uid
                        """,
                        (topic_uid,),
                    )
                ).fetchall()
                result.setdefault(memory_space_id, []).extend(
                    str(row["timeline_uid"]) for row in timeline_rows
                )
            await db.commit()
        return {
            space_id: sorted(set(timeline_uids))
            for space_id, timeline_uids in result.items()
            if timeline_uids
        }

    async def list_identity_sync_pending(self) -> dict[str, list[str]]:
        """Return Timeline sources for Topics awaiting identity-aware rebuild."""
        async with self._connect() as db:
            topic_rows = await (
                await db.execute(
                    """
                    SELECT topic_uid, memory_space_id, metadata
                    FROM topic_memories WHERE status = 'active'
                    ORDER BY topic_uid
                    """
                )
            ).fetchall()
            pending = [
                (str(row["topic_uid"]), str(row["memory_space_id"]))
                for row in topic_rows
                if self._from_json(row["metadata"]).get("identity_sync_pending")
            ]
            result: dict[str, list[str]] = {}
            for topic_uid, memory_space_id in pending:
                rows = await (
                    await db.execute(
                        """
                        SELECT timeline_uid FROM topic_timeline_links
                        WHERE topic_uid = ? AND status = 'active'
                        """,
                        (topic_uid,),
                    )
                ).fetchall()
                result.setdefault(memory_space_id, []).extend(
                    str(row["timeline_uid"]) for row in rows
                )
        return {
            space_id: sorted(set(timeline_uids))
            for space_id, timeline_uids in result.items()
            if timeline_uids
        }

    async def delete_topic(self, topic_uid: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM topic_memories WHERE topic_uid = ?", (topic_uid,)
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def clear_space(self, memory_space_id: str) -> dict[str, int]:
        """Permanently remove every Topic derivative and build run for one space."""
        memory_space_id = str(memory_space_id or "").strip()
        if not memory_space_id:
            raise ValueError("memory_space_id is required")
        async with self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                topic_cursor = await db.execute(
                    "DELETE FROM topic_memories WHERE memory_space_id = ?",
                    (memory_space_id,),
                )
                run_cursor = await db.execute(
                    "DELETE FROM topic_maintenance_runs WHERE memory_space_id = ?",
                    (memory_space_id,),
                )
                fragment_cursor = await db.execute(
                    "DELETE FROM topic_fragments WHERE memory_space_id = ?",
                    (memory_space_id,),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "deleted_topics": int(topic_cursor.rowcount or 0),
            "deleted_runs": int(run_cursor.rowcount or 0),
            "deleted_fragments": int(fragment_cursor.rowcount or 0),
        }

    async def create_maintenance_run(
        self, run: TopicMaintenanceRun
    ) -> TopicMaintenanceRun:
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO topic_maintenance_runs (
                    run_uid, memory_space_id, mode, status, cursor_memory_uid,
                    total_items, processed_items, created_topics, updated_topics,
                    failed_items, started_at, completed_at, created_at, updated_at,
                    error, config, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_uid,
                    run.memory_space_id,
                    self._enum_value(run.mode),
                    self._enum_value(run.status),
                    run.cursor_memory_uid,
                    max(0, int(run.total_items)),
                    max(0, int(run.processed_items)),
                    max(0, int(run.created_topics)),
                    max(0, int(run.updated_topics)),
                    max(0, int(run.failed_items)),
                    run.started_at,
                    run.completed_at,
                    float(run.created_at),
                    now,
                    run.error,
                    self._to_json(run.config),
                    self._to_json(run.metadata),
                ),
            )
            await db.commit()
        return replace(run, updated_at=now)

    async def update_maintenance_run(
        self,
        run_uid: str,
        *,
        status: TopicMaintenanceStatus | str | None = None,
        stage: str | None = None,
        cursor_memory_uid: str | None = None,
        current_group_index: int | None = None,
        total_groups: int | None = None,
        total_items: int | None = None,
        processed_items: int | None = None,
        created_topics: int | None = None,
        updated_topics: int | None = None,
        failed_items: int | None = None,
        error: str | None = None,
    ) -> bool:
        fields = ["updated_at = ?"]
        params: list[Any] = [time.time()]
        if status is not None:
            status_value = self._enum_value(status)
            fields.append("status = ?")
            params.append(status_value)
            if status_value == TopicMaintenanceStatus.RUNNING.value:
                fields.append("started_at = COALESCE(started_at, ?)")
                params.append(time.time())
                fields.append("completed_at = NULL")
            if status_value in {
                TopicMaintenanceStatus.COMPLETED.value,
                TopicMaintenanceStatus.FAILED.value,
                TopicMaintenanceStatus.CANCELLED.value,
            }:
                fields.append("completed_at = ?")
                params.append(time.time())
        updates = {
            "cursor_memory_uid": cursor_memory_uid,
            "stage": stage,
            "current_group_index": current_group_index,
            "total_groups": total_groups,
            "total_items": total_items,
            "processed_items": processed_items,
            "created_topics": created_topics,
            "updated_topics": updated_topics,
            "failed_items": failed_items,
            "error": error,
        }
        for field_name, value in updates.items():
            if value is not None:
                fields.append(f"{field_name} = ?")
                params.append(value)
        params.append(run_uid)
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE topic_maintenance_runs SET {', '.join(fields)} WHERE run_uid = ?",
                params,
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def get_maintenance_run(self, run_uid: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM topic_maintenance_runs WHERE run_uid = ?",
                    (run_uid,),
                )
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["config"] = self._from_json(result.get("config"))
        result["metadata"] = self._from_json(result.get("metadata"))
        return result

    async def discard_maintenance_run(
        self,
        run_uid: str,
        *,
        memory_space_id: str | None = None,
    ) -> dict[str, Any]:
        """Discard a resumable run and every run-owned intermediate artifact.

        Current builds publish only after the whole batch succeeds. Published
        snapshots are still preserved for compatibility with legacy runs that
        may have materialized topics before reaching the completed state.
        """
        run_uid = str(run_uid or "").strip()
        expected_space = str(memory_space_id or "").strip() or None
        if not run_uid:
            raise ValueError("run_uid is required")
        child_tables = (
            "topic_maintenance_items",
            "topic_candidate_groups",
            "topic_build_group_jobs",
            "topic_fragment_drafts",
            "topic_build_decisions",
            "topic_build_checkpoints",
        )
        async with self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM topic_maintenance_runs WHERE run_uid = ?",
                        (run_uid,),
                    )
                ).fetchone()
                if row is None:
                    raise ValueError("Topic 构建任务不存在")
                run = dict(row)
                run_space = str(run.get("memory_space_id") or "")
                if expected_space and run_space != expected_space:
                    raise ValueError("Topic 构建任务与当前记忆空间不一致")
                status = str(run.get("status") or "")
                if status not in {
                    TopicMaintenanceStatus.FAILED.value,
                    TopicMaintenanceStatus.PENDING.value,
                    TopicMaintenanceStatus.CANCELLED.value,
                }:
                    raise ValueError(
                        "只能取消失败、待继续或已取消的 Topic 构建任务"
                    )

                deleted: dict[str, int] = {}
                for table in child_tables:
                    count_row = await (
                        await db.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE run_uid = ?",
                            (run_uid,),
                        )
                    ).fetchone()
                    deleted[table] = int(count_row[0] if count_row else 0)

                cursor = await db.execute(
                    """
                    DELETE FROM topic_maintenance_runs
                    WHERE run_uid = ? AND status IN ('failed', 'pending', 'cancelled')
                    """,
                    (run_uid,),
                )
                if not cursor.rowcount:
                    raise ValueError("Topic 构建任务状态已变化，无法清除断点")
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "run_uid": run_uid,
            "memory_space_id": run_space,
            "status": status,
            "deleted_run": 1,
            "deleted_intermediate_items": sum(deleted.values()),
            "deleted_by_table": deleted,
            "preserved_materialized_topics": True,
        }

    async def save_scan_items(
        self,
        run_uid: str,
        candidates: list[TimelineTopicCandidate],
    ) -> None:
        """Persist one completed scanner batch for crash-safe resumption."""
        if not candidates:
            return
        now = time.time()
        async with self._connect() as db:
            try:
                for candidate in candidates:
                    await db.execute(
                        """
                        INSERT INTO topic_maintenance_items (
                            run_uid, timeline_uid, source_revision, status,
                            time_cluster_key, candidate_payload, processed_at
                        ) VALUES (?, ?, ?, 'processed', ?, ?, ?)
                        ON CONFLICT(run_uid, timeline_uid) DO UPDATE SET
                            source_revision = excluded.source_revision,
                            status = excluded.status,
                            time_cluster_key = excluded.time_cluster_key,
                            candidate_payload = excluded.candidate_payload,
                            error = NULL,
                            processed_at = excluded.processed_at
                        """,
                        (
                            run_uid,
                            candidate.memory_uid,
                            int(candidate.source_revision),
                            candidate.time_cluster_key,
                            self._to_json(self._candidate_to_dict(candidate)),
                            now,
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def get_scan_items(self, run_uid: str) -> list[TimelineTopicCandidate]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT i.candidate_payload, d.metadata AS document_metadata,
                           s.session_id AS source_session_id,
                           s.first_message_id, s.last_message_id,
                           s.start_index, s.end_index,
                           s.started_at AS source_started_at,
                           s.ended_at AS source_ended_at,
                           s.traceability AS source_traceability
                    FROM topic_maintenance_items i
                    JOIN memory_registry r ON r.memory_uid = i.timeline_uid
                    JOIN documents d ON d.id = r.document_id
                    LEFT JOIN memory_source_spans s
                      ON s.memory_uid = i.timeline_uid
                    WHERE i.run_uid = ? AND i.status = 'processed'
                      AND i.source_revision = r.revision
                    ORDER BY i.processed_at, i.timeline_uid
                    """,
                    (run_uid,),
                )
            ).fetchall()
        candidates: list[TimelineTopicCandidate] = []
        for row in rows:
            payload = self._from_json(row["candidate_payload"])
            # Candidate checkpoints created before v9.7 did not persist actor
            # bindings or immutable source spans.  Rehydrate them from the
            # canonical Timeline row so resuming an old run does not silently
            # discard identity evidence.
            document_metadata = self._from_json(row["document_metadata"])
            if not isinstance(payload.get("role_bindings"), dict):
                payload["role_bindings"] = document_metadata.get(
                    "role_bindings", {}
                )
            if not isinstance(payload.get("source_window"), dict):
                payload["source_window"] = document_metadata.get(
                    "source_window", {}
                )
            source_window = dict(payload.get("source_window") or {})
            source_fields = {
                "session_id": row["source_session_id"],
                "first_message_id": row["first_message_id"],
                "last_message_id": row["last_message_id"],
                "start_index": row["start_index"],
                "end_index": row["end_index"],
                "started_at": row["source_started_at"],
                "ended_at": row["source_ended_at"],
            }
            for key, value in source_fields.items():
                if source_window.get(key) is None and value is not None:
                    source_window[key] = value
            payload["source_window"] = source_window
            payload.setdefault(
                "traceability",
                document_metadata.get("traceability")
                or row["source_traceability"],
            )
            payload.setdefault("edit_origin", document_metadata.get("edit_origin"))
            candidates.append(self._dict_to_candidate(payload))
        return candidates

    async def get_processed_timeline_uids(self, run_uid: str) -> set[str]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT i.timeline_uid
                    FROM topic_maintenance_items i
                    JOIN memory_registry r ON r.memory_uid = i.timeline_uid
                    WHERE i.run_uid = ? AND i.status = 'processed'
                      AND i.source_revision = r.revision
                    """,
                    (run_uid,),
                )
            ).fetchall()
        return {str(row[0]) for row in rows}

    async def replace_candidate_groups(
        self,
        run_uid: str,
        groups: list[TopicCandidateGroup],
    ) -> None:
        """Atomically publish the deterministic preview for one scan run."""
        async with self._connect() as db:
            try:
                await db.execute(
                    "DELETE FROM topic_candidate_groups WHERE run_uid = ?",
                    (run_uid,),
                )
                for group in groups:
                    if group.run_uid != run_uid:
                        raise ValueError("Candidate group belongs to another run")
                    await db.execute(
                        """
                        INSERT INTO topic_candidate_groups (
                            group_uid, run_uid, group_index, memory_space_id,
                            label, timeline_uids, time_cluster_keys, cohesion,
                            started_at, ended_at, shared_signals, status,
                            created_at, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            group.group_uid,
                            group.run_uid,
                            int(group.group_index),
                            group.memory_space_id,
                            group.label,
                            self._to_json(group.timeline_uids),
                            self._to_json(group.time_cluster_keys),
                            float(group.cohesion),
                            group.started_at,
                            group.ended_at,
                            self._to_json(group.shared_signals),
                            group.status,
                            float(group.created_at),
                            self._to_json(group.metadata),
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def list_candidate_groups(self, run_uid: str) -> list[TopicCandidateGroup]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM topic_candidate_groups
                    WHERE run_uid = ? ORDER BY group_index
                    """,
                    (run_uid,),
                )
            ).fetchall()
        return [self._row_to_candidate_group(row) for row in rows]

    async def begin_group_job(
        self,
        run_uid: str,
        group: TopicCandidateGroup,
        *,
        input_hash: str,
        prompt_hash: str,
        provider_id: str,
        model_id: str,
    ) -> bool:
        """Claim a group unless the exact input was already completed."""
        now = time.time()
        async with self._connect() as db:
            row = await (
                await db.execute(
                    """
                    SELECT status, input_hash, prompt_hash, provider_id, model_id
                    FROM topic_build_group_jobs
                    WHERE run_uid = ? AND group_uid = ?
                    """,
                    (run_uid, group.group_uid),
                )
            ).fetchone()
            if (
                row
                and row["status"] == "completed"
                and row["input_hash"] == input_hash
                and row["prompt_hash"] == prompt_hash
                and str(row["provider_id"] or "") == str(provider_id or "")
                and str(row["model_id"] or "") == str(model_id or "")
            ):
                return False
            await db.execute(
                """
                INSERT INTO topic_build_group_jobs (
                    run_uid, group_uid, group_index, status, attempt_count,
                    input_hash, prompt_hash, provider_id, model_id,
                    started_at, updated_at
                ) VALUES (?, ?, ?, 'running', 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_uid, group_uid) DO UPDATE SET
                    group_index = excluded.group_index,
                    status = 'running',
                    attempt_count = topic_build_group_jobs.attempt_count + 1,
                    input_hash = excluded.input_hash,
                    prompt_hash = excluded.prompt_hash,
                    provider_id = excluded.provider_id,
                    model_id = excluded.model_id,
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    run_uid,
                    group.group_uid,
                    group.group_index,
                    input_hash,
                    prompt_hash,
                    provider_id,
                    model_id,
                    now,
                    now,
                ),
            )
            await db.commit()
            return True

    async def finish_group_job(
        self,
        run_uid: str,
        group_uid: str,
        *,
        error: str | None = None,
    ) -> None:
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE topic_build_group_jobs
                SET status = ?, completed_at = ?, error = ?, updated_at = ?
                WHERE run_uid = ? AND group_uid = ?
                """,
                (
                    "failed" if error else "completed",
                    now,
                    error[:1000] if error else None,
                    now,
                    run_uid,
                    group_uid,
                ),
            )
            await db.commit()

    async def replace_group_fragments(
        self,
        run_uid: str,
        group_uid: str,
        fragments: list[TopicFragmentDraft],
    ) -> None:
        async with self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                parent = await (
                    await db.execute(
                        """
                        SELECT 1
                        FROM topic_candidate_groups g
                        JOIN topic_maintenance_runs r ON r.run_uid = g.run_uid
                        WHERE g.group_uid = ? AND g.run_uid = ?
                        """,
                        (group_uid, run_uid),
                    )
                ).fetchone()
                if parent is None:
                    raise ValueError(
                        "Topic fragment parent run/group no longer exists; "
                        "the build may have been cleared while extraction was running"
                    )
                await db.execute(
                    """
                    DELETE FROM topic_fragment_drafts
                    WHERE run_uid = ? AND candidate_group_uid = ?
                    """,
                    (run_uid, group_uid),
                )
                for fragment in fragments:
                    if fragment.run_uid != run_uid or fragment.candidate_group_uid != group_uid:
                        raise ValueError("Topic fragment belongs to another build group")
                    await db.execute(
                        """
                        INSERT INTO topic_fragment_drafts (
                            fragment_uid, run_uid, candidate_group_uid,
                            memory_space_id, label, summary, timeline_uids,
                            source_revisions, facts, keywords, time_cluster_keys,
                            importance, confidence, embedding, started_at, ended_at,
                            status, prompt_hash, input_hash, provider_id, model_id,
                            created_at, updated_at, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fragment.fragment_uid,
                            fragment.run_uid,
                            fragment.candidate_group_uid,
                            fragment.memory_space_id,
                            fragment.label,
                            fragment.summary,
                            self._to_json(fragment.timeline_uids),
                            self._to_json(fragment.source_revisions),
                            self._to_json(fragment.facts),
                            self._to_json(fragment.keywords),
                            self._to_json(fragment.time_cluster_keys),
                            float(fragment.importance),
                            float(fragment.confidence),
                            self._to_json(fragment.embedding),
                            fragment.started_at,
                            fragment.ended_at,
                            fragment.status,
                            fragment.prompt_hash,
                            fragment.input_hash,
                            fragment.provider_id,
                            fragment.model_id,
                            float(fragment.created_at),
                            float(fragment.updated_at),
                            self._to_json(fragment.metadata),
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def list_fragments(
        self,
        *,
        run_uid: str | None = None,
        memory_space_id: str | None = None,
        status: str = "draft",
    ) -> list[TopicFragmentDraft]:
        where = ["status = ?"]
        params: list[Any] = [status]
        if run_uid is not None:
            where.append("run_uid = ?")
            params.append(run_uid)
        if memory_space_id is not None:
            where.append("memory_space_id = ?")
            params.append(memory_space_id)
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    f"""
                    SELECT * FROM topic_fragment_drafts
                    WHERE {' AND '.join(where)}
                    ORDER BY started_at, created_at, fragment_uid
                    """,
                    params,
                )
            ).fetchall()
        return [self._row_to_fragment(row) for row in rows]

    async def update_fragment_embedding(
        self, fragment_uid: str, embedding: list[float]
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE topic_fragment_drafts
                SET embedding = ?, updated_at = ? WHERE fragment_uid = ?
                """,
                (self._to_json(embedding), time.time(), fragment_uid),
            )
            await db.commit()

    async def record_build_decision(
        self,
        *,
        decision_uid: str,
        run_uid: str,
        topic_uid: str | None,
        action: str,
        fragment_uids: list[str],
        candidate_scores: dict[str, Any],
        llm_output: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._connect() as db:
            await self._record_build_decision_tx(
                db,
                decision_uid=decision_uid,
                run_uid=run_uid,
                topic_uid=topic_uid,
                action=action,
                fragment_uids=fragment_uids,
                candidate_scores=candidate_scores,
                llm_output=llm_output,
                metadata=metadata,
            )
            await db.commit()

    async def _record_build_decision_tx(
        self,
        db: aiosqlite.Connection,
        *,
        decision_uid: str,
        run_uid: str,
        topic_uid: str | None,
        action: str,
        fragment_uids: list[str],
        candidate_scores: dict[str, Any],
        llm_output: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await db.execute(
            """
            INSERT OR REPLACE INTO topic_build_decisions (
                decision_uid, run_uid, topic_uid, action, fragment_uids,
                candidate_scores, llm_output, created_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_uid,
                run_uid,
                topic_uid,
                action,
                self._to_json(fragment_uids),
                self._to_json(candidate_scores),
                self._to_json(llm_output),
                time.time(),
                self._to_json(metadata or {}),
            ),
        )

    async def save_build_checkpoint(
        self,
        *,
        run_uid: str,
        checkpoint_key: str,
        stage: str,
        input_hash: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO topic_build_checkpoints (
                    run_uid, checkpoint_key, stage, input_hash, payload,
                    created_at, updated_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_uid, checkpoint_key) DO UPDATE SET
                    stage = excluded.stage,
                    input_hash = excluded.input_hash,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at,
                    metadata = excluded.metadata
                """,
                (
                    run_uid,
                    checkpoint_key,
                    stage,
                    input_hash,
                    self._to_json(payload),
                    now,
                    now,
                    self._to_json(metadata or {}),
                ),
            )
            await db.commit()

    async def get_build_checkpoint(
        self,
        run_uid: str,
        checkpoint_key: str,
    ) -> dict[str, Any] | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    """
                    SELECT * FROM topic_build_checkpoints
                    WHERE run_uid = ? AND checkpoint_key = ?
                    """,
                    (run_uid, checkpoint_key),
                )
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = self._from_json(result.get("payload"))
        result["metadata"] = self._from_json(result.get("metadata"))
        return result

    async def archive_topics_not_in(
        self, memory_space_id: str, active_topic_uids: set[str]
    ) -> int:
        async with self._connect() as db:
            cursor_count = await self._archive_topics_not_in_tx(
                db, memory_space_id, active_topic_uids, time.time()
            )
            await db.commit()
            return cursor_count

    @staticmethod
    async def _archive_topics_not_in_tx(
        db: aiosqlite.Connection,
        memory_space_id: str,
        active_topic_uids: set[str],
        now: float,
    ) -> int:
        if active_topic_uids:
            placeholders = ",".join("?" * len(active_topic_uids))
            cursor = await db.execute(
                f"""
                UPDATE topic_memories SET status = 'archived', updated_at = ?
                WHERE memory_space_id = ? AND status != 'archived'
                  AND topic_uid NOT IN ({placeholders})
                """,
                [now, memory_space_id, *sorted(active_topic_uids)],
            )
        else:
            cursor = await db.execute(
                """
                UPDATE topic_memories SET status = 'archived', updated_at = ?
                WHERE memory_space_id = ? AND status != 'archived'
                """,
                (now, memory_space_id),
            )
        return int(cursor.rowcount or 0)

    async def archive_topic_uids_not_in(
        self,
        memory_space_id: str,
        affected_topic_uids: set[str],
        active_topic_uids: set[str],
    ) -> int:
        """Archive only replaced members of an incremental reconstruction scope."""
        targets = sorted(set(affected_topic_uids) - set(active_topic_uids))
        if not targets:
            return 0
        async with self._connect() as db:
            cursor_count = await self._archive_topic_uids_not_in_tx(
                db,
                memory_space_id,
                affected_topic_uids,
                active_topic_uids,
                time.time(),
            )
            await db.commit()
            return cursor_count

    @staticmethod
    async def _archive_topic_uids_not_in_tx(
        db: aiosqlite.Connection,
        memory_space_id: str,
        affected_topic_uids: set[str],
        active_topic_uids: set[str],
        now: float,
    ) -> int:
        targets = sorted(set(affected_topic_uids) - set(active_topic_uids))
        if not targets:
            return 0
        placeholders = ",".join("?" * len(targets))
        cursor = await db.execute(
            f"""
            UPDATE topic_memories SET status = 'archived', updated_at = ?
            WHERE memory_space_id = ? AND status != 'archived'
              AND topic_uid IN ({placeholders})
            """,
            [now, memory_space_id, *targets],
        )
        return int(cursor.rowcount or 0)

    async def _load_timeline_registry(
        self, db: aiosqlite.Connection, timeline_uids: set[str]
    ) -> dict[str, aiosqlite.Row]:
        if not timeline_uids:
            return {}
        normalized = sorted(timeline_uids)
        placeholders = ",".join("?" * len(normalized))
        rows = await (
            await db.execute(
                f"""
                SELECT memory_uid, memory_layer, memory_space_id, revision, status
                FROM memory_registry WHERE memory_uid IN ({placeholders})
                """,
                normalized,
            )
        ).fetchall()
        return {str(row["memory_uid"]): row for row in rows}

    @staticmethod
    def _validate_timeline_scope(
        memory_space_id: str,
        timeline_uids: set[str],
        registry: dict[str, aiosqlite.Row],
    ) -> None:
        missing = timeline_uids - registry.keys()
        if missing:
            raise TopicSourceValidationError(
                f"Unknown Timeline memory UID(s): {', '.join(sorted(missing))}"
            )
        for timeline_uid in timeline_uids:
            row = registry[timeline_uid]
            if str(row["memory_layer"]) != "timeline":
                raise TopicSourceValidationError(
                    f"Memory {timeline_uid} is not a Timeline memory"
                )
            if str(row["memory_space_id"]) != memory_space_id:
                raise TopicSourceValidationError(
                    f"Timeline {timeline_uid} belongs to another memory space"
                )

    @staticmethod
    def _validate_topic(topic: TopicMemory) -> None:
        if not topic.topic_uid.strip():
            raise ValueError("topic_uid is required")
        if not topic.memory_space_id.strip():
            raise ValueError("memory_space_id is required")
        if not topic.title.strip() or not topic.summary.strip():
            raise ValueError("Topic title and summary are required")
        for name, value in (
            ("base_importance", topic.base_importance),
            ("importance", topic.importance),
            ("confidence", topic.confidence),
        ):
            TopicMemoryStore._validate_score(name, value)

    @staticmethod
    def _validate_snapshot_members(
        topic_uid: str,
        atoms: list[TopicMemoryAtom],
        links: list[TopicTimelineLink],
        sources: list[TopicAtomSource],
    ) -> None:
        atom_uids = {atom.atom_uid for atom in atoms}
        if len(atom_uids) != len(atoms):
            raise ValueError("Duplicate Topic atom UID")
        link_timelines = {link.timeline_uid for link in links}
        if len(link_timelines) != len(links):
            raise ValueError("A Timeline memory may link to a Topic only once")
        for atom in atoms:
            if atom.topic_uid != topic_uid:
                raise ValueError("Topic atom belongs to another Topic")
            if not atom.content.strip():
                raise ValueError("Topic atom content is required")
            TopicMemoryStore._validate_score("atom importance", atom.importance)
            TopicMemoryStore._validate_score("atom confidence", atom.confidence)
        for link in links:
            if link.topic_uid != topic_uid:
                raise ValueError("Timeline link belongs to another Topic")
            if not link.time_cluster_key.strip():
                raise ValueError("time_cluster_key is required")
            TopicMemoryStore._validate_score(
                "contribution_weight", link.contribution_weight
            )
            TopicMemoryStore._validate_score(
                "semantic_similarity", link.semantic_similarity
            )
            TopicMemoryStore._validate_score("temporal_affinity", link.temporal_affinity)
        for source in sources:
            if source.topic_atom_uid not in atom_uids:
                raise ValueError("Topic atom source references an unknown Topic atom")
            if source.timeline_uid not in link_timelines:
                raise ValueError("Topic atom source requires a matching Timeline link")
            if source.source_atom_id is None and not source.source_atom_fingerprint:
                raise ValueError("Topic atom source needs an atom ID or fingerprint")
            TopicMemoryStore._validate_score(
                "source contribution_weight", source.contribution_weight
            )

    @staticmethod
    def _validate_actor_links(
        topic_uid: str,
        atoms: list[TopicMemoryAtom],
        actor_links: list[TopicActorLink],
        atom_actor_links: list[TopicAtomActorLink],
    ) -> None:
        atom_uids = {atom.atom_uid for atom in atoms}
        actor_keys: set[tuple[str, str]] = set()
        allowed_relations = {
            "speaker", "narrator", "responder", "subject",
            "mentioned", "executor", "requester",
        }
        for link in actor_links:
            if link.topic_uid != topic_uid:
                raise ValueError("Topic actor link belongs to another Topic")
            if not link.actor_id.strip() or not link.relation_type.strip():
                raise ValueError("Topic actor link needs actor_id and relation_type")
            if link.relation_type not in allowed_relations:
                raise ValueError("Unknown Topic actor relation_type")
            key = (link.actor_id, link.relation_type)
            if key in actor_keys:
                raise ValueError("Duplicate Topic actor relation")
            actor_keys.add(key)
            TopicMemoryStore._validate_score("actor confidence", link.confidence)
        atom_actor_keys: set[tuple[str, str, str, str, str]] = set()
        for link in atom_actor_links:
            if link.topic_atom_uid not in atom_uids:
                raise ValueError("Topic atom actor link references an unknown atom")
            if not link.actor_id.strip() or not link.relation_type.strip():
                raise ValueError("Topic atom actor link needs actor_id and relation_type")
            if (link.actor_id, link.relation_type) not in actor_keys:
                raise ValueError("Atom actor relation requires a matching Topic actor link")
            if not link.fragment_uid.strip():
                raise ValueError("Topic atom actor link needs fragment provenance")
            key = (
                link.topic_atom_uid,
                link.actor_id,
                link.relation_type,
                link.fragment_uid,
                str(link.timeline_uid or ""),
            )
            if key in atom_actor_keys:
                raise ValueError("Duplicate Topic atom actor relation")
            atom_actor_keys.add(key)
            TopicMemoryStore._validate_score(
                "atom actor confidence", link.confidence
            )

    @staticmethod
    def _validate_score(name: str, value: float) -> None:
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")

    @staticmethod
    async def _insert_atom(
        db: aiosqlite.Connection, atom: TopicMemoryAtom, now: float
    ) -> None:
        await db.execute(
            """
            INSERT INTO topic_memory_atoms (
                atom_uid, topic_uid, atom_type, content, canonical_content,
                importance, confidence, status, event_started_at, event_ended_at,
                created_at, updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                atom.atom_uid,
                atom.topic_uid,
                atom.atom_type,
                atom.content.strip(),
                atom.canonical_content.strip(),
                float(atom.importance),
                float(atom.confidence),
                atom.status,
                atom.event_started_at,
                atom.event_ended_at,
                float(atom.created_at),
                now,
                TopicMemoryStore._to_json(atom.metadata),
            ),
        )

    @staticmethod
    async def _insert_link(
        db: aiosqlite.Connection,
        link: TopicTimelineLink,
        topic_revision: int,
        source_revision: int,
        now: float,
    ) -> None:
        await db.execute(
            """
            INSERT INTO topic_timeline_links (
                topic_uid, timeline_uid, time_cluster_key, contribution_weight,
                semantic_similarity, temporal_affinity, source_timeline_revision,
                topic_revision, status, created_at, updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link.topic_uid,
                link.timeline_uid,
                link.time_cluster_key,
                float(link.contribution_weight),
                float(link.semantic_similarity),
                float(link.temporal_affinity),
                source_revision,
                topic_revision,
                TopicMemoryStore._enum_value(link.status),
                float(link.created_at),
                now,
                TopicMemoryStore._to_json(link.metadata),
            ),
        )

    @staticmethod
    async def _insert_fragment_links(
        db: aiosqlite.Connection,
        topic: TopicMemory,
        topic_revision: int,
        fragments: list[TopicFragmentDraft],
        now: float,
    ) -> None:
        fragment_uids = [fragment.fragment_uid for fragment in fragments]
        if len(fragment_uids) != len(set(fragment_uids)):
            raise ValueError("A fragment may link to a Topic only once")
        if not fragment_uids:
            return
        if any(fragment.memory_space_id != topic.memory_space_id for fragment in fragments):
            raise TopicSourceValidationError(
                "Topic fragment belongs to another memory space"
            )
        for fragment in fragments:
            await db.execute(
                """
                INSERT INTO topic_fragments (
                    fragment_uid, run_uid, candidate_group_uid, memory_space_id,
                    label, summary, timeline_uids, source_revisions, facts,
                    keywords, time_cluster_keys, importance, confidence,
                    embedding, started_at, ended_at, status, prompt_hash,
                    input_hash, provider_id, model_id, created_at, updated_at,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'active', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fragment_uid) DO UPDATE SET
                    run_uid = excluded.run_uid,
                    candidate_group_uid = excluded.candidate_group_uid,
                    memory_space_id = excluded.memory_space_id,
                    label = excluded.label,
                    summary = excluded.summary,
                    timeline_uids = excluded.timeline_uids,
                    source_revisions = excluded.source_revisions,
                    facts = excluded.facts,
                    keywords = excluded.keywords,
                    time_cluster_keys = excluded.time_cluster_keys,
                    importance = excluded.importance,
                    confidence = excluded.confidence,
                    embedding = excluded.embedding,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    status = 'active',
                    prompt_hash = excluded.prompt_hash,
                    input_hash = excluded.input_hash,
                    provider_id = excluded.provider_id,
                    model_id = excluded.model_id,
                    updated_at = excluded.updated_at,
                    metadata = excluded.metadata
                """,
                (
                    fragment.fragment_uid,
                    fragment.run_uid,
                    fragment.candidate_group_uid,
                    fragment.memory_space_id,
                    fragment.label,
                    fragment.summary,
                    TopicMemoryStore._to_json(fragment.timeline_uids),
                    TopicMemoryStore._to_json(fragment.source_revisions),
                    TopicMemoryStore._to_json(fragment.facts),
                    TopicMemoryStore._to_json(fragment.keywords),
                    TopicMemoryStore._to_json(fragment.time_cluster_keys),
                    float(fragment.importance),
                    float(fragment.confidence),
                    TopicMemoryStore._to_json(fragment.embedding),
                    fragment.started_at,
                    fragment.ended_at,
                    fragment.prompt_hash,
                    fragment.input_hash,
                    fragment.provider_id,
                    fragment.model_id,
                    float(fragment.created_at),
                    now,
                    TopicMemoryStore._to_json(fragment.metadata),
                ),
            )
        placeholders = ",".join("?" * len(fragment_uids))
        rows = await (
            await db.execute(
                f"""
                SELECT fragment_uid, memory_space_id
                FROM topic_fragments
                WHERE fragment_uid IN ({placeholders})
                """,
                fragment_uids,
            )
        ).fetchall()
        stored = {
            str(row["fragment_uid"]): str(row["memory_space_id"])
            for row in rows
        }
        missing = set(fragment_uids) - stored.keys()
        if missing:
            raise TopicSourceValidationError(
                "Unknown Topic fragment UID(s): " + ", ".join(sorted(missing))
            )
        if any(stored[uid] != topic.memory_space_id for uid in fragment_uids):
            raise TopicSourceValidationError(
                "Stored Topic fragment belongs to another memory space"
            )
        weight = 1.0 / max(1, len(fragment_uids))
        for fragment in fragments:
            link = TopicFragmentLink(
                topic_uid=topic.topic_uid,
                fragment_uid=fragment.fragment_uid,
                topic_revision=topic_revision,
                contribution_weight=weight,
                metadata={
                    "narrative_schema_version": fragment.metadata.get(
                        "narrative_schema_version"
                    ),
                    "source_timeline_count": len(fragment.timeline_uids),
                },
            )
            await db.execute(
                """
                INSERT INTO topic_fragment_links (
                    topic_uid, fragment_uid, topic_revision,
                    contribution_weight, status, created_at, updated_at,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link.topic_uid,
                    link.fragment_uid,
                    topic_revision,
                    float(link.contribution_weight),
                    TopicMemoryStore._enum_value(link.status),
                    float(link.created_at),
                    now,
                    TopicMemoryStore._to_json(link.metadata),
                ),
            )
        await db.execute(
            f"""
            UPDATE topic_fragments SET status = 'active', updated_at = ?
            WHERE fragment_uid IN ({placeholders})
            """,
            [now, *fragment_uids],
        )

    @staticmethod
    async def _insert_atom_source(
        db: aiosqlite.Connection,
        source: TopicAtomSource,
        source_revision: int,
    ) -> None:
        await db.execute(
            """
            INSERT INTO topic_atom_sources (
                source_uid, topic_atom_uid, timeline_uid, source_atom_id,
                source_atom_fingerprint, source_timeline_revision, source_kind,
                contribution_weight, created_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.source_uid,
                source.topic_atom_uid,
                source.timeline_uid,
                source.source_atom_id,
                source.source_atom_fingerprint,
                source_revision,
                source.source_kind,
                float(source.contribution_weight),
                float(source.created_at),
                TopicMemoryStore._to_json(source.metadata),
            ),
        )

    @staticmethod
    async def _insert_actor_link(
        db: aiosqlite.Connection,
        link: TopicActorLink,
        now: float,
    ) -> None:
        await db.execute(
            """
            INSERT INTO topic_actor_links (
                topic_uid, actor_id, actor_type, relation_type,
                display_name_snapshot, confidence, resolution_status,
                metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link.topic_uid,
                link.actor_id,
                link.actor_type,
                link.relation_type,
                link.display_name_snapshot,
                float(link.confidence),
                link.resolution_status,
                TopicMemoryStore._to_json(link.metadata),
                float(link.created_at),
                now,
            ),
        )

    @staticmethod
    async def _insert_atom_actor_link(
        db: aiosqlite.Connection,
        link: TopicAtomActorLink,
    ) -> None:
        await db.execute(
            """
            INSERT INTO topic_atom_actor_links (
                topic_atom_uid, actor_id, relation_type, fragment_uid,
                timeline_uid, confidence, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link.topic_atom_uid,
                link.actor_id,
                link.relation_type,
                link.fragment_uid,
                str(link.timeline_uid or ""),
                float(link.confidence),
                TopicMemoryStore._to_json(link.metadata),
            ),
        )

    @staticmethod
    def _row_to_topic(row: aiosqlite.Row) -> TopicMemory:
        return TopicMemory(
            topic_uid=str(row["topic_uid"]),
            memory_space_id=str(row["memory_space_id"]),
            title=str(row["title"]),
            summary=str(row["summary"]),
            revision=int(row["revision"]),
            status=TopicMemoryStatus(str(row["status"])),
            base_importance=float(row["base_importance"]),
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            last_accessed_at=row["last_accessed_at"],
            access_count=int(row["access_count"]),
            decay_anchor_at=row["decay_anchor_at"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            metadata=TopicMemoryStore._from_json(row["metadata"]),
        )

    @staticmethod
    def _aggregate_actor_refs(
        rows: list[aiosqlite.Row] | list[dict[str, Any]],
    ) -> tuple[list[TopicActorRef], list[TopicActorRef]]:
        grouped: dict[str, TopicActorRef] = {}
        participant_roles = {"speaker", "narrator", "responder"}
        for row in rows:
            item = dict(row)
            actor_id = str(item.get("actor_id") or "").strip()
            if not actor_id:
                continue
            metadata = TopicMemoryStore._from_json(item.get("metadata"))
            actor = grouped.setdefault(
                actor_id,
                TopicActorRef(
                    actor_id=actor_id,
                    actor_type=str(item.get("actor_type") or "unknown"),
                    confidence=float(item.get("confidence") or 0.0),
                    resolution_status=str(
                        item.get("resolution_status") or "inferred"
                    ),
                ),
            )
            relation_type = str(item.get("relation_type") or "")
            if relation_type and relation_type not in actor.relation_types:
                actor.relation_types.append(relation_type)
            display_name = str(item.get("display_name_snapshot") or "").strip()
            if display_name and display_name not in actor.display_names:
                actor.display_names.append(display_name)
            actor.confidence = max(actor.confidence, float(item.get("confidence") or 0.0))
            for key, target in (
                ("fragment_uids", actor.fragment_uids),
                ("timeline_uids", actor.timeline_uids),
                ("atom_uids", actor.atom_uids),
            ):
                for value in metadata.get(key, []):
                    if value not in target:
                        target.append(value)
        participants: list[TopicActorRef] = []
        mentioned: list[TopicActorRef] = []
        for actor in grouped.values():
            if participant_roles & set(actor.relation_types):
                participants.append(actor)
            if set(actor.relation_types) - participant_roles:
                mentioned.append(actor)
        return participants, mentioned

    @staticmethod
    def _candidate_to_dict(candidate: TimelineTopicCandidate) -> dict[str, Any]:
        return {
            "memory_uid": candidate.memory_uid,
            "document_id": candidate.document_id,
            "source_revision": candidate.source_revision,
            "memory_space_id": candidate.memory_space_id,
            "session_id": candidate.session_id,
            "persona_id": candidate.persona_id,
            "role_bindings": candidate.role_bindings,
            "source_window": candidate.source_window,
            "edit_origin": candidate.edit_origin,
            "traceability": candidate.traceability,
            "content": candidate.content,
            "summary": candidate.summary,
            "topics": candidate.topics,
            "key_facts": candidate.key_facts,
            "atom_fingerprints": candidate.atom_fingerprints,
            "atom_contents": candidate.atom_contents,
            "started_at": candidate.started_at,
            "ended_at": candidate.ended_at,
            "time_cluster_key": candidate.time_cluster_key,
            "features": candidate.features,
        }

    @staticmethod
    def _fragment_to_dict(fragment: TopicFragmentDraft) -> dict[str, Any]:
        return {
            "fragment_uid": fragment.fragment_uid,
            "run_uid": fragment.run_uid,
            "candidate_group_uid": fragment.candidate_group_uid,
            "memory_space_id": fragment.memory_space_id,
            "label": fragment.label,
            "summary": fragment.summary,
            "timeline_uids": fragment.timeline_uids,
            "source_revisions": fragment.source_revisions,
            "facts": fragment.facts,
            "keywords": fragment.keywords,
            "time_cluster_keys": fragment.time_cluster_keys,
            "importance": fragment.importance,
            "confidence": fragment.confidence,
            "started_at": fragment.started_at,
            "ended_at": fragment.ended_at,
            "provider_id": fragment.provider_id,
            "model_id": fragment.model_id,
            "metadata": fragment.metadata,
        }

    @staticmethod
    def _dict_to_candidate(payload: dict[str, Any]) -> TimelineTopicCandidate:
        return TimelineTopicCandidate(
            memory_uid=str(payload.get("memory_uid") or ""),
            document_id=int(payload.get("document_id") or 0),
            source_revision=max(1, int(payload.get("source_revision") or 1)),
            memory_space_id=str(payload.get("memory_space_id") or ""),
            session_id=payload.get("session_id"),
            persona_id=(str(payload.get("persona_id") or "").strip() or None),
            role_bindings=(
                payload.get("role_bindings", {})
                if isinstance(payload.get("role_bindings"), dict)
                else {}
            ),
            source_window=(
                payload.get("source_window", {})
                if isinstance(payload.get("source_window"), dict)
                else {}
            ),
            edit_origin=(str(payload.get("edit_origin") or "").strip() or None),
            traceability=(
                str(payload.get("traceability") or "").strip() or None
            ),
            content=str(payload.get("content") or ""),
            summary=str(payload.get("summary") or ""),
            topics=[str(item) for item in payload.get("topics", [])],
            key_facts=[str(item) for item in payload.get("key_facts", [])],
            atom_fingerprints=[
                str(item) for item in payload.get("atom_fingerprints", [])
            ],
            atom_contents=[str(item) for item in payload.get("atom_contents", [])],
            started_at=payload.get("started_at"),
            ended_at=payload.get("ended_at"),
            time_cluster_key=str(payload.get("time_cluster_key") or ""),
            features=(
                payload.get("features", {})
                if isinstance(payload.get("features"), dict)
                else {}
            ),
        )

    @staticmethod
    def _row_to_candidate_group(row: aiosqlite.Row) -> TopicCandidateGroup:
        timeline_uids = json.loads(row["timeline_uids"] or "[]")
        time_cluster_keys = json.loads(row["time_cluster_keys"] or "[]")
        shared_signals = json.loads(row["shared_signals"] or "[]")
        return TopicCandidateGroup(
            group_uid=str(row["group_uid"]),
            run_uid=str(row["run_uid"]),
            group_index=int(row["group_index"]),
            memory_space_id=str(row["memory_space_id"]),
            label=str(row["label"]),
            timeline_uids=[str(item) for item in timeline_uids],
            time_cluster_keys=[str(item) for item in time_cluster_keys],
            cohesion=float(row["cohesion"]),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            shared_signals=[str(item) for item in shared_signals],
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            metadata=TopicMemoryStore._from_json(row["metadata"]),
        )

    @staticmethod
    def _row_to_fragment(row: aiosqlite.Row) -> TopicFragmentDraft:
        def json_list(name: str) -> list[Any]:
            try:
                value = json.loads(row[name] or "[]")
            except (TypeError, json.JSONDecodeError):
                return []
            return value if isinstance(value, list) else []

        return TopicFragmentDraft(
            fragment_uid=str(row["fragment_uid"]),
            run_uid=str(row["run_uid"]),
            candidate_group_uid=str(row["candidate_group_uid"]),
            memory_space_id=str(row["memory_space_id"]),
            label=str(row["label"]),
            summary=str(row["summary"]),
            timeline_uids=[str(item) for item in json_list("timeline_uids")],
            source_revisions={
                str(key): int(value)
                for key, value in TopicMemoryStore._from_json(
                    row["source_revisions"]
                ).items()
            },
            facts=[item for item in json_list("facts") if isinstance(item, dict)],
            keywords=[str(item) for item in json_list("keywords")],
            time_cluster_keys=[str(item) for item in json_list("time_cluster_keys")],
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            embedding=[float(item) for item in json_list("embedding")],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            status=str(row["status"]),
            prompt_hash=str(row["prompt_hash"]),
            input_hash=str(row["input_hash"]),
            provider_id=str(row["provider_id"] or ""),
            model_id=str(row["model_id"] or ""),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            metadata=TopicMemoryStore._from_json(row["metadata"]),
        )

    @staticmethod
    def _to_json(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)

    @staticmethod
    def _from_json(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _enum_value(value: Any) -> str:
        return str(getattr(value, "value", value))


__all__ = [
    "TopicMemoryStore",
    "TopicRevisionConflict",
    "TopicSourceValidationError",
]
