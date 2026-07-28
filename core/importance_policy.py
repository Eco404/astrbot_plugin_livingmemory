"""Versioned, deterministic importance aggregation for Timeline-derived Topics."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


IMPORTANCE_POLICY_VERSION = 2


def clamp_score(value: Any, default: float = 0.5) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    return max(0.0, min(1.0, result))


def fragment_semantic_importance(facts: Iterable[Mapping[str, Any]]) -> float:
    """Derive one fragment score from its facts without trusting a second LLM score."""
    values = sorted(
        (
            clamp_score(fact.get("importance"), 0.5)
            for fact in facts
            if str(fact.get("content") or "").strip()
        ),
        reverse=True,
    )
    if not values:
        return 0.5
    primary = values[0]
    supporting = sum(values[:3]) / min(3, len(values))
    return round(clamp_score(0.7 * primary + 0.3 * supporting), 6)


def topic_semantic_importance(fragment_scores: Iterable[tuple[float, float]]) -> float:
    """Aggregate fragment semantics while preventing numerous minor fragments diluting it."""
    rows = [
        (clamp_score(score), max(0.01, clamp_score(confidence, 0.7)))
        for score, confidence in fragment_scores
    ]
    if not rows:
        return 0.5
    highest = max(score for score, _ in rows)
    weighted_mean = sum(score * weight for score, weight in rows) / sum(
        weight for _, weight in rows
    )
    return round(clamp_score(0.6 * highest + 0.4 * weighted_mean), 6)


def evidence_strength(*, cluster_count: int, timeline_count: int) -> float:
    """Describe independent support separately from semantic importance."""
    clusters = max(1, int(cluster_count))
    timelines = max(1, int(timeline_count))
    cluster_support = min(1.0, 0.45 + 0.18 * (clusters - 1))
    source_support = min(1.0, 0.55 + 0.1 * (timelines - 1))
    return round(clamp_score(0.7 * cluster_support + 0.3 * source_support), 6)


def aggregate_source_importance(
    sources: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate Timeline states once per time cluster, with no delta accumulation."""
    grouped: dict[str, list[tuple[float, float, float, str, int]]] = defaultdict(list)
    normalized_rows: list[dict[str, Any]] = []
    for source in sources:
        uid = str(source.get("timeline_uid") or "").strip()
        if not uid:
            continue
        cluster = str(source.get("time_cluster_key") or f"timeline:{uid}")
        base = clamp_score(source.get("base_importance"), 0.5)
        effective = clamp_score(source.get("effective_importance"), base)
        weight = max(0.01, clamp_score(source.get("weight"), 1.0))
        revision = max(1, int(source.get("importance_revision") or 1))
        grouped[cluster].append((base, effective, weight, uid, revision))
        normalized_rows.append(
            {
                "timeline_uid": uid,
                "time_cluster_key": cluster,
                "base_importance": round(base, 6),
                "effective_importance": round(effective, 6),
                "weight": round(weight, 6),
                "importance_revision": revision,
            }
        )
    if not grouped:
        return {
            "source_base_component": 0.5,
            "dynamic_factor": 1.0,
            "source_importance_hash": "",
            "sources": [],
        }

    cluster_values: list[tuple[float, float, float]] = []
    for rows in grouped.values():
        total_weight = sum(row[2] for row in rows)
        cluster_base = sum(row[0] * row[2] for row in rows) / total_weight
        cluster_effective = sum(row[1] * row[2] for row in rows) / total_weight
        # One busy time cluster must not dominate a Topic merely because it was split.
        cluster_weight = max(row[2] for row in rows)
        cluster_values.append((cluster_base, cluster_effective, cluster_weight))

    total_cluster_weight = sum(row[2] for row in cluster_values)
    source_base = sum(row[0] * row[2] for row in cluster_values) / total_cluster_weight
    dynamic = sum(
        (row[1] / max(0.01, row[0])) * row[2] for row in cluster_values
    ) / total_cluster_weight
    dynamic = max(0.05, min(1.0, dynamic))
    normalized_rows.sort(key=lambda row: (row["timeline_uid"], row["time_cluster_key"]))
    digest_payload = [
        {
            "timeline_uid": row["timeline_uid"],
            "importance_revision": row["importance_revision"],
            "base_importance": row["base_importance"],
            "effective_importance": row["effective_importance"],
            "weight": row["weight"],
        }
        for row in normalized_rows
    ]
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "source_base_component": round(clamp_score(source_base), 6),
        "dynamic_factor": round(dynamic, 6),
        "source_importance_hash": digest,
        "sources": normalized_rows,
    }


def topic_base_importance(semantic: float, source_base: float) -> float:
    return round(
        clamp_score(0.8 * clamp_score(semantic) + 0.2 * clamp_score(source_base)),
        6,
    )


def topic_effective_importance(base: float, dynamic_factor: float) -> float:
    return round(clamp_score(clamp_score(base) * float(dynamic_factor)), 6)
