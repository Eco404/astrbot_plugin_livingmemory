"""Deterministic, resumable discovery of Topic-memory candidates."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import aiosqlite

from astrbot.api import logger

from ...storage.topic_memory_store import TopicMemoryStore
from ..models.topic_memory import (
    TimelineTopicCandidate,
    TopicCandidateGroup,
    TopicMaintenanceMode,
    TopicMaintenanceRun,
    TopicMaintenanceStatus,
)


ProgressCallback = Callable[[dict[str, Any]], Any]
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+")


class TopicMaintenanceManager:
    """Build reviewable candidates without writing final Topic memories."""

    def __init__(self, db_path: str, store: TopicMemoryStore | None = None):
        self.db_path = db_path
        self.store = store or TopicMemoryStore(db_path)

    async def start_scan(
        self,
        memory_space_id: str,
        *,
        mode: TopicMaintenanceMode = TopicMaintenanceMode.FULL,
        batch_size: int = 100,
        time_gap_seconds: float = 21600.0,
        similarity_threshold: float = 0.52,
        since: float | None = None,
        timeline_uids: list[str] | None = None,
        only_unindexed: bool = False,
        progress_callback: ProgressCallback | None = None,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        """Create a scan run and process it until completion or a test pause."""
        if not memory_space_id.strip():
            raise ValueError("memory_space_id is required")
        mode = TopicMaintenanceMode(mode)
        batch_size = max(1, min(int(batch_size), 1000))
        time_gap_seconds = max(60.0, float(time_gap_seconds))
        if not 0.0 <= float(similarity_threshold) <= 1.0:
            raise ValueError("similarity_threshold must be between 0 and 1")
        selected_timeline_uids = self._normalized_uids(timeline_uids)
        if mode is TopicMaintenanceMode.INCREMENTAL and (
            since is None and selected_timeline_uids is None
        ):
            raise ValueError(
                "Incremental scans require a since timestamp or selected Timeline IDs"
            )

        total = await self._count_timelines(
            memory_space_id,
            since=since,
            timeline_uids=selected_timeline_uids,
            only_unindexed=only_unindexed,
        )
        run = TopicMaintenanceRun(
            memory_space_id=memory_space_id,
            mode=mode,
            total_items=total,
            config={
                "batch_size": batch_size,
                "time_gap_seconds": time_gap_seconds,
                "similarity_threshold": float(similarity_threshold),
                "since": since,
                "timeline_uids": selected_timeline_uids,
                "only_unindexed": bool(only_unindexed),
                "candidate_schema_version": 1,
            },
        )
        await self.store.create_maintenance_run(run)
        return await self.resume_scan(
            run.run_uid,
            progress_callback=progress_callback,
            max_batches=max_batches,
        )

    async def resume_scan(
        self,
        run_uid: str,
        *,
        progress_callback: ProgressCallback | None = None,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        """Resume a persisted run; completed runs are read-only and idempotent."""
        run = await self.store.get_maintenance_run(run_uid)
        if run is None:
            raise ValueError(f"Topic maintenance run not found: {run_uid}")
        if str(run["status"]) == TopicMaintenanceStatus.COMPLETED.value:
            groups = await self.store.list_candidate_groups(run_uid)
            return self._result(run, groups)

        config = self._json_dict(run.get("config"))
        batch_size = max(1, min(int(config.get("batch_size", 100)), 1000))
        time_gap_seconds = max(60.0, float(config.get("time_gap_seconds", 21600.0)))
        similarity_threshold = float(config.get("similarity_threshold", 0.52))
        since = self._optional_float(config.get("since"))
        timeline_uids = self._normalized_uids(config.get("timeline_uids"))
        only_unindexed = bool(config.get("only_unindexed", False))
        memory_space_id = str(run["memory_space_id"])
        processed = await self.store.get_processed_timeline_uids(run_uid)
        processed_count = len(processed)
        await self.store.update_maintenance_run(
            run_uid,
            status=TopicMaintenanceStatus.RUNNING,
            processed_items=processed_count,
        )

        batch_number = 0
        try:
            while True:
                batch = await self._read_candidate_batch(
                    memory_space_id,
                    run_uid=run_uid,
                    limit=batch_size,
                    since=since,
                    timeline_uids=timeline_uids,
                    only_unindexed=only_unindexed,
                )
                if not batch:
                    break
                await self.store.save_scan_items(run_uid, batch)
                processed_count += len(batch)
                processed.update(item.memory_uid for item in batch)
                await self.store.update_maintenance_run(
                    run_uid,
                    cursor_memory_uid=batch[-1].memory_uid,
                    processed_items=processed_count,
                )
                batch_number += 1
                await self._emit_progress(
                    progress_callback,
                    {
                        "run_uid": run_uid,
                        "status": "running",
                        "processed_items": processed_count,
                        "total_items": int(run["total_items"]),
                        "cursor_memory_uid": batch[-1].memory_uid,
                    },
                )
                if max_batches is not None and batch_number >= max(1, max_batches):
                    await self.store.update_maintenance_run(
                        run_uid,
                        status=TopicMaintenanceStatus.PENDING,
                        processed_items=processed_count,
                    )
                    paused = await self.store.get_maintenance_run(run_uid)
                    return self._result(paused or run, [])

            candidates = await self.store.get_scan_items(run_uid)
            current_total = await self._count_timelines(
                memory_space_id,
                since=since,
                timeline_uids=timeline_uids,
                only_unindexed=only_unindexed,
            )
            if len(candidates) < current_total:
                await self.store.update_maintenance_run(
                    run_uid,
                    status=TopicMaintenanceStatus.PENDING,
                    total_items=current_total,
                    processed_items=len(candidates),
                )
                return await self.resume_scan(
                    run_uid,
                    progress_callback=progress_callback,
                    max_batches=max_batches,
                )
            candidates = self.assign_time_clusters(
                candidates,
                gap_seconds=time_gap_seconds,
            )
            # Persist the final cluster keys so a later LLM stage can be resumed
            # without recomputing against potentially changed source rows.
            await self.store.save_scan_items(run_uid, candidates)
            groups = self.build_candidate_groups(
                run_uid,
                memory_space_id,
                candidates,
                similarity_threshold=similarity_threshold,
            )
            await self.store.replace_candidate_groups(run_uid, groups)
            await self.store.update_maintenance_run(
                run_uid,
                status=TopicMaintenanceStatus.COMPLETED,
                total_items=current_total,
                processed_items=len(candidates),
                created_topics=0,
                updated_topics=0,
            )
            completed = await self.store.get_maintenance_run(run_uid)
            await self._emit_progress(
                progress_callback,
                {
                    "run_uid": run_uid,
                    "status": "completed",
                    "processed_items": len(candidates),
                    "total_items": int(run["total_items"]),
                    "candidate_groups": len(groups),
                },
            )
            return self._result(completed or run, groups)
        except asyncio.CancelledError:
            await asyncio.shield(
                self.store.update_maintenance_run(
                    run_uid,
                    status=TopicMaintenanceStatus.PENDING,
                    processed_items=processed_count,
                )
            )
            raise
        except Exception as exc:
            await self.store.update_maintenance_run(
                run_uid,
                status=TopicMaintenanceStatus.FAILED,
                processed_items=processed_count,
                error=str(exc)[:1000],
            )
            logger.error(
                f"[TopicMaintenance] 候选扫描失败 (run_uid={run_uid})",
                exc_info=True,
            )
            raise

    async def _count_timelines(
        self,
        memory_space_id: str,
        *,
        since: float | None,
        timeline_uids: list[str] | None = None,
        only_unindexed: bool = False,
    ) -> int:
        where = [
            "r.memory_space_id = ?",
            "r.memory_layer = 'timeline'",
            "r.status = 'active'",
        ]
        params: list[Any] = [memory_space_id]
        if since is not None:
            where.append("r.updated_at >= ?")
            params.append(float(since))
        if timeline_uids is not None:
            where.append("r.memory_uid IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(timeline_uids, ensure_ascii=False))
        if only_unindexed:
            where.append(self._unindexed_timeline_predicate())
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    f"SELECT COUNT(*) FROM memory_registry r WHERE {' AND '.join(where)}",
                    params,
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def _read_candidate_batch(
        self,
        memory_space_id: str,
        *,
        run_uid: str,
        limit: int,
        since: float | None,
        timeline_uids: list[str] | None = None,
        only_unindexed: bool = False,
    ) -> list[TimelineTopicCandidate]:
        where = [
            "r.memory_space_id = ?",
            "r.memory_layer = 'timeline'",
            "r.status = 'active'",
            "i.timeline_uid IS NULL",
        ]
        params: list[Any] = [run_uid, memory_space_id]
        if since is not None:
            where.append("r.updated_at >= ?")
            params.append(float(since))
        if timeline_uids is not None:
            where.append("r.memory_uid IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(timeline_uids, ensure_ascii=False))
        if only_unindexed:
            where.append(self._unindexed_timeline_predicate())
        params.append(int(limit))

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"""
                    SELECT r.memory_uid, r.document_id, r.revision,
                           r.memory_space_id, r.created_at AS registry_created_at,
                           d.text, d.metadata,
                           s.session_id AS source_session_id,
                           s.started_at, s.ended_at
                    FROM memory_registry r
                    JOIN documents d ON d.id = r.document_id
                    LEFT JOIN memory_source_spans s ON s.memory_uid = r.memory_uid
                    LEFT JOIN topic_maintenance_items i
                      ON i.run_uid = ? AND i.timeline_uid = r.memory_uid
                     AND i.source_revision = r.revision
                     AND i.status = 'processed'
                    WHERE {' AND '.join(where)}
                    ORDER BY COALESCE(s.started_at, r.created_at), r.document_id
                    LIMIT ?
                    """,
                    params,
                )
            ).fetchall()
            atom_map = await self._load_atoms(db, [int(row["document_id"]) for row in rows])

        return [self._row_to_candidate(row, atom_map) for row in rows]

    async def list_unindexed_timelines(
        self,
        memory_space_id: str,
    ) -> list[dict[str, Any]]:
        """List active Timeline revisions absent from the active Topic graph."""
        if not str(memory_space_id or "").strip():
            raise ValueError("memory_space_id is required")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"""
                    SELECT r.memory_uid, r.revision, r.created_at, r.updated_at,
                           d.text, d.metadata,
                           s.started_at, s.ended_at
                    FROM memory_registry r
                    JOIN documents d ON d.id = r.document_id
                    LEFT JOIN memory_source_spans s ON s.memory_uid = r.memory_uid
                    WHERE r.memory_space_id = ?
                      AND r.memory_layer = 'timeline'
                      AND r.status = 'active'
                      AND {self._unindexed_timeline_predicate()}
                    ORDER BY COALESCE(s.started_at, r.created_at), r.document_id
                    """,
                    (memory_space_id,),
                )
            ).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            metadata = self._json_dict(row["metadata"])
            summary = str(
                metadata.get("canonical_summary")
                or metadata.get("persona_summary")
                or row["text"]
                or ""
            ).strip()
            summary = re.sub(r"\s+", " ", summary)
            items.append(
                {
                    "timeline_uid": str(row["memory_uid"]),
                    "revision": max(1, int(row["revision"] or 1)),
                    "summary": summary[:300],
                    "topics": self._string_list(metadata.get("topics")),
                    "created_at": self._optional_float(row["created_at"]),
                    "updated_at": self._optional_float(row["updated_at"]),
                    "started_at": self._optional_float(row["started_at"]),
                    "ended_at": self._optional_float(row["ended_at"]),
                }
            )
        return items

    @staticmethod
    def _unindexed_timeline_predicate() -> str:
        return """
        NOT EXISTS (
            SELECT 1
            FROM topic_timeline_links indexed_link
            JOIN topic_memories indexed_topic
              ON indexed_topic.topic_uid = indexed_link.topic_uid
            WHERE indexed_link.timeline_uid = r.memory_uid
              AND indexed_link.source_timeline_revision = r.revision
              AND indexed_link.status = 'active'
              AND indexed_topic.status = 'active'
              AND indexed_topic.memory_space_id = r.memory_space_id
        )
        """.strip()

    @classmethod
    def _normalized_uids(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("timeline_uids must be a list")
        return cls._unique_strings(value)

    @staticmethod
    def _unique_strings(value: Any) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in value or []:
            normalized = str(item or "").strip()
            if normalized and normalized not in seen:
                result.append(normalized)
                seen.add(normalized)
        return result

    async def _load_atoms(
        self,
        db: aiosqlite.Connection,
        document_ids: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        if not document_ids:
            return {}
        table = await (
            await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_atoms'"
            )
        ).fetchone()
        if table is None:
            return {}
        placeholders = ",".join("?" * len(document_ids))
        rows = await (
            await db.execute(
                f"""
                SELECT id, parent_memory_id, atom_type, content, metadata
                FROM memory_atoms
                WHERE parent_memory_id IN ({placeholders}) AND status = 'active'
                ORDER BY parent_memory_id, id
                """,
                document_ids,
            )
        ).fetchall()
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(int(row["parent_memory_id"]), []).append(dict(row))
        return result

    def _row_to_candidate(
        self,
        row: aiosqlite.Row,
        atom_map: dict[int, list[dict[str, Any]]],
    ) -> TimelineTopicCandidate:
        metadata = self._json_dict(row["metadata"])
        content = str(row["text"] or "")
        summary = str(
            metadata.get("canonical_summary")
            or metadata.get("persona_summary")
            or content
        )
        topics = self._string_list(metadata.get("topics"))
        key_facts = self._string_list(metadata.get("key_facts"))
        atoms = atom_map.get(int(row["document_id"]), [])
        atom_contents = [str(atom.get("content") or "") for atom in atoms]
        atom_fingerprints = [
            self.fingerprint_text(
                f"{atom.get('atom_type') or 'unknown'}:{atom.get('content') or ''}"
            )
            for atom in atoms
            if str(atom.get("content") or "").strip()
        ]
        normalized_topics = [self.normalize_text(item) for item in topics]
        fact_fingerprints = [self.fingerprint_text(item) for item in key_facts]
        lexical_tokens = sorted(
            self.tokenize(" ".join([summary, *topics, *key_facts, *atom_contents]))
        )
        signals = sorted(
            {
                *(f"topic:{item}" for item in normalized_topics if item),
                *(f"fact:{item}" for item in fact_fingerprints),
                *(f"atom:{item}" for item in atom_fingerprints),
            }
        )
        return TimelineTopicCandidate(
            memory_uid=str(row["memory_uid"]),
            document_id=int(row["document_id"]),
            source_revision=max(1, int(row["revision"])),
            memory_space_id=str(row["memory_space_id"]),
            session_id=(row["source_session_id"] or metadata.get("session_id")),
            content=content,
            summary=summary,
            topics=topics,
            key_facts=key_facts,
            atom_fingerprints=atom_fingerprints,
            atom_contents=atom_contents,
            started_at=self._optional_float(row["started_at"]),
            ended_at=self._optional_float(row["ended_at"]),
            features={
                "normalized_topics": normalized_topics,
                "fact_fingerprints": fact_fingerprints,
                "lexical_tokens": lexical_tokens,
                "signals": signals,
                "feature_schema_version": 1,
            },
        )

    @classmethod
    def assign_time_clusters(
        cls,
        candidates: list[TimelineTopicCandidate],
        *,
        gap_seconds: float,
    ) -> list[TimelineTopicCandidate]:
        """Group chronologically adjacent fragments without using row counts."""
        ordered = sorted(
            candidates,
            key=lambda item: (
                item.started_at is None,
                item.started_at or 0.0,
                item.document_id,
            ),
        )
        result: list[TimelineTopicCandidate] = []
        cluster_key = ""
        previous_end: float | None = None
        for candidate in ordered:
            if candidate.started_at is None:
                cluster_key = cls._stable_key(
                    "time-unknown", candidate.memory_space_id, candidate.memory_uid
                )
                previous_end = None
            elif (
                not cluster_key
                or previous_end is None
                or candidate.started_at - previous_end > gap_seconds
            ):
                cluster_key = cls._stable_key(
                    "time", candidate.memory_space_id, candidate.memory_uid
                )
            result.append(replace(candidate, time_cluster_key=cluster_key))
            if candidate.ended_at is not None:
                previous_end = max(previous_end or candidate.ended_at, candidate.ended_at)
            elif candidate.started_at is not None:
                previous_end = max(previous_end or candidate.started_at, candidate.started_at)
        return result

    @classmethod
    def build_candidate_groups(
        cls,
        run_uid: str,
        memory_space_id: str,
        candidates: list[TimelineTopicCandidate],
        *,
        similarity_threshold: float,
    ) -> list[TopicCandidateGroup]:
        """Create broad deterministic proposals; they are not final Topics."""
        if not candidates:
            return []
        parents = list(range(len(candidates)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        pair_scores: dict[tuple[int, int], float] = {}
        for left in range(len(candidates)):
            for right in range(left + 1, len(candidates)):
                score = cls.candidate_similarity(candidates[left], candidates[right])
                pair_scores[(left, right)] = score
                same_time_cluster = bool(
                    candidates[left].time_cluster_key
                    and candidates[left].time_cluster_key
                    == candidates[right].time_cluster_key
                )
                # A time cluster is deliberately a broad review window. It may
                # contain several subjects and must later be split by the LLM;
                # treating it as one final Topic would be incorrect.
                if same_time_cluster or score >= similarity_threshold:
                    union(left, right)

        grouped: dict[int, list[int]] = {}
        for index in range(len(candidates)):
            grouped.setdefault(find(index), []).append(index)

        ordered_groups = sorted(
            grouped.values(),
            key=lambda indexes: min(
                candidates[index].started_at or float("inf") for index in indexes
            ),
        )
        result: list[TopicCandidateGroup] = []
        for group_index, indexes in enumerate(ordered_groups, 1):
            members = [candidates[index] for index in indexes]
            scores = [
                pair_scores[tuple(sorted((left, right)))]
                for position, left in enumerate(indexes)
                for right in indexes[position + 1 :]
            ]
            cohesion = sum(scores) / len(scores) if scores else 1.0
            timeline_uids = [item.memory_uid for item in members]
            time_cluster_keys = sorted({item.time_cluster_key for item in members})
            shared_signals = cls._shared_signals(members)
            label = cls._candidate_label(members)
            group_uid = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"livingmemory:topic-candidate:{run_uid}:"
                    + ":".join(sorted(timeline_uids)),
                )
            )
            starts = [item.started_at for item in members if item.started_at is not None]
            ends = [item.ended_at for item in members if item.ended_at is not None]
            result.append(
                TopicCandidateGroup(
                    group_uid=group_uid,
                    run_uid=run_uid,
                    group_index=group_index,
                    memory_space_id=memory_space_id,
                    label=label,
                    timeline_uids=timeline_uids,
                    time_cluster_keys=time_cluster_keys,
                    cohesion=round(cohesion, 6),
                    started_at=min(starts) if starts else None,
                    ended_at=max(ends) if ends else None,
                    shared_signals=shared_signals,
                    metadata={
                        "candidate_count": len(members),
                        "time_cluster_count": len(time_cluster_keys),
                        "similarity_threshold": similarity_threshold,
                        "requires_llm_review": True,
                        "time_cluster_is_broad_context": True,
                        "algorithm_version": 1,
                    },
                )
            )
        return result

    @classmethod
    def candidate_similarity(
        cls,
        left: TimelineTopicCandidate,
        right: TimelineTopicCandidate,
    ) -> float:
        left_features = left.features
        right_features = right.features
        topic_score = cls._jaccard(
            left_features.get("normalized_topics", []),
            right_features.get("normalized_topics", []),
        )
        fact_score = cls._jaccard(
            left_features.get("fact_fingerprints", []),
            right_features.get("fact_fingerprints", []),
        )
        atom_score = cls._jaccard(left.atom_fingerprints, right.atom_fingerprints)
        lexical_score = cls._jaccard(
            left_features.get("lexical_tokens", []),
            right_features.get("lexical_tokens", []),
        )
        same_time_cluster = bool(
            left.time_cluster_key
            and left.time_cluster_key == right.time_cluster_key
        )
        score = (
            0.35 * topic_score
            + 0.30 * fact_score
            + 0.25 * atom_score
            + 0.25 * lexical_score
            + (0.15 if same_time_cluster else 0.0)
        )
        return min(1.0, round(score, 6))

    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return "".join(character for character in normalized if character.isalnum())

    @classmethod
    def fingerprint_text(cls, value: str) -> str:
        return hashlib.sha256(cls.normalize_text(value).encode("utf-8")).hexdigest()

    @classmethod
    def tokenize(cls, value: str) -> set[str]:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        tokens: set[str] = set()
        for chunk in _WORD_RE.findall(normalized):
            if _CJK_RE.fullmatch(chunk):
                if len(chunk) == 1:
                    tokens.add(chunk)
                else:
                    tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
            elif len(chunk) >= 2:
                tokens.add(chunk)
        return tokens

    @staticmethod
    def _jaccard(left: Any, right: Any) -> float:
        left_set, right_set = set(left or []), set(right or [])
        if not left_set or not right_set:
            return 0.0
        return len(left_set & right_set) / len(left_set | right_set)

    @staticmethod
    def _shared_signals(members: list[TimelineTopicCandidate]) -> list[str]:
        counts: Counter[str] = Counter()
        for member in members:
            counts.update(set(member.features.get("signals", [])))
        threshold = 1 if len(members) == 1 else 2
        return sorted(signal for signal, count in counts.items() if count >= threshold)

    @staticmethod
    def _candidate_label(members: list[TimelineTopicCandidate]) -> str:
        topics = [topic.strip() for member in members for topic in member.topics if topic.strip()]
        if topics:
            return Counter(topics).most_common(1)[0][0]
        summary = next((member.summary.strip() for member in members if member.summary.strip()), "")
        return summary[:48] or f"候选组 {members[0].memory_uid[:8]}"

    @staticmethod
    def _stable_key(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
        return f"{prefix}-{digest}"

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    async def _emit_progress(
        callback: ProgressCallback | None,
        payload: dict[str, Any],
    ) -> None:
        if callback is None:
            return
        result = callback(payload)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _result(
        run: dict[str, Any],
        groups: list[TopicCandidateGroup],
    ) -> dict[str, Any]:
        return {
            "run_uid": str(run["run_uid"]),
            "memory_space_id": str(run["memory_space_id"]),
            "mode": str(run["mode"]),
            "status": str(run["status"]),
            "total_items": int(run["total_items"]),
            "processed_items": int(run["processed_items"]),
            "candidate_groups": len(groups),
            "groups": groups,
            "cursor_memory_uid": run.get("cursor_memory_uid"),
            "error": run.get("error"),
        }


__all__ = ["TopicMaintenanceManager"]
