from __future__ import annotations

import json
import re
import time
from types import SimpleNamespace

import pytest

from astrbot_plugin_livingmemory.core.managers.user_profile_fact_maintainer import (
    UserProfileFactMaintainer,
    UserProfileFactMaintenancePlan,
    UserProfileFactValidationError,
)
from astrbot_plugin_livingmemory.core.managers.user_profile_maintenance_manager import (
    UserProfileMaintenanceManager,
)
from astrbot_plugin_livingmemory.core.models.user_profile import (
    UserProfileFact,
    UserProfileFactCategory,
    UserProfileFactSource,
    UserProfileFactStatus,
    UserProfileProjectionEvent,
)
from astrbot_plugin_livingmemory.core.user_profile_settings import (
    effective_user_profile_settings,
)
from astrbot_plugin_livingmemory.storage.user_profile_store import UserProfileStore


class _AcceptingProvider:
    provider_config = {"id": "profile-test", "model": "deterministic"}

    async def text_chat(self, *, prompt, system_prompt, **kwargs):
        match = re.search(
            r"Candidate sources: (.*?)\nHistorical behavior evidence:",
            prompt,
            re.S,
        )
        candidates = json.loads(match.group(1))
        operations = [
            {
                "source_uid": item["source_uid"],
                "operation": "accept_new",
                "category": "preference",
                "confidence": 0.95,
                "importance": 0.8,
                "profile_value": 0.9,
                "inference_kind": "explicit",
                "sensitive": False,
                "reason": "explicit self report",
            }
            for item in candidates
        ]
        return SimpleNamespace(completion_text=json.dumps({"operations": operations}))


class _ContractCorrectionProvider:
    provider_config = {"id": "correction-test", "model": "deterministic"}

    def __init__(self, invalid_responses: int):
        self.invalid_responses = invalid_responses
        self.prompts = []

    async def text_chat(self, *, prompt, system_prompt, **kwargs):
        self.prompts.append(prompt)
        if len(self.prompts) <= self.invalid_responses:
            source_uid = f"invented-source-{len(self.prompts)}"
        else:
            match = re.search(
                r"Candidate sources: (.*?)\nHistorical behavior evidence:",
                prompt,
                re.S,
            )
            source_uid = json.loads(match.group(1))[0]["source_uid"]
        return SimpleNamespace(
            completion_text=json.dumps(
                {"operations": [{"source_uid": source_uid, "operation": "ignore"}]}
            )
        )


def _timeline_payload(*, revision: int = 1, facts: list[str] | None = None):
    facts = ["Alice明确说自己喜欢无糖茶"] if facts is None else facts
    actor_id = "test:human:user-1"
    timestamp = time.time() + revision
    return {
        "profile_actor_id": actor_id,
        "metadata": {
            "memory_uid": "timeline-1",
            "revision": revision,
            "session_id": "bot-1:private:user-1",
            "persona_id": "persona-1",
            "memory_space_id": "space-1",
            "memory_layer": "timeline",
            "importance": 0.7,
            "create_time": timestamp,
            "updated_at": timestamp,
            "key_facts": facts,
            "key_fact_attributions": [
                {
                    "fact_index": index,
                    "subject_refs": [{"actor_id": actor_id}],
                    "claim_type": "speaker_self",
                    "confidence": 1.0,
                    "attribution_status": "verified",
                }
                for index in range(len(facts))
            ],
            "key_fact_profiles": [
                {
                    "fact_index": index,
                    "fact_type": "preference",
                    "durability": "high",
                    "selection_reason": "stable_personal_fact",
                }
                for index in range(len(facts))
            ],
            "role_bindings": {
                "actors": [
                    {
                        "actor_id": actor_id,
                        "actor_type": "human",
                        "platform": "test",
                        "sender_id": "user-1",
                        "observed_names": ["Alice"],
                    }
                ]
            },
        },
    }


def test_candidate_extraction_requires_exact_current_actor():
    payload = _timeline_payload()["metadata"]
    payload["key_facts"].extend(["Bob喜欢咖啡", "某人住在上海"])
    payload["key_fact_attributions"].extend(
        [
            {
                "fact_index": 1,
                "subject_refs": [{"actor_id": "test:human:user-2"}],
                "claim_type": "speaker_reports_other",
                "confidence": 1.0,
                "attribution_status": "verified",
            },
            {
                "fact_index": 2,
                "subject_refs": [{"actor_id": ""}],
                "claim_type": "speaker_self",
                "confidence": 0.5,
                "attribution_status": "unresolved",
            },
        ]
    )

    candidates = UserProfileFactMaintainer.extract_candidates(
        payload, actor_id="test:human:user-1"
    )

    assert [item.raw_fact for item in candidates] == ["Alice明确说自己喜欢无糖茶"]
    assert candidates[0].actor_id == "test:human:user-1"


