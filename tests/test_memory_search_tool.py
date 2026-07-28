"""Tests for the active long-term memory search tool."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.tools.memory_search_tool import MemorySearchTool


@pytest.fixture
def memory_engine():
    engine = Mock()
    engine.search_memories = AsyncMock(return_value=[])
    return engine


@pytest.fixture
def astr_context():
    return Mock()


def _make_run_context():
    event = Mock()
    event.unified_msg_origin = "test:private:session-1"

    run_context = Mock()
    run_context.context = Mock()
    run_context.context.event = event
    return run_context


@pytest.mark.asyncio
async def test_memory_search_tool_uses_filtering_settings(memory_engine, astr_context):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(
            {
                "recall_engine": {"top_k": 3, "max_k": 8},
                "filtering_settings": {
                    "use_session_filtering": True,
                    "use_persona_filtering": True,
                },
            }
        ),
        memory_engine=memory_engine,
    )
    memory_engine.search_memories = AsyncMock(return_value=[])

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        raw_result = await tool.call(_make_run_context(), query="喜欢的游戏", k=6)

    result = json.loads(raw_result)
    assert result["query"] == "喜欢的游戏"
    assert result["applied_filters"] == {
        "session_filtered": True,
        "persona_filtered": True,
    }
    memory_engine.search_memories.assert_awaited_once_with(
        query="喜欢的游戏",
        k=18,
        session_id="test:private:session-1",
        persona_id="persona_a",
        track_access=False,
    )
    assert result["diagnostics"]["timeline"]["final_limit"] == 6


@pytest.mark.asyncio
async def test_memory_search_tool_disables_filters_when_config_disabled(
    memory_engine, astr_context
):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(
            {
                "filtering_settings": {
                    "use_session_filtering": False,
                    "use_persona_filtering": False,
                }
            }
        ),
        memory_engine=memory_engine,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        await tool.call(_make_run_context(), query="项目约定")

    memory_engine.search_memories.assert_awaited_once_with(
        query="项目约定",
        k=15,
        session_id=None,
        persona_id=None,
        track_access=False,
    )
    get_persona.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_search_tool_serializes_results(memory_engine, astr_context):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )
    memory_engine.search_memories = AsyncMock(
        return_value=[
            Mock(
                doc_id=7,
                content="用户喜欢高难度动作游戏",
                final_score=0.91,
                metadata={
                    "importance": 0.8,
                    "session_id": "test:private:session-1",
                    "persona_id": "persona_a",
                    "create_time": 100.0,
                    "last_access_time": 200.0,
                },
            )
        ]
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        raw_result = await tool.call(_make_run_context(), query="游戏偏好")

    result = json.loads(raw_result)
    assert result["count"] == 1
    assert result["results"][0] == {
        "id": 7,
        "content": "用户喜欢高难度动作游戏",
        "score": 0.91,
        "importance": 0.8,
        "session_id": "test:private:session-1",
        "persona_id": "persona_a",
        "create_time": 100.0,
        "last_access_time": 200.0,
    }
    memory_engine.record_memory_access.assert_called_once_with([7])


@pytest.mark.asyncio
async def test_memory_search_tool_binds_timeline_fact_time(memory_engine, astr_context):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )
    fact = "示例甲下单了奶茶"
    memory_engine.search_memories = AsyncMock(
        return_value=[
            Mock(
                doc_id=7,
                content=f"我记得这件事。{fact}",
                final_score=0.91,
                metadata={
                    "importance": 0.8,
                    "create_time": 1722168000.0,
                    "key_facts": [fact],
                    "key_fact_temporal": [
                        {
                            "evidence_started_at": 1722168000.0,
                            "evidence_ended_at": 1722168000.0,
                            "time_basis": "message_evidence",
                        }
                    ],
                },
            )
        ]
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        raw_result = await tool.call(_make_run_context(), query="奶茶")

    content = json.loads(raw_result)["results"][0]["content"]
    assert content.count(fact) == 1
    assert "[记录于 2024-07-28" in content


@pytest.mark.asyncio
async def test_memory_search_tool_records_topic_access_after_serializing_result(
    memory_engine, astr_context
):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )
    topic = SimpleNamespace(
        title="工资核对",
        summary="核对工资天数",
        importance=0.8,
    )
    topic_result = SimpleNamespace(
        topic_uid="topic-wage",
        topic=topic,
        content="工资核对\n核对工资天数",
        final_score=0.8,
        sources=[],
        affect_match_score=0.0,
        affect_match_boost=0.0,
        selected_affect_events=[],
    )
    topic_outcome = SimpleNamespace(results=[topic_result])
    fragment_outcome = SimpleNamespace(results=[], available_count=0)
    topic_pipeline = SimpleNamespace(
        config={"recall_top_k": 1, "timeline_supplement_k": 0},
        search=AsyncMock(return_value=topic_outcome),
        search_fragment_supplements=AsyncMock(return_value=fragment_outcome),
        select_timeline_supplements=Mock(return_value=[]),
        record_topic_access=AsyncMock(return_value=1),
        source_timeline_document_ids=AsyncMock(return_value=[31]),
    )
    memory_engine.topic_memory_enabled = True
    memory_engine.topic_recall_pipeline = topic_pipeline
    memory_engine.search_memories = AsyncMock(return_value=[])

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        raw_result = await tool.call(_make_run_context(), query="工资")

    result = json.loads(raw_result)
    assert result["results"][0]["memory_layer"] == "topic"
    topic_pipeline.record_topic_access.assert_awaited_once_with([topic_result])
    memory_engine.record_memory_access.assert_called_once_with([31])


@pytest.mark.asyncio
async def test_memory_search_tool_serializes_non_dict_metadata(
    memory_engine, astr_context
):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )
    memory_engine.search_memories = AsyncMock(
        return_value=[
            Mock(
                doc_id=8,
                content="用户提到过想学摄影",
                final_score=0.55,
                metadata=None,
            )
        ]
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        raw_result = await tool.call(_make_run_context(), query="摄影")

    result = json.loads(raw_result)
    assert result["results"][0] == {
        "id": 8,
        "content": "用户提到过想学摄影",
        "score": 0.55,
        "importance": None,
        "session_id": None,
        "persona_id": None,
        "create_time": None,
        "last_access_time": None,
    }


@pytest.mark.asyncio
async def test_memory_search_tool_limits_k_by_config(memory_engine, astr_context):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager({"recall_engine": {"top_k": 5, "max_k": 4}}),
        memory_engine=memory_engine,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        await tool.call(_make_run_context(), query="偏好", k=9)

    memory_engine.search_memories.assert_awaited_once_with(
        query="偏好",
        k=12,
        session_id="test:private:session-1",
        persona_id="persona_a",
        track_access=False,
    )


@pytest.mark.asyncio
async def test_memory_search_tool_clamps_low_k_to_one(memory_engine, astr_context):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(
            {
                "recall_engine": {"top_k": 3, "max_k": 8},
                "filtering_settings": {
                    "use_session_filtering": True,
                    "use_persona_filtering": True,
                },
            }
        ),
        memory_engine=memory_engine,
    )
    memory_engine.search_memories = AsyncMock(return_value=[])

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"

        await tool.call(_make_run_context(), query="test query", k=0)
        called_k_zero = memory_engine.search_memories.await_args.kwargs["k"]
        assert called_k_zero == 3

        memory_engine.search_memories.reset_mock()
        await tool.call(_make_run_context(), query="test query", k=-3)
        called_k_negative = memory_engine.search_memories.await_args.kwargs["k"]
        assert called_k_negative == 3


@pytest.mark.asyncio
async def test_memory_search_tool_falls_back_to_default_k_for_invalid_input(
    memory_engine, astr_context
):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager({"recall_engine": {"top_k": 3, "max_k": 8}}),
        memory_engine=memory_engine,
    )
    memory_engine.search_memories = AsyncMock(return_value=[])

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        await tool.call(_make_run_context(), query="test query", k="bad")

    memory_engine.search_memories.assert_awaited_once_with(
        query="test query",
        k=9,
        session_id="test:private:session-1",
        persona_id="persona_a",
        track_access=False,
    )


@pytest.mark.asyncio
async def test_memory_search_tool_returns_structured_error_for_empty_query(
    memory_engine, astr_context
):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )

    raw_result = await tool.call(_make_run_context(), query="   ")
    result = json.loads(raw_result)

    assert result["results"] == []
    assert result["error"] == "query is empty"
    memory_engine.search_memories.assert_not_called()


@pytest.mark.asyncio
async def test_memory_search_tool_returns_not_initialized_error(memory_engine):
    tool = MemorySearchTool(
        context=None,
        config_manager=None,
        memory_engine=memory_engine,
    )

    raw_result = await tool.call(_make_run_context(), query="测试")
    result = json.loads(raw_result)

    assert result == {
        "query": "测试",
        "count": 0,
        "results": [],
        "error": "memory search tool is not initialized",
    }
    memory_engine.search_memories.assert_not_called()


@pytest.mark.asyncio
async def test_memory_search_tool_hides_internal_exception_details(
    memory_engine, astr_context
):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )
    memory_engine.search_memories = AsyncMock(
        side_effect=RuntimeError("secret db path")
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        raw_result = await tool.call(_make_run_context(), query="异常")

    result = json.loads(raw_result)
    assert result["error"] == "internal_error"
    assert "secret db path" not in raw_result


@pytest.mark.asyncio
async def test_memory_search_tool_propagates_cancellation(memory_engine, astr_context):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )
    memory_engine.search_memories = AsyncMock(side_effect=asyncio.CancelledError())

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        with pytest.raises(asyncio.CancelledError):
            await tool.call(_make_run_context(), query="取消")


def test_memory_search_tool_exposes_optional_temporal_contract(
    memory_engine, astr_context
):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )
    temporal = tool.parameters["properties"]["temporal"]
    assert "temporal" not in tool.parameters["required"]
    assert temporal["properties"]["mode"]["enum"] == [
        "range",
        "earliest",
        "latest",
    ]
    assert temporal["properties"]["start"]["format"] == "date-time"
    assert temporal["properties"]["end"]["format"] == "date-time"


@pytest.mark.asyncio
async def test_memory_search_tool_rejects_invalid_temporal_constraint(
    memory_engine, astr_context
):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )
    raw_result = await tool.call(
        _make_run_context(),
        query="七月的安排",
        temporal={"mode": "range"},
    )
    result = json.loads(raw_result)
    assert result["error"] == "invalid_temporal_constraint"
    memory_engine.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_search_tool_applies_temporal_constraint_and_reports_anchor(
    memory_engine, astr_context
):
    tool = MemorySearchTool(
        context=astr_context,
        config_manager=ConfigManager(),
        memory_engine=memory_engine,
    )
    memory_engine.resolve_session_scope = AsyncMock(
        return_value=["test:private:session-1"]
    )
    memory_engine.memory_identity_store = SimpleNamespace(
        list_timeline_document_ids=AsyncMock(return_value=[7]),
        get_time_anchors_by_document_ids=AsyncMock(
            return_value={
                7: {
                    "memory_uid": "timeline-7",
                    "started_at": 1784505600.0,
                    "ended_at": 1784509200.0,
                    "time_basis": "timeline_source_span",
                    "time_fallback": False,
                }
            }
        ),
    )
    memory_engine.search_memories = AsyncMock(
        return_value=[
            HybridResult(
                doc_id=7,
                final_score=0.9,
                rrf_score=0.9,
                bm25_score=0.1,
                vector_score=0.9,
                content="七月二十日的安排",
                metadata={"importance": 0.8},
                score_breakdown={"document_vector_score": 0.9},
            )
        ]
    )
    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        raw_result = await tool.call(
            _make_run_context(),
            query="七月二十日的安排",
            temporal={
                "mode": "range",
                "start": "2026-07-20T00:00:00Z",
                "end": "2026-07-20T23:59:59Z",
            },
        )
    result = json.loads(raw_result)
    assert result["count"] == 1
    assert result["applied_filters"]["temporal"]["mode"] == "range"
    assert result["results"][0]["matched_source_uids"] == ["timeline-7"]
    assert result["results"][0]["time_basis"] == "timeline_source_span"
