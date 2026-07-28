"""Optional temporal constraints for explicit recall requests."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

TEMPORAL_MODES = {"range", "earliest", "latest"}
TEMPORAL_ORDERS = {"relevance", "earliest", "latest"}


@dataclass(frozen=True, slots=True)
class TemporalConstraint:
    """A caller-supplied time range and result ordering policy."""

    mode: str = "range"
    start_at: float | None = None
    end_at: float | None = None
    order: str = "relevance"

    @classmethod
    def from_payload(cls, value: Any) -> TemporalConstraint | None:
        if value is None or value == "":
            return None
        if not isinstance(value, dict):
            raise ValueError("temporal must be an object")

        mode = str(value.get("mode") or "range").strip().lower()
        if mode not in TEMPORAL_MODES:
            raise ValueError("temporal.mode must be range, earliest, or latest")

        start_at = cls._parse_time(value.get("start"), "temporal.start")
        end_at = cls._parse_time(value.get("end"), "temporal.end")
        if start_at is not None and end_at is not None and start_at > end_at:
            raise ValueError("temporal.start must not be later than temporal.end")
        if mode == "range" and start_at is None and end_at is None:
            raise ValueError("temporal range requires start or end")

        default_order = mode if mode in {"earliest", "latest"} else "relevance"
        order = str(value.get("order") or default_order).strip().lower()
        if order not in TEMPORAL_ORDERS:
            raise ValueError("temporal.order must be relevance, earliest, or latest")
        return cls(mode=mode, start_at=start_at, end_at=end_at, order=order)

    @staticmethod
    def _parse_time(value: Any, field: str) -> float | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError(f"{field} must be an RFC3339 timestamp or epoch seconds")
        if isinstance(value, (int, float)):
            parsed = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                parsed = float(text)
            except ValueError:
                normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
                try:
                    parsed_datetime = datetime.fromisoformat(normalized)
                except ValueError as exc:
                    raise ValueError(
                        f"{field} must be an RFC3339 timestamp or epoch seconds"
                    ) from exc
                if parsed_datetime.tzinfo is None:
                    raise ValueError(f"{field} must include a timezone offset")
                parsed = parsed_datetime.timestamp()
        else:
            raise ValueError(f"{field} must be an RFC3339 timestamp or epoch seconds")
        if not math.isfinite(parsed):
            raise ValueError(f"{field} must be finite")
        return parsed

    @property
    def has_range(self) -> bool:
        return self.start_at is not None or self.end_at is not None

    def overlaps(self, started_at: Any, ended_at: Any) -> bool:
        start = self._finite_or_none(started_at)
        end = self._finite_or_none(ended_at)
        if start is None and end is None:
            return False
        if start is None:
            start = end
        if end is None:
            end = start
        if start is not None and end is not None and end < start:
            start, end = end, start
        if self.start_at is not None and end is not None and end < self.start_at:
            return False
        if self.end_at is not None and start is not None and start > self.end_at:
            return False
        return True

    def sort_value(self, started_at: Any, ended_at: Any) -> float:
        start = self._finite_or_none(started_at)
        end = self._finite_or_none(ended_at)
        if self.order == "latest":
            return end if end is not None else start if start is not None else -math.inf
        return start if start is not None else end if end is not None else math.inf

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "start": self.start_at,
            "end": self.end_at,
            "order": self.order,
        }

    @staticmethod
    def _finite_or_none(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None


def timeline_time_anchor(metadata: Any) -> tuple[float | None, float | None, str, bool]:
    """Resolve a Timeline event range without consulting raw conversations."""
    data = metadata if isinstance(metadata, dict) else {}
    source = data.get("source_window")
    source = source if isinstance(source, dict) else {}
    start = TemporalConstraint._finite_or_none(source.get("started_at"))
    end = TemporalConstraint._finite_or_none(source.get("ended_at"))
    if start is not None or end is not None:
        start = start if start is not None else end
        end = end if end is not None else start
        return start, end, "timeline_source_span", False

    start = TemporalConstraint._finite_or_none(data.get("temporal_started_at"))
    end = TemporalConstraint._finite_or_none(data.get("temporal_ended_at"))
    if start is not None or end is not None:
        start = start if start is not None else end
        end = end if end is not None else start
        basis = str(data.get("temporal_time_basis") or "timeline_source_span")
        return start, end, basis, bool(data.get("temporal_time_fallback", False))

    created = TemporalConstraint._finite_or_none(data.get("create_time"))
    if created is not None:
        return created, created, "timeline_created_at", True
    return None, None, "unavailable", True


def sources_time_anchor(
    sources: list[dict[str, Any]] | None,
) -> tuple[float | None, float | None, str, bool]:
    starts: list[float] = []
    ends: list[float] = []
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        start = TemporalConstraint._finite_or_none(source.get("started_at"))
        end = TemporalConstraint._finite_or_none(source.get("ended_at"))
        if start is not None:
            starts.append(start)
        if end is not None:
            ends.append(end)
        if start is not None and end is None:
            ends.append(start)
        if end is not None and start is None:
            starts.append(end)
    if starts or ends:
        start = min(starts or ends)
        end = max(ends or starts)
        return start, end, "timeline_source_span", False
    return None, None, "unavailable", True


def matching_sources(
    constraint: TemporalConstraint,
    sources: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return the discrete source spans that overlap the requested range."""
    if not constraint.has_range:
        return [item for item in (sources or []) if isinstance(item, dict)]
    return [
        item
        for item in (sources or [])
        if isinstance(item, dict)
        and constraint.overlaps(item.get("started_at"), item.get("ended_at"))
    ]


__all__ = [
    "TEMPORAL_MODES",
    "TEMPORAL_ORDERS",
    "TemporalConstraint",
    "matching_sources",
    "sources_time_anchor",
    "timeline_time_anchor",
]
