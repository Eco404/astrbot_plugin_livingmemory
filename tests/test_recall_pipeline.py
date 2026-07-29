from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.retrieval.recall_pipeline import RecallPipeline
from astrbot_plugin_livingmemory.core.utils import (
    format_memories_for_fake_tool_call,
    format_memories_for_injection,
)


def _result(
    memory_id: int,
    content: str,
    *,
    score: float,
    vector: float,
    metadata: dict | None = None,
) -> HybridResult:
    return HybridResult(
        doc_id=memory_id,
        final_score=score,
        rrf_score=score,
        bm25_score=0.2,
        vector_score=vector,
        content=content,
        metadata=metadata or {},
        score_breakdown={"document_vector_score": vector},
    )


class _FakeEngine:
    def __init__(self, results_by_query: dict[str, list[HybridResult]]):
        self.results_by_query = results_by_query
        self.calls: list[dict] = []
        self.accessed: list[int] = []

    async def search_memories(self, **kwargs):
        self.calls.append(kwargs)
        return [
            replace(item, metadata=dict(item.metadata))
            for item in self.results_by_query.get(kwargs["query"], [])
        ]

    def record_memory_access(self, memory_ids: list[int]):
        self.accessed.extend(memory_ids)


def test_query_branches_remove_current_and_control_assistant_weight():
    pipeline = RecallPipeline(
        _FakeEngine({}), {"recent_context_max_age_seconds": 0}
    )
    messages = [
        {"role": "user", "content": "之前说下雨了"},
        {"role": "assistant", "content": "雷暴时不要出门"},
        {"role": "user", "content": "陪我聊聊天"},
        {"role": "assistant", "content": "继续聊雷雨"},
        {"role": "user", "content": "现在改插件遇到暴雨"},
    ]

    excluded = pipeline.build_query_branches(
        "现在改插件遇到暴雨",
        messages,
        expansion_enabled=True,
        assistant_mode="exclude",
    )
    assert [item.name for item in excluded] == ["current", "recent_user"]
    assert excluded[1].text == "之前说下雨了 | 陪我聊聊天"
    assert excluded[1].weight == pytest.approx(0.45)

    low = pipeline.build_query_branches(
        "现在改插件遇到暴雨",
        messages,
        expansion_enabled=True,
        assistant_mode="low_weight",
    )
    assert [item.name for item in low] == [
        "current",
        "recent_user",
        "recent_assistant",
    ]
    assert low[-1].weight == pytest.approx(0.20)

    normal = pipeline.build_query_branches(
        "现在改插件遇到暴雨",
        messages,
        expansion_enabled=True,
        assistant_mode="normal",
    )
    assert normal[-1].weight == pytest.approx(0.40)


def test_query_branch_weights_are_runtime_configurable():
    pipeline = RecallPipeline(
        _FakeEngine({}),
        {
            "recent_user_weight": 0.25,
            "recent_assistant_weight": 0.30,
            "recent_context_max_age_seconds": 0,
        },
    )
    messages = [
        {"role": "user", "content": "历史用户消息"},
        {"role": "assistant", "content": "历史 Bot 回复"},
    ]
    normal = pipeline.build_query_branches(
        "当前问题",
        messages,
        expansion_enabled=True,
        assistant_mode="normal",
    )
    assert normal[1].weight == pytest.approx(0.25)
    assert normal[2].weight == pytest.approx(0.30)
    low = pipeline.build_query_branches(
        "当前问题",
        messages,
        expansion_enabled=True,
        assistant_mode="low_weight",
    )
    assert low[2].weight == pytest.approx(0.15)


def test_query_branches_exclude_stale_and_undated_context():
    now = time.time()
    pipeline = RecallPipeline(
        _FakeEngine({}),
        {
            "inject_with_recent_context": True,
            "recent_context_max_age_seconds": 7200,
            "assistant_context_mode": "normal",
        },
    )

    branches = pipeline.build_query_branches(
        "当前问题",
        [
            {"role": "user", "content": "一小时内", "timestamp": now - 3600},
            {"role": "assistant", "content": "三小时前", "timestamp": now - 10800},
            {"role": "user", "content": "缺少时间"},
            {"role": "user", "content": "当前问题", "timestamp": now},
        ],
    )

    assert [branch.name for branch in branches] == ["current", "recent_user"]
    assert branches[1].text == "一小时内"


def test_query_branches_allow_undated_context_when_age_limit_is_disabled():
    pipeline = RecallPipeline(
        _FakeEngine({}),
        {
            "inject_with_recent_context": True,
            "recent_context_max_age_seconds": 0,
        },
    )

    branches = pipeline.build_query_branches(
        "当前问题",
        [
            {"role": "user", "content": "没有时间但允许使用"},
            {"role": "user", "content": "当前问题"},
        ],
    )

    assert branches[1].text == "没有时间但允许使用"