def test_legacy_summary_candidates_are_always_pending_and_cannot_supersede():
    payload = {
        "profile_actor_id": "test:human:user-1",
        "identity_resolution": {
            "identity_basis": "legacy_private_session",
            "evidence_basis": "timeline_summary_only",
            "source_granularity": "timeline",
        },
        "metadata": {
            "memory_uid": "legacy-timeline",
            "revision": 1,
            "create_time": 1000,
            "key_facts": ["旧摘要称用户偏好简短回答"],
        },
    }
    candidates = UserProfileFactMaintainer.extract_candidates(
        payload,
        actor_id="test:human:user-1",
        legacy_attribution_confidence=0.45,
    )
    assert len(candidates) == 1
    assert candidates[0].claim_type == "legacy_summary_candidate"
    assert candidates[0].metadata["evidence_basis"] == "timeline_summary_only"

    plan = UserProfileFactMaintainer()._validate_plan(
        fact_namespace_uid="facts-1",
        payload={
            "operations": [
                {
                    "source_uid": candidates[0].source_uid,
                    "operation": "accept_new",
                    "category": "communication_preference",
                    "confidence": 0.99,
                    "importance": 0.8,
                    "profile_value": 0.9,
                    "inference_kind": "explicit",
                }
            ]
        },
        candidates=candidates,
        existing_facts=[],
        settings=effective_user_profile_settings(),
    )
    assert len(plan.facts) == 1
    assert plan.facts[0].status == UserProfileFactStatus.PENDING
    assert plan.facts[0].confidence == pytest.approx(0.45)


def test_candidate_extraction_marks_one_off_events_as_behavior_evidence():
    metadata = _timeline_payload(facts=["stable preference", "ordinary event"])[
        "metadata"
    ]
    metadata["key_fact_profiles"] = [
        {
            "fact_index": 0,
            "fact_type": "preference",
            "durability": "high",
            "selection_reason": "stable_personal_fact",
        },
        {
            "fact_index": 1,
            "fact_type": "event",
            "durability": "low",
            "selection_reason": "completed_action",
        },
    ]
    candidates = UserProfileFactMaintainer.extract_candidates(
        metadata, actor_id="test:human:user-1"
    )
    assert [item.metadata["profile_signal"] for item in candidates] == [
        "profile_direct",
        "behavior_evidence",
    ]

    plan = UserProfileFactMaintainer()._validate_plan(
        fact_namespace_uid="facts-1",
        payload={
            "operations": [
                {
                    "source_uid": candidates[1].source_uid,
                    "operation": "accept_new",
                    "category": "current_state",
                    "confidence": 0.99,
                    "importance": 0.9,
                    "profile_value": 0.99,
                    "inference_kind": "explicit",
                },
                {
                    "source_uid": candidates[0].source_uid,
                    "operation": "ignore",
                },
            ]
        },
        candidates=candidates,
        existing_facts=[],
        settings=effective_user_profile_settings(),
    )
    assert plan.facts == []
    assert set(plan.ignored_source_uids) == {
        candidates[0].source_uid,
        candidates[1].source_uid,
    }
    assert plan.diagnostics["policy_rejections"] == {"one_off_behavior": 1}


def test_medium_durability_situational_preference_is_behavior_evidence():
    metadata = _timeline_payload(facts=["situational choice"])["metadata"]
    metadata["key_fact_profiles"] = [
        {
            "fact_index": 0,
            "fact_type": "preference",
            "durability": "medium",
            "selection_reason": "future_utility",
        }
    ]
    candidates = UserProfileFactMaintainer.extract_candidates(
        metadata, actor_id="test:human:user-1"
    )
    assert candidates[0].metadata["profile_signal"] == "behavior_evidence"


