"""Embedding artifact signatures for Topic build and recall compatibility."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable


TOPIC_FRAGMENT_EMBEDDING_FORMAT = "topic-fragment-retrieval-v1"
TOPIC_CENTROID_EMBEDDING_FORMAT = "topic-centroid-from-fragments-v1"
TOPIC_DIRECT_EMBEDDING_FORMAT = "topic-direct-retrieval-v1"
SUPPORTED_TOPIC_EMBEDDING_FORMATS = frozenset(
    {TOPIC_CENTROID_EMBEDDING_FORMAT, TOPIC_DIRECT_EMBEDDING_FORMAT}
)


@dataclass(frozen=True, slots=True)
class EmbeddingSignature:
    provider_id: str
    model_id: str
    dimension: int
    input_format_version: str
    generated_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def provider_identity(provider: Any) -> tuple[str, str]:
    """Resolve stable public provider/model identifiers without credentials."""
    if provider is None:
        return "", ""
    config = getattr(provider, "provider_config", {}) or {}

    def config_value(*keys: str) -> str:
        for key in keys:
            value = (
                config.get(key)
                if isinstance(config, dict)
                else getattr(config, key, None)
            )
            if value not in (None, ""):
                return str(value)
        return ""

    provider_id = config_value("id", "provider_id") or str(
        getattr(provider, "provider_id", "") or type(provider).__name__
    )
    model_id = config_value("model", "model_name") or str(
        getattr(provider, "model", "")
        or getattr(provider, "model_name", "")
        or ""
    )
    return provider_id, model_id


def provider_dimension(provider: Any) -> int:
    if provider is None:
        return 0
    getter = getattr(provider, "get_dim", None)
    if callable(getter):
        try:
            return max(0, int(getter() or 0))
        except (TypeError, ValueError, RuntimeError):
            pass
    for name in ("dimension", "dimensions", "embedding_dimension"):
        try:
            value = int(getattr(provider, name, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def make_embedding_signature(
    provider: Any,
    *,
    dimension: int,
    input_format_version: str,
    generated_at: float | None = None,
) -> dict[str, Any]:
    provider_id, model_id = provider_identity(provider)
    return EmbeddingSignature(
        provider_id=provider_id,
        model_id=model_id,
        dimension=max(0, int(dimension)),
        input_format_version=str(input_format_version),
        generated_at=float(generated_at or time.time()),
    ).to_dict()


def signature_mismatch_reason(
    signature: dict[str, Any] | None,
    provider: Any,
    *,
    expected_formats: Iterable[str],
    dimension: int | None = None,
) -> str | None:
    """Return a stable incompatibility reason, or None when compatible."""
    if not isinstance(signature, dict) or not signature:
        return "missing_signature"
    current_provider_id, current_model_id = provider_identity(provider)
    stored_provider_id = str(signature.get("provider_id") or "")
    stored_model_id = str(signature.get("model_id") or "")
    if not stored_provider_id or stored_provider_id != current_provider_id:
        return "provider_changed"
    if stored_model_id != current_model_id:
        return "model_changed"
    try:
        stored_dimension = int(signature.get("dimension") or 0)
    except (TypeError, ValueError):
        return "invalid_dimension"
    expected_dimension = int(
        dimension if dimension is not None else provider_dimension(provider)
    )
    if stored_dimension <= 0:
        return "invalid_dimension"
    if expected_dimension > 0 and stored_dimension != expected_dimension:
        return "dimension_changed"
    if str(signature.get("input_format_version") or "") not in {
        str(value) for value in expected_formats
    }:
        return "input_format_changed"
    return None


__all__ = [
    "EmbeddingSignature",
    "SUPPORTED_TOPIC_EMBEDDING_FORMATS",
    "TOPIC_CENTROID_EMBEDDING_FORMAT",
    "TOPIC_DIRECT_EMBEDDING_FORMAT",
    "TOPIC_FRAGMENT_EMBEDDING_FORMAT",
    "make_embedding_signature",
    "provider_dimension",
    "provider_identity",
    "signature_mismatch_reason",
]
