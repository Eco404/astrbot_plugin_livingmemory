"""Shared vector and lexical similarity helpers for Topic memory."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

_TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)
_WORD_RE = re.compile(r"[\u3400-\u9fff]+|[a-z0-9_]+", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")


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
    return [
        sum(item[index] for item in valid) / len(valid) for index in range(dimension)
    ]


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def canonical_text(value: str) -> str:
    """Normalize text for stable equality and fingerprint comparisons."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def lexical_tokens(value: str) -> set[str]:
    """Return the conservative lexical token contract used by Topic grouping."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    tokens: set[str] = set()
    for chunk in _WORD_RE.findall(normalized):
        if _CJK_RE.fullmatch(chunk):
            if len(chunk) == 1:
                tokens.add(chunk)
            else:
                tokens.update(
                    chunk[index : index + 2] for index in range(len(chunk) - 1)
                )
        elif len(chunk) >= 2:
            tokens.add(chunk)
    return tokens


def retrieval_text_features(value: str) -> set[str]:
    """Extract the stable lexical feature contract used by recall scoring."""
    normalized = str(value or "").casefold()
    features = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", normalized):
        if len(chunk) == 1:
            features.add(chunk)
        else:
            features.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return features or {"<empty>"}


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


def weighted_jaccard_similarity(
    left: set[str],
    right: set[str],
    document_frequency: Mapping[str, int],
    document_count: int,
) -> float:
    """Weight rare shared terms more strongly than corpus-wide terms."""
    union = left | right
    if not union:
        return 0.0

    def weight(token: str) -> float:
        return 1.0 + math.log(
            (max(1, document_count) + 1.0) / (document_frequency.get(token, 0) + 1.0)
        )

    denominator = sum(weight(token) for token in union)
    numerator = sum(weight(token) for token in left & right)
    return numerator / denominator if denominator else 0.0


def mmr_score(
    relevance: float, similarities: Iterable[float], diversity: float
) -> float:
    """Return maximal-marginal-relevance with a bounded diversity weight."""
    penalty = max((float(item) for item in similarities), default=0.0)
    weight = min(1.0, max(0.0, float(diversity)))
    return (1.0 - weight) * float(relevance) - weight * penalty


__all__ = [
    "average_vectors",
    "canonical_text",
    "cosine_similarity",
    "jaccard_similarity",
    "lexical_tokens",
    "mmr_score",
    "normalize_text",
    "retrieval_text_features",
    "text_features",
    "weighted_jaccard_similarity",
]