@pytest.mark.asyncio
async def test_pipeline_filters_weak_candidates_and_tracks_only_selected():
    weather = _result(1, "示例市暴雨和雷电天气", score=0.9, vector=0.88)
    duplicate_weather = _result(2, "示例市暴雨雷电和雨天出行", score=0.84, vector=0.82)
    coding = _result(3, "AstrBot 记忆插件代码优化", score=0.8, vector=0.78)
    weak = _result(4, "睡前故事和早餐", score=0.72, vector=0.18)
    engine = _FakeEngine(
        {"改记忆插件时外面下暴雨": [weather, duplicate_weather, coding, weak]}
    )
    pipeline = RecallPipeline(
        engine,
        {
            "candidate_multiplier": 3,
            "min_relevance_score": 0.38,
            "relative_score_floor": 0.65,
            "mmr_lambda": 0.55,
        },
    )

    outcome = await pipeline.search(
        current_query="改记忆插件时外面下暴雨",
        final_k=2,
        track_access=True,
    )

    selected_ids = [item.doc_id for item in outcome.results]
    assert selected_ids == [1, 3]
    assert engine.accessed == selected_ids
    weak_candidate = next(item for item in outcome.candidates if item.result.doc_id == 4)
    assert weak_candidate.filter_reason == "below_min_relevance"
    assert engine.calls[0]["k"] == 6
    assert engine.calls[0]["track_access"] is False


@pytest.mark.asyncio
async def test_pipeline_suppresses_timeline_already_visible_in_context():
    overlapping = _result(
        1,
        "刚才的雷雨",
        score=0.9,
        vector=0.9,
        metadata={
            "session_id": "session-1",
            "source_window": {"start_index": 90, "end_index": 100},
        },
    )
    older = _result(
        2,
        "更早以前的雷雨",
        score=0.8,
        vector=0.8,
        metadata={
            "session_id": "session-1",
            "source_window": {"start_index": 20, "end_index": 30},
        },
    )
    engine = _FakeEngine({"雷雨": [overlapping, older]})
    pipeline = RecallPipeline(engine, {"context_overlap_suppression": True})

    outcome = await pipeline.search(
        current_query="雷雨",
        final_k=2,
        session_id=None,
        context_session_id="session-1",
        visible_message_start_index=80,
        visible_message_end_index=101,
        track_access=False,
    )

    assert [item.doc_id for item in outcome.results] == [2]
    assert outcome.overlap_suppressed == 1
    candidate = next(item for item in outcome.candidates if item.result.doc_id == 1)
    assert candidate.filter_reason == "current_context_overlap"


def test_injection_does_not_repeat_key_fact_already_in_content():
    fact = "用户在下午遇到了暴雨"
    rendered = format_memories_for_injection(
        [
            {
                "content": f"用户本来准备出门。{fact}",
                "metadata": {
                    "importance": 0.7,
                    "topics": ["天气"],
                    "key_facts": [fact],
                    "summary_schema_version": "v2",
                },
            }
        ]
    )
    assert rendered.count(fact) == 1


def test_injection_binds_temporal_anchor_without_repeating_fact():
    fact = "示例甲下单了奶茶"
    recorded_at = 1722168000.0
    rendered = format_memories_for_injection(
        [
            {
                "content": f"我记得这件事。{fact}",
                "metadata": {
                    "importance": 0.7,
                    "key_facts": [fact],
                    "key_fact_temporal": [
                        {
                            "evidence_started_at": recorded_at,
                            "evidence_ended_at": recorded_at,
                            "time_basis": "message_evidence",
                        }
                    ],
                },
            }
        ]
    )

    assert rendered.count(fact) == 1
    assert "[记录于 2024-07-28" in rendered


def test_fake_tool_call_binds_temporal_anchor_without_repeating_fact():
    fact = "示例甲下单了奶茶"
    messages = format_memories_for_fake_tool_call(
        [
            {
                "id": 1,
                "content": f"我记得这件事。{fact}",
                "metadata": {
                    "key_facts": [fact],
                    "key_fact_temporal": [
                        {
                            "evidence_started_at": 1722168000.0,
                            "evidence_ended_at": 1722168000.0,
                            "time_basis": "message_evidence",
                        }
                    ],
                },
            }
        ],
        query="奶茶",
    )
    result = json.loads(messages[1]["content"])
    content = result["results"][0]["content"]
    assert content.count(fact) == 1
    assert "[记录于 2024-07-28" in content
