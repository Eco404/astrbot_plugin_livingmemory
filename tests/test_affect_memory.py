from __future__ import annotations

import pytest

from astrbot_plugin_livingmemory.core.affect_memory import (
    affect_similarity,
    aggregate_affect_profile,
    extract_query_affect,
    format_affect_context,
    normalize_affect_event,
)
from astrbot_plugin_livingmemory.core.models.topic_memory import TopicFragmentDraft


def _event(
    emotion: str,
    category: str,
    *,
    valence: float,
    actor_id: str = "qq:human:u1",
    temporal_status: str = "historical",
) -> dict:
    return {
        "event_uid": f"event-{emotion}",
        "actor_id": actor_id,
        "display_name_snapshot": "空雨",
        "emotion": emotion,
        "description": emotion,
        "trigger": "项目进展",
        "target": "项目",
        "evidence_type": "explicit",
        "temporal_status": temporal_status,
        "valence": valence,
        "arousal": 0.7,
        "dominance": 0.4,
        "intensity": 0.8,
        "confidence": 0.9,
        "categories": [{"label": category, "score": 0.95}],
        "source_timeline_uids": ["timeline-1"],
        "source_atom_fingerprints": ["a" * 64],
        "source_fact_keys": ["timeline-1:atom:" + "a" * 64],
    }


def test_query_affect_only_uses_explicit_cues_or_affect_intent():
    neutral = extract_query_affect("六月工资少发了多少？")
    explicit = extract_query_affect("当时工资少发后我是不是很生气？")
    intent = extract_query_affect("那件事里我的感受如何？")

    assert neutral["needs_affect"] is False
    assert explicit["explicit"] is True
    assert explicit["categories"][0]["label"] == "anger"
    assert intent["needs_affect"] is True
    assert intent["explicit"] is False


def test_affect_profile_preserves_opposite_grounded_events():
    fragment = TopicFragmentDraft(
        run_uid="run-1",
        candidate_group_uid="group-1",
        memory_space_id="space-1",
        label="项目情绪变化",
        summary="空雨先挫败，后来如释重负",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[{"content": "项目最终恢复"}],
        affect_events=[
            _event("感到挫败", "frustration", valence=0.18),
            _event("感到释然", "relief", valence=0.80, temporal_status="resolved"),
        ],
    )

    profile, salience = aggregate_affect_profile([fragment])

    assert len(profile) == 2
    assert {item["categories"][0]["label"] for item in profile} == {
        "frustration",
        "relief",
    }
    assert salience == pytest.approx(0.72)


def test_repeated_emotion_does_not_crowd_out_opposite_prototype():
    frustration_one = _event("第一次挫败", "frustration", valence=0.15)
    frustration_two = _event("第二次挫败", "frustration", valence=0.17)
    frustration_two["event_uid"] = "event-frustration-2"
    frustration_two["intensity"] = 0.95
    relief = _event(
        "后来释然",
        "relief",
        valence=0.82,
        temporal_status="resolved",
    )
    relief["intensity"] = 0.55
    fragment = TopicFragmentDraft(
        run_uid="run-1",
        candidate_group_uid="group-1",
        memory_space_id="space-1",
        label="情绪转折",
        summary="先挫败，后来释然",
        timeline_uids=["timeline-1"],
        source_revisions={"timeline-1": 1},
        facts=[{"content": "事情最终解决"}],
        affect_events=[frustration_one, frustration_two, relief],
    )

    profile, _ = aggregate_affect_profile([fragment], max_events=2)

    assert [item["categories"][0]["label"] for item in profile] == [
        "frustration",
        "relief",
    ]
    assert profile[0]["event_uid"] == "event-frustration-2"


def test_affect_similarity_and_rendering_keep_time_status():
    event = normalize_affect_event(
        _event("很担心", "concern", valence=0.3, temporal_status="resolved")
    )
    query = extract_query_affect("之前是不是很担心项目？")

    assert event is not None
    assert affect_similarity(query, [event]) > 0.7
    rendered = format_affect_context([event])
    assert "后来已缓解" in rendered
    assert "空雨" in rendered
