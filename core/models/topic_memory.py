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


class TopicMaintenanceMode(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    REPAIR = "repair"


class TopicMaintenanceStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
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
    confidence: float = 0.7
    started_at: float | None = None
    ended_at: float | None = None
    last_accessed_at: float | None = None
    access_count: int = 0
    decay_anchor_at: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
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
    topics: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    atom_fingerprints: list[str] = field(default_factory=list)
    atom_contents: list[str] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    time_cluster_key: str = ""
    features: dict[str, Any] = field(default_factory=dict)


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
    embedding: list[float] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    status: str = "draft"
    prompt_hash: str = ""
    input_hash: str = ""
    provider_id: str = ""
    model_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "TopicAtomSource",
    "TopicLinkStatus",
    "TopicMaintenanceMode",
    "TopicMaintenanceRun",
    "TopicMaintenanceStatus",
    "TopicMemory",
    "TopicMemoryAtom",
    "TopicMemoryStatus",
    "TopicTimelineLink",
    "TimelineTopicCandidate",
    "TopicCandidateGroup",
    "TopicFragmentDraft",
]
