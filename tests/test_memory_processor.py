"""
Tests for MemoryProcessor.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_livingmemory.core.models.conversation_models import (
    Message,
    build_role_bindings,
)
from astrbot_plugin_livingmemory.core.models.identity_profile import (
    SupplementalIdentityStore,
)
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor


class _DummyLLMProvider:
    def __init__(self, completion_text: str):
        self._completion_text = completion_text
        self.text_chat = AsyncMock(side_effect=self._chat)

    async def _chat(self, prompt: str, system_prompt: str):
        return SimpleNamespace(completion_text=self._completion_text)


class _SequenceLLMProvider:
    def __init__(self, *completion_texts: str):
        self._completion_texts = list(completion_texts)
        self.text_chat = AsyncMock(side_effect=self._chat)

    async def _chat(self, prompt: str, system_prompt: str):
        return SimpleNamespace(completion_text=self._completion_texts.pop(0))


def _make_messages():
    return [
        Message(
            id=1,
            session_id="s1",
            role="user",
            content="明天下午三点开会",
            sender_id="u1",
            sender_name="张三",
            group_id=None,
            platform="test",
            metadata={},
        ),
        Message(
            id=2,
            session_id="s1",
            role="assistant",
            content="收到，我会提醒你",
            sender_id="bot",
            sender_name="Bot",
            group_id=None,
            platform="test",
            metadata={"is_bot_message": True},
        ),
    ]


@pytest.mark.asyncio
async def test_process_conversation_success():
    llm = _DummyLLMProvider(
        """{
            "summary":"我记录了张三明天下午三点开会，并给出提醒",
            "topics":["会议提醒"],
            "key_facts":["张三明天下午三点开会"],
            "sentiment":"neutral",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, importance = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert "张三" in content
    assert metadata["interaction_type"] == "private_chat"
    assert "会议提醒" in metadata["topics"]
    assert importance == 0.8


@pytest.mark.asyncio
async def test_process_conversation_retains_low_quality_non_json_after_repair():
    llm = _DummyLLMProvider("summary=测试, importance=0.6")
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )
    assert content
    assert metadata["summary_quality"] == "low"
    assert metadata["summary_rebuild_recommended"] is True
    assert metadata["summary_quality_report"]["status"] == "rejected"
    # AsyncMock advertises **kwargs, so the first call also covers one tool-mode
    # capability negotiation before falling back to the test provider signature.
    assert llm.text_chat.await_count == 3