@pytest.mark.asyncio
async def test_fact_contract_rejects_secret_even_when_model_accepts():
    source = UserProfileFactSource(
        timeline_uid="timeline-secret",
        timeline_revision=1,
        fact_index=0,
        raw_fact="我的 API key 不应进入画像",
        actor_id="test:human:user-1",
    )
    provider = _AcceptingProvider()
    plan = await UserProfileFactMaintainer(provider).maintain(
        fact_namespace_uid="facts-1",
        candidates=[source],
        existing_facts=[],
        settings=effective_user_profile_settings(),
    )

    assert not plan.facts
    assert plan.ignored_source_uids == [source.source_uid]
    assert source.raw_fact not in json.dumps(plan.diagnostics, ensure_ascii=False)


@pytest.mark.asyncio
async def test_fact_contract_correction_repeats_allowed_uid_whitelist():
    source = UserProfileFactSource(
        timeline_uid="timeline-correction",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户明确表示偏好简洁回答",
        actor_id="test:human:user-1",
    )
    provider = _ContractCorrectionProvider(invalid_responses=2)

    plan = await UserProfileFactMaintainer(provider).maintain(
        fact_namespace_uid="facts-1",
        candidates=[source],
        existing_facts=[],
        settings=effective_user_profile_settings(
            {"user_profile.contract_correction_retries": 2}
        ),
    )

    assert len(provider.prompts) == 3
    assert plan.ignored_source_uids == [source.source_uid]
    assert plan.diagnostics["contract_correction_used"] is True
    assert plan.diagnostics["contract_correction_attempts"] == 2
    assert all(
        source.source_uid in prompt
        and "Allowed candidate source_uids" in prompt
        and "Never invent, shorten, or substitute an identifier" in prompt
        for prompt in provider.prompts[1:]
    )


@pytest.mark.asyncio
async def test_fact_contract_correction_can_be_disabled():
    source = UserProfileFactSource(
        timeline_uid="timeline-no-correction",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户明确表示偏好简洁回答",
        actor_id="test:human:user-1",
    )
    provider = _ContractCorrectionProvider(invalid_responses=1)

    with pytest.raises(UserProfileFactValidationError, match="unknown source_uid"):
        await UserProfileFactMaintainer(provider).maintain(
            fact_namespace_uid="facts-1",
            candidates=[source],
            existing_facts=[],
            settings=effective_user_profile_settings(
                {"user_profile.contract_correction_retries": 0}
            ),
        )

    assert len(provider.prompts) == 1


def test_behavioral_inference_uses_actual_sources_not_reported_counts():
    source = UserProfileFactSource(
        timeline_uid="timeline-1",
        timeline_revision=1,
        fact_index=0,
        raw_fact="Alice本次要求回答简短",
        actor_id="test:human:user-1",
        evidence_started_at=1000,
        evidence_ended_at=1000,
        metadata={"profile_signal": "behavior_pattern"},
    )
    maintainer = UserProfileFactMaintainer()
    plan = maintainer._validate_plan(
        fact_namespace_uid="facts-1",
        payload={
            "operations": [
                {
                    "source_uid": source.source_uid,
                    "operation": "accept_new",
                    "category": "communication_preference",
                    "confidence": 0.99,
                    "importance": 0.8,
                    "profile_value": 0.9,
                    "inference_kind": "behavioral_inference",
                    "independent_timeline_count": 99,
                    "evidence_span_days": 999,
                }
            ]
        },
        candidates=[source],
        existing_facts=[],
        settings=effective_user_profile_settings(),
    )
    assert plan.facts == []
    assert plan.ignored_source_uids == [source.source_uid]
    assert plan.diagnostics["policy_rejections"] == {
        "insufficient_inference_timelines": 1
    }


def test_fact_contract_requires_profile_value_and_restricts_behavior_support():
    primary = UserProfileFactSource(
        timeline_uid="timeline-primary",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户明确表示偏好简洁回答",
        actor_id="test:human:user-1",
        metadata={"profile_signal": "profile_direct"},
    )
    evidence = UserProfileFactSource(
        timeline_uid="timeline-evidence",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户本次要求回答简短",
        actor_id="test:human:user-1",
        metadata={"profile_signal": "behavior_evidence"},
    )
    maintainer = UserProfileFactMaintainer()
    base = {
        "source_uid": primary.source_uid,
        "operation": "accept_new",
        "category": "preference",
        "confidence": 0.95,
        "importance": 0.8,
        "inference_kind": "explicit",
    }
    with pytest.raises(UserProfileFactValidationError, match="profile_value"):
        maintainer._validate_plan(
            fact_namespace_uid="facts-1",
            payload={"operations": [base]},
            candidates=[primary],
            existing_facts=[],
            settings=effective_user_profile_settings(),
        )

    plan = maintainer._validate_plan(
        fact_namespace_uid="facts-1",
        payload={
            "operations": [
                {
                    **base,
                    "profile_value": 0.9,
                    "supporting_source_uids": [evidence.source_uid],
                }
            ]
        },
        candidates=[primary],
        supporting_evidence=[evidence],
        existing_facts=[],
        settings=effective_user_profile_settings(),
    )
    assert plan.facts == []
    assert set(plan.ignored_source_uids) == {primary.source_uid, evidence.source_uid}
    assert plan.diagnostics["policy_rejections"] == {"invalid_behavior_support_mode": 1}


