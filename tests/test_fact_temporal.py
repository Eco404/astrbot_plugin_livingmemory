from datetime import datetime, timezone

import pytest
from astrbot_plugin_livingmemory.core.fact_temporal import (
    build_key_fact_temporal,
    normalize_fact_temporal,
    resolve_event_time,
)


def test_build_key_fact_temporal_uses_cited_message_range():
    rows = build_key_fact_temporal(
        ["示例甲确认了报销"],
        [{"fact_index": 0, "message_refs": ["M2", "M1"]}],
        [
            {"message_ref": "M1", "timestamp": 100.0},
            {"message_ref": "M2", "timestamp": 200.0},
            {"message_ref": "M3", "timestamp": 300.0},
        ],
    )

    assert rows[0]["evidence_started_at"] == 100.0
    assert rows[0]["evidence_ended_at"] == 200.0
    assert rows[0]["time_basis"] == "message_evidence"
    assert rows[0]["message_refs"] == ["M2", "M1"]


def test_relative_event_time_uses_evidence_instead_of_current_time():
    reference = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc).timestamp()
    resolved = resolve_event_time("示例甲两小时前确认了会议", reference)
    assert resolved == pytest.approx(reference - 2 * 60 * 60)


def test_absolute_chinese_time_keeps_hour_and_minute():
    resolved = resolve_event_time("2026年7月28日下午三点十五分开会", None)
    assert resolved is not None
    value = datetime.fromtimestamp(resolved, tz=timezone.utc).astimezone()
    assert (value.year, value.month, value.day, value.hour, value.minute) == (
        2026,
        7,
        28,
        15,
        15,
    )


def test_relative_day_keeps_explicit_time_of_day():
    reference = datetime(2026, 7, 27, 12, 30, tzinfo=timezone.utc).timestamp()
    resolved = resolve_event_time("昨天晚上八点见面", reference)
    assert resolved is not None
    value = datetime.fromtimestamp(resolved, tz=timezone.utc).astimezone()
    assert (value.year, value.month, value.day, value.hour, value.minute) == (
        2026,
        7,
        26,
        20,
        0,
    )


def test_legacy_fact_temporal_is_explicit_fallback():
    row = normalize_fact_temporal(
        {},
        fallback_started_at=100.0,
        fallback_ended_at=200.0,
        fallback_basis="timeline_window",
    )
    assert row["event_started_at"] is None
    assert row["evidence_started_at"] == 100.0
    assert row["evidence_ended_at"] == 200.0
    assert row["time_basis"] == "timeline_window"
    assert row["time_precision"] == "range"