@pytest.mark.asyncio
async def test_process_conversation_accepts_strict_no_memory_decision():
    llm = _DummyLLMProvider(
        """{
            "memory_decision":"no_memory",
            "no_memory_reason":"ack_only",
            "summary":"",
            "topics":[],
            "key_facts":[],
            "key_fact_evidence":[],
            "key_fact_attributions":[],
            "key_fact_profiles":[],
            "message_coverage":[
                {"message_ref":"M1","disposition":"context","fact_indexes":[],"reason_code":"supporting_context","reason":"仅简短确认"},
                {"message_ref":"M2","disposition":"omitted","fact_indexes":[],"reason_code":"routine_response","reason":"无持久信息"}
            ],
            "sentiment":"neutral",
            "importance":0.1
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, importance = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        allow_no_memory=True,
    )

    assert content == ""
    assert metadata["memory_decision"] == "no_memory"
    assert metadata["no_memory_reason"] == "ack_only"
    assert metadata["summary_quality"] == "normal"
    assert metadata["summary_rebuild_recommended"] is False
    assert importance == 0.1


@pytest.mark.asyncio
async def test_invalid_no_memory_falls_back_to_low_quality_timeline():
    llm = _DummyLLMProvider(
        """{
            "memory_decision":"no_memory",
            "no_memory_reason":"ack_only",
            "summary":"",
            "topics":[],
            "key_facts":[],
            "key_fact_evidence":[],
            "message_coverage":[
                {"message_ref":"M1","disposition":"context","fact_indexes":[],"reason":"仅简短确认"},
                {"message_ref":"M2","disposition":"omitted","fact_indexes":[],"reason":"无持久信息"}
            ],
            "sentiment":"neutral",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, importance = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        allow_no_memory=True,
    )

    assert content
    assert metadata["memory_decision"] == "store"
    assert metadata["rejected_memory_decision"] == "no_memory"
    assert metadata["summary_quality"] == "low"
    assert metadata["summary_rebuild_recommended"] is True
    assert importance == 0.8


@pytest.mark.asyncio
async def test_persona_prompt_is_included_when_available():
    llm = _DummyLLMProvider(
        """{
            "summary":"我愉快地记录了这次交流",
            "topics":["闲聊"],
            "key_facts":["用户问候"],
            "sentiment":"positive",
            "importance":0.5
        }"""
    )
    context = Mock()
    context.persona_manager = Mock()
    context.persona_manager.get_persona = AsyncMock(
        return_value=SimpleNamespace(system_prompt="你是活泼助手")
    )

    processor = MemoryProcessor(llm_provider=llm, context=context)

    system_prompt = await processor._build_system_prompt_with_persona("persona_1")
    assert "人格设定" in system_prompt
    assert "活泼助手" in system_prompt


@pytest.mark.asyncio
async def test_supplemental_identity_is_injected_by_stable_sender_id():
    llm = _DummyLLMProvider(
        """{
            "summary":"我记录了空雨的安排",
            "topics":["安排"],
            "key_facts":["空雨是男性，使用他作为代词"],
            "sentiment":"neutral",
            "importance":0.6
        }"""
    )
    messages = _make_messages()
    messages[0].sender_id = "1141337347"
    messages[0].sender_name = "空雨"
    messages[0].platform = "qq_official"
    processor = MemoryProcessor(
        llm_provider=llm,
        context=None,
        identity_profile_store=SupplementalIdentityStore(
            profiles=[
                {
                    "platform": "qq",
                    "user_id": "1141337347",
                    "display_name": "空雨",
                    "gender": "男性",
                    "pronouns": ["他", "他的"],
                }
            ]
        ),
    )

    await processor.process_conversation(messages, is_group_chat=False)

    prompt = llm.text_chat.await_args.kwargs["prompt"]
    system_prompt = llm.text_chat.await_args.kwargs["system_prompt"]
    assert prompt.startswith("# 补充人物资料（仅用于消歧）")
    assert '"user_id":"1141337347"' in prompt
    assert '"gender":"男性"' in prompt
    assert '"pronouns":["他","他的"]' in prompt
    assert "人格设定只描述你自己" in system_prompt


@pytest.mark.asyncio
async def test_supplemental_identity_does_not_match_same_name_with_other_id():
    llm = _DummyLLMProvider(
        """{
            "summary":"我记录了空雨的安排",
            "topics":["安排"],
            "key_facts":["空雨有一个安排"],
            "sentiment":"neutral",
            "importance":0.6
        }"""
    )
    messages = _make_messages()
    messages[0].sender_id = "different-account"
    messages[0].sender_name = "空雨"
    messages[0].platform = "qq"
    processor = MemoryProcessor(
        llm_provider=llm,
        context=None,
        identity_profile_store=SupplementalIdentityStore(
            profiles=[
                {
                    "platform": "qq",
                    "user_id": "1141337347",
                    "display_name": "空雨",
                    "gender": "男性",
                }
            ]
        ),
    )

    await processor.process_conversation(messages, is_group_chat=False)

    prompt = llm.text_chat.await_args.kwargs["prompt"]
    assert not prompt.startswith("# 补充人物资料（仅用于消歧）")


# ── New tests for dual-channel summary and quality validator ──────────────────


@pytest.mark.asyncio
async def test_dual_channel_summary_stores_canonical_and_persona():
    """
    process_conversation 应在 metadata 中同时存储
    canonical_summary（检索用）和 persona_summary（人格风格用）。
    """
    llm = _DummyLLMProvider(
        """{
            "summary":"我记录了张三明天下午三点开会，并给出提醒",
            "topics":["会议提醒"],
            "key_facts":["张三明天下午三点开会"],
            "sentiment":"neutral",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, importance = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    # canonical_summary 应存在且包含事实内容
    assert "canonical_summary" in metadata
    assert len(metadata["canonical_summary"]) > 0

    # persona_summary 应存在（等于原始 LLM summary）
    assert "persona_summary" in metadata
    assert "张三" in metadata["persona_summary"]

    # content 应使用 canonical_summary（事实导向）
    assert content == metadata["canonical_summary"]

    # schema 版本标记
    assert metadata.get("summary_schema_version") == "v6-fact-selection"


@pytest.mark.asyncio
async def test_canonical_summary_includes_key_facts():
    """canonical_summary 应将 key_facts 拼接到摘要中，提升检索覆盖率。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"张三提到了一个重要事项",
            "topics":["备忘"],
            "key_facts":["明天下午三点开会", "需要准备PPT"],
            "sentiment":"neutral",
            "importance":0.7
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    # canonical_summary 应包含 key_facts 内容
    assert "明天下午三点开会" in metadata["canonical_summary"]
    assert "需要准备PPT" in metadata["canonical_summary"]


@pytest.mark.asyncio
async def test_summary_quality_normal_for_valid_response():
    """有效的 LLM 响应应标记为 summary_quality=normal。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"用户告知明天下午三点有重要会议需要参加",
            "topics":["会议"],
            "key_facts":["明天下午三点开会"],
            "sentiment":"neutral",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert metadata.get("summary_quality") == "normal"


@pytest.mark.asyncio
async def test_source_grounding_contract_repairs_once_and_persists_report():
    llm = _SequenceLLMProvider(
        """{
            "summary":"某用户提到了一次会议",
            "topics":["会议"],
            "key_facts":["某用户需要开会"],
            "sentiment":"neutral",
            "importance":0.7
        }""",
        """{
            "summary":"张三说明2026年7月28日下午三点开会，我确认会进行提醒",
            "topics":["会议提醒"],
            "key_facts":["张三2026年7月28日下午三点开会"],
            "key_fact_evidence":[{"fact_index":0,"message_refs":["M1"]}],
            "key_fact_attributions":[
                {"fact_index":0,"subject_refs":[{"actor_ref":"A2","display_name_snapshot":"张三"}],"claim_type":"speaker_self","confidence":1.0}
            ],
            "key_fact_profiles":[
                {"fact_index":0,"fact_type":"plan","durability":"high","selection_reason":"future_utility"}
            ],
            "message_coverage":[
                {"message_ref":"M1","disposition":"fact","fact_indexes":[0],"reason_code":"durable_fact","reason":"会议时间来源"},
                {"message_ref":"M2","disposition":"context","fact_indexes":[],"reason_code":"routine_response","reason":"Bot确认提醒"}
            ],
            "sentiment":"neutral",
            "importance":0.7
        }""",
    )
    processor = MemoryProcessor(
        llm_provider=llm,
        context=None,
        config={"timeline_require_source_grounding": True},
    )

    _, metadata, _ = await processor.process_conversation(_make_messages())

    assert llm.text_chat.await_count == 3
    assert metadata["summary_quality"] == "repaired"
    assert metadata["summary_repair_attempted"] is True
    assert metadata["summary_quality_report"]["grounded_fact_count"] == 1
    assert metadata["summary_quality_report"]["covered_message_count"] == 2
    assert metadata["key_fact_evidence"] == [
        {"fact_index": 0, "message_refs": ["M1"]}
    ]
    assert metadata["key_fact_temporal"][0]["time_basis"] == "message_evidence"
    assert metadata["key_fact_temporal"][0]["evidence_started_at"] == pytest.approx(
        _make_messages()[0].timestamp
    )
    assert metadata["key_fact_temporal"][0]["event_started_at"] is not None


@pytest.mark.asyncio
async def test_proactive_bot_self_action_is_not_transferred_to_private_peer():
    messages = [
        Message(
            id=1,
            session_id="s1",
            role="user",
            content="刚理完头发，准备找点吃的",
            sender_id="u1",
            sender_name="空雨",
            platform="test",
        ),
        Message(
            id=2,
            session_id="s1",
            role="assistant",
            content="脑子总算回来一点了，想开电脑写一小段，把散乱念头理顺。你也别忘了晚饭。",
            sender_id="bot",
            sender_name="唯",
            platform="test",
            metadata={"is_bot_message": True, "persona_name": "唯"},
        ),
        Message(
            id=3,
            session_id="s1",
            role="user",
            content="嗯，准备附近转一转",
            sender_id="u1",
            sender_name="空雨",
            platform="test",
        ),
    ]
    llm = _SequenceLLMProvider(
        """{
            "memory_decision":"store","no_memory_reason":"none",
            "summary":"空雨打算开电脑写一小段，我提醒他吃晚饭",
            "topics":["晚间安排"],
            "key_facts":["空雨打算开电脑写一小段"],
            "key_fact_evidence":[{"fact_index":0,"message_refs":["M2"]}],
            "key_fact_attributions":[{"fact_index":0,"subject_refs":[{"actor_ref":"A2","display_name_snapshot":"空雨"}],"claim_type":"speaker_self","confidence":0.9}],
            "key_fact_profiles":[{"fact_index":0,"fact_type":"plan","durability":"medium","selection_reason":"future_utility"}],
            "message_coverage":[
                {"message_ref":"M1","disposition":"context","fact_indexes":[],"reason_code":"supporting_context","reason":"晚间背景"},
                {"message_ref":"M2","disposition":"fact","fact_indexes":[0],"reason_code":"durable_fact","reason":"计划来源"},
                {"message_ref":"M3","disposition":"context","fact_indexes":[],"reason_code":"supporting_context","reason":"后续安排"}
            ],
            "sentiment":"positive","importance":0.5
        }""",
        """{
            "memory_decision":"store","no_memory_reason":"none",
            "summary":"我打算开电脑写一小段整理念头，也提醒空雨别忘了吃晚饭",
            "topics":["晚间安排"],
            "key_facts":["我打算开电脑写一小段整理念头"],
            "key_fact_evidence":[{"fact_index":0,"message_refs":["M2"]}],
            "key_fact_attributions":[{"fact_index":0,"subject_refs":[{"actor_ref":"A1","display_name_snapshot":"唯"}],"claim_type":"speaker_self","confidence":1.0}],
            "key_fact_profiles":[{"fact_index":0,"fact_type":"plan","durability":"medium","selection_reason":"future_utility"}],
            "message_coverage":[
                {"message_ref":"M1","disposition":"context","fact_indexes":[],"reason_code":"supporting_context","reason":"晚间背景"},
                {"message_ref":"M2","disposition":"fact","fact_indexes":[0],"reason_code":"durable_fact","reason":"Bot自述计划"},
                {"message_ref":"M3","disposition":"context","fact_indexes":[],"reason_code":"supporting_context","reason":"后续安排"}
            ],
            "sentiment":"positive","importance":0.5
        }""",
    )
    processor = MemoryProcessor(
        llm_provider=llm,
        context=None,
        config={"timeline_require_source_grounding": True},
    )

    _, metadata, _ = await processor.process_conversation(
        messages,
        persona_id="persona-1",
    )

    assert metadata["summary_quality"] == "repaired"
    assert metadata["key_fact_attributions"][0]["subject_refs"][0][
        "actor_id"
    ] == "test:assistant:bot"
    prompt = llm.text_chat.await_args_list[1].kwargs["prompt"]
    assert "[speaker=A1 | role=assistant]" in prompt
    assert "称呼语只表示被称呼者" in prompt


def test_source_grounding_accepts_durable_bot_relationship_interaction():
    processor = MemoryProcessor(llm_provider=Mock(), context=None)
    messages = _make_messages()
    role_bindings = build_role_bindings(messages)
    _, actor_refs = processor._actor_prompt_block(role_bindings)
    report = processor.assess_summary_quality(
        {
            "memory_decision": "store",
            "no_memory_reason": "none",
            "summary": "我主动提醒空雨按时吃晚饭，并继续陪他讨论晚间安排",
            "topics": ["晚间陪伴"],
            "key_facts": ["我主动提醒空雨按时吃晚饭"],
            "key_fact_evidence": [
                {"fact_index": 0, "message_refs": ["M2"]}
            ],
            "key_fact_attributions": [
                {
                    "fact_index": 0,
                    "subject_refs": [
                        {
                            "actor_ref": "A1",
                            "display_name_snapshot": "Bot",
                        }
                    ],
                    "claim_type": "speaker_self",
                    "confidence": 1.0,
                }
            ],
            "key_fact_profiles": [
                {
                    "fact_index": 0,
                    "fact_type": "relationship_interaction",
                    "durability": "medium",
                    "selection_reason": "relationship_significance",
                }
            ],
            "message_coverage": [
                {
                    "message_ref": "M1",
                    "disposition": "context",
                    "fact_indexes": [],
                    "reason_code": "supporting_context",
                    "reason": "用户说明晚间安排",
                },
                {
                    "message_ref": "M2",
                    "disposition": "fact",
                    "fact_indexes": [0],
                    "reason_code": "durable_interaction",
                    "reason": "Bot 主动提供持续关系支持",
                },
            ],
            "sentiment": "positive",
            "importance": 0.6,
        },
        messages=messages,
        require_source_grounding=True,
        role_bindings=role_bindings,
        actor_refs=actor_refs,
    )

    assert report.acceptable is True, [issue.code for issue in report.errors]


def test_durable_interaction_requires_relationship_fact_profile():
    processor = MemoryProcessor(llm_provider=Mock(), context=None)
    report = processor.assess_summary_quality(
        {
            "memory_decision": "store",
            "no_memory_reason": "none",
            "summary": "我记录了张三明天下午三点开会",
            "topics": ["会议"],
            "key_facts": ["张三明天下午三点开会"],
            "key_fact_evidence": [
                {"fact_index": 0, "message_refs": ["M1"]}
            ],
            "key_fact_attributions": [
                {
                    "fact_index": 0,
                    "subject_refs": [
                        {
                            "actor_ref": "A2",
                            "display_name_snapshot": "张三",
                        }
                    ],
                    "claim_type": "speaker_self",
                    "confidence": 1.0,
                }
            ],
            "key_fact_profiles": [
                {
                    "fact_index": 0,
                    "fact_type": "observation",
                    "durability": "medium",
                    "selection_reason": "future_utility",
                }
            ],
            "message_coverage": [
                {
                    "message_ref": "M1",
                    "disposition": "fact",
                    "fact_indexes": [0],
                    "reason_code": "durable_interaction",
                    "reason": "错误地按关系互动分类",
                },
                {
                    "message_ref": "M2",
                    "disposition": "context",
                    "fact_indexes": [],
                    "reason_code": "routine_response",
                    "reason": "确认回复",
                },
            ],
            "sentiment": "neutral",
            "importance": 0.6,
        },
        messages=_make_messages(),
        require_source_grounding=True,
    )

    assert "durable_interaction_without_profile" in {
        issue.code for issue in report.errors
    }


def test_summary_quality_rejects_unanchored_relative_fact_when_grounding_required():
    processor = MemoryProcessor(llm_provider=Mock(), context=None)
    report = processor.assess_summary_quality(
        {
            "summary": "我记录了张三的会议安排",
            "topics": ["会议"],
            "key_facts": ["张三两小时前确认了会议"],
            "key_fact_evidence": [{"fact_index": 0, "message_refs": ["M1"]}],
            "message_coverage": [
                {
                    "message_ref": "M1",
                    "disposition": "fact",
                    "fact_indexes": [0],
                    "reason": "会议来源",
                },
                {
                    "message_ref": "M2",
                    "disposition": "context",
                    "fact_indexes": [],
                    "reason": "确认回复",
                },
            ],
            "sentiment": "neutral",
            "importance": 0.6,
        },
        messages=_make_messages(),
        require_source_grounding=True,
    )

    assert "relative_fact_time_without_absolute_anchor" in {
        issue.code for issue in report.errors
    }


@pytest.mark.asyncio
async def test_source_grounding_contract_marks_unknown_message_reference_low():
    response = """{
        "summary":"张三说明天下午三点开会，我确认会进行提醒",
        "topics":["会议提醒"],
        "key_facts":["张三明天下午三点开会"],
        "key_fact_evidence":[{"fact_index":0,"message_refs":["M99"]}],
        "message_coverage":[
            {"message_ref":"M1","disposition":"fact","fact_indexes":[0],"reason":"会议时间来源"},
            {"message_ref":"M2","disposition":"context","fact_indexes":[],"reason":"Bot确认提醒"}
        ],
        "sentiment":"neutral",
        "importance":0.7
    }"""
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(response),
        context=None,
        config={"timeline_require_source_grounding": True},
    )

    _, metadata, _ = await processor.process_conversation(_make_messages())
    assert "unknown_fact_evidence" in {
        issue["code"] for issue in metadata["summary_quality_report"]["issues"]
    }
    assert metadata["summary_quality"] == "low"


@pytest.mark.asyncio
async def test_summary_quality_marks_empty_summary_low_after_repair():
    llm = _DummyLLMProvider(
        """{
            "summary":"",
            "topics":["闲聊"],
            "key_facts":["用户问候"],
            "sentiment":"neutral",
            "importance":0.5
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )
    assert content
    assert metadata["summary_quality"] == "low"


@pytest.mark.asyncio
async def test_summary_quality_marks_missing_key_facts_low_after_repair():
    llm = _DummyLLMProvider(
        """{
            "summary":"用户进行了一次普通对话",
            "topics":["闲聊"],
            "key_facts":[],
            "sentiment":"neutral",
            "importance":0.5
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )
    assert metadata["summary_quality"] == "low"


@pytest.mark.asyncio
async def test_summary_quality_marks_generic_terms_low_after_repair():
    llm = _DummyLLMProvider(
        """{
            "summary":"某用户提到了一些事情",
            "topics":["闲聊"],
            "key_facts":["某用户说了话"],
            "sentiment":"neutral",
            "importance":0.5
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )
    assert metadata["summary_quality"] == "low"


def test_validate_summary_quality_directly():
    """直接测试 _validate_summary_quality 的各种边界情况。"""
    from unittest.mock import MagicMock

    processor = MemoryProcessor(llm_provider=MagicMock(), context=None)

    # 正常情况
    assert (
        processor._validate_summary_quality(
                {
                    "summary": "张三明确表示喜欢吃寿司",
                    "topics": ["饮食偏好"],
                    "key_facts": ["张三喜欢寿司"],
                    "sentiment": "positive",
                    "importance": 0.7,
                }
        )
        == "normal"
    )

    # summary 过短
    assert (
        processor._validate_summary_quality(
            {
                "summary": "短",
                "key_facts": ["fact"],
                "importance": 0.5,
            }
        )
        == "low"
    )

    # importance 超出范围
    assert (
        processor._validate_summary_quality(
            {
                "summary": "用户明确表示喜欢吃寿司",
                "key_facts": ["用户喜欢寿司"],
                "importance": 1.5,
            }
        )
        == "low"
    )

    # 泛化词检测
    assert (
        processor._validate_summary_quality(
            {
                "summary": "有人提到了一些事情",
                "key_facts": ["有人说话"],
                "importance": 0.5,
            }
        )
        == "low"
    )


def test_build_memory_from_structured_data_uses_standard_storage_format():
    processor = MemoryProcessor(llm_provider=Mock(), context=None)

    content, metadata, importance = processor.build_memory_from_structured_data(
        {
            "summary": "用户希望主动记忆工具复用自动总结格式",
            "topics": ["LivingMemory", "主动记忆"],
            "key_facts": ["主动记忆应复用 MemoryProcessor 格式化流程"],
            "sentiment": "neutral",
            "importance": 0.8,
        },
        is_group_chat=False,
        fallback_excerpt="fallback",
    )

    assert content == metadata["canonical_summary"]
    assert metadata["persona_summary"] == "用户希望主动记忆工具复用自动总结格式"
    assert metadata["topics"] == ["LivingMemory", "主动记忆"]
    assert metadata["key_facts"] == ["主动记忆应复用 MemoryProcessor 格式化流程"]
    assert metadata["sentiment"] == "neutral"
    assert metadata["interaction_type"] == "private_chat"
    assert metadata["summary_schema_version"] == "v6-fact-selection"
    assert metadata["summary_quality"] == "normal"
    assert importance == 0.8


def test_build_memory_from_structured_data_flags_low_quality_for_out_of_range_importance():
    """与自动总结路径一致：原始 importance 越界时应判为 low quality。"""
    processor = MemoryProcessor(llm_provider=Mock(), context=None)

    _, metadata, importance = processor.build_memory_from_structured_data(
        {
            "summary": "用户希望主动记忆工具复用自动总结格式",
            "topics": ["测试"],
            "key_facts": ["importance 越界"],
            "sentiment": "neutral",
            "importance": 1.5,
        },
        is_group_chat=False,
        fallback_excerpt="fallback",
    )

    assert metadata["summary_quality"] == "low"
    assert importance == 1.0


# ── 群聊路径测试 ──────────────────────────────────────────────────────────────


def _make_group_messages():
    """构造一组群聊消息（含 group_id）"""
    return [
        Message(
            id=1,
            session_id="aiocqhttp:GroupMessage:88888",
            role="user",
            content="大家觉得 AI 工具怎么样？",
            sender_id="10001",
            sender_name="张三",
            group_id="88888",
            platform="aiocqhttp",
            metadata={},
        ),
        Message(
            id=2,
            session_id="aiocqhttp:GroupMessage:88888",
            role="user",
            content="我觉得 ChatGPT 写代码效率提升了 30%",
            sender_id="10002",
            sender_name="李四",
            group_id="88888",
            platform="aiocqhttp",
            metadata={},
        ),
        Message(
            id=3,
            session_id="aiocqhttp:GroupMessage:88888",
            role="assistant",
            content="AI 工具确实能提升效率，但需要仔细审查生成的代码",
            sender_id="bot",
            sender_name="Bot",
            group_id="88888",
            platform="aiocqhttp",
            metadata={"is_bot_message": True},
        ),
    ]


@pytest.mark.asyncio
async def test_process_group_chat_sets_interaction_type():
    """群聊路径应将 interaction_type 设置为 group_chat。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"群聊讨论了 AI 工具的使用效果",
            "topics":["AI工具","工作效率"],
            "key_facts":["张三认为 ChatGPT 效率提升 30%","需要仔细审查 AI 生成代码"],
            "participants":["张三","李四"],
            "sentiment":"positive",
            "importance":0.75
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, importance = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert metadata["interaction_type"] == "group_chat"
    assert importance == 0.75


@pytest.mark.asyncio
async def test_process_group_chat_extracts_participants_from_message_identity():
    """Participant display data must come from messages, not LLM inventions."""
    llm = _DummyLLMProvider(
        """{
            "summary":"群聊讨论了 AI 工具的使用效果",
            "topics":["AI工具"],
            "key_facts":["张三认为 ChatGPT 效率提升 30%"],
            "participants":["张三","李四","王五"],
            "sentiment":"positive",
            "importance":0.7
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert "participants" in metadata
    assert "张三" in metadata["participants"]
    assert "李四" in metadata["participants"]
    assert "王五" not in metadata["participants"]
    assert "我(Bot: Bot)" in metadata["participants"]
    assert metadata["role_bindings"]["narrator_actor_id"] == "qq:assistant:bot"
    assert {
        actor["actor_id"] for actor in metadata["role_bindings"]["actors"]
    } == {
        "qq:human:10001",
        "qq:human:10002",
        "qq:assistant:bot",
    }


@pytest.mark.asyncio
async def test_process_group_chat_dual_channel_summary():
    """群聊路径也应生成双通道摘要（canonical_summary + persona_summary）。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"群聊讨论了 AI 工具的使用效果，建议内部部署私有化 LLM",
            "topics":["AI工具","数据安全"],
            "key_facts":["建议公司内部部署私有化 LLM","注意数据安全"],
            "participants":["张三","李四"],
            "sentiment":"positive",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert "canonical_summary" in metadata
    assert "persona_summary" in metadata
    assert metadata.get("summary_schema_version") == "v6-fact-selection"
    # canonical_summary 应包含 key_facts
    assert "私有化 LLM" in metadata["canonical_summary"]
    # content 应等于 canonical_summary
    assert content == metadata["canonical_summary"]


@pytest.mark.asyncio
async def test_process_group_chat_missing_llm_participants_uses_actor_snapshot():
    """Missing LLM participants must not discard deterministic speakers."""
    llm = _DummyLLMProvider(
        """{
            "summary":"群聊讨论了一些话题",
            "topics":["闲聊"],
            "key_facts":["大家聊了很多"],
            "sentiment":"neutral",
            "importance":0.5
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    # 缺少 participants 时应补充默认空列表
    assert "participants" in metadata
    assert isinstance(metadata["participants"], list)
    assert metadata["participants"] == ["张三", "李四", "我(Bot: Bot)"]


@pytest.mark.asyncio
async def test_process_private_chat_no_participants_field():
    """私聊路径不应在 metadata 中包含 participants 字段。"""
    llm = _DummyLLMProvider(
        """{
            "summary":"用户告知明天下午三点有重要会议",
            "topics":["会议"],
            "key_facts":["明天下午三点开会"],
            "sentiment":"neutral",
            "importance":0.8
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert "participants" not in metadata
    assert metadata["interaction_type"] == "private_chat"


@pytest.mark.asyncio
async def test_process_group_chat_long_content():
    """群聊长内容（多条消息）应正常处理，不崩溃。"""
    long_messages = []
    for i in range(20):
        long_messages.append(
            Message(
                id=i + 1,
                session_id="aiocqhttp:GroupMessage:99999",
                role="user",
                content=f"成员{i % 5} 说：这是第 {i + 1} 条消息，内容比较详细，包含了很多信息。"
                * 3,
                sender_id=str(10000 + i % 5),
                sender_name=f"成员{i % 5}",
                group_id="99999",
                platform="aiocqhttp",
                metadata={},
            )
        )

    llm = _DummyLLMProvider(
        """{
            "summary":"群聊成员进行了多轮讨论，涉及多个话题",
            "topics":["群聊","讨论"],
            "key_facts":["多名成员参与讨论","讨论内容丰富"],
            "participants":["成员0","成员1","成员2","成员3","成员4"],
            "sentiment":"neutral",
            "importance":0.6
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, importance = await processor.process_conversation(
        messages=long_messages,
        is_group_chat=True,
        persona_id=None,
    )

    assert isinstance(content, str) and len(content) > 0
    assert metadata["interaction_type"] == "group_chat"
    assert len(metadata["participants"]) == 5
    assert 0.0 <= importance <= 1.0


@pytest.mark.asyncio
async def test_process_group_chat_retains_generic_terms_as_low_quality():
    llm = _DummyLLMProvider(
        """{
            "summary":"某用户在群里说了一些话",
            "topics":["闲聊"],
            "key_facts":["有人说话了"],
            "participants":["某用户"],
            "sentiment":"neutral",
            "importance":0.4
        }"""
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )
    assert metadata["summary_quality"] == "low"


def test_format_conversation_sanitizes_multimodal_private_message():
    processor = MemoryProcessor(llm_provider=None, context=None)
    message = Message(
        id=1,
        session_id="s1",
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            {"type": "text", "text": "这张图里有会议安排"},
        ],
        sender_id="u1",
        sender_name="张三",
        group_id=None,
        platform="test",
        metadata={},
    )

    formatted = processor._format_conversation([message])

    assert "这张图里有会议安排" in formatted
    assert "image_url" not in formatted
    assert "example.test" not in formatted


def test_format_conversation_uses_placeholder_for_image_only_group_message():
    processor = MemoryProcessor(llm_provider=None, context=None)
    message = Message(
        id=1,
        session_id="g1",
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
        ],
        sender_id="u1",
        sender_name="张三",
        group_id="group1",
        platform="test",
        metadata={},
    )

    formatted = processor._format_conversation([message])

    assert "张三" in formatted
    assert "[图片消息]" in formatted
    assert "image_url" not in formatted