def test_standalone_historical_evidence_operation_is_ignored_safely():
    candidate = UserProfileFactSource(
        timeline_uid="timeline-current",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户明确偏好简洁回答",
        actor_id="test:human:user-1",
        metadata={"profile_signal": "profile_direct"},
    )
    historical = UserProfileFactSource(
        timeline_uid="timeline-history",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户本次要求回答简短",
        actor_id="test:human:user-1",
        metadata={"profile_signal": "behavior_evidence"},
    )
    plan = UserProfileFactMaintainer()._validate_plan(
        fact_namespace_uid="facts-1",
        payload={
            "operations": [
                {
                    "source_uid": historical.source_uid,
                    "operation": "ignore",
                },
                {
                    "source_uid": candidate.source_uid,
                    "operation": "ignore",
                },
            ]
        },
        candidates=[candidate],
        supporting_evidence=[historical],
        existing_facts=[],
        settings=effective_user_profile_settings(),
    )
    assert plan.ignored_source_uids == [candidate.source_uid]
    assert plan.diagnostics["policy_rejections"] == {
        "standalone_historical_evidence": 1
    }


class _CrossBatchInferenceProvider:
    provider_config = {"id": "profile-test", "model": "deterministic"}

    async def text_chat(self, *, prompt, system_prompt, **kwargs):
        candidates = json.loads(
            re.search(
                r"Candidate sources: (.*?)\nHistorical behavior evidence:",
                prompt,
                re.S,
            ).group(1)
        )
        evidence = json.loads(
            re.search(
                r"Historical behavior evidence: (.*?)\nOutput shape:",
                prompt,
                re.S,
            ).group(1)
        )
        operations = []
        for item in candidates:
            if item["profile_signal"] == "behavior_pattern":
                operations.append(
                    {
                        "source_uid": item["source_uid"],
                        "supporting_source_uids": [
                            row["source_uid"] for row in evidence
                        ],
                        "operation": "accept_new",
                        "category": "habit",
                        "confidence": 0.95,
                        "importance": 0.8,
                        "profile_value": 0.9,
                        "inference_kind": "behavioral_inference",
                        "sensitive": False,
                    }
                )
            else:
                operations.append(
                    {"source_uid": item["source_uid"], "operation": "ignore"}
                )
        return SimpleNamespace(completion_text=json.dumps({"operations": operations}))


def _behavior_payload(timeline_uid: str, timestamp: float, *, pattern: bool):
    payload = _timeline_payload(
        facts=["repeated behavior" if pattern else "one occurrence"]
    )
    metadata = payload["metadata"]
    metadata["memory_uid"] = timeline_uid
    metadata["create_time"] = timestamp
    metadata["updated_at"] = timestamp
    metadata["key_fact_temporal"] = [
        {"fact_index": 0, "start_at": timestamp, "end_at": timestamp}
    ]
    metadata["key_fact_profiles"] = [
        {
            "fact_index": 0,
            "fact_type": "event",
            "durability": "high" if pattern else "low",
            "selection_reason": (
                "repeated_completed_action" if pattern else "completed_action"
            ),
        }
    ]
    return payload


