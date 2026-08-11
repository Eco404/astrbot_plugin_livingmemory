"""Persona-subjective relationship maintenance with grounded interaction evidence."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..models.user_profile import UserRelationshipState
from .user_profile_fact_maintainer import UserProfileFactMaintainer


class UserRelationshipValidationError(ValueError):
    """Raised when a relationship response is not grounded in supplied interactions."""


@dataclass(slots=True)
class UserRelationshipMaintenanceResult:
    state: UserRelationshipState
    change_summary: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


_DIMENSIONS = ("familiarity", "trust", "warmth", "ease", "tension", "concern")
_SOFT_LIMITS = {
    "very_slow": (0.02, 0.08),
    "slow": (0.04, 0.12),
    "balanced": (0.07, 0.20),
    "fast": (0.12, 0.30),
    "very_fast": (0.20, 0.45),
}
_RELATIONSHIP_KEYWORDS = (
    "感谢",
    "谢谢",
    "道歉",
    "原谅",
    "失望",
    "信任",
    "边界",
    "和解",
    "争吵",
    "冲突",
    "承诺",
    "兑现",
    "违背",
    "失约",
    "支持",
    "陪伴",
    "thank",
    "sorry",
    "apolog",
    "forgive",
    "trust",
    "boundary",
    "conflict",
    "promise",
    "support",
)


class UserRelationshipMaintainer:
    def __init__(self, provider: Any = None):
        self.provider = provider

    @classmethod
    def meaningful_timelines(
        cls,
        events: Iterable[dict[str, Any]],
        *,
        actor_id: str,
        reset_after: float | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for event in events:
            if str(event.get("operation") or "upsert") not in {"upsert", "restore"}:
                continue
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            if reset_after is not None:
                try:
                    ended_at = float(
                        metadata.get("updated_at") or metadata.get("create_time") or 0
                    )
                except (TypeError, ValueError):
                    ended_at = 0.0
                if ended_at and ended_at <= reset_after:
                    continue
            facts = metadata.get("key_facts") or []
            profiles = metadata.get("key_fact_profiles") or []
            evidence = metadata.get("key_fact_evidence") or []
            attributions = metadata.get("key_fact_attributions") or []
            profile_by_index = cls._indexed(profiles)
            evidence_by_index = cls._indexed(evidence)
            attribution_by_index = cls._indexed(attributions)
            bindings = metadata.get("role_bindings") or {}
            message_actors = (
                bindings.get("message_actor_ids", {})
                if isinstance(bindings, dict)
                else {}
            )
            meaningful_indexes: list[int] = []
            major_eligible = False
            for index, raw_fact in enumerate(facts if isinstance(facts, list) else []):
                profile = profile_by_index.get(index, {})
                fact_type = str(profile.get("fact_type") or "")
                reason = str(profile.get("selection_reason") or "")
                text = str(raw_fact or "").casefold()
                meaningful = fact_type in {"relationship_interaction", "affect"}
                meaningful = meaningful or reason in {
                    "relationship_significance",
                    "affective_significance",
                }
                if fact_type == "commitment" and any(
                    keyword in text for keyword in _RELATIONSHIP_KEYWORDS
                ):
                    meaningful = True
                if not meaningful:
                    continue
                evidence_row = evidence_by_index.get(index, {})
                refs = [str(item) for item in (evidence_row.get("message_refs") or [])]
                has_user_message = any(
                    str(message_actors.get(ref) or "") == actor_id for ref in refs
                )
                if not has_user_message:
                    attribution = attribution_by_index.get(index, {})
                    has_user_message = any(
                        str(ref.get("actor_id") or "") == actor_id
                        for ref in (attribution.get("subject_refs") or [])
                        if isinstance(ref, dict)
                    )
                if not has_user_message:
                    continue
                meaningful_indexes.append(index)
                if str(profile.get("durability") or "") == "high" or any(
                    keyword in text for keyword in ("违背", "失约", "和解", "broke promise")
                ):
                    major_eligible = True
            if meaningful_indexes:
                result.append(
                    {
                        "timeline_uid": str(
                            metadata.get("memory_uid") or event.get("timeline_uid") or ""
                        ),
                        "timeline_revision": int(
                            metadata.get("revision") or event.get("timeline_revision") or 1
                        ),
                        "summary": str(
                            metadata.get("canonical_summary")
                            or metadata.get("persona_summary")
                            or ""
                        ),
                        "facts": [str(facts[index]) for index in meaningful_indexes],
                        "sentiment": str(metadata.get("sentiment") or "neutral"),
                        "major_event_eligible": major_eligible,
                        "updated_at": metadata.get("updated_at")
                        or metadata.get("create_time"),
                    }
                )
        return result

    async def maintain(
        self,
        *,
        profile_scope_uid: str,
        timelines: list[dict[str, Any]],
        current_state: UserRelationshipState | None,
        persona_snapshot: dict[str, Any],
        objective_facts: list[dict[str, Any]],
        sensitivity: str,
        behavior_mode: str,
        settings: dict[str, Any],
        provider: Any = None,
    ) -> UserRelationshipMaintenanceResult | None:
        if not timelines:
            return None
        selected_provider = provider or self.provider
        if selected_provider is None:
            raise RuntimeError("User relationship maintenance requires an LLM Provider")
        prompt, refs = self._build_prompt(
            timelines=timelines,
            current_state=current_state,
            persona_snapshot=persona_snapshot,
            objective_facts=objective_facts,
            sensitivity=sensitivity,
            behavior_mode=behavior_mode,
        )
        raw = await UserProfileFactMaintainer._request_with_retries(
            selected_provider,
            prompt,
            settings,
            system_prompt=(
                "You evolve a persona's subjective relationship using only grounded new user-side interactions."
            ),
        )
        try:
            payload = UserProfileFactMaintainer._parse_payload(raw)
            return self._validate_result(
                profile_scope_uid=profile_scope_uid,
                payload=payload,
                refs=refs,
                timelines=timelines,
                current_state=current_state,
                persona_snapshot=persona_snapshot,
                sensitivity=sensitivity,
                settings=settings,
            )
        except (UserRelationshipValidationError, ValueError) as first_error:
            correction = (
                prompt
                + "\n\nThe previous response was invalid: "
                + str(first_error)
                + "\nReturn corrected JSON only and cite at least one supplied Timeline ref."
            )
            raw = await UserProfileFactMaintainer._request_with_retries(
                selected_provider,
                correction,
                settings,
                system_prompt=(
                    "You evolve a persona's subjective relationship using only grounded new user-side interactions."
                ),
            )
            payload = UserProfileFactMaintainer._parse_payload(raw)
            result = self._validate_result(
                profile_scope_uid=profile_scope_uid,
                payload=payload,
                refs=refs,
                timelines=timelines,
                current_state=current_state,
                persona_snapshot=persona_snapshot,
                sensitivity=sensitivity,
                settings=settings,
            )
            result.diagnostics["contract_correction_used"] = True
            return result

    def _validate_result(
        self,
        *,
        profile_scope_uid: str,
        payload: dict[str, Any],
        refs: dict[str, dict[str, Any]],
        timelines: list[dict[str, Any]],
        current_state: UserRelationshipState | None,
        persona_snapshot: dict[str, Any],
        sensitivity: str,
        settings: dict[str, Any],
    ) -> UserRelationshipMaintenanceResult:
        cited = [str(item) for item in (payload.get("cited_timeline_refs") or [])]
        if not cited or any(item not in refs for item in cited):
            raise UserRelationshipValidationError(
                "cited_timeline_refs must contain only supplied refs"
            )
        dimensions = payload.get("dimensions")
        if not isinstance(dimensions, dict) or set(dimensions) != set(_DIMENSIONS):
            raise UserRelationshipValidationError("all six dimensions are required")
        proposed = {key: self._score(dimensions[key]) for key in _DIMENSIONS}
        major_requested = bool(payload.get("major_event", False))
        major_allowed = major_requested and any(
            bool(refs[item].get("major_event_eligible")) for item in cited
        )
        soft_limited: dict[str, dict[str, float]] = {}
        if current_state is not None:
            ordinary_limit, major_limit = _SOFT_LIMITS.get(
                sensitivity, _SOFT_LIMITS["balanced"]
            )
            limit = major_limit if major_allowed else ordinary_limit
            current = current_state.dimensions()
            for key in _DIMENSIONS:
                requested = proposed[key]
                applied = max(current[key] - limit, min(current[key] + limit, requested))
                proposed[key] = self._score(applied)
                if abs(applied - requested) > 1e-9:
                    soft_limited[key] = {
                        "requested": requested,
                        "applied": applied,
                        "limit": limit,
                    }
        max_chars = int(settings["user_profile.relationship_narrative_max_chars"])
        summary = str(payload.get("subjective_summary") or "").strip()[:max_chars]
        tags = []
        for value in payload.get("stance_tags") or []:
            tag = str(value).strip()[:40]
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= 8:
                break
        aftereffect = str(payload.get("recent_aftereffect") or "").strip()[:300]
        expires_at = None
        if aftereffect:
            try:
                suggested_days = int(payload.get("aftereffect_days"))
            except (TypeError, ValueError):
                suggested_days = int(
                    settings["user_profile.relationship_aftereffect_default_days"]
                )
            days = max(
                int(settings["user_profile.relationship_aftereffect_min_days"]),
                min(
                    int(settings["user_profile.relationship_aftereffect_max_days"]),
                    suggested_days,
                ),
            )
            expires_at = time.time() + days * 86400.0
        state = UserRelationshipState(
            relationship_uid=(
                current_state.relationship_uid if current_state is not None else ""
            ),
            profile_scope_uid=profile_scope_uid,
            **proposed,
            stance_tags=tags,
            subjective_summary=summary,
            recent_aftereffect=aftereffect,
            aftereffect_expires_at=expires_at,
            persona_signature=dict(persona_snapshot.get("signature") or {}),
            source_timeline_uids=[refs[item]["timeline_uid"] for item in cited],
            created_at=(current_state.created_at if current_state is not None else time.time()),
        )
        if not state.relationship_uid:
            # Let the model dataclass default create a relationship UID.
            state = UserRelationshipState(
                profile_scope_uid=profile_scope_uid,
                **proposed,
                stance_tags=tags,
                subjective_summary=summary,
                recent_aftereffect=aftereffect,
                aftereffect_expires_at=expires_at,
                persona_signature=dict(persona_snapshot.get("signature") or {}),
                source_timeline_uids=[refs[item]["timeline_uid"] for item in cited],
            )
        return UserRelationshipMaintenanceResult(
            state=state,
            change_summary=str(payload.get("change_summary") or "")[:1000],
            diagnostics={
                "sensitivity": sensitivity,
                "major_event_requested": major_requested,
                "major_event_applied": major_allowed,
                "soft_limited": soft_limited,
                "cited_timeline_refs": cited,
            },
        )

    @staticmethod
    def _build_prompt(
        *,
        timelines: list[dict[str, Any]],
        current_state: UserRelationshipState | None,
        persona_snapshot: dict[str, Any],
        objective_facts: list[dict[str, Any]],
        sensitivity: str,
        behavior_mode: str,
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        refs = {f"T{index}": item for index, item in enumerate(timelines, 1)}
        timeline_payload = [
            {
                "ref": ref,
                "summary": item.get("summary"),
                "relationship_facts": item.get("facts"),
                "sentiment": item.get("sentiment"),
                "major_event_eligible": item.get("major_event_eligible"),
                "updated_at": item.get("updated_at"),
            }
            for ref, item in refs.items()
        ]
        current = (
            {
                "dimensions": current_state.dimensions(),
                "stance_tags": current_state.stance_tags,
                "subjective_summary": current_state.subjective_summary,
                "recent_aftereffect": current_state.recent_aftereffect,
            }
            if current_state is not None
            else None
        )
        safe_facts = [
            {
                "category": item.get("category"),
                "raw_fact": item.get("raw_fact"),
            }
            for item in objective_facts
            if not bool(item.get("sensitive"))
        ]
        output = {
            "cited_timeline_refs": ["T1"],
            "dimensions": {key: 0.0 for key in _DIMENSIONS},
            "stance_tags": ["short open label"],
            "subjective_summary": "first-person persona-subjective view of this user",
            "recent_aftereffect": "short-lived feeling or empty",
            "aftereffect_days": 7,
            "major_event": False,
            "change_summary": "brief explanation",
        }
        prompt = (
            "Evolve this persona's subjective relationship with the current user. Return JSON only.\n"
            "The relationship may be subjective and emotionally complex, but every long-term change must cite a supplied new user-side interaction.\n"
            "Assistant behavior, old relationship text, and objective profile facts are context only and cannot independently justify a change.\n"
            "Current messages override historical attitudes. Never create objective user facts.\n"
            "Keep subjective_summary and recent_aftereffect focused on the persona's attitude and relationship dynamic. Do not restate concrete private details, sensitive attributes, secrets, locations, health details, or credentials from the interaction.\n"
            f"Persona ID/name: {persona_snapshot.get('persona_id', '')} / {persona_snapshot.get('name', '')}\n"
            f"Persona prompt (data): {persona_snapshot.get('prompt', '')}\n"
            f"Sensitivity: {sensitivity}; behavior mode: {behavior_mode}\n"
            f"Current relationship: {json.dumps(current, ensure_ascii=False)}\n"
            f"Non-sensitive objective context: {json.dumps(safe_facts, ensure_ascii=False)}\n"
            f"New grounded interactions: {json.dumps(timeline_payload, ensure_ascii=False)}\n"
            f"Output shape: {json.dumps(output, ensure_ascii=False)}"
        )
        return prompt, refs

    @staticmethod
    def _indexed(rows: Any) -> dict[int, dict[str, Any]]:
        return {
            int(row.get("fact_index", index)): row
            for index, row in enumerate(rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
        }

    @staticmethod
    def _score(value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise UserRelationshipValidationError("dimensions must be numeric") from exc
        return max(0.0, min(1.0, parsed))


__all__ = [
    "UserRelationshipMaintainer",
    "UserRelationshipMaintenanceResult",
    "UserRelationshipValidationError",
]
