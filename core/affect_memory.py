"""Source-grounded affect representation and lightweight recall helpers."""

from __future__ import annotations

import math
import re
from typing import Any, Iterable


AFFECT_SCHEMA_VERSION = 1
AFFECT_TAXONOMY = "livingmemory-affect-v1"
AFFECT_CATEGORIES = (
    "joy",
    "sadness",
    "anger",
    "fear",
    "surprise",
    "disgust",
    "trust",
    "anticipation",
    "affection",
    "relief",
    "frustration",
    "concern",
    "loneliness",
    "gratitude",
    "shame",
    "guilt",
)

_CATEGORY_VAD: dict[str, tuple[float, float, float]] = {
    "joy": (0.88, 0.67, 0.72),
    "sadness": (0.16, 0.36, 0.28),
    "anger": (0.12, 0.82, 0.76),
    "fear": (0.10, 0.84, 0.18),
    "surprise": (0.60, 0.82, 0.48),
    "disgust": (0.10, 0.62, 0.62),
    "trust": (0.76, 0.34, 0.67),
    "anticipation": (0.66, 0.61, 0.58),
    "affection": (0.86, 0.46, 0.62),
    "relief": (0.78, 0.24, 0.62),
    "frustration": (0.18, 0.74, 0.38),
    "concern": (0.30, 0.62, 0.32),
    "loneliness": (0.18, 0.30, 0.22),
    "gratitude": (0.88, 0.45, 0.66),
    "shame": (0.13, 0.54, 0.15),
    "guilt": (0.18, 0.49, 0.20),
}

_CATEGORY_CUES: dict[str, tuple[str, ...]] = {
    "joy": ("开心", "高兴", "快乐", "兴奋", "愉快", "happy", "joy"),
    "sadness": ("难过", "伤心", "悲伤", "低落", "想哭", "sad", "upset"),
    "anger": ("生气", "愤怒", "恼火", "火大", "angry", "mad"),
    "fear": ("害怕", "恐惧", "紧张", "不安", "fear", "afraid", "scared"),
    "surprise": ("惊讶", "意外", "震惊", "surprised", "shocked"),
    "disgust": ("恶心", "厌恶", "反感", "disgust"),
    "trust": ("信任", "放心", "可靠", "trust"),
    "anticipation": ("期待", "盼望", "憧憬", "anticipat", "look forward"),
    "affection": ("喜欢", "爱", "在乎", "亲近", "依恋", "affection", "love"),
    "relief": ("松了口气", "安心", "释然", "如释重负", "relief"),
    "frustration": ("烦", "烦躁", "挫败", "无奈", "崩溃", "frustrat"),
    "concern": ("担心", "忧虑", "操心", "关心", "concern", "worry"),
    "loneliness": ("孤独", "孤单", "寂寞", "lonely"),
    "gratitude": ("感谢", "感激", "谢谢", "gratitude", "grateful"),
    "shame": ("羞耻", "丢脸", "羞愧", "shame"),
    "guilt": ("内疚", "愧疚", "自责", "guilt", "guilty"),
}

_AFFECT_INTENT_RE = re.compile(
    r"情绪|感受|心情|态度|感情|关系|当时.{0,4}(?:怎么|怎样|如何)|"
    r"feel(?:ing)?|emotion|mood|relationship",
    re.IGNORECASE,
)


