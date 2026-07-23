"""Stable logical identity for rebuildable formal Topic fragments."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def logical_fragment_uid(
    *,
    memory_space_id: str,
    timeline_uids: list[str],
    facts: list[dict[str, Any]],
) -> str:
    """Derive identity from provenance, never from a physical build run."""
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
            normalized = " ".join(
                str(fact.get("content") or "").strip().casefold().split()
            )
            fact_sources.append(
                {
                    "content_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                    "timelines": source_timelines,
                }
            )
    payload = json.dumps(
        {
            "space": str(memory_space_id),
            "timelines": sorted({str(value) for value in timeline_uids if value}),
            "facts": sorted(
                fact_sources,
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"livingmemory:logical-fragment:{payload}")
    )


__all__ = ["logical_fragment_uid"]
