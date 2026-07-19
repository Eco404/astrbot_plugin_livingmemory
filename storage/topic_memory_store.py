"""Transactional SQLite storage for derived topic memories."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import aiosqlite

from ..core.models.topic_memory import (
    TopicAtomSource,
    TopicLinkStatus,
    TopicMaintenanceMode,
    TopicMaintenanceRun,
    TopicMaintenanceStatus,
    TopicMemory,
    TopicMemoryAtom,
    TopicMemoryStatus,
    TopicTimelineLink,
    TimelineTopicCandidate,
    TopicCandidateGroup,
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
            CREATE TABLE IF NOT EXISTS topic_maintenance_runs (
                run_uid TEXT PRIMARY KEY,
                memory_space_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                cursor_memory_uid TEXT,
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

    async def save_topic_snapshot(
        self,
        topic: TopicMemory,
        *,
        atoms: list[TopicMemoryAtom],
        links: list[TopicTimelineLink],
        atom_sources: list[TopicAtomSource],
        expected_revision: int | None = None,
    ) -> TopicMemory:
        """Atomically replace one generated Topic snapshot and its provenance."""
        self._validate_topic(topic)
        self._validate_snapshot_members(topic.topic_uid, atoms, links, atom_sources)
        timeline_uids = {link.timeline_uid for link in links}
        timeline_uids.update(source.timeline_uid for source in atom_sources)

        async with self._connect() as db:
            try:
                cursor = await db.execute(
                    "SELECT revision, created_at FROM topic_memories WHERE topic_uid = ?",
                    (topic.topic_uid,),
                )
                existing = await cursor.fetchone()
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
                        raise TopicRevisionConflict(
                            f"Topic {topic.topic_uid} does not exist"
                        )
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
                    "DELETE FROM topic_memory_atoms WHERE topic_uid = ?",
                    (topic.topic_uid,),
                )
                await db.execute(
                    "DELETE FROM topic_timeline_links WHERE topic_uid = ?",
                    (topic.topic_uid,),
                )
                for atom in atoms:
                    await self._insert_atom(db, atom, now)
                for link in links:
                    source_revision = int(registry[link.timeline_uid]["revision"])
                    await self._insert_link(db, link, revision, source_revision, now)
                for source in atom_sources:
                    source_revision = int(registry[source.timeline_uid]["revision"])
                    await self._insert_atom_source(db, source, source_revision)
                await db.commit()
            except Exception:
                await db.rollback()
                raise

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
        return self._row_to_topic(row) if row else None

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

    async def get_topic_provenance(self, topic_uid: str) -> dict[str, Any]:
        async with self._connect() as db:
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
        return {
            "links": [dict(row) for row in links],
            "atoms": [dict(row) for row in atoms],
            "atom_sources": [dict(row) for row in sources],
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

    async def delete_topic(self, topic_uid: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM topic_memories WHERE topic_uid = ?", (topic_uid,)
            )
            await db.commit()
            return bool(cursor.rowcount)

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
        cursor_memory_uid: str | None = None,
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
            if status_value in {
                TopicMaintenanceStatus.COMPLETED.value,
                TopicMaintenanceStatus.FAILED.value,
                TopicMaintenanceStatus.CANCELLED.value,
            }:
                fields.append("completed_at = ?")
                params.append(time.time())
        updates = {
            "cursor_memory_uid": cursor_memory_uid,
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
        return dict(row) if row else None

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
                    SELECT i.candidate_payload
                    FROM topic_maintenance_items i
                    JOIN memory_registry r ON r.memory_uid = i.timeline_uid
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
    def _candidate_to_dict(candidate: TimelineTopicCandidate) -> dict[str, Any]:
        return {
            "memory_uid": candidate.memory_uid,
            "document_id": candidate.document_id,
            "source_revision": candidate.source_revision,
            "memory_space_id": candidate.memory_space_id,
            "session_id": candidate.session_id,
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
    def _dict_to_candidate(payload: dict[str, Any]) -> TimelineTopicCandidate:
        return TimelineTopicCandidate(
            memory_uid=str(payload.get("memory_uid") or ""),
            document_id=int(payload.get("document_id") or 0),
            source_revision=max(1, int(payload.get("source_revision") or 1)),
            memory_space_id=str(payload.get("memory_space_id") or ""),
            session_id=payload.get("session_id"),
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
