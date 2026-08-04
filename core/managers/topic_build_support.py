"""Shared deterministic helpers for Topic construction."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable
from typing import (
    Any,
)

from ..models.topic_memory import TopicFragmentDraft
from ..topic_similarity import (
    average_vectors,
    canonical_text,
    cosine_similarity,
)
from .topic_build_contracts import (
    _CONFIDENCE_CALIBRATION_VERSION,
)


class TopicBuildSupportMixin:
    @staticmethod
    def _checkpoint_hash(payload: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    @staticmethod
    def _score(value: Any, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _repair_audit(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
        normalization_types = {
            "normalized_atom_fragment_provenance",
            "normalized_fact_atom_fingerprints",
            "normalized_synthesis_fragment_scope",
        }
        fallback_types = {
            "fragment_batch_fallback",
            "invalid_synthesis_output",
            "missing_fragment_atom_coverage",
            "missing_timeline_atom_coverage",
            "replaced_invalid_synthesis_atoms_array",
        }
        counts = {"normalization": 0, "repair": 0, "fallback": 0}
        types: Counter[str] = Counter()
        weighted_units = 0.0
        for event in events:
            event_type = str(event.get("type") or "unknown")
            types[event_type] += 1
            if event_type in normalization_types:
                category, weight = "normalization", 0.0
            elif event_type in fallback_types or event_type.startswith("dropped_"):
                category, weight = "fallback", 1.5
            else:
                category, weight = "repair", 0.5
            counts[category] += 1
            weighted_units += weight
        return {
            **counts,
            "weighted_units": round(weighted_units, 3),
            "types": dict(sorted(types.items())),
        }

    @staticmethod
    def _score_distribution(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"count": 0}
        ordered = sorted(float(value) for value in values)

        def percentile(ratio: float) -> float:
            position = (len(ordered) - 1) * ratio
            lower = math.floor(position)
            upper = math.ceil(position)
            if lower == upper:
                return ordered[lower]
            fraction = position - lower
            return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

        return {
            "count": len(ordered),
            "min": round(ordered[0], 6),
            "p10": round(percentile(0.10), 6),
            "median": round(percentile(0.50), 6),
            "p90": round(percentile(0.90), 6),
            "max": round(ordered[-1], 6),
        }

    @staticmethod
    def _calibrate_confidence(
        raw_confidence: float,
        *,
        independent_clusters: int,
        supporting_timelines: int,
    ) -> tuple[float, dict[str, Any]]:
        """Shrink model certainty toward a prior until evidence is independent.

        Extra Timeline memories from one nearby episode add less evidence than the
        same claim recurring in separate time clusters. This avoids treating a long
        conversation as many independent confirmations.
        """
        raw = max(0.0, min(1.0, float(raw_confidence)))
        cluster_count = max(0, int(independent_clusters))
        timeline_count = max(0, int(supporting_timelines))
        evidence_weight = min(
            8.0,
            float(cluster_count) + 0.25 * max(0, timeline_count - cluster_count),
        )
        evidence_weight = max(1.0, evidence_weight)
        prior = 0.60
        prior_weight = 2.0
        calibrated = (prior * prior_weight + raw * evidence_weight) / (
            prior_weight + evidence_weight
        )
        return round(calibrated, 6), {
            "version": _CONFIDENCE_CALIBRATION_VERSION,
            "raw_confidence": round(raw, 6),
            "prior": prior,
            "prior_weight": prior_weight,
            "evidence_weight": round(evidence_weight, 6),
            "independent_cluster_count": cluster_count,
            "supporting_timeline_count": timeline_count,
        }

    @staticmethod
    def _unique_strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(
            dict.fromkeys(str(item).strip() for item in value if str(item).strip())
        )

    @staticmethod
    def _norm(value: Any) -> str:
        return canonical_text(str(value or ""))

    @staticmethod
    def _cosine(left: Any, right: Any) -> float:
        try:
            a, b = [float(item) for item in left], [float(item) for item in right]
        except (TypeError, ValueError):
            return 0.0
        return cosine_similarity(a, b)

    @staticmethod
    def _average_vectors(vectors: list[list[float]]) -> list[float]:
        return average_vectors(vectors)

    @classmethod
    def _timeline_fragment_similarity(
        cls, timeline_uid: str, fragments: list[TopicFragmentDraft]
    ) -> float:
        values = [
            item.confidence for item in fragments if timeline_uid in item.timeline_uids
        ]
        return round(sum(values) / len(values), 6) if values else 0.5

    @classmethod
    def _timeline_importance_contribution_weights(
        cls,
        timeline_uids: list[str],
        fragments: list[TopicFragmentDraft],
    ) -> dict[str, float]:
        """Allocate this Topic's fact mass across its source Timelines."""
        normalized_uids = list(dict.fromkeys(str(uid) for uid in timeline_uids if uid))
        if not normalized_uids:
            return {}
        allowed = set(normalized_uids)
        masses = {uid: 0.0 for uid in normalized_uids}
        seen_facts: set[tuple[str, str]] = set()
        for fragment in fragments:
            fragment_key = str(
                fragment.logical_fragment_uid or fragment.fragment_uid or "fragment"
            )
            for fact in fragment.facts:
                fact_key = str(fact.get("fact_uid") or cls._norm(fact.get("content")))
                dedupe_key = (fragment_key, fact_key)
                if not fact_key or dedupe_key in seen_facts:
                    continue
                seen_facts.add(dedupe_key)
                sources = list(
                    dict.fromkeys(
                        str(uid)
                        for uid in fact.get("source_timeline_uids", [])
                        if str(uid) in allowed
                    )
                )
                if not sources:
                    sources = [uid for uid in fragment.timeline_uids if uid in allowed]
                if not sources:
                    continue
                fact_mass = cls._score(fact.get("importance"), 0.5) * max(
                    0.05,
                    cls._score(fact.get("confidence"), 0.7),
                )
                share = fact_mass / len(sources)
                for uid in sources:
                    masses[uid] += share

        total = sum(masses.values())
        if total <= 0.0:
            for fragment in fragments:
                sources = [uid for uid in fragment.timeline_uids if uid in allowed]
                if not sources:
                    continue
                fragment_mass = cls._score(fragment.importance, 0.5) * max(
                    0.05,
                    cls._score(fragment.confidence, 0.7),
                )
                share = fragment_mass / len(sources)
                for uid in sources:
                    masses[uid] += share
            total = sum(masses.values())
        if total <= 0.0:
            equal = 1.0 / len(normalized_uids)
            return {uid: equal for uid in normalized_uids}
        return {uid: masses[uid] / total for uid in normalized_uids}

    @staticmethod
    async def _gather_cancel_on_error(awaitables: list[Any]) -> list[Any]:
        """Gather in input order and cancel sibling provider calls on failure."""
        if not awaitables:
            return []
        tasks = [asyncio.create_task(awaitable) for awaitable in awaitables]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    @staticmethod
    async def _emit(
        callback,
        run_uid: str,
        stage: str,
        current: int,
        total: int,
        **details: Any,
    ) -> None:
        if callback is None:
            return
        result = callback(
            {
                "run_uid": run_uid,
                "stage": stage,
                "current": current,
                "total": total,
                **details,
            }
        )
        if hasattr(result, "__await__"):
            await result


__all__ = ["TopicBuildSupportMixin"]
