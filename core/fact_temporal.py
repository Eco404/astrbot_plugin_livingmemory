"""Deterministic fact-level temporal anchors and event-time resolution."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

_ABSOLUTE_DATE_RE = re.compile(
    r"(?:20\d{2})\s*(?:[-/.年])\s*\d{1,2}\s*(?:[-/.月])\s*\d{1,2}(?:日|号)?"
)
_RELATIVE_TIME_RE = re.compile(
    r"(?:前天|昨天|今天|明天|后天|大后天|刚才|刚刚|稍后|"
    r"上周|本周|下周|下下周|上个?月|下个?月|去年|明年|"
    r"[一二两三四五六七八九十\d]+\s*(?:分钟|小时|天|周|个月|年)(?:前|后))"
)
_CHINESE_NUMBER = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_LOCAL_TIMEZONE = datetime.now(timezone.utc).astimezone().tzinfo


def optional_timestamp(value: Any) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return timestamp if timestamp > 0 else None


def contains_relative_time(text: str) -> bool:
    return bool(_RELATIVE_TIME_RE.search(str(text or "")))


def contains_absolute_date(text: str) -> bool:
    return bool(_ABSOLUTE_DATE_RE.search(str(text or "")))


def _number(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if value in _CHINESE_NUMBER:
        return _CHINESE_NUMBER[value]
    if value.startswith("十") and len(value) == 2:
        tail = _CHINESE_NUMBER.get(value[1])
        return 10 + tail if tail is not None else None
    if value.endswith("十") and len(value) == 2:
        head = _CHINESE_NUMBER.get(value[0])
        return head * 10 if head is not None else None
    if "十" in value and len(value) == 3:
        head = _CHINESE_NUMBER.get(value[0])
        tail = _CHINESE_NUMBER.get(value[2])
        if head is not None and tail is not None:
            return head * 10 + tail
    return None


def _apply_time_of_day(value: datetime, text: str) -> datetime:
    match = re.search(
        r"(?:(上午|早上|中午|下午|晚上|凌晨)\s*)?"
        r"([一二两三四五六七八九十\d]+)\s*"
        r"(?:[:：点时]\s*([一二两三四五六七八九十\d]+)?\s*分?)",
        str(text or ""),
    )
    if not match:
        return value
    hour = _number(match.group(2))
    minute = _number(match.group(3) or "0") or 0
    if hour is None or not 0 <= minute <= 59:
        return value
    period = match.group(1) or ""
    if (period in {"下午", "晚上"} and hour < 12) or (
        period == "中午" and hour < 11
    ):
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23:
        return value
    return value.replace(hour=hour, minute=minute, second=0, microsecond=0)


def resolve_event_time(text: str, reference_time: float | None) -> float | None:
    """Resolve an event timestamp using the fact's evidence time as the anchor."""
    reference = optional_timestamp(reference_time)
    value = str(text or "")

    absolute = re.search(
        r"(20\d{2})\s*(?:[-/.年])\s*(\d{1,2})\s*(?:[-/.月])\s*(\d{1,2})",
        value,
    )
    if absolute:
        try:
            base = datetime(
                int(absolute.group(1)),
                int(absolute.group(2)),
                int(absolute.group(3)),
                tzinfo=_LOCAL_TIMEZONE,
            )
            base = _apply_time_of_day(base, value[absolute.end() :])
            return base.timestamp()
        except ValueError:
            return None

    if reference is None:
        return None
    anchor = datetime.fromtimestamp(reference, tz=_LOCAL_TIMEZONE)

    relative_amount = re.search(
        r"([一二两三四五六七八九十\d]+)\s*(分钟|小时|天|周|个月|年)(前|后)",
        value,
    )
    if relative_amount:
        amount = _number(relative_amount.group(1))
        if amount is not None:
            unit = relative_amount.group(2)
            sign = -1 if relative_amount.group(3) == "前" else 1
            if unit == "分钟":
                delta = timedelta(minutes=amount * sign)
            elif unit == "小时":
                delta = timedelta(hours=amount * sign)
            elif unit == "天":
                delta = timedelta(days=amount * sign)
            elif unit == "周":
                delta = timedelta(weeks=amount * sign)
            elif unit == "个月":
                delta = timedelta(days=30 * amount * sign)
            else:
                delta = timedelta(days=365 * amount * sign)
            return (anchor + delta).timestamp()

    day_offsets = (
        ("大后天", 3),
        ("前天", -2),
        ("昨天", -1),
        ("今天", 0),
        ("明天", 1),
        ("后天", 2),
    )
    for token, offset in day_offsets:
        if token in value:
            resolved = anchor + timedelta(days=offset)
            return _apply_time_of_day(resolved, value).timestamp()

    weekday = re.search(r"(上周|本周|下周|下下周)?周([一二三四五六日天])", value)
    if weekday:
        weekday_indexes = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        week_offsets = {"上周": -7, "本周": 0, "下周": 7, "下下周": 14}
        prefix = weekday.group(1) or "本周"
        days = weekday_indexes[weekday.group(2)] - anchor.weekday() + week_offsets[prefix]
        resolved = anchor + timedelta(days=days)
        return _apply_time_of_day(resolved, value).timestamp()

    month_day = re.search(r"(\d{1,2})月(\d{1,2})[日号]", value)
    if month_day:
        try:
            resolved = anchor.replace(
                month=int(month_day.group(1)),
                day=int(month_day.group(2)),
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            return _apply_time_of_day(resolved, value[month_day.end() :]).timestamp()
        except ValueError:
            return None
    return None


def normalize_fact_temporal(
    value: Any,
    *,
    fallback_started_at: float | None = None,
    fallback_ended_at: float | None = None,
    fallback_basis: str = "timeline_window",
) -> dict[str, Any]:
    row = dict(value) if isinstance(value, dict) else {}
    evidence_start = optional_timestamp(row.get("evidence_started_at"))
    evidence_end = optional_timestamp(row.get("evidence_ended_at"))
    if evidence_start is None:
        evidence_start = optional_timestamp(fallback_started_at)
    if evidence_end is None:
        evidence_end = optional_timestamp(fallback_ended_at) or evidence_start
    if evidence_start is None:
        evidence_start = evidence_end
    if evidence_start is not None and evidence_end is not None and evidence_start > evidence_end:
        evidence_start, evidence_end = evidence_end, evidence_start
    event_start = optional_timestamp(row.get("event_started_at"))
    event_end = optional_timestamp(row.get("event_ended_at")) or event_start
    result = {
        "evidence_started_at": evidence_start,
        "evidence_ended_at": evidence_end,
        "event_started_at": event_start,
        "event_ended_at": event_end,
        "time_basis": str(row.get("time_basis") or fallback_basis),
        "time_precision": str(
            row.get("time_precision")
            or ("range" if evidence_start != evidence_end else "fallback")
        ),
        "message_refs": [
            str(ref) for ref in row.get("message_refs", []) if str(ref).strip()
        ],
    }
    event_time_basis = str(row.get("event_time_basis") or "").strip()
    if event_time_basis:
        result["event_time_basis"] = event_time_basis
    return result


def build_key_fact_temporal(
    key_facts: list[str],
    evidence_rows: Iterable[dict[str, Any]],
    source_message_refs: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_by_ref = {
        str(row.get("message_ref") or ""): row
        for row in source_message_refs
        if str(row.get("message_ref") or "")
    }
    all_timestamps = [
        timestamp
        for row in source_by_ref.values()
        if (timestamp := optional_timestamp(row.get("timestamp"))) is not None
    ]
    evidence_by_index: dict[int, dict[str, Any]] = {}
    for row in evidence_rows:
        try:
            evidence_by_index[int(row.get("fact_index"))] = row
        except (TypeError, ValueError):
            continue
    fallback_start = min(all_timestamps) if all_timestamps else None
    fallback_end = max(all_timestamps) if all_timestamps else fallback_start
    result: list[dict[str, Any]] = []
    for index, fact in enumerate(key_facts):
        evidence = evidence_by_index.get(index, {})
        refs = [
            str(ref)
            for ref in evidence.get("message_refs", [])
            if str(ref) in source_by_ref
        ]
        timestamps = [
            timestamp
            for ref in refs
            if (timestamp := optional_timestamp(source_by_ref[ref].get("timestamp")))
            is not None
        ]
        evidence_start = min(timestamps) if timestamps else fallback_start
        evidence_end = max(timestamps) if timestamps else fallback_end
        event_time = resolve_event_time(str(fact), evidence_end or evidence_start)
        result.append(
            {
                "fact_index": index,
                "message_refs": refs,
                "evidence_started_at": evidence_start,
                "evidence_ended_at": evidence_end,
                "event_started_at": event_time,
                "event_ended_at": event_time,
                "time_basis": "message_evidence" if timestamps else "timeline_window",
                "time_precision": (
                    "exact" if len(set(timestamps)) == 1 else "range"
                )
                if timestamps
                else "fallback",
                "event_time_basis": (
                    "absolute_text"
                    if event_time is not None and contains_absolute_date(str(fact))
                    else "relative_to_evidence"
                    if event_time is not None
                    else "unknown"
                ),
            }
        )
    return result


def aggregate_fact_temporal(values: Iterable[Any]) -> dict[str, Any]:
    rows = [normalize_fact_temporal(value) for value in values]
    if not rows:
        return {}
    evidence_starts = [row["evidence_started_at"] for row in rows if row["evidence_started_at"] is not None]
    evidence_ends = [row["evidence_ended_at"] for row in rows if row["evidence_ended_at"] is not None]
    event_starts = [row["event_started_at"] for row in rows if row["event_started_at"] is not None]
    event_ends = [row["event_ended_at"] for row in rows if row["event_ended_at"] is not None]
    bases = {row["time_basis"] for row in rows}
    precisions = {row["time_precision"] for row in rows}
    return {
        "evidence_started_at": min(evidence_starts) if evidence_starts else None,
        "evidence_ended_at": max(evidence_ends) if evidence_ends else None,
        "event_started_at": min(event_starts) if event_starts else None,
        "event_ended_at": max(event_ends) if event_ends else None,
        "time_basis": next(iter(bases)) if len(bases) == 1 else "mixed_sources",
        "time_precision": next(iter(precisions)) if len(precisions) == 1 else "range",
        "message_refs": sorted({ref for row in rows for ref in row.get("message_refs", [])}),
    }


__all__ = [
    "aggregate_fact_temporal",
    "build_key_fact_temporal",
    "contains_absolute_date",
    "contains_relative_time",
    "normalize_fact_temporal",
    "optional_timestamp",
    "resolve_event_time",
]