def _score(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def affect_signature(
    *, provider_id: str = "", model_id: str = "", prompt_version: str = ""
) -> dict[str, Any]:
    return {
        "schema_version": AFFECT_SCHEMA_VERSION,
        "taxonomy": AFFECT_TAXONOMY,
        "dimensions": ["valence", "arousal", "dominance"],
        "categories": list(AFFECT_CATEGORIES),
        "provider_id": str(provider_id or ""),
        "model_id": str(model_id or ""),
        "prompt_version": str(prompt_version or ""),
    }


def normalize_affect_event(value: Any) -> dict[str, Any] | None:
    """Normalize one already source-validated event without inventing evidence."""
    if not isinstance(value, dict):
        return None
    description = str(value.get("description") or "").strip()
    emotion = str(value.get("emotion") or "").strip()
    source_timeline_uids = _strings(value.get("source_timeline_uids"))
    if not description or not emotion or not source_timeline_uids:
        return None
    categories: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value.get("categories", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip().lower()
        if label not in AFFECT_CATEGORIES or label in seen:
            continue
        seen.add(label)
        categories.append({"label": label, "score": _score(item.get("score"), 0.0)})
    categories.sort(key=lambda item: item["score"], reverse=True)
    normalized = {
        "event_uid": str(value.get("event_uid") or "").strip(),
        "actor_id": str(value.get("actor_id") or "unresolved").strip()
        or "unresolved",
        "display_name_snapshot": str(
            value.get("display_name_snapshot") or ""
        ).strip(),
        "emotion": emotion,
        "description": description,
        "trigger": str(value.get("trigger") or "").strip(),
        "target": str(value.get("target") or "").strip(),
        "evidence_type": str(value.get("evidence_type") or "contextual").strip(),
        "temporal_status": str(
            value.get("temporal_status") or "historical"
        ).strip(),
        "valence": _score(value.get("valence")),
        "arousal": _score(value.get("arousal")),
        "dominance": _score(value.get("dominance")),
        "intensity": _score(value.get("intensity")),
        "confidence": _score(value.get("confidence"), 0.7),
        "categories": categories[:4],
        "source_timeline_uids": source_timeline_uids,
        "source_atom_fingerprints": _strings(
            value.get("source_atom_fingerprints")
        ),
        "source_fact_keys": _strings(value.get("source_fact_keys")),
    }
    return normalized


def aggregate_affect_profile(
    fragments: Iterable[Any], *, max_events: int = 3
) -> tuple[list[dict[str, Any]], float]:
    """Keep a few grounded prototypes rather than averaging opposite emotions."""
    candidates: list[dict[str, Any]] = []
    for fragment in fragments:
        for raw in getattr(fragment, "affect_events", []) or []:
            event = normalize_affect_event(raw)
            if event is None:
                continue
            event = dict(event)
            event["fragment_uid"] = str(getattr(fragment, "fragment_uid", ""))
            evidence_weight = {
                "explicit": 1.0,
                "behavioral": 0.9,
                "contextual": 0.8,
                "model_inferred": 0.65,
            }.get(event["evidence_type"], 0.65)
            event["support_score"] = round(
                event["confidence"]
                * event["intensity"]
                * evidence_weight,
                6,
            )
            candidates.append(event)
    candidates.sort(key=lambda item: item["support_score"], reverse=True)
    prototypes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in candidates:
        primary_category = str(
            (event.get("categories") or [{}])[0].get("label") or ""
        )
        key = (
            event["actor_id"],
            primary_category or _norm(event["emotion"]),
        )
        if key in seen:
            continue
        seen.add(key)
        prototypes.append(event)

    selected: list[dict[str, Any]] = []
    limit = max(1, int(max_events))
    while prototypes and len(selected) < limit:
        if not selected:
            selected.append(prototypes.pop(0))
            continue
        best_index = max(
            range(len(prototypes)),
            key=lambda index: _prototype_selection_score(
                prototypes[index], selected
            ),
        )
        selected.append(prototypes.pop(best_index))
        if len(selected) >= limit:
            break
    salience = max((item["support_score"] for item in selected), default=0.0)
    return selected, round(salience, 6)


def extract_query_affect(text: str) -> dict[str, Any]:
    """Cheap explicit-cue extraction for the recall hot path; no model call."""
    normalized = str(text or "").strip().lower()
    categories: list[dict[str, Any]] = []
    for label, cues in _CATEGORY_CUES.items():
        hits = sum(1 for cue in cues if cue in normalized)
        if hits:
            categories.append({"label": label, "score": min(1.0, 0.72 + 0.12 * hits)})
    categories.sort(key=lambda item: item["score"], reverse=True)
    needs_affect = bool(categories or _AFFECT_INTENT_RE.search(normalized))
    if not categories:
        return {
            "needs_affect": needs_affect,
            "explicit": False,
            "confidence": 0.0,
            "categories": [],
            "valence": 0.5,
            "arousal": 0.5,
            "dominance": 0.5,
        }
    weight_sum = sum(item["score"] for item in categories)
    vad = [0.0, 0.0, 0.0]
    for item in categories:
        prototype = _CATEGORY_VAD[item["label"]]
        for index in range(3):
            vad[index] += prototype[index] * item["score"] / weight_sum
    return {
        "needs_affect": True,
        "explicit": True,
        "confidence": max(item["score"] for item in categories),
        "categories": categories[:4],
        "valence": vad[0],
        "arousal": vad[1],
        "dominance": vad[2],
    }


def affect_similarity(query: dict[str, Any], events: Iterable[dict[str, Any]]) -> float:
    if not query.get("explicit"):
        return 0.0
    query_categories = {
        str(item.get("label") or ""): _score(item.get("score"), 0.0)
        for item in query.get("categories", [])
        if isinstance(item, dict)
    }
    best = 0.0
    for raw in events:
        event = normalize_affect_event(raw)
        if event is None:
            continue
        event_categories = {
            item["label"]: item["score"] for item in event["categories"]
        }
        category = _cosine_map(query_categories, event_categories)
        distance = math.sqrt(
            sum(
                (float(query[name]) - float(event[name])) ** 2
                for name in ("valence", "arousal", "dominance")
            )
        ) / math.sqrt(3.0)
        vad = max(0.0, 1.0 - distance)
        similarity = (category * 0.65 + vad * 0.35) * event["confidence"]
        best = max(best, similarity)
    return max(0.0, min(1.0, best))


def select_affect_events(
    events: Iterable[dict[str, Any]],
    query: dict[str, Any],
    *,
    limit: int = 1,
    min_confidence: float = 0.65,
) -> list[dict[str, Any]]:
    if not query.get("needs_affect"):
        return []
    ranked: list[tuple[float, dict[str, Any]]] = []
    for raw in events:
        event = normalize_affect_event(raw)
        if event is None or event["confidence"] < min_confidence:
            continue
        match = affect_similarity(query, [event]) if query.get("explicit") else 0.5
        score = match * 0.65 + event["confidence"] * event["intensity"] * 0.35
        ranked.append((score, event))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [event for _, event in ranked[: max(0, int(limit))]]


def format_affect_context(events: Iterable[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for event in events:
        actor = event.get("display_name_snapshot") or event.get("actor_id") or "相关人物"
        status = {
            "historical": "当时",
            "ongoing": "持续",
            "resolved": "后来已缓解",
            "uncertain": "状态不确定",
        }.get(str(event.get("temporal_status") or ""), "当时")
        rendered.append(f"{actor}{status}{event.get('description')}")
    return "；".join(rendered)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _norm(value: Any) -> str:
    return re.sub(r"\W+", "", str(value or "").lower())


def _cosine_map(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0.0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def _prototype_selection_score(
    event: dict[str, Any], selected: list[dict[str, Any]]
) -> float:
    """Balance grounded support with distance from already kept prototypes."""
    distances = []
    for existing in selected:
        distance = math.sqrt(
            sum(
                (float(event[name]) - float(existing[name])) ** 2
                for name in ("valence", "arousal", "dominance")
            )
        ) / math.sqrt(3.0)
        event_labels = {
            str(item.get("label") or "")
            for item in event.get("categories", [])
            if isinstance(item, dict)
        }
        existing_labels = {
            str(item.get("label") or "")
            for item in existing.get("categories", [])
            if isinstance(item, dict)
        }
        if event_labels and existing_labels and event_labels.isdisjoint(existing_labels):
            distance = min(1.0, distance + 0.25)
        distances.append(distance)
    diversity = min(distances, default=1.0)
    return float(event.get("support_score") or 0.0) * 0.7 + diversity * 0.3


__all__ = [
    "AFFECT_CATEGORIES",
    "AFFECT_SCHEMA_VERSION",
    "AFFECT_TAXONOMY",
    "affect_signature",
    "affect_similarity",
    "aggregate_affect_profile",
    "extract_query_affect",
    "format_affect_context",
    "normalize_affect_event",
    "select_affect_events",
]