@pytest.mark.asyncio
async def test_behavioral_inference_can_use_evidence_from_prior_batches(tmp_path):
    store = UserProfileStore(str(tmp_path / "cross-batch.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    assert scope is not None
    manager = UserProfileMaintenanceManager(
        store,
        provider=_CrossBatchInferenceProvider(),
        config=effective_user_profile_settings(
            {"user_profile.maintenance_batch_timeline_limit": 1}
        ),
    )
    base = 2_000_000_000.0
    for index, days in enumerate((0, 8, 16), start=1):
        payload = _behavior_payload(
            f"timeline-{index}", base + days * 86400, pattern=index == 3
        )
        await store.enqueue_projection_event(
            UserProfileProjectionEvent(
                timeline_uid=f"timeline-{index}",
                timeline_revision=1,
                operation="upsert",
                memory_space_id="space-1",
                profile_scope_uid=scope.profile_scope_uid,
                payload=payload,
            )
        )
        await manager.drain_scope(scope.profile_scope_uid)

    facts = await store.list_serving_facts(scope.fact_namespace_uid)
    assert len(facts) == 1
    assert facts[0]["category"] == "habit"
    assert facts[0]["inference_kind"] == "behavioral_inference"
    remaining = await store.list_unassigned_behavior_evidence(scope.profile_scope_uid)
    assert remaining == []
    await manager.close()


def test_lifecycle_marks_expired_historical_facts_without_staling_pins():
    now = time.time()
    expired_source = UserProfileFactSource(
        timeline_uid="timeline-expired",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户曾有一个短期计划",
        actor_id="test:human:user-1",
        evidence_ended_at=now - 10 * 86400,
        metadata={
            "fact_temporal": {
                "event_ended_at": now - 10 * 86400,
            }
        },
    )
    ordinary = UserProfileFact(
        fact_namespace_uid="facts-1",
        category=UserProfileFactCategory.PLAN_COMMITMENT,
        representative_source_uid=expired_source.source_uid,
    )
    pinned = UserProfileFact(
        fact_namespace_uid="facts-1",
        category=UserProfileFactCategory.PLAN_COMMITMENT,
        representative_source_uid=expired_source.source_uid,
        pinned=True,
    )
    plan = UserProfileFactMaintenancePlan(facts=[ordinary, pinned])

    UserProfileMaintenanceManager._apply_lifecycle(
        plan,
        [expired_source],
        effective_user_profile_settings(),
    )

    assert ordinary.status == UserProfileFactStatus.STALE
    assert pinned.status == UserProfileFactStatus.ACTIVE


@pytest.mark.asyncio
async def test_projection_task_replaces_revisions_and_delete_withdraws_sources(
    tmp_path,
):
    store = UserProfileStore(str(tmp_path / "profile-phase2.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
        display_name="Alice",
    )
    assert scope is not None
    settings = effective_user_profile_settings(
        {
            "user_profile.maintenance_retry_base_seconds": 5,
            "user_profile.maintenance_retry_max_seconds": 60,
        }
    )
    manager = UserProfileMaintenanceManager(
        store,
        provider=_AcceptingProvider(),
        config=settings,
    )

    await store.enqueue_projection_event(
        UserProfileProjectionEvent(
            timeline_uid="timeline-1",
            timeline_revision=1,
            operation="upsert",
            memory_space_id="space-1",
            profile_scope_uid=scope.profile_scope_uid,
            payload=_timeline_payload(revision=1),
        )
    )
    await manager.drain_scope(scope.profile_scope_uid)
    serving = await store.list_serving_facts(scope.fact_namespace_uid)
    assert [item["raw_fact"] for item in serving] == ["Alice明确说自己喜欢无糖茶"]

    await store.enqueue_projection_event(
        UserProfileProjectionEvent(
            timeline_uid="timeline-1",
            timeline_revision=2,
            operation="upsert",
            memory_space_id="space-1",
            profile_scope_uid=scope.profile_scope_uid,
            payload=_timeline_payload(
                revision=2, facts=["Alice明确说自己现在喜欢黑咖啡"]
            ),
        )
    )
    await manager.drain_scope(scope.profile_scope_uid)
    serving = await store.list_serving_facts(scope.fact_namespace_uid)
    assert [item["raw_fact"] for item in serving] == ["Alice明确说自己现在喜欢黑咖啡"]

    await store.enqueue_projection_event(
        UserProfileProjectionEvent(
            timeline_uid="timeline-1",
            timeline_revision=2,
            operation="delete",
            memory_space_id="space-1",
            profile_scope_uid=scope.profile_scope_uid,
            payload={"profile_actor_id": "test:human:user-1"},
        )
    )
    await manager.drain_scope(scope.profile_scope_uid)
    assert await store.list_serving_facts(scope.fact_namespace_uid) == []
    historical = await store.list_facts_for_maintenance(scope.fact_namespace_uid)
    assert historical
    assert all(item["status"] == "archived" for item in historical)
    await manager.close()
