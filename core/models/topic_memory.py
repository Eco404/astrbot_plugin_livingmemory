"""Data contracts for the automatically maintained topic-memory layer."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TopicMemoryStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    REBUILDING = "rebuilding"
    ARCHIVED = "archived"
    ERROR = "error"


class TopicLinkStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"


class TopicRelationType(str, Enum):
    RELATED = "related"
    # Read compatibility for databases created before v9.11.
    RELATED_SUBTOPIC = "related_subtopic"


class TopicActorRelationType(str, Enum):
    SPEAKER = "speaker"
    NARRATOR = "narrator"
    RESPONDER = "responder"
    SUBJECT = "subject"
    MENTIONED = "mentioned"
    EXECUTOR = "executor"
    REQUESTER = "requester"


class TopicActorResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    EVIDENCE_CONFIRMED = "evidence_confirmed"
    TIMELINE_BOUND = "timeline_bound"
    SESSION_INFERRED = "session_inferred"
    PROFILE_INFERRED = "profile_inferred"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"


class TopicMaintenanceMode(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    REPAIR = "repair"


class TopicMaintenanceStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_REVIEW = "completed_with_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class TopicMemory:
    memory_space_id: str
    title: str
    summary: str
    topic_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    revision: int = 0
    status: TopicMemoryStatus = TopicMemoryStatus.ACTIVE
    base_importance: float = 0.5
    importance: float = 0.5
    semantic_importance: float = 0.5
    source_base_component: float = 0.5
    evidence_strength: float = 0.5
    importance_policy_version: int = 1
    source_importance_hash: str = ""
    confidence: float = 0.7
    started_at: float | None = None
    ended_at: float | None = None
    last_accessed_at: float | None = None
    access_count: int = 0
    decay_anchor_at: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    embedding_signature: dict[str, Any] = field(default_factory=dict)
    affect_profile: list[dict[str, Any]] = field(default_factory=list)
    affective_salience: float = 0.0
    affect_signature: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    participants: list[TopicActorRef] = field(default_factory=list)
    mentioned_actors: list[TopicActorRef] = field(default_factory=list)


@dataclass(slots=True)
class TopicActorRef:
    """Read-only aggregate view of one actor within a Topic revision."""

    actor_id: str
    actor_type: str
    relation_types: list[str] = field(default_factory=list)
    display_names: list[str] = field(default_factory=list)
    confidence: float = 1.0
    resolution_status: str = TopicActorResolutionStatus.RESOLVED.value
    fragment_uids: list[str] = field(default_factory=list)
    timeline_uids: list[str] = field(default_factory=list)
    atom_uids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicActorLink:
    """Authoritative Topic-to-actor relation row."""

    topic_uid: str
    actor_id: str
    actor_type: str
    relation_type: str
    display_name_snapshot: str | None = None
    confidence: float = 1.0
    resolution_status: str = TopicActorResolutionStatus.RESOLVED.value
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicAtomActorLink:
    """Fact-level actor relation with fragment and Timeline provenance."""

    topic_atom_uid: str
    actor_id: str
    relation_type: str
    fragment_uid: str
    timeline_uid: str | None = None
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicMemoryAtom:
    topic_uid: str
    atom_type: str
    content: str
    atom_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    canonical_content: str = ""
    importance: float = 0.5
    confidence: float = 0.7
    status: str = "active"
    event_started_at: float | None = None
    event_ended_at: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicTimelineLink:
    topic_uid: str
    timeline_uid: str
    time_cluster_key: str
    contribution_weight: float = 1.0
    semantic_similarity: float = 1.0
    temporal_affinity: float = 1.0
    source_timeline_revision: int = 1
    topic_revision: int = 0
    status: TopicLinkStatus = TopicLinkStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicFragmentLink:
    """Formal serving-layer link from one Topic revision to one fragment."""

    topic_uid: str
    fragment_uid: str
    topic_revision: int = 0
    contribution_weight: float = 1.0
    status: TopicLinkStatus = TopicLinkStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicAtomSource:
    topic_atom_uid: str
    timeline_uid: str
    source_atom_id: int | None = None
    source_atom_fingerprint: str | None = None
    source_timeline_revision: int = 1
    contribution_weight: float = 1.0
    source_kind: str = "atom"
    source_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicRelation:
    memory_space_id: str
    left_topic_uid: str
    right_topic_uid: str
    confidence: float
    semantic_similarity: float
    relation_type: TopicRelationType = TopicRelationType.RELATED
    relation_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "active"
    build_run_uid: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicMaintenanceRun:
    memory_space_id: str
    mode: TopicMaintenanceMode
    run_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: TopicMaintenanceStatus = TopicMaintenanceStatus.PENDING
    cursor_memory_uid: str | None = None
    total_items: int = 0
    processed_items: int = 0
    created_topics: int = 0
    updated_topics: int = 0
    failed_items: int = 0
    started_at: float | None = None
    completed_at: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TimelineTopicCandidate:
    """Read-only Timeline projection used by deterministic Topic discovery."""

    memory_uid: str
    document_id: int
    source_revision: int
    memory_space_id: str
    session_id: str | None
    content: str
    summary: str
    base_importance: float = 0.5
    effective_importance: float = 0.5
    importance_revision: int = 1
    topics: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    key_fact_temporal: list[dict[str, Any]] = field(default_factory=list)
    key_fact_attributions: list[dict[str, Any]] = field(default_factory=list)
    atom_fingerprints: list[str] = field(default_factory=list)
    atom_contents: list[str] = field(default_factory=list)
    atom_temporal: list[dict[str, Any]] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    time_cluster_key: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    persona_id: str | None = None
    role_bindings: dict[str, Any] = field(default_factory=dict)
    source_window: dict[str, Any] = field(default_factory=dict)
    edit_origin: str | None = None
    traceability: str | None = None


@dataclass(slots=True)
class TopicCandidateGroup:
    """A deterministic proposal that must be reviewed by a later LLM stage."""

    run_uid: str
    group_index: int
    memory_space_id: str
    label: str
    timeline_uids: list[str]
    time_cluster_keys: list[str]
    cohesion: float
    group_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float | None = None
    ended_at: float | None = None
    shared_signals: list[str] = field(default_factory=list)
    status: str = "preview"
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicFragmentDraft:
    """Source-grounded LLM output before cross-time Topic synthesis."""

    run_uid: str
    candidate_group_uid: str
    memory_space_id: str
    label: str
    summary: str
    timeline_uids: list[str]
    source_revisions: dict[str, int]
    facts: list[dict[str, Any]]
    keywords: list[str] = field(default_factory=list)
    time_cluster_keys: list[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.7
    fragment_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    logical_fragment_uid: str = ""
    fragment_revision: int = 1
    embedding: list[float] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    status: str = "draft"
    prompt_hash: str = ""
    input_hash: str = ""
    provider_id: str = ""
    model_id: str = ""
    embedding_signature: dict[str, Any] = field(default_factory=dict)
    affect_events: list[dict[str, Any]] = field(default_factory=list)
    affect_signature: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "TopicActorLink",
    "TopicActorRef",
    "TopicActorRelationType",
    "TopicActorResolutionStatus",
    "TopicAtomActorLink",
    "TopicAtomSource",
    "TopicLinkStatus",
    "TopicMaintenanceMode",
    "TopicMaintenanceRun",
    "TopicMaintenanceStatus",
    "TopicMemory",
    "TopicMemoryAtom",
    "TopicMemoryStatus",
    "TopicRelation",
    "TopicRelationType",
    "TopicFragmentLink",
    "TopicTimelineLink",
    "TimelineTopicCandidate",
    "TopicCandidateGroup",
    "TopicFragmentDraft",
]
