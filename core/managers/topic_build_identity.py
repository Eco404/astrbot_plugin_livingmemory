"""Actor identity, attribution, and source-reference decoding for Topic construction."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import (
    Any,
)

from ..fact_temporal import (
    aggregate_fact_temporal,
    normalize_fact_temporal,
)
from ..models.conversation_models import stable_actor_id
from ..models.identity_profile import (
    SupplementalIdentityProfile,
    identity_prompt_payload,
)
from ..models.platform_identity import canonical_platform
from ..models.topic_memory import (
    TimelineTopicCandidate,
    TopicActorLink,
    TopicFragmentDraft,
    TopicMemory,
)
from .topic_build_contracts import (
    _ACTOR_RELATION_ALIASES,
    _NARRATIVE_SCHEMA_VERSION,
    TopicBuildValidationError,
)


class TopicBuildIdentityMixin:
    def _component_review_llm_context(
        self,
        fragments: list[TopicFragmentDraft],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        payload: list[dict[str, Any]] = []
        fragment_refs: dict[str, str] = {}
        for index, fragment in enumerate(fragments, 1):
            ref = f"P{index}"
            fragment_refs[ref] = fragment.fragment_uid
            payload.append(
                {
                    "ref": ref,
                    "label": fragment.label,
                    "summary": fragment.summary,
                    "facts": [
                        {
                            "type": str(fact.get("type") or "factual"),
                            "content": str(fact.get("content") or "").strip(),
                        }
                        for fact in fragment.facts[:8]
                        if str(fact.get("content") or "").strip()
                    ],
                    "fact_count": len(fragment.facts),
                    "keywords": list(fragment.keywords[:12]),
                    "started_at": fragment.started_at,
                    "ended_at": fragment.ended_at,
                }
            )
        prompt_roles = self._fragment_role_payload(fragments)
        prompt_roles.pop("timeline_narrators", None)
        return {
            "supplemental_identity_hints": self._fragment_identity_payload(fragments),
            "conversation_roles": prompt_roles,
            "fragments": payload,
        }, fragment_refs

    def _decode_component_review_refs(
        self,
        parsed: dict[str, Any],
        fragment_refs: dict[str, str],
        fragments: list[TopicFragmentDraft],
    ) -> list[list[str]]:
        raw_groups = parsed.get("groups")
        if not isinstance(raw_groups, list) or not raw_groups:
            raise TopicBuildValidationError(
                "component review groups must be a non-empty array"
            )
        groups: list[list[str]] = []
        seen_refs: set[str] = set()
        for index, group in enumerate(raw_groups):
            if not isinstance(group, dict):
                raise TopicBuildValidationError(
                    f"component review group {index} must be an object"
                )
            refs = self._unique_strings(group.get("fragment_refs"))
            if not refs:
                raise TopicBuildValidationError(
                    f"component review group {index} has no fragment refs"
                )
            unknown = [ref for ref in refs if ref not in fragment_refs]
            duplicates = [ref for ref in refs if ref in seen_refs]
            if unknown or duplicates:
                raise TopicBuildValidationError(
                    f"component review group {index} has invalid fragment refs: "
                    f"unknown={unknown}, duplicate={duplicates}"
                )
            seen_refs.update(refs)
            groups.append([fragment_refs[ref] for ref in refs])
        missing = [ref for ref in fragment_refs if ref not in seen_refs]
        if missing:
            raise TopicBuildValidationError(
                "component review did not cover fragment refs: " + ", ".join(missing)
            )
        return self._validate_component_uid_groups(groups, fragments)

    @staticmethod
    def _validate_component_uid_groups(
        raw_groups: Any,
        fragments: list[TopicFragmentDraft],
    ) -> list[list[str]]:
        if not isinstance(raw_groups, list) or not raw_groups:
            raise TopicBuildValidationError(
                "component review checkpoint groups must be a non-empty array"
            )
        allowed = [fragment.fragment_uid for fragment in fragments]
        allowed_set = set(allowed)
        order = {uid: index for index, uid in enumerate(allowed)}
        seen: set[str] = set()
        groups: list[list[str]] = []
        for index, raw_group in enumerate(raw_groups):
            if not isinstance(raw_group, list) or not raw_group:
                raise TopicBuildValidationError(
                    f"component review checkpoint group {index} is invalid"
                )
            group = [str(uid or "").strip() for uid in raw_group]
            if any(not uid for uid in group):
                raise TopicBuildValidationError(
                    f"component review checkpoint group {index} has an empty UID"
                )
            unknown = [uid for uid in group if uid not in allowed_set]
            duplicates = [uid for uid in group if uid in seen]
            if unknown or duplicates or len(group) != len(set(group)):
                raise TopicBuildValidationError(
                    f"component review checkpoint group {index} has invalid UIDs"
                )
            seen.update(group)
            groups.append(sorted(group, key=order.__getitem__))
        if seen != allowed_set:
            raise TopicBuildValidationError(
                "component review checkpoint does not preserve fragment scope"
            )
        groups.sort(key=lambda group: min(order[uid] for uid in group))
        return groups

    def _fragment_llm_context(
        self, inputs: list[TimelineTopicCandidate]
    ) -> tuple[
        dict[str, Any],
        dict[str, str],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        """Build a compact prompt payload with batch-local, reversible refs."""
        timelines: list[dict[str, Any]] = []
        timeline_refs: dict[str, str] = {}
        source_refs: dict[str, dict[str, Any]] = {}
        for timeline_index, item in enumerate(inputs, 1):
            timeline_ref = f"T{timeline_index}"
            timeline_refs[timeline_ref] = item.memory_uid
            source_facts: list[dict[str, Any]] = []
            atom_contents = {
                self._norm(content)
                for content in item.atom_contents
                if self._norm(content)
            }
            attribution_by_fact = {
                self._norm(content): item.key_fact_attributions[fact_index]
                for fact_index, content in enumerate(item.key_facts)
                if self._norm(content)
                and fact_index < len(item.key_fact_attributions)
                and isinstance(item.key_fact_attributions[fact_index], dict)
            }
            for atom_index, (content, fingerprint) in enumerate(
                zip(item.atom_contents, item.atom_fingerprints, strict=False),
                1,
            ):
                content = str(content or "").strip()
                fingerprint = str(fingerprint or "").strip()
                if not content or not fingerprint:
                    continue
                source_ref = f"{timeline_ref}.A{atom_index}"
                temporal = normalize_fact_temporal(
                    item.atom_temporal[atom_index - 1]
                    if atom_index - 1 < len(item.atom_temporal)
                    else {},
                    fallback_started_at=item.started_at,
                    fallback_ended_at=item.ended_at,
                )
                attribution = attribution_by_fact.get(self._norm(content), {})
                source_facts.append(
                    {
                        "ref": source_ref,
                        "kind": "atom",
                        "content": content,
                        "temporal": temporal,
                        "attribution": attribution,
                    }
                )
                source_refs[source_ref] = {
                    "timeline_uid": item.memory_uid,
                    "fingerprint": fingerprint,
                    "source_key": f"{item.memory_uid}:atom:{fingerprint}",
                    "temporal": temporal,
                    "attribution": attribution,
                }
            key_index = 0
            for fact_index, content in enumerate(item.key_facts):
                content = str(content or "").strip()
                if not content or self._norm(content) in atom_contents:
                    continue
                key_index += 1
                source_ref = f"{timeline_ref}.K{key_index}"
                temporal = normalize_fact_temporal(
                    item.key_fact_temporal[fact_index]
                    if fact_index < len(item.key_fact_temporal)
                    else {},
                    fallback_started_at=item.started_at,
                    fallback_ended_at=item.ended_at,
                )
                attribution = (
                    item.key_fact_attributions[fact_index]
                    if fact_index < len(item.key_fact_attributions)
                    and isinstance(item.key_fact_attributions[fact_index], dict)
                    else {}
                )
                source_facts.append(
                    {
                        "ref": source_ref,
                        "kind": "key_fact",
                        "content": content,
                        "temporal": temporal,
                        "attribution": attribution,
                    }
                )
                source_refs[source_ref] = {
                    "timeline_uid": item.memory_uid,
                    "fingerprint": None,
                    "source_key": (
                        f"{item.memory_uid}:key_fact:"
                        + hashlib.sha256(
                            self._norm(content).encode("utf-8")
                        ).hexdigest()
                    ),
                    "temporal": temporal,
                    "attribution": attribution,
                }
            timelines.append(
                {
                    "ref": timeline_ref,
                    "narrator_actor_id": (
                        item.role_bindings.get("narrator_actor_id")
                        or f"assistant-persona:{item.persona_id or 'default'}"
                    ),
                    "summary": item.summary,
                    "topics": item.topics,
                    "source_facts": source_facts,
                    "started_at": item.started_at,
                    "ended_at": item.ended_at,
                    "ambiguity_flags": item.features.get("ambiguity_flags", []),
                    "evidence_status": item.features.get(
                        "evidence_status", "not_needed"
                    ),
                    "raw_evidence": item.features.get("raw_evidence", []),
                }
            )
        prompt_roles = self._conversation_role_payload(inputs)
        narrators_by_uid = prompt_roles.get("timeline_narrators", {})
        prompt_roles["timeline_narrators"] = {
            ref: narrators_by_uid.get(timeline_uid, f"assistant-persona:default")
            for ref, timeline_uid in timeline_refs.items()
        }
        actor_refs: dict[str, dict[str, Any]] = {}
        actor_payload: list[dict[str, Any]] = []
        seen_actor_ids: set[str] = set()
        for bucket in ("human_participants", "assistant_personas"):
            for actor in prompt_roles.get(bucket, []):
                if not isinstance(actor, dict):
                    continue
                actor_id = str(actor.get("actor_id") or "").strip()
                if not actor_id or actor_id in seen_actor_ids:
                    continue
                seen_actor_ids.add(actor_id)
                actor_ref = f"A{len(actor_payload) + 1}"
                normalized = {
                    **actor,
                    "ref": actor_ref,
                    "actor_id": actor_id,
                }
                actor_refs[actor_ref] = normalized
                actor_payload.append(normalized)
        actor_ref_by_id = {
            str(actor.get("actor_id") or ""): actor_ref
            for actor_ref, actor in actor_refs.items()
            if str(actor.get("actor_id") or "")
        }
        inputs_by_uid = {item.memory_uid: item for item in inputs}
        for timeline in timelines:
            timeline_uid = timeline_refs[str(timeline["ref"])]
            item = inputs_by_uid[timeline_uid]
            for source_fact in timeline["source_facts"]:
                source_fact["attribution"] = self._localize_source_attribution(
                    source_fact.get("attribution"),
                    item=item,
                    actor_ref_by_id=actor_ref_by_id,
                )
                source_refs[str(source_fact["ref"])]["attribution"] = dict(
                    source_fact["attribution"]
                )
        return (
            {
                "supplemental_identity_hints": self._candidate_identity_payload(inputs),
                "conversation_roles": prompt_roles,
                "actor_refs": actor_payload,
                "timelines": timelines,
            },
            timeline_refs,
            source_refs,
            actor_refs,
        )

    def _localize_source_attribution(
        self,
        attribution: Any,
        *,
        item: TimelineTopicCandidate,
        actor_ref_by_id: dict[str, str],
    ) -> dict[str, Any]:
        """Rebind Timeline-local actor refs to this fragment prompt scope."""
        if not isinstance(attribution, dict):
            return {}
        localized = {
            key: value for key, value in attribution.items() if key != "subject_refs"
        }
        localized_subjects: list[dict[str, Any]] = []
        for raw_subject in attribution.get("subject_refs", []):
            if not isinstance(raw_subject, dict):
                continue
            raw_ref = str(raw_subject.get("actor_ref") or "").strip()
            actor_id = str(raw_subject.get("actor_id") or "").strip()
            normalized_actor_id = self._normalize_source_actor_id(
                item,
                actor_id=actor_id,
                actor_ref=raw_ref,
            )
            local_ref = actor_ref_by_id.get(normalized_actor_id, "")
            if raw_ref in {"none", "unresolved"} and not local_ref:
                local_ref = raw_ref
            if not local_ref:
                local_ref = "unresolved"
            subject = {
                "actor_ref": local_ref,
                "display_name_snapshot": str(
                    raw_subject.get("display_name_snapshot") or ""
                ).strip(),
            }
            if normalized_actor_id and local_ref not in {"none", "unresolved"}:
                subject["actor_id"] = normalized_actor_id
            localized_subjects.append(subject)
        localized["subject_refs"] = localized_subjects
        localized["actor_ref_scope"] = "fragment_prompt"
        return localized

    @classmethod
    def _normalize_source_actor_id(
        cls,
        item: TimelineTopicCandidate,
        *,
        actor_id: str,
        actor_ref: str,
    ) -> str:
        """Resolve a stored Timeline actor into the stable Topic actor namespace."""
        bindings = item.role_bindings if isinstance(item.role_bindings, dict) else {}
        if not actor_id and actor_ref in {"", "none", "unresolved"}:
            return ""
        source_actor_id = actor_id
        if not source_actor_id and actor_ref.startswith("A"):
            try:
                actor_index = int(actor_ref[1:]) - 1
            except ValueError:
                actor_index = -1
            actors = [
                actor for actor in bindings.get("actors", []) if isinstance(actor, dict)
            ]
            if 0 <= actor_index < len(actors):
                source_actor_id = str(actors[actor_index].get("actor_id") or "")

        private_scope = cls._private_session_scope(item.session_id)
        session_platform = private_scope.split("\0", 1)[0] if private_scope else ""
        narrator_actor_id = str(bindings.get("narrator_actor_id") or "")
        for actor in bindings.get("actors", []):
            if not isinstance(actor, dict):
                continue
            bound_actor_id = str(actor.get("actor_id") or "").strip()
            if source_actor_id not in {bound_actor_id, ""}:
                continue
            actor_type = str(actor.get("actor_type") or "human")
            sender_id = str(actor.get("sender_id") or "").strip()
            if actor_type == "assistant" and item.persona_id:
                return f"assistant-persona:{item.persona_id}"
            if sender_id:
                platform = canonical_platform(actor.get("platform")) or session_platform
                return stable_actor_id(platform, sender_id, actor_type)
            if bound_actor_id:
                return bound_actor_id
        if source_actor_id and source_actor_id == narrator_actor_id and item.persona_id:
            return f"assistant-persona:{item.persona_id}"
        return source_actor_id

    def _decode_fragment_refs(
        self,
        parsed: dict[str, Any],
        timeline_refs: dict[str, str],
        source_refs: dict[str, dict[str, Any]],
        actor_refs: dict[str, dict[str, Any]],
        *,
        require_source_accounting: bool = False,
    ) -> dict[str, Any]:
        """Resolve model-facing refs into the existing internal provenance schema."""
        raw_fragments = parsed.get("fragments")
        if not isinstance(raw_fragments, list) or not raw_fragments:
            raise TopicBuildValidationError("fragments must be a non-empty array")
        decoded: list[dict[str, Any]] = []
        cited_source_refs: set[str] = set()
        for fragment_index, raw in enumerate(raw_fragments):
            if not isinstance(raw, dict):
                raise TopicBuildValidationError(
                    f"fragment {fragment_index} must be an object"
                )
            declared_refs = self._unique_strings(raw.get("timeline_refs"))
            unknown_timelines = [
                ref for ref in declared_refs if ref not in timeline_refs
            ]
            if not declared_refs or unknown_timelines:
                raise TopicBuildValidationError(
                    f"fragment {fragment_index} has invalid timeline refs: "
                    f"{unknown_timelines or declared_refs}"
                )
            timeline_uids = [timeline_refs[ref] for ref in declared_refs]
            raw_facts = raw.get("facts")
            if not isinstance(raw_facts, list):
                raise TopicBuildValidationError(
                    f"fragment {fragment_index} facts must be an array"
                )
            facts: list[dict[str, Any]] = []
            for fact_index, fact in enumerate(raw_facts):
                if not isinstance(fact, dict):
                    raise TopicBuildValidationError(
                        f"fragment {fragment_index} fact {fact_index} must be an object"
                    )
                cited_refs = self._unique_strings(fact.get("source_refs"))
                unknown_sources = [ref for ref in cited_refs if ref not in source_refs]
                if not cited_refs or unknown_sources:
                    raise TopicBuildValidationError(
                        f"fragment {fragment_index} fact {fact_index} has invalid "
                        f"source refs: {unknown_sources or cited_refs}"
                    )
                cited_source_refs.update(cited_refs)
                fact_timeline_uids = list(
                    dict.fromkeys(
                        str(source_refs[ref]["timeline_uid"]) for ref in cited_refs
                    )
                )
                outside = [
                    uid for uid in fact_timeline_uids if uid not in timeline_uids
                ]
                if outside:
                    raise TopicBuildValidationError(
                        f"fragment {fragment_index} fact {fact_index} cites a source "
                        "outside fragment.timeline_refs"
                    )
                fingerprints = list(
                    dict.fromkeys(
                        str(source_refs[ref]["fingerprint"])
                        for ref in cited_refs
                        if source_refs[ref].get("fingerprint")
                    )
                )
                temporal = aggregate_fact_temporal(
                    source_refs[ref].get("temporal", {}) for ref in cited_refs
                )
                facts.append(
                    {
                        **fact,
                        **temporal,
                        "source_timeline_uids": fact_timeline_uids,
                        "source_atom_fingerprints": fingerprints,
                        "source_fact_keys": sorted(
                            {
                                str(source_refs[ref].get("source_key") or "")
                                for ref in cited_refs
                                if source_refs[ref].get("source_key")
                            }
                        ),
                        "actor_refs": self._decode_actor_relations(
                            fact.get("actor_refs"),
                            actor_refs,
                            scope=f"fragment {fragment_index} fact {fact_index}",
                        ),
                    }
                )
            raw_affect_events = raw.get("affect_events", [])
            if not isinstance(raw_affect_events, list):
                raise TopicBuildValidationError(
                    f"fragment {fragment_index} affect_events must be an array"
                )
            affect_events: list[dict[str, Any]] = []
            for event_index, event in enumerate(raw_affect_events):
                if not isinstance(event, dict):
                    raise TopicBuildValidationError(
                        f"fragment {fragment_index} affect event {event_index} "
                        "must be an object"
                    )
                event_source_refs = self._unique_strings(event.get("source_refs"))
                unknown_event_sources = [
                    ref for ref in event_source_refs if ref not in source_refs
                ]
                if not event_source_refs or unknown_event_sources:
                    raise TopicBuildValidationError(
                        f"fragment {fragment_index} affect event {event_index} has "
                        f"invalid source refs: {unknown_event_sources or event_source_refs}"
                    )
                event_timeline_uids = list(
                    dict.fromkeys(
                        str(source_refs[ref]["timeline_uid"])
                        for ref in event_source_refs
                    )
                )
                if any(uid not in timeline_uids for uid in event_timeline_uids):
                    raise TopicBuildValidationError(
                        f"fragment {fragment_index} affect event {event_index} "
                        "cites a source outside fragment.timeline_refs"
                    )
                actor_ref = str(event.get("actor_ref") or "").strip()
                if actor_ref == "unresolved":
                    actor_id = "unresolved"
                    actor_name = str(event.get("display_name_snapshot") or "").strip()
                elif actor_ref in actor_refs:
                    actor_id = str(actor_refs[actor_ref].get("actor_id") or "")
                    actor_name = str(
                        event.get("display_name_snapshot")
                        or actor_refs[actor_ref].get("display_name")
                        or actor_refs[actor_ref].get("sender_name")
                        or ""
                    ).strip()
                else:
                    raise TopicBuildValidationError(
                        f"fragment {fragment_index} affect event {event_index} "
                        f"has unknown actor_ref {actor_ref or '<empty>'}"
                    )
                affect_events.append(
                    {
                        **event,
                        "actor_id": actor_id or "unresolved",
                        "display_name_snapshot": actor_name,
                        "source_timeline_uids": event_timeline_uids,
                        "source_atom_fingerprints": list(
                            dict.fromkeys(
                                str(source_refs[ref]["fingerprint"])
                                for ref in event_source_refs
                                if source_refs[ref].get("fingerprint")
                            )
                        ),
                        "source_fact_keys": sorted(
                            {
                                str(source_refs[ref].get("source_key") or "")
                                for ref in event_source_refs
                                if source_refs[ref].get("source_key")
                            }
                        ),
                    }
                )
            decoded.append(
                {
                    **raw,
                    "timeline_uids": timeline_uids,
                    "facts": facts,
                    "affect_events": affect_events,
                }
            )

        raw_omissions = parsed.get("omitted_source_refs")
        if raw_omissions is None:
            if require_source_accounting:
                raise TopicBuildValidationError(
                    "omitted_source_refs is required for source accounting"
                )
            raw_omissions = []
        if not isinstance(raw_omissions, list):
            raise TopicBuildValidationError("omitted_source_refs must be an array")
        decoded_omissions: list[dict[str, Any]] = []
        omitted_refs: set[str] = set()
        replacement_reasons = {"duplicate", "superseded"}
        allowed_reasons = {"duplicate", "superseded", "non_durable", "invalid_source"}
        for index, omission in enumerate(raw_omissions):
            if not isinstance(omission, dict):
                raise TopicBuildValidationError(
                    f"omitted_source_refs item {index} must be an object"
                )
            source_ref = str(omission.get("source_ref") or "").strip()
            reason = str(omission.get("reason") or "").strip()
            detail = str(omission.get("detail") or "").strip()
            replacement_ref = str(omission.get("replacement_ref") or "").strip()
            if source_ref not in source_refs or source_ref in omitted_refs:
                raise TopicBuildValidationError(
                    f"omitted_source_refs item {index} has invalid source_ref "
                    f"{source_ref or '<empty>'}"
                )
            if reason not in allowed_reasons or not detail:
                raise TopicBuildValidationError(
                    f"omitted_source_refs item {index} needs a valid reason and detail"
                )
            if reason in replacement_reasons and not replacement_ref:
                raise TopicBuildValidationError(
                    f"omitted_source_refs item {index} reason {reason} requires "
                    "replacement_ref"
                )
            if replacement_ref and replacement_ref not in source_refs:
                raise TopicBuildValidationError(
                    f"omitted_source_refs item {index} has unknown replacement_ref "
                    f"{replacement_ref}"
                )
            omitted_refs.add(source_ref)
            source = source_refs[source_ref]
            decoded_omissions.append(
                {
                    "source_ref": source_ref,
                    "source_timeline_uid": str(source["timeline_uid"]),
                    "source_atom_fingerprint": source.get("fingerprint"),
                    "reason": reason,
                    "detail": detail,
                    "replacement_ref": replacement_ref or None,
                }
            )
        overlap = sorted(cited_source_refs & omitted_refs)
        if overlap:
            raise TopicBuildValidationError(
                "source refs cannot be both cited and omitted: " + ", ".join(overlap)
            )
        invalid_replacements = sorted(
            {
                str(item["replacement_ref"])
                for item in decoded_omissions
                if item.get("replacement_ref")
                and item["replacement_ref"] not in cited_source_refs
            }
        )
        if invalid_replacements:
            raise TopicBuildValidationError(
                "omission replacement refs must be cited by retained facts: "
                + ", ".join(invalid_replacements)
            )
        unaccounted = sorted(set(source_refs) - cited_source_refs - omitted_refs)
        if require_source_accounting and unaccounted:
            raise TopicBuildValidationError(
                "source facts were neither retained nor explicitly omitted: "
                + ", ".join(unaccounted)
            )
        return {
            "fragments": decoded,
            "omitted_source_refs": decoded_omissions,
            "source_accounting_complete": not unaccounted,
        }

    @classmethod
    def _decode_actor_relations(
        cls,
        values: Any,
        actor_refs: dict[str, dict[str, Any]],
        *,
        scope: str,
    ) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values, list):
            raise TopicBuildValidationError(f"{scope} must be an array")
        allowed_roles = {
            "speaker",
            "narrator",
            "responder",
            "subject",
            "mentioned",
            "executor",
            "requester",
        }
        decoded: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise TopicBuildValidationError(
                    f"{scope} item {index} must be an object"
                )
            actor_ref = str(value.get("actor_ref") or "").strip()
            relation_type = cls._normalize_actor_relation(value.get("relation_type"))
            unresolved_label = str(
                value.get("display_name_snapshot") or value.get("label") or ""
            ).strip()
            if actor_ref == "unresolved" and not unresolved_label:
                raise TopicBuildValidationError(
                    f"{scope} item {index} needs a local unresolved label"
                )
            if actor_ref != "unresolved" and actor_ref not in actor_refs:
                raise TopicBuildValidationError(
                    f"{scope} item {index} references unknown actor {actor_ref}"
                )
            if relation_type not in allowed_roles:
                raise TopicBuildValidationError(
                    f"{scope} item {index} has invalid relation_type {relation_type}"
                )
            actor = (
                actor_refs[actor_ref]
                if actor_ref != "unresolved"
                else {
                    "actor_id": "unresolved-pending:"
                    + hashlib.sha256(unresolved_label.encode("utf-8")).hexdigest()[:16],
                    "actor_type": "unknown",
                    "observed_names": [unresolved_label],
                    "resolution_status": "unresolved",
                }
            )
            key = (str(actor["actor_id"]), relation_type)
            if key in seen:
                continue
            seen.add(key)
            decoded.append(
                {
                    "actor_id": str(actor["actor_id"]),
                    "actor_type": str(actor.get("actor_type") or "unknown"),
                    "relation_type": relation_type,
                    "display_name_snapshot": next(
                        (
                            str(name).strip()
                            for name in actor.get("observed_names", [])
                            if str(name).strip()
                        ),
                        None,
                    ),
                    "confidence": cls._score(value.get("confidence"), 0.7),
                    "resolution_status": str(
                        actor.get("resolution_status") or "inferred"
                    ),
                    "actor_ref": actor_ref,
                }
            )
        return decoded

    @staticmethod
    def _normalize_actor_relation(value: Any) -> str:
        relation = str(value or "").strip().casefold().replace("-", "_")
        return _ACTOR_RELATION_ALIASES.get(relation, relation)

    @staticmethod
    def _dedupe_actor_relations(
        values: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for value in values:
            key = (
                str(value.get("actor_id") or ""),
                str(value.get("relation_type") or ""),
            )
            if not all(key) or key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @staticmethod
    def _valid_actor_relation_for_type(value: dict[str, Any]) -> bool:
        """Reject impossible stable participation roles while preserving semantics."""
        relation = str(value.get("relation_type") or "").strip()
        actor_type = str(value.get("actor_type") or "unknown").strip()
        if relation in {"narrator", "responder"}:
            return actor_type == "assistant"
        if relation == "speaker":
            return actor_type != "assistant"
        return True

    def _synthesis_llm_context(
        self, fragments: list[TopicFragmentDraft]
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
        """Strip nested provenance and expose only semantic fields plus local refs."""
        payload: list[dict[str, Any]] = []
        fact_refs: dict[str, str] = {}
        actors_by_id: dict[str, dict[str, Any]] = {}
        for fragment in fragments:
            for key in ("participant_refs", "mentioned_actor_refs"):
                for actor in fragment.metadata.get(key, []):
                    if not isinstance(actor, dict):
                        continue
                    actor_id = str(actor.get("actor_id") or "").strip()
                    if actor_id:
                        actors_by_id.setdefault(actor_id, dict(actor))
            for fact in fragment.facts:
                for actor in fact.get("actor_refs", []):
                    if not isinstance(actor, dict):
                        continue
                    actor_id = str(actor.get("actor_id") or "").strip()
                    if actor_id:
                        actors_by_id.setdefault(actor_id, dict(actor))
        actor_refs: dict[str, dict[str, Any]] = {}
        actor_id_to_ref: dict[str, str] = {}
        actor_payload: list[dict[str, Any]] = []
        for actor_id, actor in actors_by_id.items():
            actor_ref = f"A{len(actor_payload) + 1}"
            actor_id_to_ref[actor_id] = actor_ref
            normalized = {
                key: value
                for key, value in actor.items()
                if key
                not in {
                    "ref",
                    "actor_ref",
                    "relation_type",
                    "confidence",
                    "source",
                    "source_fact_uids",
                    "fragment_uids",
                    "timeline_uids",
                    "atom_uids",
                }
            }
            normalized.update({"actor_ref": actor_ref, "actor_id": actor_id})
            actor_refs[actor_ref] = normalized
            actor_payload.append(normalized)
        next_fact = 1
        for fragment_index, fragment in enumerate(fragments, 1):
            facts: list[dict[str, Any]] = []
            for fact in fragment.facts:
                fact_uid = str(fact.get("fact_uid") or "").strip()
                content = str(fact.get("content") or "").strip()
                if not fact_uid or not content:
                    continue
                fact_ref = f"F{next_fact}"
                next_fact += 1
                fact_refs[fact_ref] = fact_uid
                facts.append(
                    {
                        "ref": fact_ref,
                        "type": str(fact.get("type") or "factual"),
                        "content": content,
                        "importance": self._score(
                            fact.get("importance"), fragment.importance
                        ),
                        "confidence": self._score(
                            fact.get("confidence"), fragment.confidence
                        ),
                        "actor_refs": [
                            {
                                "actor_ref": actor_id_to_ref[actor_id],
                                "relation_type": str(
                                    actor.get("relation_type") or "mentioned"
                                ),
                                "confidence": self._score(actor.get("confidence"), 0.7),
                            }
                            for actor in fact.get("actor_refs", [])
                            if isinstance(actor, dict)
                            and (actor_id := str(actor.get("actor_id") or "").strip())
                            in actor_id_to_ref
                        ],
                    }
                )
            payload.append(
                {
                    "ref": f"P{fragment_index}",
                    "label": fragment.label,
                    "summary": fragment.summary,
                    "facts": facts,
                    "importance": fragment.importance,
                    "confidence": fragment.confidence,
                }
            )
        prompt_roles = self._fragment_role_payload(fragments)
        prompt_roles.pop("timeline_narrators", None)
        return (
            {
                "supplemental_identity_hints": self._fragment_identity_payload(
                    fragments
                ),
                "conversation_roles": prompt_roles,
                "actor_refs": actor_payload,
                "fragments": payload,
            },
            fact_refs,
            actor_refs,
        )

    def _candidate_identity_payload(
        self, inputs: list[TimelineTopicCandidate]
    ) -> list[dict[str, Any]]:
        actor_ids = {
            str(actor.get("actor_id") or "").strip()
            for actor in self._conversation_role_payload(inputs).get(
                "human_participants", []
            )
            if isinstance(actor, dict) and str(actor.get("actor_id") or "").strip()
        }
        matched = self._profiles_for_actor_ids(actor_ids)
        return identity_prompt_payload(matched)

    def _fragment_identity_payload(
        self, fragments: list[TopicFragmentDraft]
    ) -> list[dict[str, Any]]:
        actor_ids = {
            str(actor.get("actor_id") or "").strip()
            for fragment in fragments
            for actor in (
                *fragment.metadata.get("participant_refs", []),
                *fragment.metadata.get("mentioned_actor_refs", []),
                *(
                    actor
                    for fact in fragment.facts
                    if isinstance(fact, dict)
                    for actor in fact.get("actor_refs", [])
                ),
            )
            if isinstance(actor, dict) and str(actor.get("actor_id") or "").strip()
        }
        matched = self._profiles_for_actor_ids(actor_ids)
        return identity_prompt_payload(matched)

    def _conversation_role_payload(
        self, inputs: list[TimelineTopicCandidate]
    ) -> dict[str, Any]:
        """Carry stable Timeline actors into fragment construction."""
        humans: list[dict[str, Any]] = []
        assistants: list[dict[str, Any]] = []
        timeline_narrators: dict[str, str] = {}
        private_identity_context = self._private_session_identity_context(inputs)

        def append_unique(target: list[dict[str, Any]], value: dict[str, Any]) -> None:
            actor_id = str(value.get("actor_id") or "")
            if actor_id and any(item.get("actor_id") == actor_id for item in target):
                existing = next(
                    item for item in target if item.get("actor_id") == actor_id
                )
                for name in value.get("observed_names", []):
                    if name not in existing.setdefault("observed_names", []):
                        existing["observed_names"].append(name)
                for source in value.get("resolution_sources", []):
                    if source not in existing.setdefault("resolution_sources", []):
                        existing["resolution_sources"].append(source)
                existing["identity_confidence"] = max(
                    float(existing.get("identity_confidence", 0.0)),
                    float(value.get("identity_confidence", 0.0)),
                )
                existing["resolution_status"] = self._actor_resolution_status(
                    float(existing["identity_confidence"])
                )
                if value.get("supplemental_identity_hint"):
                    existing["supplemental_identity_hint"] = value[
                        "supplemental_identity_hint"
                    ]
                return
            target.append(value)

        for item in inputs:
            bindings = (
                item.role_bindings if isinstance(item.role_bindings, dict) else {}
            )
            narrator = str(bindings.get("narrator_actor_id") or "").strip()
            normalized_actor_ids: dict[str, str] = {}
            evidence_status = str(item.features.get("evidence_status", "not_needed"))
            if evidence_status in {"attached", "identity_backfilled"}:
                binding_source = "raw_message_span"
                binding_confidence = 1.0
            else:
                binding_source = "timeline_role_bindings"
                binding_confidence = 0.95
            for actor in bindings.get("actors", []):
                if not isinstance(actor, dict):
                    continue
                normalized = {
                    key: actor[key]
                    for key in (
                        "actor_id",
                        "actor_type",
                        "platform",
                        "sender_id",
                        "observed_names",
                        "persona_id",
                        "persona_name",
                        "synthetic_narrator",
                    )
                    if key in actor
                }
                actor_type = str(actor.get("actor_type") or "human")
                sender_id = str(actor.get("sender_id") or "").strip()
                private_scope = self._private_session_scope(item.session_id)
                session_platform = (
                    private_scope.split("\0", 1)[0] if private_scope else ""
                )
                platform = canonical_platform(actor.get("platform")) or session_platform
                if actor_type == "assistant" and item.persona_id:
                    normalized_actor_id = f"assistant-persona:{item.persona_id}"
                elif sender_id:
                    normalized_actor_id = stable_actor_id(
                        platform,
                        sender_id,
                        actor_type,
                    )
                else:
                    normalized_actor_id = str(actor.get("actor_id") or "").strip()
                original_actor_id = str(actor.get("actor_id") or "").strip()
                if original_actor_id:
                    normalized_actor_ids[original_actor_id] = normalized_actor_id
                normalized["actor_id"] = normalized_actor_id
                normalized["platform"] = platform or "unknown"
                normalized["resolution_sources"] = [binding_source]
                normalized["identity_confidence"] = binding_confidence
                normalized["resolution_status"] = self._actor_resolution_status(
                    binding_confidence
                )
                if actor_type == "assistant":
                    append_unique(assistants, normalized)
                else:
                    append_unique(humans, normalized)
            narrator = normalized_actor_ids.get(narrator, narrator)
            if not narrator:
                narrator = f"assistant-persona:{item.persona_id or 'default'}"
                append_unique(
                    assistants,
                    {
                        "actor_id": narrator,
                        "actor_type": "assistant",
                        "observed_names": [item.persona_id or "助手"],
                        "persona_id": item.persona_id or "default",
                        "synthetic_narrator": True,
                        "resolution_sources": ["persona_fallback"],
                        "identity_confidence": 0.68,
                        "resolution_status": "inferred",
                    },
                )
            timeline_narrators[item.memory_uid] = narrator

        for private_context in private_identity_context.values():
            if private_context.get("conflict"):
                continue
            append_unique(
                humans,
                {
                    "actor_id": private_context["actor_id"],
                    "actor_type": "human",
                    "platform": private_context["platform"],
                    "sender_id": private_context["peer_id"],
                    "observed_names": list(private_context.get("names", [])),
                    "resolution_sources": list(
                        private_context.get("resolution_sources", [])
                    ),
                    "identity_confidence": float(
                        private_context.get("identity_confidence", 0.82)
                    ),
                    "resolution_status": str(
                        private_context.get("resolution_status") or "session_inferred"
                    ),
                },
            )

        profile_by_actor_id = {
            str(actor.get("actor_id") or ""): profile.to_prompt_dict()
            for actor in humans
            for profile in self._active_identity_profiles()
            if profile.matches_actor_id(actor.get("actor_id"))
        }
        for actor in humans:
            identity = profile_by_actor_id.get(str(actor.get("actor_id") or ""))
            if identity:
                actor["supplemental_identity_hint"] = identity
                for name in (
                    identity.get("display_name"),
                    *(identity.get("aliases") or []),
                ):
                    display_name = str(name or "").strip()
                    if display_name and display_name not in actor.setdefault(
                        "observed_names", []
                    ):
                        actor["observed_names"].append(display_name)
        return {
            "timeline_narration": "first_person_assistant",
            "output_perspective": "preserve_first_person_assistant",
            "human_participants": humans,
            "assistant_personas": assistants,
            "timeline_narrators": timeline_narrators,
        }

    @staticmethod
    def _actor_resolution_status(confidence: float) -> str:
        if confidence >= 0.99:
            return "evidence_confirmed"
        if confidence >= 0.90:
            return "timeline_bound"
        return "inferred"

    @classmethod
    def _calibrated_attribution_confidence(
        cls,
        inputs: list[TimelineTopicCandidate],
        proposed: float,
    ) -> float:
        """Cap model certainty at the strongest available identity evidence."""
        statuses = {
            str(item.features.get("evidence_status", "not_needed")) for item in inputs
        }
        has_complete_bindings = all(
            bool(item.role_bindings.get("actors"))
            and bool(item.role_bindings.get("narrator_actor_id"))
            for item in inputs
        )
        if statuses and statuses <= {"attached", "identity_backfilled"}:
            ceiling = 0.99
        elif has_complete_bindings:
            ceiling = 0.95
        else:
            ceiling = 0.78
        return round(min(max(0.0, float(proposed)), ceiling), 6)

    @classmethod
    def _fragment_role_payload(
        cls, fragments: list[TopicFragmentDraft]
    ) -> dict[str, Any]:
        humans: list[dict[str, Any]] = []
        assistants: list[dict[str, Any]] = []
        timeline_narrators: dict[str, str] = {}
        for fragment in fragments:
            roles = fragment.metadata.get("conversation_roles")
            if not isinstance(roles, dict):
                continue
            for value in roles.get("human_participants", []):
                if isinstance(value, dict) and value not in humans:
                    humans.append(dict(value))
            for value in roles.get("assistant_personas", []):
                if isinstance(value, dict) and value not in assistants:
                    assistants.append(dict(value))
            raw_narrators = roles.get("timeline_narrators", {})
            if isinstance(raw_narrators, dict):
                timeline_narrators.update(
                    {
                        str(key): str(value)
                        for key, value in raw_narrators.items()
                        if str(key) and str(value)
                    }
                )
        return {
            "input_perspective": "first_person_assistant_fragments",
            "output_perspective": "preserve_first_person_assistant",
            "human_participants": humans,
            "assistant_personas": assistants,
            "timeline_narrators": timeline_narrators,
        }

    @classmethod
    def _topic_participant_index(
        cls, fragments: list[TopicFragmentDraft]
    ) -> dict[str, Any]:
        """Build a revision-scoped actor index with fragment/Timeline provenance."""
        indexed: dict[tuple[str, str], dict[str, Any]] = {}
        for fragment in fragments:
            roles = fragment.metadata.get("conversation_roles")
            if not isinstance(roles, dict):
                continue
            narrator_ids = {
                str(value)
                for value in roles.get("timeline_narrators", {}).values()
                if str(value)
            }
            for bucket, actor_type in (
                ("human_participants", "human"),
                ("assistant_personas", "assistant"),
            ):
                for actor in roles.get(bucket, []):
                    if not isinstance(actor, dict):
                        continue
                    actor_id = str(actor.get("actor_id") or "").strip()
                    if not actor_id:
                        continue
                    key = (actor_id, actor_type)
                    entry = indexed.setdefault(
                        key,
                        {
                            "actor_id": actor_id,
                            "actor_type": actor_type,
                            "display_names": [],
                            "roles": [],
                            "fragment_uids": [],
                            "timeline_uids": [],
                            "resolution_status": str(
                                actor.get("resolution_status") or "inferred"
                            ),
                            "confidence": float(actor.get("identity_confidence", 0.68)),
                            "resolution_sources": [],
                        },
                    )
                    actor_confidence = float(actor.get("identity_confidence", 0.68))
                    if actor_confidence > float(entry["confidence"]):
                        entry["confidence"] = actor_confidence
                        entry["resolution_status"] = str(
                            actor.get("resolution_status")
                            or cls._actor_resolution_status(actor_confidence)
                        )
                    for source in actor.get("resolution_sources", []):
                        source = str(source).strip()
                        if source and source not in entry["resolution_sources"]:
                            entry["resolution_sources"].append(source)
                    for name in actor.get("observed_names", []):
                        name = str(name).strip()
                        if name and name not in entry["display_names"]:
                            entry["display_names"].append(name)
                    role = (
                        "narrator"
                        if actor_type == "assistant" and actor_id in narrator_ids
                        else "speaker"
                    )
                    if role not in entry["roles"]:
                        entry["roles"].append(role)
                    if fragment.fragment_uid not in entry["fragment_uids"]:
                        entry["fragment_uids"].append(fragment.fragment_uid)
                    for timeline_uid in fragment.timeline_uids:
                        if timeline_uid not in entry["timeline_uids"]:
                            entry["timeline_uids"].append(timeline_uid)
        return {
            "schema_version": 1,
            "participants": list(indexed.values()),
            "mentioned_actors": [],
        }

    @staticmethod
    def _actor_index_from_links(
        links: list[TopicActorLink],
    ) -> dict[str, Any]:
        indexed: dict[str, dict[str, Any]] = {}
        for link in links:
            entry = indexed.setdefault(
                link.actor_id,
                {
                    "actor_id": link.actor_id,
                    "actor_type": link.actor_type,
                    "display_names": [],
                    "roles": [],
                    "fragment_uids": [],
                    "timeline_uids": [],
                    "resolution_status": link.resolution_status,
                    "confidence": link.confidence,
                },
            )
            if (
                link.display_name_snapshot
                and link.display_name_snapshot not in entry["display_names"]
            ):
                entry["display_names"].append(link.display_name_snapshot)
            if link.relation_type not in entry["roles"]:
                entry["roles"].append(link.relation_type)
            entry["confidence"] = max(float(entry["confidence"]), link.confidence)
            for key in ("fragment_uids", "timeline_uids"):
                for value in link.metadata.get(key, []):
                    if value not in entry[key]:
                        entry[key].append(value)
        participants: list[dict[str, Any]] = []
        mentioned: list[dict[str, Any]] = []
        participant_roles = {"speaker", "narrator", "responder"}
        for entry in indexed.values():
            if participant_roles & set(entry["roles"]):
                participants.append(entry)
            if set(entry["roles"]) - participant_roles:
                mentioned.append(entry)
        return {
            "schema_version": 2,
            "participants": participants,
            "mentioned_actors": mentioned,
        }

    def _validate_role_anchored_fragment(
        self,
        label: str,
        summary: str,
        facts: list[dict[str, Any]],
        inputs: list[TimelineTopicCandidate],
    ) -> None:
        """Require an explicit narrator map without banning first-person memory."""
        roles = self._conversation_role_payload(inputs)
        narrators = roles.get("timeline_narrators", {})
        if any(item.memory_uid not in narrators for item in inputs):
            raise TopicBuildValidationError(
                "fragment is missing a Timeline narrator actor binding"
            )
        texts = [
            label,
            summary,
            *(str(fact.get("content") or "") for fact in facts),
        ]
        exact_human_names = {
            str(name).strip()
            for item in roles["human_participants"]
            for name in item.get("observed_names", [])
            if str(name).strip() not in {"", "用户"}
        }
        if exact_human_names and any(
            re.search(
                r"(?:用户(?!体验|界面|配置|数据|需求|反馈|账户|账号|权限|设置)|"
                r"对方|叙述者)",
                text,
            )
            for text in texts
        ):
            raise TopicBuildValidationError(
                "fragment must use the mapped human display name instead of a "
                "generic role"
            )

    def _repair_unambiguous_generic_human_roles(
        self,
        value: str,
        inputs: list[TimelineTopicCandidate],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Repair generic human labels only when one stable human is in scope."""
        roles = self._conversation_role_payload(inputs)
        humans = {
            str(item.get("actor_id") or ""): item
            for item in roles.get("human_participants", [])
            if str(item.get("actor_id") or "")
        }
        if len(humans) != 1:
            return value, []
        human = next(iter(humans.values()))
        names = self._unique_strings(human.get("observed_names"))
        names = [name for name in names if name not in {"用户", "对方", "叙述者"}]
        if not names:
            return value, []
        replacement = names[0]
        pattern = re.compile(
            r"用户(?!体验|界面|配置|数据|需求|反馈|账户|账号|权限|设置)|对方|叙述者"
        )
        repaired, count = pattern.subn(replacement, value)
        if not count:
            return value, []
        return repaired, [
            {
                "from": "generic_human_role",
                "to": replacement,
                "count": count,
                "actor_id": str(human.get("actor_id") or ""),
            }
        ]

    def _validate_role_anchored_synthesis(
        self,
        synthesis: dict[str, Any],
        fragments: list[TopicFragmentDraft],
    ) -> None:
        """Keep a fully normalized fragment set normalized after Topic synthesis."""
        if not fragments or not all(
            fragment.metadata.get("narrative_schema_version")
            == _NARRATIVE_SCHEMA_VERSION
            for fragment in fragments
        ):
            return
        roles = self._fragment_role_payload(fragments)
        proxy_inputs = [
            TimelineTopicCandidate(
                memory_uid=timeline_uid,
                document_id=0,
                source_revision=1,
                memory_space_id="",
                session_id=None,
                content="",
                summary="",
                persona_id=persona_name,
            )
            for timeline_uid, persona_name in roles.get(
                "timeline_narrators", {}
            ).items()
        ]
        texts = [
            str(synthesis.get("title") or ""),
            str(synthesis.get("summary") or ""),
            *(
                str(atom.get("content") or "")
                for atom in synthesis.get("atoms", [])
                if isinstance(atom, dict)
            ),
        ]
        self._validate_role_anchored_fragment(
            texts[0],
            texts[1],
            [{"content": value} for value in texts[2:]],
            proxy_inputs,
        )
        exact_human_names = {
            str(name).strip()
            for item in roles.get("human_participants", [])
            for name in item.get("observed_names", [])
            if str(name).strip() not in {"", "用户"}
        }
        if exact_human_names and any(
            re.search(
                r"(?:用户(?!体验|界面|配置|数据|需求|反馈|账户|账号|权限|设置)|"
                r"对方|叙述者)",
                text,
            )
            for text in texts
        ):
            raise TopicBuildValidationError(
                "Topic synthesis replaced a mapped human name with a generic role"
            )

    def _active_identity_profiles(self) -> list[SupplementalIdentityProfile]:
        context = self._runtime_context.get()
        if context is not None:
            return list(context.supplemental_identity_profiles)
        return self.identity_profile_store.profiles

    def _profiles_for_actor_ids(
        self, actor_ids: Iterable[Any]
    ) -> list[SupplementalIdentityProfile]:
        stable_ids = {
            str(value or "").strip() for value in actor_ids if str(value or "").strip()
        }
        return [
            profile
            for profile in self._active_identity_profiles()
            if any(profile.matches_actor_id(actor_id) for actor_id in stable_ids)
        ]

    def _decode_synthesis_refs(
        self,
        parsed: dict[str, Any],
        fact_refs: dict[str, str],
        actor_refs: dict[str, dict[str, Any]],
        fragments: list[TopicFragmentDraft],
    ) -> dict[str, Any]:
        fact_owners = {
            str(fact.get("fact_uid")): fragment.fragment_uid
            for fragment in fragments
            for fact in fragment.facts
            if str(fact.get("fact_uid") or "")
        }
        raw_atoms = parsed.get("atoms")
        if not isinstance(raw_atoms, list):
            raise TopicBuildValidationError("atoms must be an array")
        atoms: list[dict[str, Any]] = []
        covered: set[str] = set()
        for atom_index, atom in enumerate(raw_atoms):
            if not isinstance(atom, dict):
                raise TopicBuildValidationError(f"atom {atom_index} must be an object")
            cited_refs = self._unique_strings(atom.get("source_fact_refs"))
            unknown_refs = [ref for ref in cited_refs if ref not in fact_refs]
            if not cited_refs or unknown_refs:
                raise TopicBuildValidationError(
                    f"atom {atom_index} has invalid source fact refs: "
                    f"{unknown_refs or cited_refs}"
                )
            source_fact_uids = list(dict.fromkeys(fact_refs[ref] for ref in cited_refs))
            fragment_uids = sorted(
                {fact_owners[uid] for uid in source_fact_uids if uid in fact_owners}
            )
            covered.update(fragment_uids)
            atoms.append(
                {
                    **atom,
                    "fragment_uids": fragment_uids,
                    "source_fact_uids": source_fact_uids,
                }
            )
        coverable = {
            fragment.fragment_uid
            for fragment in fragments
            if any(str(fact.get("fact_uid") or "") for fact in fragment.facts)
        }
        missing = sorted(coverable - covered)
        if missing:
            raise TopicBuildValidationError(
                "atoms do not cite facts from every fact-bearing fragment: "
                + ", ".join(missing)
            )
        return {
            **parsed,
            "fragment_uids": sorted(fragment.fragment_uid for fragment in fragments),
            "atoms": atoms,
        }

    @staticmethod
    def _candidate_prompt_payload(item: TimelineTopicCandidate) -> dict[str, Any]:
        return {
            "timeline_uid": item.memory_uid,
            "revision": item.source_revision,
            "persona_id": item.persona_id,
            "summary": item.summary,
            "topics": item.topics,
            "key_facts": item.key_facts,
            "key_fact_attributions": item.key_fact_attributions,
            "atoms": [
                {"content": content, "fingerprint": fingerprint}
                for content, fingerprint in zip(
                    item.atom_contents, item.atom_fingerprints, strict=False
                )
            ],
            "started_at": item.started_at,
            "ended_at": item.ended_at,
            "role_bindings": item.role_bindings,
            "source_window": item.source_window,
            "ambiguity_flags": item.features.get("ambiguity_flags", []),
            "evidence_status": item.features.get("evidence_status"),
            "raw_evidence": item.features.get("raw_evidence", []),
        }

    @staticmethod
    def _fragment_synthesis_payload(item: TopicFragmentDraft) -> dict[str, Any]:
        return {
            "fragment_uid": item.fragment_uid,
            "label": item.label,
            "summary": item.summary,
            "facts": item.facts,
            "importance": item.importance,
            "confidence": item.confidence,
        }

    @staticmethod
    def _fragment_embedding_text(item: TopicFragmentDraft) -> str:
        facts = " ".join(str(fact.get("content") or "") for fact in item.facts)
        return f"{item.label}\n{item.summary}\n{facts}"[:12000]

    @staticmethod
    def _topic_embedding_text(topic: TopicMemory) -> str:
        keywords = " ".join(str(value) for value in topic.metadata.get("keywords", []))
        return f"{topic.title}\n{topic.summary}\n{keywords}"[:12000]


__all__ = ["TopicBuildIdentityMixin"]
