"""Materialization and source-grounded validation of Topic snapshots."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from typing import (
    Any,
)

from astrbot.api import logger

from ..affect_memory import (
    affect_signature,
    aggregate_affect_profile,
    normalize_affect_event,
)
from ..embedding_signature import (
    TOPIC_CENTROID_EMBEDDING_FORMAT,
    make_embedding_signature,
)
from ..fact_temporal import (
    aggregate_fact_temporal,
    normalize_fact_temporal,
)
from ..importance_policy import (
    IMPORTANCE_POLICY_VERSION,
    aggregate_source_importance,
    evidence_strength,
    fragment_semantic_importance,
    topic_base_importance,
    topic_effective_importance,
    topic_semantic_importance,
)
from ..models.conversation_models import (
    build_role_bindings,
    stable_actor_id,
)
from ..models.platform_identity import canonical_platform
from ..models.topic_memory import (
    TimelineTopicCandidate,
    TopicActorLink,
    TopicAtomActorLink,
    TopicAtomSource,
    TopicCandidateGroup,
    TopicFragmentDraft,
    TopicMemory,
    TopicMemoryAtom,
    TopicMemoryStatus,
    TopicTimelineLink,
)
from .topic_build_contracts import (
    _FRAGMENT_PROMPT_VERSION,
    _MATCHING_ALGORITHM_VERSION,
    _NARRATIVE_SCHEMA_VERSION,
    TopicBuildValidationError,
)
from .topic_fragment_identity import (
    fragment_semantic_discriminator,
    logical_fragment_uid,
)
from .topic_maintenance_manager import TopicMaintenanceManager


class TopicSnapshotPublisherMixin:
    def _materialize_snapshot(
        self,
        run_uid: str,
        memory_space_id: str,
        synthesis: dict[str, Any],
        fragments: list[TopicFragmentDraft],
        candidate_map: dict[str, TimelineTopicCandidate],
        existing: TopicMemory | None,
    ) -> tuple[
        TopicMemory,
        list[TopicMemoryAtom],
        list[TopicTimelineLink],
        list[TopicAtomSource],
        list[TopicActorLink],
        list[TopicAtomActorLink],
    ]:
        timeline_uids = sorted(
            {uid for item in fragments for uid in item.timeline_uids}
        )
        cluster_sizes: Counter[str] = Counter()
        timeline_cluster: dict[str, str] = {}
        for uid in timeline_uids:
            candidate = candidate_map.get(uid)
            key = candidate.time_cluster_key if candidate else ""
            if not key:
                key = next(
                    (
                        str(
                            item.metadata.get("timeline_cluster_map", {}).get(uid) or ""
                        )
                        for item in fragments
                        if uid in item.timeline_uids
                    ),
                    "",
                )
            key = key or f"unknown:{uid}"
            timeline_cluster[uid] = key
            cluster_sizes[key] += 1
        embedding = self._average_vectors([item.embedding for item in fragments])
        topic_uid = (
            existing.topic_uid
            if existing
            else str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"livingmemory:topic:{memory_space_id}:"
                    f"{self._norm(synthesis['title'])}:{timeline_uids[0] if timeline_uids else run_uid}",
                )
            )
        )
        starts = [item.started_at for item in fragments if item.started_at is not None]
        ends = [item.ended_at for item in fragments if item.ended_at is not None]
        evidence_clusters = set(timeline_cluster.values())
        semantic_importance = topic_semantic_importance(
            (item.importance, item.confidence) for item in fragments
        )
        source_importance = aggregate_source_importance(
            {
                "timeline_uid": uid,
                "time_cluster_key": timeline_cluster[uid],
                "base_importance": (
                    candidate_map[uid].base_importance if uid in candidate_map else 0.5
                ),
                "effective_importance": (
                    candidate_map[uid].effective_importance
                    if uid in candidate_map
                    else 0.5
                ),
                "importance_revision": (
                    candidate_map[uid].importance_revision
                    if uid in candidate_map
                    else 1
                ),
                "weight": min(
                    1.0,
                    0.6 + 0.4 / max(1, cluster_sizes[timeline_cluster[uid]]),
                ),
            }
            for uid in timeline_uids
        )
        source_base_component = float(source_importance["source_base_component"])
        base_importance = topic_base_importance(
            semantic_importance,
            source_base_component,
        )
        importance = topic_effective_importance(
            base_importance,
            float(source_importance["dynamic_factor"]),
        )
        topic_evidence_strength = evidence_strength(
            cluster_count=len(evidence_clusters),
            timeline_count=len(timeline_uids),
        )
        raw_topic_confidence = self._score(synthesis.get("confidence"), 0.7)
        topic_confidence, topic_confidence_audit = self._calibrate_confidence(
            raw_topic_confidence,
            independent_clusters=len(evidence_clusters),
            supporting_timelines=len(timeline_uids),
        )
        repair_events = [
            item
            for item in synthesis.get("validation_repairs", [])
            if isinstance(item, dict)
        ] + [
            value
            for fragment in fragments
            for value in fragment.metadata.get("validation_repairs", [])
            if isinstance(value, dict)
        ]
        repair_audit = self._repair_audit(repair_events)
        repair_count = len(repair_events)
        quality_units = max(
            1,
            len(synthesis.get("atoms", []))
            + sum(len(fragment.facts) for fragment in fragments),
        )
        repair_ratio = min(
            1.0,
            float(repair_audit["weighted_units"]) / quality_units,
        )
        quality_penalty = min(0.15, repair_ratio * 0.12)
        topic_confidence = max(0.0, topic_confidence - quality_penalty)
        affect_profile, affective_salience = aggregate_affect_profile(fragments)
        topic = TopicMemory(
            topic_uid=topic_uid,
            memory_space_id=memory_space_id,
            title=str(synthesis["title"]),
            summary=str(synthesis["summary"]),
            revision=existing.revision if existing else 0,
            status=TopicMemoryStatus.ACTIVE,
            base_importance=base_importance,
            importance=importance,
            semantic_importance=semantic_importance,
            source_base_component=source_base_component,
            evidence_strength=topic_evidence_strength,
            importance_policy_version=IMPORTANCE_POLICY_VERSION,
            source_importance_hash=str(source_importance["source_importance_hash"]),
            confidence=topic_confidence,
            started_at=min(starts) if starts else None,
            ended_at=max(ends) if ends else None,
            last_accessed_at=existing.last_accessed_at if existing else None,
            access_count=existing.access_count if existing else 0,
            decay_anchor_at=time.time(),
            created_at=existing.created_at if existing else time.time(),
            embedding_signature=make_embedding_signature(
                self.embedding_provider,
                dimension=len(embedding),
                input_format_version=TOPIC_CENTROID_EMBEDDING_FORMAT,
            ),
            affect_profile=affect_profile,
            affective_salience=affective_salience,
            affect_signature=affect_signature(
                provider_id=self._provider_identity(self.llm_provider)[0],
                model_id=self._provider_identity(self.llm_provider)[1],
                prompt_version=_FRAGMENT_PROMPT_VERSION,
            ),
            metadata={
                "build_run_uid": run_uid,
                "fragment_uids": [item.fragment_uid for item in fragments],
                "source_timeline_uids": timeline_uids,
                "keywords": sorted(
                    {
                        str(keyword).strip()
                        for item in fragments
                        for keyword in item.keywords
                        if str(keyword).strip()
                    }
                ),
                "time_cluster_count": len(evidence_clusters),
                "importance_projection": {
                    "policy_version": IMPORTANCE_POLICY_VERSION,
                    "semantic_importance": semantic_importance,
                    "source_base_component": source_base_component,
                    "dynamic_factor": source_importance["dynamic_factor"],
                    "evidence_strength": topic_evidence_strength,
                    "source_importance_hash": source_importance[
                        "source_importance_hash"
                    ],
                },
                "embedding": embedding,
                "automatic": True,
                "manually_editable": False,
                "algorithm_version": _MATCHING_ALGORITHM_VERSION,
                "confidence_calibration": topic_confidence_audit,
                "quality": {
                    "deterministic_repair_count": repair_count,
                    "evaluated_units": quality_units,
                    "deterministic_repair_ratio": repair_ratio,
                    "confidence_penalty": quality_penalty,
                    "repair_audit": repair_audit,
                },
                "supplemental_identity_hash": (
                    self._runtime_context.get().supplemental_identity_hash
                    if self._runtime_context.get() is not None
                    else ""
                ),
                "narrative_schema_version": (
                    _NARRATIVE_SCHEMA_VERSION
                    if fragments
                    and all(
                        item.metadata.get("narrative_schema_version")
                        == _NARRATIVE_SCHEMA_VERSION
                        for item in fragments
                    )
                    else "mixed_or_legacy"
                ),
                "conversation_roles": self._fragment_role_payload(fragments),
                "participant_index": self._topic_participant_index(fragments),
            },
        )
        links = [
            TopicTimelineLink(
                topic_uid=topic_uid,
                timeline_uid=uid,
                time_cluster_key=timeline_cluster[uid],
                contribution_weight=min(
                    1.0, 0.6 + 0.4 / max(1, cluster_sizes[timeline_cluster[uid]])
                ),
                semantic_similarity=self._timeline_fragment_similarity(uid, fragments),
                temporal_affinity=1.0 / max(1, cluster_sizes[timeline_cluster[uid]]),
                source_timeline_revision=(
                    candidate_map[uid].source_revision if uid in candidate_map else 1
                ),
                metadata={
                    "source_base_importance": (
                        candidate_map[uid].base_importance
                        if uid in candidate_map
                        else 0.5
                    ),
                    "source_effective_importance": (
                        candidate_map[uid].effective_importance
                        if uid in candidate_map
                        else 0.5
                    ),
                    "source_importance_revision": (
                        candidate_map[uid].importance_revision
                        if uid in candidate_map
                        else 1
                    ),
                    "importance_policy_version": IMPORTANCE_POLICY_VERSION,
                },
            )
            for uid in timeline_uids
        ]
        fact_map = {
            str(fact.get("fact_uid")): (fragment, fact)
            for fragment in fragments
            for fact in fragment.facts
            if str(fact.get("fact_uid") or "")
        }
        atoms: list[TopicMemoryAtom] = []
        sources: list[TopicAtomSource] = []
        merged_atom_payloads: dict[tuple[str, str], dict[str, Any]] = {}
        for raw_atom in synthesis.get("atoms", []):
            content = str(raw_atom.get("content") or "").strip()
            atom_type = str(raw_atom.get("type") or "factual")
            if not content:
                continue
            key = (atom_type, self._norm(content))
            current = merged_atom_payloads.setdefault(
                key,
                {
                    **raw_atom,
                    "content": content,
                    "fragment_uids": [],
                    "source_fact_uids": [],
                },
            )
            current["fragment_uids"] = sorted(
                set(current["fragment_uids"]) | set(raw_atom.get("fragment_uids", []))
            )
            current["source_fact_uids"] = sorted(
                set(current["source_fact_uids"])
                | set(raw_atom.get("source_fact_uids", []))
            )
            current["importance"] = max(
                self._score(current.get("importance"), semantic_importance),
                self._score(raw_atom.get("importance"), semantic_importance),
            )
            current["confidence"] = max(
                self._score(current.get("confidence"), topic.confidence),
                self._score(raw_atom.get("confidence"), topic.confidence),
            )
        for atom_index, atom_payload in enumerate(merged_atom_payloads.values()):
            content = str(atom_payload.get("content") or "").strip()
            if not content:
                continue
            atom_uid = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"livingmemory:topic-atom:{topic_uid}:{self._norm(content)}",
                )
            )
            source_fact_uids = [
                uid
                for uid in atom_payload.get("source_fact_uids", [])
                if uid in fact_map
            ]
            source_fragment_uids = sorted(
                {fact_map[uid][0].fragment_uid for uid in source_fact_uids}
            )
            atom_timeline_uids = {
                timeline_uid
                for fact_uid in source_fact_uids
                for timeline_uid in self._unique_strings(
                    fact_map[fact_uid][1].get(
                        "source_timeline_uids",
                        fact_map[fact_uid][0].timeline_uids,
                    )
                )
            }
            raw_atom_confidence = self._score(
                atom_payload.get("confidence"), raw_topic_confidence
            )
            atom_confidence, atom_confidence_audit = self._calibrate_confidence(
                raw_atom_confidence,
                independent_clusters=len(
                    {
                        timeline_cluster[uid]
                        for uid in atom_timeline_uids
                        if uid in timeline_cluster
                    }
                ),
                supporting_timelines=len(atom_timeline_uids),
            )
            atom_temporal = normalize_fact_temporal(
                aggregate_fact_temporal(
                    fact_map[fact_uid][1] for fact_uid in source_fact_uids
                ),
                fallback_started_at=topic.started_at,
                fallback_ended_at=topic.ended_at,
                fallback_basis="topic_window",
            )
            display_started_at = atom_temporal.get("event_started_at")
            display_ended_at = atom_temporal.get("event_ended_at")
            event_time_is_fallback = display_started_at is None
            if display_started_at is None:
                display_started_at = atom_temporal.get("evidence_started_at")
                display_ended_at = atom_temporal.get("evidence_ended_at")
            atom = TopicMemoryAtom(
                atom_uid=atom_uid,
                topic_uid=topic_uid,
                atom_type=str(atom_payload.get("type") or "factual"),
                content=content,
                canonical_content=self._norm(content),
                importance=self._score(
                    atom_payload.get("importance"), semantic_importance
                ),
                confidence=atom_confidence,
                event_started_at=display_started_at,
                event_ended_at=display_ended_at,
                metadata={
                    "source_fragment_uids": source_fragment_uids,
                    "source_fact_uids": source_fact_uids,
                    "index": atom_index,
                    "confidence_calibration": atom_confidence_audit,
                    **atom_temporal,
                    "event_time_is_fallback": event_time_is_fallback,
                },
            )
            atoms.append(atom)
            seen_sources: set[tuple[str, str]] = set()
            source_rows: list[tuple[str, str, str]] = []
            for fact_uid in source_fact_uids:
                fragment, fact = fact_map[fact_uid]
                fact_timeline_uids = self._unique_strings(
                    fact.get("source_timeline_uids", fragment.timeline_uids)
                )
                fingerprints = self._unique_strings(
                    fact.get("source_atom_fingerprints")
                )
                mapped_timelines: set[str] = set()
                for fact_fingerprint in fingerprints:
                    source_kind = str(
                        fact.get("source_kinds_by_fingerprint", {}).get(
                            fact_fingerprint,
                            "atom_fingerprint"
                            if fact.get("source_atom_fingerprints")
                            else "fact_fingerprint",
                        )
                    )
                    timelines_by_fingerprint = fact.get(
                        "source_timeline_uids_by_fingerprint", {}
                    )
                    fingerprint_timelines = (
                        timelines_by_fingerprint.get(fact_fingerprint, [])
                        if isinstance(timelines_by_fingerprint, dict)
                        else []
                    )
                    for timeline_uid in fingerprint_timelines or fact_timeline_uids:
                        mapped_timelines.add(str(timeline_uid))
                        source_rows.append(
                            (str(timeline_uid), str(fact_fingerprint), source_kind)
                        )
                fallback_fingerprint = TopicMaintenanceManager.fingerprint_text(
                    str(fact.get("content") or content)
                )
                for timeline_uid in fact_timeline_uids:
                    if timeline_uid not in mapped_timelines:
                        source_rows.append(
                            (
                                str(timeline_uid),
                                fallback_fingerprint,
                                "fact_fingerprint",
                            )
                        )
            source_weight = 1.0 / max(1, len(set(source_rows)))
            for timeline_uid, fact_fingerprint, source_kind in source_rows:
                key = (timeline_uid, fact_fingerprint)
                if key in seen_sources or timeline_uid not in timeline_uids:
                    continue
                seen_sources.add(key)
                sources.append(
                    TopicAtomSource(
                        topic_atom_uid=atom_uid,
                        timeline_uid=timeline_uid,
                        source_atom_fingerprint=fact_fingerprint,
                        source_kind=source_kind,
                        contribution_weight=source_weight,
                    )
                )
        source_timelines = {source.timeline_uid for source in sources}
        missing_source_timelines = sorted(set(timeline_uids) - source_timelines)
        if missing_source_timelines:
            raise TopicBuildValidationError(
                "Topic Timeline links without atom provenance: "
                + ", ".join(missing_source_timelines)
            )
        actor_links: list[TopicActorLink] = []
        atom_actor_links: list[TopicAtomActorLink] = []
        atom_by_fact_uid: dict[str, list[TopicMemoryAtom]] = {}
        for atom in atoms:
            for fact_uid in self._unique_strings(atom.metadata.get("source_fact_uids")):
                atom_by_fact_uid.setdefault(fact_uid, []).append(atom)
        seen_atom_actor: set[tuple[str, str, str, str, str]] = set()
        for raw_link in synthesis.get("actor_links", []):
            if not isinstance(raw_link, dict):
                continue
            actor_id = str(raw_link.get("actor_id") or "").strip()
            relation_type = str(raw_link.get("relation_type") or "").strip()
            if not actor_id or not relation_type:
                continue
            source_fact_uids = [
                uid
                for uid in self._unique_strings(raw_link.get("source_fact_uids"))
                if uid in fact_map
            ]
            actor_fragment_uids = sorted(
                set(self._unique_strings(raw_link.get("fragment_uids")))
                | {fact_map[uid][0].fragment_uid for uid in source_fact_uids}
            )
            actor_timeline_uids = sorted(
                set(self._unique_strings(raw_link.get("timeline_uids")))
                | {
                    timeline_uid
                    for uid in source_fact_uids
                    for timeline_uid in self._unique_strings(
                        fact_map[uid][1].get(
                            "source_timeline_uids",
                            fact_map[uid][0].timeline_uids,
                        )
                    )
                }
            )
            actor_links.append(
                TopicActorLink(
                    topic_uid=topic_uid,
                    actor_id=actor_id,
                    actor_type=str(raw_link.get("actor_type") or "unknown"),
                    relation_type=relation_type,
                    display_name_snapshot=(
                        str(raw_link.get("display_name_snapshot") or "").strip() or None
                    ),
                    confidence=self._score(raw_link.get("confidence"), 0.7),
                    resolution_status=str(
                        raw_link.get("resolution_status") or "inferred"
                    ),
                    metadata={
                        "source_fact_uids": source_fact_uids,
                        "fragment_uids": actor_fragment_uids,
                        "timeline_uids": actor_timeline_uids,
                        "atom_uids": sorted(
                            {
                                atom.atom_uid
                                for fact_uid in source_fact_uids
                                for atom in atom_by_fact_uid.get(fact_uid, [])
                            }
                        ),
                    },
                )
            )
            for fact_uid in source_fact_uids:
                fragment, fact = fact_map[fact_uid]
                fact_timelines = self._unique_strings(
                    fact.get("source_timeline_uids", fragment.timeline_uids)
                ) or [""]
                for atom in atom_by_fact_uid.get(fact_uid, []):
                    for timeline_uid in fact_timelines:
                        key = (
                            atom.atom_uid,
                            actor_id,
                            relation_type,
                            fragment.fragment_uid,
                            timeline_uid,
                        )
                        if key in seen_atom_actor:
                            continue
                        seen_atom_actor.add(key)
                        atom_actor_links.append(
                            TopicAtomActorLink(
                                topic_atom_uid=atom.atom_uid,
                                actor_id=actor_id,
                                relation_type=relation_type,
                                fragment_uid=fragment.fragment_uid,
                                timeline_uid=timeline_uid or None,
                                confidence=self._score(raw_link.get("confidence"), 0.7),
                                metadata={"source_fact_uid": fact_uid},
                            )
                        )
        topic.metadata["participant_index"] = self._actor_index_from_links(actor_links)
        return topic, atoms, links, sources, actor_links, atom_actor_links

    @staticmethod
    def _normalize_published_actor_provenance(
        actor_links: list[TopicActorLink],
        atom_actor_links: list[TopicAtomActorLink],
        formal_fragments: list[TopicFragmentDraft],
    ) -> tuple[list[TopicActorLink], list[TopicAtomActorLink]]:
        """Replace transient incremental projection IDs with formal fragments."""
        fragments_by_uid = {
            fragment.fragment_uid: fragment for fragment in formal_fragments
        }
        timeline_to_fragments: dict[str, list[str]] = {}
        for fragment in formal_fragments:
            for timeline_uid in fragment.timeline_uids:
                timeline_to_fragments.setdefault(str(timeline_uid), []).append(
                    fragment.fragment_uid
                )

        normalized_actor_links: list[TopicActorLink] = []
        for link in actor_links:
            metadata = dict(link.metadata or {})
            referenced = [
                str(value) for value in metadata.get("fragment_uids", []) if str(value)
            ]
            invalid = [uid for uid in referenced if uid not in fragments_by_uid]
            if invalid:
                replacements = {
                    fragment_uid
                    for timeline_uid in metadata.get("timeline_uids", [])
                    for fragment_uid in timeline_to_fragments.get(str(timeline_uid), [])
                }
                metadata["fragment_uids"] = sorted(
                    {uid for uid in referenced if uid in fragments_by_uid}
                    | replacements
                )
                metadata["provenance_fragment_remapped_from"] = sorted(set(invalid))
            normalized_actor_links.append(
                TopicActorLink(
                    topic_uid=link.topic_uid,
                    actor_id=link.actor_id,
                    actor_type=link.actor_type,
                    relation_type=link.relation_type,
                    display_name_snapshot=link.display_name_snapshot,
                    confidence=link.confidence,
                    resolution_status=link.resolution_status,
                    created_at=link.created_at,
                    updated_at=link.updated_at,
                    metadata=metadata,
                )
            )

        normalized_atom_links: list[TopicAtomActorLink] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for link in atom_actor_links:
            target_uids = [link.fragment_uid]
            if link.fragment_uid not in fragments_by_uid:
                target_uids = list(
                    timeline_to_fragments.get(str(link.timeline_uid or ""), [])
                )
                if not target_uids:
                    raise ValueError(
                        "Topic 人物事实来源无法映射到正式片段："
                        f"{link.fragment_uid} / {link.timeline_uid or '--'}"
                    )
            for fragment_uid in target_uids:
                key = (
                    link.topic_atom_uid,
                    link.actor_id,
                    link.relation_type,
                    fragment_uid,
                    str(link.timeline_uid or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                metadata = dict(link.metadata or {})
                if fragment_uid != link.fragment_uid:
                    metadata["provenance_fragment_remapped_from"] = link.fragment_uid
                normalized_atom_links.append(
                    TopicAtomActorLink(
                        topic_atom_uid=link.topic_atom_uid,
                        actor_id=link.actor_id,
                        relation_type=link.relation_type,
                        fragment_uid=fragment_uid,
                        timeline_uid=link.timeline_uid,
                        confidence=link.confidence,
                        metadata=metadata,
                    )
                )
        return normalized_actor_links, normalized_atom_links

    def _fallback_fragments(
        self,
        run_uid: str,
        group: TopicCandidateGroup,
        inputs: list[TimelineTopicCandidate],
        prompt_hash: str,
        input_hash: str,
        provider_id: str,
        model_id: str,
        *,
        fragment_index_offset: int,
        reason: str,
    ) -> list[TopicFragmentDraft]:
        """Build conservative per-Timeline fragments without trusting LLM output."""
        result: list[TopicFragmentDraft] = []
        for local_index, candidate in enumerate(inputs):
            index = fragment_index_offset + local_index
            fragment_uid = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"livingmemory:fragment-fallback:{run_uid}:{group.group_uid}:"
                    f"{index}:{candidate.memory_uid}",
                )
            )
            fact_contents = self._unique_strings(
                [*candidate.key_facts, *candidate.atom_contents]
            )
            if not fact_contents:
                fallback_content = str(
                    candidate.summary or candidate.content or ""
                ).strip()
                if fallback_content:
                    fact_contents = [fallback_content]
            conversation_roles = self._conversation_role_payload([candidate])
            fallback_summary = (
                candidate.summary or candidate.content or "Timeline memory"
            )
            facts: list[dict[str, Any]] = []
            for fact_index, content in enumerate(fact_contents):
                fingerprints = sorted(
                    {
                        fingerprint
                        for atom_content, fingerprint in zip(
                            candidate.atom_contents,
                            candidate.atom_fingerprints,
                            strict=False,
                        )
                        if self._norm(atom_content) == self._norm(content)
                    }
                )
                temporal_candidates: list[dict[str, Any]] = []
                for source_index, source_content in enumerate(candidate.atom_contents):
                    if self._norm(source_content) == self._norm(content):
                        temporal_candidates.append(
                            candidate.atom_temporal[source_index]
                            if source_index < len(candidate.atom_temporal)
                            else {}
                        )
                for source_index, source_content in enumerate(candidate.key_facts):
                    if self._norm(source_content) == self._norm(content):
                        temporal_candidates.append(
                            candidate.key_fact_temporal[source_index]
                            if source_index < len(candidate.key_fact_temporal)
                            else {}
                        )
                temporal = normalize_fact_temporal(
                    aggregate_fact_temporal(temporal_candidates),
                    fallback_started_at=candidate.started_at,
                    fallback_ended_at=candidate.ended_at,
                )
                facts.append(
                    {
                        "fact_uid": str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"livingmemory:fragment-fallback-fact:{fragment_uid}:"
                                f"{fact_index}",
                            )
                        ),
                        "type": "factual",
                        "content": content,
                        "importance": 0.5,
                        "confidence": 0.7,
                        "source_timeline_uids": [candidate.memory_uid],
                        "source_atom_fingerprints": fingerprints,
                        "source_fact_keys": (
                            [
                                f"{candidate.memory_uid}:atom:{fingerprint}"
                                for fingerprint in fingerprints
                            ]
                            or [
                                f"{candidate.memory_uid}:fallback:"
                                + hashlib.sha256(
                                    self._norm(content).encode("utf-8")
                                ).hexdigest()
                            ]
                        ),
                        "source_timeline_uids_by_fingerprint": {
                            fingerprint: [candidate.memory_uid]
                            for fingerprint in fingerprints
                        },
                        **temporal,
                    }
                )
            fallback_repairs = [
                {
                    "type": "fragment_batch_fallback",
                    "reason": reason[:500],
                }
            ]
            result.append(
                TopicFragmentDraft(
                    fragment_uid=fragment_uid,
                    logical_fragment_uid=logical_fragment_uid(
                        memory_space_id=group.memory_space_id,
                        timeline_uids=[candidate.memory_uid],
                        facts=facts,
                    ),
                    run_uid=run_uid,
                    candidate_group_uid=group.group_uid,
                    memory_space_id=group.memory_space_id,
                    label=(candidate.topics[0] if candidate.topics else group.label)
                    or "Timeline memory",
                    summary=fallback_summary,
                    timeline_uids=[candidate.memory_uid],
                    source_revisions={candidate.memory_uid: candidate.source_revision},
                    facts=facts,
                    keywords=self._unique_strings(candidate.topics)[:20],
                    time_cluster_keys=[candidate.time_cluster_key]
                    if candidate.time_cluster_key
                    else [],
                    importance=fragment_semantic_importance(facts),
                    confidence=0.7,
                    started_at=candidate.started_at,
                    ended_at=candidate.ended_at,
                    prompt_hash=prompt_hash,
                    input_hash=input_hash,
                    provider_id=provider_id,
                    model_id=model_id,
                    metadata={
                        "fragment_prompt_version": _FRAGMENT_PROMPT_VERSION,
                        "importance_policy_version": IMPORTANCE_POLICY_VERSION,
                        "llm_fragment_importance": None,
                        "deterministic_fallback": True,
                        "narrative_schema_version": _NARRATIVE_SCHEMA_VERSION,
                        "conversation_roles": conversation_roles,
                        "participant_refs": self._deterministic_fragment_participants(
                            [candidate]
                        ),
                        "mentioned_actor_refs": [],
                        "source_accounting": {
                            "complete": True,
                            "omitted_source_refs": [],
                            "mode": "deterministic_fallback",
                        },
                        "supplemental_identity_hash": (
                            self._runtime_context.get().supplemental_identity_hash
                            if self._runtime_context.get() is not None
                            else ""
                        ),
                        "repair_audit": self._repair_audit(fallback_repairs),
                        "validation_repairs": fallback_repairs,
                    },
                )
            )
        return result

    async def _prepare_candidate_evidence(
        self, inputs: list[TimelineTopicCandidate]
    ) -> None:
        """Backfill actor bindings and attach raw messages only for risky sources."""
        if self.conversation_store is None:
            for item in inputs:
                flags = self._candidate_ambiguity_flags(item)
                item.features["ambiguity_flags"] = flags
                item.features["evidence_status"] = "store_unavailable"
            return
        limit = max(1, min(200, int(self.config.get("evidence_max_messages", 80))))
        for item in inputs:
            flags = self._candidate_ambiguity_flags(item)
            needs_identity_backfill = not bool(item.role_bindings.get("actors"))
            needs_content_evidence = bool(flags)
            if not needs_identity_backfill and not needs_content_evidence:
                item.features["ambiguity_flags"] = []
                item.features["evidence_status"] = "not_needed"
                continue
            source = item.source_window
            first_id = source.get("first_message_id")
            last_id = source.get("last_message_id")
            if first_id is None or last_id is None or not item.session_id:
                item.features["ambiguity_flags"] = flags or [
                    "legacy_source_span_without_message_ids"
                ]
                item.features["evidence_status"] = "unavailable"
                continue
            try:
                messages = await self.conversation_store.get_messages_by_id_span(
                    item.session_id,
                    int(first_id),
                    int(last_id),
                    limit=limit,
                )
            except Exception:
                logger.warning(
                    "[TopicMemory] 无法读取 Timeline 原始证据: %s",
                    item.memory_uid,
                    exc_info=True,
                )
                messages = []
            if not messages:
                item.features["ambiguity_flags"] = flags
                item.features["evidence_status"] = "unavailable"
                continue
            if needs_identity_backfill:
                item.role_bindings = build_role_bindings(messages, item.persona_id)
                flags = self._candidate_ambiguity_flags(item)
            item.features["ambiguity_flags"] = self._unique_strings(
                [*flags, *item.role_bindings.get("ambiguity_flags", [])]
            )
            item.features["evidence_status"] = (
                "attached" if needs_content_evidence else "identity_backfilled"
            )
            if needs_content_evidence:
                item.features["raw_evidence"] = [
                    {
                        "message_id": message.id,
                        "role": message.role,
                        "actor_id": stable_actor_id(
                            message.platform,
                            message.sender_id,
                            "assistant"
                            if message.role == "assistant"
                            or message.metadata.get("is_bot_message")
                            else "human",
                        ),
                        "sender_id": message.sender_id,
                        "sender_name": message.sender_name,
                        "timestamp": message.timestamp,
                        "content": str(message.content)[:2000],
                    }
                    for message in messages
                ]

    @staticmethod
    def _candidate_ambiguity_flags(
        item: TimelineTopicCandidate,
    ) -> list[str]:
        flags: list[str] = []
        bindings = item.role_bindings if isinstance(item.role_bindings, dict) else {}
        actors = [
            actor for actor in bindings.get("actors", []) if isinstance(actor, dict)
        ]
        humans = [actor for actor in actors if actor.get("actor_type") != "assistant"]
        narr = str(bindings.get("narrator_actor_id") or "").strip()
        if not actors:
            flags.append("missing_role_bindings")
        if not narr:
            flags.append("missing_narrator_actor")
        if bindings.get("ambiguity_flags"):
            flags.extend(str(value) for value in bindings["ambiguity_flags"])
        text = " ".join([item.summary, *item.key_facts, *item.atom_contents])
        is_group = bool(item.session_id and "GroupMessage" in item.session_id)
        if (
            is_group
            and len(humans) > 1
            and re.search(
                r"(?:^|[，。；、\s])(他|她|对方|那个人)(?:的|说|提|认|表|回|$)", text
            )
        ):
            flags.append("ambiguous_human_pronoun")
        actor_ids = [str(actor.get("actor_id") or "") for actor in actors]
        if len(actor_ids) != len(set(actor_ids)):
            flags.append("duplicate_actor_id")
        return list(dict.fromkeys(flags))

    def _validate_fragments(
        self,
        parsed: dict[str, Any],
        run_uid: str,
        group: TopicCandidateGroup,
        inputs: list[TimelineTopicCandidate],
        prompt_hash: str,
        input_hash: str,
        provider_id: str,
        model_id: str,
        fragment_index_offset: int = 0,
    ) -> list[TopicFragmentDraft]:
        raw_fragments = parsed.get("fragments")
        if not isinstance(raw_fragments, list) or not raw_fragments:
            raise TopicBuildValidationError("fragments must be a non-empty array")
        allowed = {item.memory_uid: item for item in inputs}
        omissions_by_timeline: dict[str, list[dict[str, Any]]] = {}
        for omission in parsed.get("omitted_source_refs", []):
            if not isinstance(omission, dict):
                continue
            timeline_uid = str(omission.get("source_timeline_uid") or "").strip()
            if timeline_uid in allowed:
                omissions_by_timeline.setdefault(timeline_uid, []).append(
                    dict(omission)
                )
        allowed_fingerprints = {
            fingerprint for item in inputs for fingerprint in item.atom_fingerprints
        }
        fingerprints_by_timeline = {
            item.memory_uid: set(item.atom_fingerprints) for item in inputs
        }
        atoms_by_timeline = {
            item.memory_uid: list(
                zip(item.atom_contents, item.atom_fingerprints, strict=False)
            )
            for item in inputs
        }
        covered: set[str] = set()
        result: list[TopicFragmentDraft] = []
        private_identity_context = self._private_session_identity_context(inputs)
        for local_index, raw in enumerate(raw_fragments):
            index = fragment_index_offset + local_index
            if not isinstance(raw, dict):
                raise TopicBuildValidationError("each fragment must be an object")
            timeline_uids = self._unique_strings(raw.get("timeline_uids"))
            if not timeline_uids or not set(timeline_uids) <= allowed.keys():
                raise TopicBuildValidationError(
                    "fragment contains an unknown Timeline UID"
                )
            covered.update(timeline_uids)
            facts = raw.get("facts")
            if not isinstance(facts, list):
                raise TopicBuildValidationError("fragment facts must be an array")
            normalized_facts: list[dict[str, Any]] = []
            validation_repairs: list[dict[str, Any]] = []
            fact_covered_timelines: set[str] = set()
            for fact_index, fact in enumerate(facts):
                if (
                    not isinstance(fact, dict)
                    or not str(fact.get("content") or "").strip()
                ):
                    raise TopicBuildValidationError("each fact needs content")
                fact_sources = self._unique_strings(fact.get("source_timeline_uids"))
                if not fact_sources or not set(fact_sources) <= set(timeline_uids):
                    raise TopicBuildValidationError(
                        "fact provenance is outside its fragment"
                    )
                fact_covered_timelines.update(fact_sources)
                requested_fingerprints = [
                    value.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", value) else value
                    for value in self._unique_strings(
                        fact.get("source_atom_fingerprints")
                    )
                ]
                source_fingerprints = {
                    value
                    for timeline_uid in fact_sources
                    for value in fingerprints_by_timeline.get(timeline_uid, set())
                }
                fingerprints = [
                    value
                    for value in requested_fingerprints
                    if value in allowed_fingerprints and value in source_fingerprints
                ]
                dropped_count = len(requested_fingerprints) - len(fingerprints)
                inferred = False
                if not fingerprints:
                    normalized_content = self._norm(fact.get("content"))
                    fingerprints = sorted(
                        {
                            fingerprint
                            for timeline_uid in fact_sources
                            for atom_content, fingerprint in atoms_by_timeline.get(
                                timeline_uid, []
                            )
                            if normalized_content
                            and self._norm(atom_content) == normalized_content
                        }
                    )
                    inferred = bool(fingerprints)
                timelines_by_fingerprint = {
                    fingerprint: sorted(
                        timeline_uid
                        for timeline_uid in fact_sources
                        if fingerprint
                        in fingerprints_by_timeline.get(timeline_uid, set())
                    )
                    for fingerprint in fingerprints
                }
                if dropped_count or inferred:
                    validation_repairs.append(
                        {
                            "type": "normalized_fact_atom_fingerprints",
                            "fact_index": fact_index,
                            "dropped_unknown_atom_fingerprints": dropped_count,
                            "inferred_exact_atom_fingerprints": len(fingerprints)
                            if inferred
                            else 0,
                        }
                    )
                normalized_facts.append(
                    {
                        "fact_uid": str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"livingmemory:fragment-fact:{run_uid}:"
                                f"{group.group_uid}:{index}:{fact_index}",
                            )
                        ),
                        "type": str(fact.get("type") or "factual"),
                        "content": str(fact["content"]).strip(),
                        "importance": self._score(fact.get("importance"), 0.5),
                        "confidence": self._score(fact.get("confidence"), 0.7),
                        "source_timeline_uids": fact_sources,
                        "source_atom_fingerprints": fingerprints,
                        "source_fact_keys": self._unique_strings(
                            fact.get("source_fact_keys")
                        ),
                        "source_timeline_uids_by_fingerprint": timelines_by_fingerprint,
                        "actor_refs": [
                            dict(value)
                            for value in fact.get("actor_refs", [])
                            if isinstance(value, dict)
                        ],
                        **normalize_fact_temporal(
                            fact,
                            fallback_started_at=min(
                                (
                                    allowed[uid].started_at
                                    for uid in fact_sources
                                    if allowed[uid].started_at is not None
                                ),
                                default=None,
                            ),
                            fallback_ended_at=max(
                                (
                                    allowed[uid].ended_at
                                    for uid in fact_sources
                                    if allowed[uid].ended_at is not None
                                ),
                                default=None,
                            ),
                        ),
                    }
                )
            uncovered_timelines = sorted(set(timeline_uids) - fact_covered_timelines)
            if uncovered_timelines:
                raise TopicBuildValidationError(
                    "fragment Timeline refs without supporting facts: "
                    + ", ".join(uncovered_timelines)
                )
            label = str(raw.get("label") or "").strip()
            summary = str(raw.get("summary") or "").strip()
            if not label or not summary:
                raise TopicBuildValidationError(
                    "fragment label and summary are required"
                )
            source_items = [allowed[uid] for uid in timeline_uids]
            role_repairs: list[dict[str, Any]] = []
            label, repaired = self._repair_unambiguous_generic_human_roles(
                label, source_items
            )
            role_repairs.extend(repaired)
            summary, repaired = self._repair_unambiguous_generic_human_roles(
                summary, source_items
            )
            role_repairs.extend(repaired)
            for fact in normalized_facts:
                fact["content"], repaired = (
                    self._repair_unambiguous_generic_human_roles(
                        str(fact["content"]), source_items
                    )
                )
                role_repairs.extend(repaired)
            if role_repairs:
                validation_repairs.append(
                    {
                        "type": "narrative_role_repair",
                        "replacements": role_repairs,
                    }
                )
            self._validate_role_anchored_fragment(
                label,
                summary,
                normalized_facts,
                source_items,
            )
            starts = [
                item.started_at for item in source_items if item.started_at is not None
            ]
            ends = [item.ended_at for item in source_items if item.ended_at is not None]
            fragment_uid = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"livingmemory:fragment:{run_uid}:{group.group_uid}:{index}:"
                    + ":".join(timeline_uids),
                )
            )
            mentioned_actor_refs = self._dedupe_actor_relations(
                [
                    dict(actor)
                    for fact in normalized_facts
                    for actor in fact.get("actor_refs", [])
                    if isinstance(actor, dict)
                    and str(actor.get("relation_type") or "")
                    not in {"speaker", "narrator", "responder"}
                ]
            )
            self._scope_unresolved_actor_ids(
                fragment_uid,
                [],
                mentioned_actor_refs,
                normalized_facts,
                source_items=source_items,
                private_identity_context=private_identity_context,
            )
            participant_refs = self._deterministic_fragment_participants(
                source_items,
                facts=normalized_facts,
            )
            actors_by_name: dict[str, set[str]] = {}
            for value in [
                *participant_refs,
                *mentioned_actor_refs,
                *[
                    actor
                    for fact in normalized_facts
                    for actor in fact.get("actor_refs", [])
                    if isinstance(actor, dict)
                ],
            ]:
                name_key = self._norm(value.get("display_name_snapshot"))
                actor_id = str(value.get("actor_id") or "").strip()
                if name_key and actor_id:
                    actors_by_name.setdefault(name_key, set()).add(actor_id)
            actor_ids_by_name = {
                name: next(iter(actor_ids))
                for name, actor_ids in actors_by_name.items()
                if len(actor_ids) == 1
            }
            affect_events: list[dict[str, Any]] = []
            for event_index, raw_event in enumerate(raw.get("affect_events", [])):
                event = normalize_affect_event(raw_event)
                if event is None:
                    raise TopicBuildValidationError(
                        f"fragment {index} affect event {event_index} is incomplete"
                    )
                if not set(event["source_timeline_uids"]) <= set(timeline_uids):
                    raise TopicBuildValidationError(
                        f"fragment {index} affect event {event_index} provenance "
                        "is outside its fragment"
                    )
                if event["evidence_type"] not in {
                    "explicit",
                    "behavioral",
                    "contextual",
                    "model_inferred",
                }:
                    raise TopicBuildValidationError(
                        f"fragment {index} affect event {event_index} has invalid "
                        "evidence_type"
                    )
                if event["temporal_status"] not in {
                    "historical",
                    "ongoing",
                    "resolved",
                    "uncertain",
                }:
                    raise TopicBuildValidationError(
                        f"fragment {index} affect event {event_index} has invalid "
                        "temporal_status"
                    )
                if event["evidence_type"] == "model_inferred":
                    event["confidence"] = min(event["confidence"], 0.65)
                if event["actor_id"] == "unresolved":
                    name_key = self._norm(event["display_name_snapshot"])
                    event["actor_id"] = actor_ids_by_name.get(name_key) or (
                        f"unresolved:{fragment_uid}:affect:{event_index}"
                    )
                event["event_uid"] = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"livingmemory:affect-event:{fragment_uid}:{event_index}",
                    )
                )
                affect_events.append(event)
            raw_fragment_importance = self._score(raw.get("importance"), 0.5)
            semantic_fragment_importance = fragment_semantic_importance(
                normalized_facts
            )
            result.append(
                TopicFragmentDraft(
                    fragment_uid=fragment_uid,
                    logical_fragment_uid=logical_fragment_uid(
                        memory_space_id=group.memory_space_id,
                        timeline_uids=timeline_uids,
                        facts=normalized_facts,
                    ),
                    run_uid=run_uid,
                    candidate_group_uid=group.group_uid,
                    memory_space_id=group.memory_space_id,
                    label=label,
                    summary=summary,
                    timeline_uids=timeline_uids,
                    source_revisions={
                        uid: allowed[uid].source_revision for uid in timeline_uids
                    },
                    facts=normalized_facts,
                    keywords=self._unique_strings(raw.get("keywords"))[:20],
                    time_cluster_keys=sorted(
                        {allowed[uid].time_cluster_key for uid in timeline_uids}
                    ),
                    importance=semantic_fragment_importance,
                    confidence=self._score(raw.get("confidence"), 0.7),
                    affect_events=affect_events,
                    affect_signature=affect_signature(
                        provider_id=provider_id,
                        model_id=model_id,
                        prompt_version=_FRAGMENT_PROMPT_VERSION,
                    ),
                    started_at=min(starts) if starts else None,
                    ended_at=max(ends) if ends else None,
                    prompt_hash=prompt_hash,
                    input_hash=input_hash,
                    provider_id=provider_id,
                    model_id=model_id,
                    metadata={
                        "fragment_prompt_version": _FRAGMENT_PROMPT_VERSION,
                        "importance_policy_version": IMPORTANCE_POLICY_VERSION,
                        "llm_fragment_importance": raw_fragment_importance,
                        "narrative_schema_version": _NARRATIVE_SCHEMA_VERSION,
                        "conversation_roles": self._conversation_role_payload(
                            source_items
                        ),
                        "participant_refs": participant_refs,
                        "mentioned_actor_refs": mentioned_actor_refs,
                        "attribution_confidence": self._score(
                            self._calibrated_attribution_confidence(
                                source_items,
                                self._score(
                                    raw.get("attribution_confidence"),
                                    self._score(raw.get("confidence"), 0.7),
                                ),
                            ),
                            0.7,
                        ),
                        "ambiguity_flags": self._unique_strings(
                            [
                                *(
                                    raw.get("ambiguity_flags", [])
                                    if isinstance(raw.get("ambiguity_flags"), list)
                                    else []
                                ),
                                *[
                                    value
                                    for item in source_items
                                    for value in item.features.get(
                                        "ambiguity_flags", []
                                    )
                                ],
                            ]
                        ),
                        "evidence_status": sorted(
                            {
                                str(item.features.get("evidence_status", "not_needed"))
                                for item in source_items
                            }
                        ),
                        "source_message_refs": sorted(
                            {
                                int(message["message_id"])
                                for item in source_items
                                for message in item.features.get("raw_evidence", [])
                                if message.get("message_id") is not None
                            }
                        ),
                        "source_accounting": {
                            "complete": bool(
                                parsed.get("source_accounting_complete", False)
                            ),
                            "omitted_source_refs": [
                                omission
                                for item in source_items
                                for omission in omissions_by_timeline.get(
                                    item.memory_uid, []
                                )
                            ],
                        },
                        "supplemental_identity_hash": (
                            self._runtime_context.get().supplemental_identity_hash
                            if self._runtime_context.get() is not None
                            else ""
                        ),
                        "repair_audit": self._repair_audit(validation_repairs),
                        "validation_repairs": validation_repairs,
                    },
                )
            )
            if validation_repairs:
                repair_audit = self._repair_audit(validation_repairs)
                log = (
                    logger.info
                    if not repair_audit["repair"] and not repair_audit["fallback"]
                    else logger.warning
                )
                log(
                    "[TopicMemory] LLM 片段输出已经确定性校正 "
                    "(group_uid=%s, fragment_index=%s, normalizations=%s, "
                    "repairs=%s, fallbacks=%s, types=%s)",
                    group.group_uid,
                    index,
                    repair_audit["normalization"],
                    repair_audit["repair"],
                    repair_audit["fallback"],
                    ",".join(
                        sorted(
                            {
                                str(item.get("type") or "unknown")
                                for item in validation_repairs
                                if isinstance(item, dict)
                            }
                        )
                    ),
                )
        if covered != allowed.keys():
            raise TopicBuildValidationError(
                "LLM fragments did not cover every Timeline input"
            )
        self._disambiguate_logical_fragment_collisions(result)
        return result

    @staticmethod
    def _disambiguate_logical_fragment_collisions(
        fragments: list[TopicFragmentDraft],
    ) -> None:
        """Keep provenance identity stable unless one source splits into facets."""
        by_logical_uid: dict[str, list[TopicFragmentDraft]] = {}
        for fragment in fragments:
            by_logical_uid.setdefault(fragment.logical_fragment_uid, []).append(
                fragment
            )
        for base_uid, colliding in by_logical_uid.items():
            if len(colliding) < 2:
                continue
            discriminators: dict[str, TopicFragmentDraft] = {}
            for fragment in colliding:
                discriminator = fragment_semantic_discriminator(
                    label=fragment.label,
                    summary=fragment.summary,
                    facts=fragment.facts,
                )
                if discriminator in discriminators:
                    raise TopicBuildValidationError(
                        "LLM returned duplicate semantic fragments for the same "
                        "source facts"
                    )
                discriminators[discriminator] = fragment
            for discriminator, fragment in discriminators.items():
                fragment.logical_fragment_uid = logical_fragment_uid(
                    memory_space_id=fragment.memory_space_id,
                    timeline_uids=fragment.timeline_uids,
                    facts=fragment.facts,
                    semantic_discriminator=discriminator,
                )
                fragment.metadata["logical_identity_disambiguation"] = {
                    "base_logical_fragment_uid": base_uid,
                    "semantic_discriminator": discriminator,
                    "reason": "shared_source_split_into_multiple_fragments",
                }

    def _scope_unresolved_actor_ids(
        self,
        fragment_uid: str,
        participant_refs: list[dict[str, Any]],
        mentioned_actor_refs: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        *,
        source_items: list[TimelineTopicCandidate],
        private_identity_context: dict[str, dict[str, Any]],
    ) -> None:
        """Resolve private-chat peers before falling back to local identities.

        Stable role bindings remain authoritative.  When all source Timelines lack
        bindings, same-name unresolved actors share a deterministic session-local ID
        instead of being split by physical fragment.  Group chats and conflicting
        private bindings retain the safer fragment-local behavior.
        """
        values = [*participant_refs, *mentioned_actor_refs]
        values.extend(
            actor
            for fact in facts
            for actor in fact.get("actor_refs", [])
            if isinstance(actor, dict)
        )
        source_scopes = {
            scope
            for item in source_items
            if (scope := self._private_session_scope(item.session_id)) is not None
        }
        private_context: dict[str, Any] | None = None
        if len(source_scopes) == 1:
            private_context = private_identity_context.get(next(iter(source_scopes)))
        replacements: dict[str, str] = {}
        for actor in values:
            actor_id = str(actor.get("actor_id") or "")
            if not actor_id.startswith("unresolved-pending:"):
                continue
            name_key = self._norm(actor.get("display_name_snapshot"))
            resolved_to_private_peer = bool(
                private_context
                and not private_context.get("conflict")
                and name_key
                and name_key in private_context.get("name_keys", set())
            )
            if resolved_to_private_peer:
                replacements.setdefault(actor_id, str(private_context["actor_id"]))
                actor["actor_type"] = "human"
                actor["resolution_status"] = str(
                    private_context.get("resolution_status") or "session_inferred"
                )
                actor["resolution_sources"] = list(
                    private_context.get("resolution_sources", [])
                )
            elif (
                private_context
                and not private_context.get("conflict")
                and not private_context.get("has_role_binding")
            ):
                scope_hash = hashlib.sha256(
                    str(private_context["scope"]).encode("utf-8")
                ).hexdigest()[:16]
                replacements.setdefault(
                    actor_id,
                    "unresolved:session:"
                    + scope_hash
                    + ":"
                    + actor_id.removeprefix("unresolved-pending:"),
                )
                actor["resolution_status"] = "unresolved"
                actor["resolution_sources"] = ["private_session_name_scope"]
            else:
                replacements.setdefault(
                    actor_id,
                    "unresolved:"
                    + fragment_uid
                    + ":"
                    + actor_id.removeprefix("unresolved-pending:"),
                )
                actor["resolution_status"] = "unresolved"
            actor["actor_id"] = replacements[actor_id]

    @staticmethod
    def _private_session_scope(session_id: str | None) -> str | None:
        parts = str(session_id or "").split(":", 2)
        if len(parts) != 3:
            return None
        message_type = parts[1].strip().casefold()
        if "friend" not in message_type and "private" not in message_type:
            return None
        peer_id = parts[2].strip()
        if not peer_id:
            return None
        platform_token = parts[0].strip()
        if re.fullmatch(r"qq\d+", platform_token, re.IGNORECASE):
            platform = "qq"
        else:
            platform = canonical_platform(platform_token) or "unknown"
        return f"{platform}\0{peer_id}"

    def _private_session_identity_context(
        self,
        inputs: list[TimelineTopicCandidate],
    ) -> dict[str, dict[str, Any]]:
        """Build deterministic private-peer identities across participating Timelines."""
        contexts: dict[str, dict[str, Any]] = {}
        for item in inputs:
            scope = self._private_session_scope(item.session_id)
            if scope is None:
                continue
            platform, peer_id = scope.split("\0", 1)
            context = contexts.setdefault(
                scope,
                {
                    "scope": scope,
                    "platform": platform,
                    "peer_id": peer_id,
                    "bound_actors": {},
                },
            )
            bindings = (
                item.role_bindings if isinstance(item.role_bindings, dict) else {}
            )
            for actor in bindings.get("actors", []):
                if not isinstance(actor, dict):
                    continue
                actor_type = str(actor.get("actor_type") or "human")
                if actor_type == "assistant":
                    continue
                sender_id = str(actor.get("sender_id") or "").strip()
                actor_platform = canonical_platform(actor.get("platform")) or platform
                normalized_actor_id = (
                    stable_actor_id(actor_platform, sender_id, "human")
                    if sender_id
                    else str(actor.get("actor_id") or "").strip()
                )
                if not normalized_actor_id:
                    continue
                bound = context["bound_actors"].setdefault(
                    normalized_actor_id,
                    {"names": [], "actor_id": normalized_actor_id},
                )
                for name in actor.get("observed_names", []):
                    display_name = str(name or "").strip()
                    if display_name and display_name not in bound["names"]:
                        bound["names"].append(display_name)

        for context in contexts.values():
            bound_actors = context.pop("bound_actors")
            context["has_role_binding"] = bool(bound_actors)
            context["conflict"] = len(bound_actors) > 1
            if len(bound_actors) == 1:
                bound = next(iter(bound_actors.values()))
                actor_id = str(bound["actor_id"])
                names = list(bound["names"])
                resolution_sources = ["timeline_role_bindings"]
                resolution_status = "timeline_bound"
                confidence = 0.95
            else:
                actor_id = stable_actor_id(
                    context["platform"], context["peer_id"], "human"
                )
                names = []
                resolution_sources = ["private_session_peer"]
                resolution_status = "session_inferred"
                confidence = 0.82
            if not context["conflict"]:
                for profile in self._active_identity_profiles():
                    if not profile.matches_actor_id(actor_id):
                        continue
                    for name in profile.names:
                        if name not in names:
                            names.append(name)
            context.update(
                {
                    "actor_id": actor_id,
                    "names": names,
                    "name_keys": {
                        self._norm(name) for name in names if self._norm(name)
                    },
                    "resolution_sources": resolution_sources,
                    "resolution_status": resolution_status,
                    "identity_confidence": confidence,
                }
            )
        return contexts

    def _deterministic_fragment_participants(
        self,
        inputs: list[TimelineTopicCandidate],
        *,
        facts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Derive actual speakers/narrators without asking the model to identify them."""
        roles = self._conversation_role_payload(inputs)
        fact_actor_ids = {
            str(actor.get("actor_id") or "").strip()
            for fact in (facts or [])
            for actor in fact.get("actor_refs", [])
            if isinstance(actor, dict) and str(actor.get("actor_id") or "").strip()
        }
        has_grounded_human = any(
            str(actor.get("actor_id") or "").strip() in fact_actor_ids
            for actor in roles.get("human_participants", [])
            if isinstance(actor, dict)
        )
        narrators = {
            str(value)
            for value in roles.get("timeline_narrators", {}).values()
            if str(value)
        }
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for bucket in ("human_participants", "assistant_personas"):
            for actor in roles.get(bucket, []):
                if not isinstance(actor, dict):
                    continue
                actor_id = str(actor.get("actor_id") or "").strip()
                if not actor_id:
                    continue
                actor_type = str(actor.get("actor_type") or "unknown")
                if (
                    actor_type == "human"
                    and has_grounded_human
                    and actor_id not in fact_actor_ids
                ):
                    continue
                relations = [
                    "narrator"
                    if actor_id in narrators
                    else ("responder" if actor_type == "assistant" else "speaker")
                ]
                if (
                    actor_type == "assistant"
                    and not actor.get("synthetic_narrator")
                    and "responder" not in relations
                ):
                    relations.append("responder")
                for relation in relations:
                    key = (actor_id, relation)
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append(
                        {
                            "actor_id": actor_id,
                            "actor_type": actor_type,
                            "relation_type": relation,
                            "display_name_snapshot": next(
                                (
                                    str(name).strip()
                                    for name in actor.get("observed_names", [])
                                    if str(name).strip()
                                ),
                                None,
                            ),
                            "confidence": float(actor.get("identity_confidence", 0.68)),
                            "resolution_status": str(
                                actor.get("resolution_status") or "inferred"
                            ),
                            "source": (
                                "fact_actor_refs"
                                if actor_id in fact_actor_ids
                                else "timeline_window_role_bindings"
                            ),
                            "participation_scope": (
                                "fragment_fact"
                                if actor_id in fact_actor_ids
                                else "timeline_window"
                            ),
                        }
                    )
        return result

    def _validate_synthesis(
        self, parsed: dict[str, Any], fragments: list[TopicFragmentDraft]
    ) -> dict[str, Any]:
        allowed = {item.fragment_uid for item in fragments}
        fragments_by_uid = {item.fragment_uid: item for item in fragments}
        fact_owners = {
            str(fact.get("fact_uid")): fragment.fragment_uid
            for fragment in fragments
            for fact in fragment.facts
            if str(fact.get("fact_uid") or "")
        }
        facts_by_uid = {
            str(fact.get("fact_uid")): (fragment, fact)
            for fragment in fragments
            for fact in fragment.facts
            if str(fact.get("fact_uid") or "")
        }
        supplied = set(self._unique_strings(parsed.get("fragment_uids")))
        raw_validation_repairs = parsed.get("validation_repairs")
        validation_repairs: list[dict[str, Any]] = (
            [dict(item) for item in raw_validation_repairs if isinstance(item, dict)]
            if isinstance(raw_validation_repairs, list)
            else []
        )
        if raw_validation_repairs is not None and not isinstance(
            raw_validation_repairs, list
        ):
            validation_repairs.append({"type": "discarded_invalid_validation_repairs"})
        unknown_fragments = sorted(supplied - allowed)
        missing_fragments = sorted(allowed - supplied)
        if unknown_fragments or missing_fragments:
            validation_repairs.append(
                {
                    "type": "normalized_synthesis_fragment_scope",
                    "dropped_unknown_fragment_uids": unknown_fragments,
                    "added_missing_fragment_uids": missing_fragments,
                }
            )
        title = str(parsed.get("title") or "").strip()
        summary = str(parsed.get("summary") or "").strip()
        if not title:
            title = next(
                (item.label.strip() for item in fragments if item.label.strip()),
                "Topic memory",
            )
            validation_repairs.append({"type": "filled_missing_synthesis_title"})
        if not summary:
            summary = "；".join(
                dict.fromkeys(
                    item.summary.strip() for item in fragments if item.summary.strip()
                )
            )[:12000]
            summary = summary or title
            validation_repairs.append({"type": "filled_missing_synthesis_summary"})
        raw_atoms = parsed.get("atoms")
        if not isinstance(raw_atoms, list):
            validation_repairs.append(
                {"type": "replaced_invalid_synthesis_atoms_array"}
            )
            raw_atoms = []
        atoms: list[dict[str, Any]] = []
        covered: set[str] = set()
        for atom_index, raw in enumerate(raw_atoms):
            if not isinstance(raw, dict) or not str(raw.get("content") or "").strip():
                validation_repairs.append(
                    {
                        "type": "dropped_invalid_synthesis_atom",
                        "atom_index": atom_index,
                    }
                )
                continue
            sources = set(self._unique_strings(raw.get("fragment_uids")))
            unknown_sources = sorted(sources - allowed)
            if unknown_sources:
                validation_repairs.append(
                    {
                        "type": "dropped_unknown_atom_fragment_uids",
                        "atom_index": atom_index,
                        "fragment_uids": unknown_sources,
                    }
                )
                sources &= allowed
            source_fact_uids = set(self._unique_strings(raw.get("source_fact_uids")))
            unknown_fact_uids = sorted(source_fact_uids - fact_owners.keys())
            if unknown_fact_uids:
                validation_repairs.append(
                    {
                        "type": "dropped_unknown_source_fact_uids",
                        "atom_index": atom_index,
                        "source_fact_uids": unknown_fact_uids,
                    }
                )
                source_fact_uids &= fact_owners.keys()
            if not source_fact_uids:
                validation_repairs.append(
                    {
                        "type": "dropped_ungrounded_synthesis_atom",
                        "atom_index": atom_index,
                    }
                )
                continue
            grounded_sources = {fact_owners[uid] for uid in source_fact_uids}
            if sources != grounded_sources:
                validation_repairs.append(
                    {
                        "type": "normalized_atom_fragment_provenance",
                        "atom_index": atom_index,
                        "declared_fragment_uids": sorted(sources),
                        "grounded_fragment_uids": sorted(grounded_sources),
                    }
                )
            # The fact IDs are authoritative. Discard fragment IDs that have no
            # cited fact instead of treating an unsupported declaration as proof.
            sources = grounded_sources
            covered.update(sources)
            atoms.append(
                {
                    "type": str(raw.get("type") or "factual"),
                    "content": str(raw["content"]).strip(),
                    "importance": self._score(raw.get("importance"), 0.5),
                    "confidence": self._score(raw.get("confidence"), 0.7),
                    "fragment_uids": sorted(sources),
                    "source_fact_uids": sorted(source_fact_uids),
                }
            )
        coverable = {
            item.fragment_uid
            for item in fragments
            if any(str(fact.get("fact_uid") or "") for fact in item.facts)
        }
        for fragment_uid in sorted(coverable - covered):
            fragment = fragments_by_uid[fragment_uid]
            added = 0
            merged = 0
            for fact in fragment.facts:
                fact_uid = str(fact.get("fact_uid") or "")
                content = str(fact.get("content") or "").strip()
                if not fact_uid or not content:
                    continue
                existing_atom = next(
                    (
                        atom
                        for atom in atoms
                        if self._norm(atom.get("content")) == self._norm(content)
                        and str(atom.get("type") or "factual")
                        == str(fact.get("type") or "factual")
                    ),
                    None,
                )
                if existing_atom is not None:
                    existing_atom["fragment_uids"] = sorted(
                        set(existing_atom["fragment_uids"]) | {fragment_uid}
                    )
                    existing_atom["source_fact_uids"] = sorted(
                        set(existing_atom["source_fact_uids"]) | {fact_uid}
                    )
                    merged += 1
                    continue
                atoms.append(
                    {
                        "type": str(fact.get("type") or "factual"),
                        "content": content,
                        "importance": self._score(
                            fact.get("importance"), fragment.importance
                        ),
                        "confidence": self._score(
                            fact.get("confidence"), fragment.confidence
                        ),
                        "fragment_uids": [fragment_uid],
                        "source_fact_uids": [fact_uid],
                    }
                )
                added += 1
            validation_repairs.append(
                {
                    "type": "missing_fragment_atom_coverage",
                    "fragment_uid": fragment_uid,
                    "added_passthrough_atoms": added,
                    "merged_source_facts": merged,
                }
            )
        required_timelines = {
            timeline_uid
            for fragment in fragments
            for timeline_uid in fragment.timeline_uids
        }
        cited_fact_uids = {
            uid
            for atom in atoms
            for uid in self._unique_strings(atom.get("source_fact_uids"))
            if uid in facts_by_uid
        }
        covered_timelines = {
            timeline_uid
            for fact_uid in cited_fact_uids
            for timeline_uid in self._unique_strings(
                facts_by_uid[fact_uid][1].get("source_timeline_uids")
            )
        }
        for timeline_uid in sorted(required_timelines - covered_timelines):
            if timeline_uid in covered_timelines:
                continue
            supporting = sorted(
                (
                    (fragment, fact)
                    for fragment in fragments
                    for fact in fragment.facts
                    if timeline_uid
                    in self._unique_strings(fact.get("source_timeline_uids"))
                    and str(fact.get("fact_uid") or "")
                    and str(fact.get("content") or "").strip()
                ),
                key=lambda item: (
                    -self._score(item[1].get("importance"), 0.5),
                    str(item[1].get("fact_uid")),
                ),
            )
            if not supporting:
                raise TopicBuildValidationError(
                    "Timeline has no source-grounded fact: " + timeline_uid
                )
            fragment, fact = supporting[0]
            fact_uid = str(fact["fact_uid"])
            content = str(fact["content"]).strip()
            atom_type = str(fact.get("type") or "factual")
            existing_atom = next(
                (
                    atom
                    for atom in atoms
                    if self._norm(atom.get("content")) == self._norm(content)
                    and str(atom.get("type") or "factual") == atom_type
                ),
                None,
            )
            if existing_atom is not None:
                existing_atom["fragment_uids"] = sorted(
                    set(existing_atom["fragment_uids"]) | {fragment.fragment_uid}
                )
                existing_atom["source_fact_uids"] = sorted(
                    set(existing_atom["source_fact_uids"]) | {fact_uid}
                )
                added = 0
            else:
                atoms.append(
                    {
                        "type": atom_type,
                        "content": content,
                        "importance": self._score(fact.get("importance"), 0.5),
                        "confidence": self._score(fact.get("confidence"), 0.7),
                        "fragment_uids": [fragment.fragment_uid],
                        "source_fact_uids": [fact_uid],
                    }
                )
                added = 1
            covered_timelines.update(
                self._unique_strings(fact.get("source_timeline_uids"))
            )
            validation_repairs.append(
                {
                    "type": "missing_timeline_atom_coverage",
                    "timeline_uid": timeline_uid,
                    "source_fact_uid": fact_uid,
                    "added_passthrough_atom": added,
                }
            )
        if validation_repairs:
            repair_audit = self._repair_audit(validation_repairs)
            coverage_repairs = sum(
                1
                for repair in validation_repairs
                if repair.get("type") == "missing_fragment_atom_coverage"
            )
            log = (
                logger.info
                if not repair_audit["repair"] and not repair_audit["fallback"]
                else logger.warning
            )
            log(
                "[TopicMemory] 已确定性规范化 LLM 合成输出 "
                "(normalizations=%s, repairs=%s, fallbacks=%s, "
                "coverage_fallbacks=%s)",
                repair_audit["normalization"],
                repair_audit["repair"],
                repair_audit["fallback"],
                coverage_repairs,
            )
        actor_links_by_key: dict[tuple[str, str], dict[str, Any]] = {}

        def merge_actor_link(
            raw_link: dict[str, Any], fact_uids: Iterable[str]
        ) -> None:
            if not self._valid_actor_relation_for_type(raw_link):
                return
            actor_id = str(raw_link.get("actor_id") or "").strip()
            relation_type = str(raw_link.get("relation_type") or "").strip()
            if not actor_id or not relation_type:
                return
            grounded_fact_uids = sorted(
                {str(uid) for uid in fact_uids if str(uid) in fact_owners}
            )
            key = (actor_id, relation_type)
            existing_link = actor_links_by_key.setdefault(
                key,
                {
                    **raw_link,
                    "actor_id": actor_id,
                    "relation_type": relation_type,
                    "source_fact_uids": [],
                },
            )
            existing_link["source_fact_uids"] = sorted(
                set(existing_link["source_fact_uids"]) | set(grounded_fact_uids)
            )
            existing_link["confidence"] = max(
                self._score(existing_link.get("confidence"), 0.7),
                self._score(raw_link.get("confidence"), 0.7),
            )

        # The synthesis model may reorganize facts, but it must not decide identity
        # participation a second time. Stable participation and semantic actor roles
        # are already grounded and validated on the formal fragments below.
        for fragment in fragments:
            for key in ("participant_refs", "mentioned_actor_refs"):
                for raw_link in fragment.metadata.get(key, []):
                    if isinstance(raw_link, dict):
                        merge_actor_link(
                            {
                                **raw_link,
                                "fragment_uids": [fragment.fragment_uid],
                                "timeline_uids": list(fragment.timeline_uids),
                            },
                            [],
                        )
            for fact in fragment.facts:
                fact_uid = str(fact.get("fact_uid") or "")
                for raw_link in fact.get("actor_refs", []):
                    if isinstance(raw_link, dict):
                        merge_actor_link(raw_link, [fact_uid])
        return {
            "title": title,
            "summary": summary,
            "importance": self._score(parsed.get("importance"), 0.5),
            "confidence": self._score(parsed.get("confidence"), 0.7),
            "fragment_uids": sorted(allowed),
            "atoms": atoms,
            "actor_links": list(actor_links_by_key.values()),
            "validation_repairs": validation_repairs,
        }


__all__ = ["TopicSnapshotPublisherMixin"]
