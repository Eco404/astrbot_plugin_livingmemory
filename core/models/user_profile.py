"""Data contracts for private-chat user profiles and persona relationships."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


_PROFILE_NAMESPACE = uuid.UUID("ad492c39-6c95-4c57-91e6-93d1a85ce290")


class UserProfileStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    DELETED = "deleted"


class UserProfileFactCategory(str, Enum):
    STABLE_INFO = "stable_info"
    PREFERENCE = "preference"
    HABIT = "habit"
    CURRENT_STATE = "current_state"
    PLAN_COMMITMENT = "plan_commitment"
    COMMUNICATION_PREFERENCE = "communication_preference"


class UserProfileFactStatus(str, Enum):
    ACTIVE = "active"
    PENDING = "pending"
    CONFLICT = "conflict"
    SUPERSEDED = "superseded"
    STALE = "stale"
    ARCHIVED = "archived"
    EXCLUDED = "excluded"


class UserProfileInferenceKind(str, Enum):
    EXPLICIT = "explicit"
    DIRECT_OBSERVATION = "direct_observation"
    BEHAVIORAL_INFERENCE = "behavioral_inference"


class UserRelationshipSensitivity(str, Enum):
    VERY_SLOW = "very_slow"
    SLOW = "slow"
    BALANCED = "balanced"
    FAST = "fast"
    VERY_FAST = "very_fast"


class UserRelationshipBehaviorMode(str, Enum):
    RESTRAINED = "restrained"
    NATURAL = "natural"
    HIGH_AUTONOMY = "high_autonomy"
    UNRESTRICTED = "unrestricted"


class UserProfileProjectionOperation(str, Enum):
    UPSERT = "upsert"
    ARCHIVE = "archive"
    RESTORE = "restore"
    DELETE = "delete"


class UserProfileTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING_FACTS = "running_facts"
    FACTS_COMPLETED = "facts_completed"
    FACTS_FAILED = "facts_failed"
    RUNNING_RELATIONSHIP = "running_relationship"
    COMPLETED = "completed"
    COMPLETED_PARTIAL = "completed_partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


def profile_scope_uid(bot_account: str, persona_id: str, logical_user_uid: str) -> str:
    payload = json.dumps(
        {
            "bot_account": str(bot_account).strip(),
            "persona_id": str(persona_id).strip(),
            "logical_user_uid": str(logical_user_uid).strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"profile-scope-v1-{uuid.uuid5(_PROFILE_NAMESPACE, payload)}"


def default_fact_namespace_uid(scope_uid: str) -> str:
    return f"profile-facts-v1-{uuid.uuid5(_PROFILE_NAMESPACE, str(scope_uid))}"


def fact_fingerprint(actor_id: str, raw_fact: str, category: str = "") -> str:
    payload = "\x1f".join(
        (
            str(actor_id).strip().casefold(),
            str(category).strip().casefold(),
            " ".join(str(raw_fact).split()).casefold(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class UserProfileScope:
    logical_user_uid: str
    bot_account: str
    persona_id: str
    profile_scope_uid: str = ""
    fact_namespace_uid: str = ""
    enabled: bool = True
    auto_enable_blocked: bool = False
    projection_cursor: int = 0
    reset_after: float | None = None
    has_gap: bool = False
    relationship_frozen: bool = False
    relationship_reset_after: float | None = None
    relationship_sensitivity_override: str | None = None
    relationship_behavior_override: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.profile_scope_uid:
            self.profile_scope_uid = profile_scope_uid(
                self.bot_account, self.persona_id, self.logical_user_uid
            )
        if not self.fact_namespace_uid:
            self.fact_namespace_uid = default_fact_namespace_uid(self.profile_scope_uid)


@dataclass(slots=True)
class UserProfileFactSource:
    timeline_uid: str
    timeline_revision: int
    fact_index: int
    raw_fact: str
    actor_id: str
    source_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    profile_fact_uid: str | None = None
    fact_fingerprint: str = ""
    claim_type: str = "speaker_self"
    attribution_confidence: float = 1.0
    timeline_quality: dict[str, Any] = field(default_factory=dict)
    evidence_started_at: float | None = None
    evidence_ended_at: float | None = None
    source_account_actor_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.fact_fingerprint:
            self.fact_fingerprint = fact_fingerprint(self.actor_id, self.raw_fact)
        if not self.source_account_actor_id:
            self.source_account_actor_id = self.actor_id


@dataclass(slots=True)
class UserProfileFact:
    fact_namespace_uid: str
    category: UserProfileFactCategory | str
    representative_source_uid: str
    profile_fact_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: UserProfileFactStatus | str = UserProfileFactStatus.ACTIVE
    confidence: float = 0.85
    importance: float = 0.5
    inference_kind: UserProfileInferenceKind | str = UserProfileInferenceKind.EXPLICIT
    sensitive: bool = False
    admin_confirmed: bool = False
    pinned: bool = False
    first_seen_at: float | None = None
    last_confirmed_at: float | None = None
    fixed_injection_until: float | None = None
    review_after: float | None = None
    superseded_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class UserRelationshipState:
    profile_scope_uid: str
    relationship_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    revision: int = 0
    familiarity: float = 0.0
    trust: float = 0.0
    warmth: float = 0.0
    ease: float = 0.0
    tension: float = 0.0
    concern: float = 0.0
    stance_tags: list[str] = field(default_factory=list)
    subjective_summary: str = ""
    recent_aftereffect: str = ""
    aftereffect_expires_at: float | None = None
    persona_signature: dict[str, Any] = field(default_factory=dict)
    source_timeline_uids: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def dimensions(self) -> dict[str, float]:
        return {
            key: max(0.0, min(1.0, float(getattr(self, key))))
            for key in (
                "familiarity",
                "trust",
                "warmth",
                "ease",
                "tension",
                "concern",
            )
        }


@dataclass(slots=True)
class UserProfileProjectionEvent:
    timeline_uid: str
    timeline_revision: int
    operation: UserProfileProjectionOperation | str
    memory_space_id: str
    profile_scope_uid: str | None = None
    event_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "pending"
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class UserProfileTask:
    profile_scope_uid: str
    task_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: UserProfileTaskStatus | str = UserProfileTaskStatus.PENDING
    settings_snapshot: dict[str, Any] = field(default_factory=dict)
    provider_signature: dict[str, Any] = field(default_factory=dict)
    retries: int = 0
    error: str | None = None
    result_summary: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


__all__ = [
    "UserProfileFact",
    "UserProfileFactCategory",
    "UserProfileFactSource",
    "UserProfileFactStatus",
    "UserProfileInferenceKind",
    "UserProfileProjectionEvent",
    "UserProfileProjectionOperation",
    "UserProfileScope",
    "UserProfileStatus",
    "UserProfileTask",
    "UserProfileTaskStatus",
    "UserRelationshipBehaviorMode",
    "UserRelationshipSensitivity",
    "UserRelationshipState",
    "default_fact_namespace_uid",
    "fact_fingerprint",
    "profile_scope_uid",
]
