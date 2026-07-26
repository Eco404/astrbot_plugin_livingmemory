"""Stable logical identity for rebuildable formal Topic fragments."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def fragment_semantic_discriminator(
    *,
    label: str,
    summary: str,
    facts: list[dict[str, Any]],
) -> str:
    """Return a deterministic semantic facet used only to resolve collisions."""
    semantic_facts = [
        {
            "type": _normalize_text(fact.get("type")),
            "content": _normalize_text(fact.get("content")),
        }
        for fact in facts
    ]
    payload = json.dumps(
        {
            "label": _normalize_text(label),
            "summary": _normalize_text(summary),
            "facts": sorted(
                semantic_facts,
                key=lambda item: json.dumps(
                    item, ensure_ascii=False, sort_keys=True
                ),
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def logical_fragment_uid(
    *,
    memory_space_id: str,
    timeline_uids: list[str],
    facts: list[dict[str, Any]],
    semantic_discriminator: str = "",
) -> str:
    """Derive identity from provenance, with optional collision disambiguation."""
    fact_sources: list[dict[str, Any]] = []
    for fact in facts:
        source_fact_keys = sorted(
            {str(value) for value in fact.get("source_fact_keys", []) if value}
        )
        fingerprints = sorted(
            {str(value) for value in fact.get("source_atom_fingerprints", []) if value}
        )
        source_timelines = sorted(
            {str(value) for value in fact.get("source_timeline_uids", []) if value}
        )
        if source_fact_keys:
            fact_sources.append({"source_fact_keys": source_fact_keys})
        elif fingerprints:
            fact_sources.append(
                {"atom_fingerprints": fingerprints, "timelines": source_timelines}
            )
        else:
            normalized = _normalize_text(fact.get("content"))
            fact_sources.append(
                {
                    "content_hash": hashlib.sha256(
                        normalized.encode("utf-8")
                    ).hexdigest(),
                    "timelines": source_timelines,
                }
            )
    identity = {
        "space": str(memory_space_id),
        "timelines": sorted({str(value) for value in timeline_uids if value}),
        "facts": sorted(
            fact_sources,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        ),
    }
    if semantic_discriminator:
        identity["semantic_discriminator"] = str(semantic_discriminator)
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"livingmemory:logical-fragment:{payload}")
    )


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


__all__ = ["fragment_semantic_discriminator", "logical_fragment_uid"]
