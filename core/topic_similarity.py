"""Shared vector and lexical similarity helpers for Topic memory."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence


_TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return a finite cosine score, or zero for incompatible vectors."""
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    score = dot / (left_norm * right_norm)
    return score if math.isfinite(score) else 0.0


def average_vectors(vectors: Iterable[Sequence[float]]) -> list[float]:
    """Average vectors with one shared dimension and ignore invalid entries."""
    valid = [list(map(float, item)) for item in vectors if item]
    if not valid:
        return []
    dimension = len(valid[0])
    valid = [item for item in valid if len(item) == dimension]
    if not valid:
        return []
    return [sum(item[index] for item in valid) / len(valid) for index in range(dimension)]


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def text_features(value: str) -> set[str]:
    """Extract conservative lexical features for Chinese and latin text."""
    normalized = normalize_text(value)
    features = set(_TOKEN_RE.findall(normalized))
    compact = "".join(char for char in normalized if not char.isspace())
    if len(compact) >= 2:
        features.update(compact[index : index + 2] for index in range(len(compact) - 1))
    return {item for item in features if item}


def jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def mmr_score(relevance: float, similarities: Iterable[float], diversity: float) -> float:
    """Return maximal-marginal-relevance with a bounded diversity weight."""
    penalty = max((float(item) for item in similarities), default=0.0)
    weight = min(1.0, max(0.0, float(diversity)))
    return (1.0 - weight) * float(relevance) - weight * penalty


__all__ = [
    "average_vectors",
    "cosine_similarity",
    "jaccard_similarity",
    "mmr_score",
    "normalize_text",
    "text_features",
]
