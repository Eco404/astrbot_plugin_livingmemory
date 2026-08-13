from __future__ import annotations

import json
import re
import sys
import time
from types import SimpleNamespace

import pytest

from astrbot_plugin_livingmemory.core.managers.user_profile_fact_maintainer import (
    UserProfileFactMaintainer,
    UserProfileFactMaintenancePlan,
    UserProfileFactValidationError,
)
from astrbot_plugin_livingmemory.core.managers.user_profile_behavior_synthesizer import (
    UserProfileBehaviorSynthesizer,
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


class _RetryBudgetProvider:
    def __init__(self, *, fail_with_timeout: bool = False):
        self.fail_with_timeout = fail_with_timeout
        self.request_max_retries = None

    async def text_chat(self, *, prompt, system_prompt, request_max_retries=None):
        self.request_max_retries = request_max_retries
        if self.fail_with_timeout:
            raise TimeoutError("simulated provider deadline")
        return SimpleNamespace(completion_text="ok")


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
        if "Candidate clusters:" not in prompt:
            evidence = json.loads(
                re.search(r"Evidence: (.*?)\nOutput shape:", prompt, re.S).group(1)
            )
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "candidate_clusters": [
                            {
                                "category": "habit",
                                "hypothesis": "用户近期通常重复这一行为",
                                "evidence_refs": [
                                    row["evidence_ref"] for row in evidence
                                ],
                                "reason": "repeated across timelines",
                            }
                        ]
                    }
                )
            )
        clusters = json.loads(
            re.search(
                r"Candidate clusters: (.*?)\nCandidate evidence:", prompt, re.S
            ).group(1)
        )
        return SimpleNamespace(
            completion_text=json.dumps(
                {
                    "decisions": [
                        {
                            "cluster_ref": clusters[0]["cluster_ref"],
                            "outcome": "publish",
                            "operation": "accept_new",
                            "category": "habit",
                            "derived_claim": "用户近期通常重复这一行为",
                            "evidence_refs": clusters[0]["evidence_refs"],
                            "confidence": 0.95,
                            "importance": 0.8,
                            "profile_value": 0.9,
                            "sensitive": False,
                        }
                    ],
                }
            )
        )


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
            f"timeline-{index}", base + days * 86400, pattern=False
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
    assert facts[0]["derived_claim"] == "用户近期通常重复这一行为"
    assert facts[0]["display_text"] == "用户近期通常重复这一行为"
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


def test_legacy_candidate_grouping_is_conservative_and_retains_sources():
    def source(uid, text):
        return UserProfileFactSource(
            source_uid=uid,
            timeline_uid=f"timeline-{uid}",
            timeline_revision=1,
            fact_index=0,
            raw_fact=text,
            actor_id="test:human:user-1",
            claim_type="legacy_summary_candidate",
            metadata={"evidence_basis": "timeline_summary_only"},
        )

    first = source("a", "用户 喜欢科幻小说。")
    duplicate = source("b", "用户喜欢科幻小说")
    distinct = source("c", "用户不喜欢科幻小说")

    representatives, groups = UserProfileFactMaintainer.group_legacy_candidates(
        [first, duplicate, distinct]
    )

    assert len(representatives) == 2
    assert sorted(len(group) for group in groups.values()) == [1, 2]
    assert {item.source_uid for group in groups.values() for item in group} == {
        "a",
        "b",
        "c",
    }


def test_behavior_discovery_prompt_uses_short_refs_and_readable_local_time():
    source = UserProfileFactSource(
        source_uid="source-with-a-long-persistent-identifier",
        timeline_uid="timeline-with-a-long-persistent-identifier",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户到公司后买了早餐",
        actor_id="test:human:user-1",
        evidence_started_at=1786499333.0,
        evidence_ended_at=1786499333.0,
        metadata={
            "profile_signal": "behavior_evidence",
            "fact_temporal": {"event_time_basis": "unknown"},
        },
    )

    prompt = UserProfileBehaviorSynthesizer._build_discovery_prompt(
        {"E001": source}, {}, effective_user_profile_settings()
    )

    assert '"evidence_ref": "E001"' in prompt
    assert '"timeline_ref": "T001"' in prompt
    assert "2026-08-12T09:48:53+08:00" in prompt
    assert source.source_uid not in prompt
    assert source.timeline_uid not in prompt


def test_behavior_decision_cannot_expand_discovered_cluster_evidence():
    first = UserProfileFactSource(
        source_uid="source-1",
        timeline_uid="timeline-1",
        timeline_revision=1,
        fact_index=0,
        raw_fact="first",
        actor_id="test:human:user-1",
        metadata={"profile_signal": "behavior_evidence"},
    )
    second = UserProfileFactSource(
        source_uid="source-2",
        timeline_uid="timeline-2",
        timeline_revision=1,
        fact_index=0,
        raw_fact="second",
        actor_id="test:human:user-1",
        metadata={"profile_signal": "behavior_evidence"},
    )
    clusters = [
        {
            "cluster_ref": "C001",
            "category": "habit",
            "hypothesis": "repeated action",
            "evidence_refs": ["E001"],
            "existing_profile_ref": None,
            "reason": "similar",
        }
    ]

    with pytest.raises(UserProfileFactValidationError, match="candidate cluster"):
        UserProfileBehaviorSynthesizer._decode_decision_payload(
            {
                "decisions": [
                    {
                        "cluster_ref": "C001",
                        "outcome": "accumulate",
                        "evidence_refs": ["E001", "E002"],
                    }
                ]
            },
            clusters=clusters,
            evidence_refs={"E001": first, "E002": second},
            existing_refs={},
        )


def test_behavior_decision_cannot_accept_new_for_linked_existing_pattern():
    source = UserProfileFactSource(
        source_uid="source-1",
        timeline_uid="timeline-1",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户上午到办公室",
        actor_id="test:human:user-1",
        metadata={"profile_signal": "behavior_evidence"},
    )
    clusters = [
        {
            "cluster_ref": "C001",
            "category": "habit",
            "hypothesis": "用户通常上午到办公室",
            "evidence_refs": ["E001"],
            "existing_profile_ref": "P001",
            "reason": "supports the existing pattern",
        }
    ]

    with pytest.raises(UserProfileFactValidationError, match="exact existing_profile_ref"):
        UserProfileBehaviorSynthesizer._decode_decision_payload(
            {
                "decisions": [
                    {
                        "cluster_ref": "C001",
                        "outcome": "publish",
                        "operation": "accept_new",
                        "category": "habit",
                        "derived_claim": "用户通常上午到办公室",
                        "evidence_refs": ["E001"],
                    }
                ]
            },
            clusters=clusters,
            evidence_refs={"E001": source},
            existing_refs={
                "P001": {
                    "profile_fact_uid": "fact-existing",
                    "category": "habit",
                }
            },
        )


def test_behavior_discovery_prefilter_defers_clusters_below_hard_thresholds():
    settings = effective_user_profile_settings()

    def source(ref, timeline, days):
        return (
            ref,
            UserProfileFactSource(
                source_uid=f"source-{ref}",
                timeline_uid=timeline,
                timeline_revision=1,
                fact_index=0,
                raw_fact="repeated behavior",
                actor_id="test:human:user-1",
                evidence_ended_at=2_000_000_000 + days * 86400,
                metadata={"profile_signal": "behavior_evidence"},
            ),
        )

    evidence_refs = dict(
        [
            source("E001", "timeline-1", 0),
            source("E002", "timeline-2", 8),
            source("E003", "timeline-3", 16),
            source("E004", "timeline-4", 17),
        ]
    )
    mature = {
        "cluster_ref": "C001",
        "evidence_refs": ["E001", "E002", "E003"],
    }
    short_span = {
        "cluster_ref": "C002",
        "evidence_refs": ["E002", "E003", "E004"],
    }

    ready, deferred = UserProfileBehaviorSynthesizer._partition_discovered_clusters(
        [mature, short_span], evidence_refs=evidence_refs, settings=settings
    )

    assert ready == [mature]
    assert deferred == [short_span]


def test_behavior_claim_rejects_false_minute_precision_from_observation_bounds():
    exact = UserProfileFactSource(
        timeline_uid="timeline-1",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户在09:56前后到达公司",
        actor_id="test:human:user-1",
        metadata={"profile_signal": "behavior_evidence"},
    )
    already_there = UserProfileFactSource(
        timeline_uid="timeline-2",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户到公司后买了早餐",
        actor_id="test:human:user-1",
        metadata={"profile_signal": "behavior_evidence"},
    )

    assert UserProfileBehaviorSynthesizer._uses_unsupported_minute_range(
        "通常在09:47-09:56到达公司", [exact, already_there]
    )
    assert not UserProfileBehaviorSynthesizer._uses_unsupported_minute_range(
        "通常在上午10点前后到达公司", [exact, already_there]
    )


def test_behavior_candidate_expansion_recovers_indirect_time_aligned_support():
    base = 2_000_000_000.0

    def source(ref, timeline, text, days, hour, minute):
        timestamp = base + days * 86400 + (hour * 60 + minute) * 60
        return (
            ref,
            UserProfileFactSource(
                source_uid=f"source-{ref}",
                timeline_uid=timeline,
                timeline_revision=1,
                fact_index=0,
                raw_fact=text,
                actor_id="test:human:user-1",
                evidence_ended_at=timestamp,
                metadata={"profile_signal": "behavior_evidence"},
            ),
        )

    evidence_refs = dict(
        [
            source("E001", "timeline-1", "用户上午到达办公室开始值班", 0, 9, 47),
            source("E002", "timeline-2", "用户早上到达办公室开始值班", 2, 9, 56),
            source("E003", "timeline-3", "用户到办公室后买了早餐", 16, 9, 48),
            source("E004", "timeline-4", "用户晚上到办公室取回物品", 20, 21, 30),
            source("E005", "timeline-5", "用户上午到达别处参加活动", 22, 9, 50),
        ]
    )
    clusters = [
        {
            "cluster_ref": "C001",
            "category": "habit",
            "hypothesis": "用户通常上午到达办公室开始值班",
            "evidence_refs": ["E001", "E002"],
            "existing_profile_ref": None,
            "reason": "similar action and time",
        }
    ]

    expanded, count = UserProfileBehaviorSynthesizer._expand_discovered_clusters(
        clusters,
        evidence_refs=evidence_refs,
        settings=effective_user_profile_settings(),
    )

    assert expanded[0]["evidence_refs"] == ["E001", "E002", "E003"]
    assert count == 1


def test_behavior_temporal_candidate_requires_context_time_and_span():
    base = 2_000_000_000.0

    def source(ref, timeline, text, days, hour, minute):
        return (
            ref,
            UserProfileFactSource(
                source_uid=f"source-{ref}",
                timeline_uid=timeline,
                timeline_revision=1,
                fact_index=0,
                raw_fact=text,
                actor_id="test:human:user-1",
                evidence_ended_at=(
                    base + days * 86400 + (hour * 60 + minute) * 60
                ),
                metadata={"profile_signal": "behavior_evidence"},
            ),
        )

    evidence_refs = dict(
        [
            source("E001", "timeline-1", "用户到办公室开始值班", 0, 9, 47),
            source("E002", "timeline-2", "用户到办公室开始值班", 2, 9, 56),
            source("E003", "timeline-3", "用户到办公室后买早餐", 16, 9, 48),
            source("E004", "timeline-4", "用户到办公室取物品", 20, 21, 30),
            source("E005", "timeline-5", "用户到车站参加活动", 22, 9, 50),
        ]
    )

    clusters, count = UserProfileBehaviorSynthesizer._add_temporal_candidates(
        [],
        evidence_refs=evidence_refs,
        existing_refs={},
        settings=effective_user_profile_settings(),
    )

    assert count == 1
    assert clusters[0]["evidence_refs"] == ["E001", "E002", "E003"]
    assert "办公室" in clusters[0]["hypothesis"]


def test_behavior_temporal_candidate_links_one_existing_pattern():
    base = 2_000_000_000.0
    evidence_refs = {}
    for index, days in enumerate((0, 8, 16), start=1):
        ref = f"E{index:03d}"
        evidence_refs[ref] = UserProfileFactSource(
            source_uid=f"source-{index}",
            profile_fact_uid="fact-existing" if index < 3 else None,
            timeline_uid=f"timeline-{index}",
            timeline_revision=1,
            fact_index=0,
            raw_fact="用户到办公室开始值班",
            actor_id="test:human:user-1",
            evidence_ended_at=base + days * 86400 + 9 * 3600,
            metadata={"profile_signal": "behavior_evidence"},
        )
    evidence_refs["E004"] = UserProfileFactSource(
        source_uid="source-unrelated",
        timeline_uid="timeline-unrelated",
        timeline_revision=1,
        fact_index=0,
        raw_fact="用户晚上在公园散步",
        actor_id="test:human:user-1",
        evidence_ended_at=base + 20 * 86400 + 20 * 3600,
        metadata={"profile_signal": "behavior_evidence"},
    )

    clusters, count = UserProfileBehaviorSynthesizer._add_temporal_candidates(
        [],
        evidence_refs=evidence_refs,
        existing_refs={
            "P001": {
                "profile_fact_uid": "fact-existing",
                "category": "habit",
                "status": "active",
            }
        },
        settings=effective_user_profile_settings(),
    )

    assert count == 1
    assert clusters[0]["existing_profile_ref"] == "P001"


def test_behavior_temporal_candidates_degrade_when_posseg_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "jieba.posseg", None)
    base = 2_000_000_000.0
    evidence_refs = {
        f"E{index:03d}": UserProfileFactSource(
            source_uid=f"source-{index}",
            timeline_uid=f"timeline-{index}",
            timeline_revision=1,
            fact_index=0,
            raw_fact="用户到办公室开始值班",
            actor_id="test:human:user-1",
            evidence_ended_at=base + days * 86400 + 9 * 3600,
            metadata={"profile_signal": "behavior_evidence"},
        )
        for index, days in enumerate((0, 8, 16), start=1)
    }

    clusters, count = UserProfileBehaviorSynthesizer._add_temporal_candidates(
        [],
        evidence_refs=evidence_refs,
        existing_refs={},
        settings=effective_user_profile_settings(),
    )

    assert clusters == []
    assert count == 0


class _OverlappingBehaviorCorrectionProvider:
    provider_config = {"id": "behavior-correction", "model": "deterministic"}

    def __init__(self):
        self.prompts = []

    async def text_chat(self, *, prompt, system_prompt, **kwargs):
        self.prompts.append(prompt)
        if "Candidate clusters:" not in prompt:
            evidence = json.loads(
                re.search(r"Evidence: (.*?)\nOutput shape:", prompt, re.S).group(1)
            )
            refs = [row["evidence_ref"] for row in evidence]
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "candidate_clusters": [
                            {
                                "category": "habit",
                                "hypothesis": "用户通常上午到办公室",
                                "evidence_refs": refs,
                            },
                            {
                                "category": "habit",
                                "hypothesis": "用户通常上午开始值班",
                                "evidence_refs": refs,
                            },
                        ]
                    }
                )
            )
        clusters = json.loads(
            re.search(
                r"Candidate clusters: (.*?)\nCandidate evidence:", prompt, re.S
            ).group(1)
        )
        decisions = []
        for index, cluster in enumerate(clusters):
            decisions.append(
                {
                    "cluster_ref": cluster["cluster_ref"],
                    "outcome": "publish" if index == 0 or len(self.prompts) == 2 else "discard",
                    "operation": "accept_new",
                    "category": "habit",
                    "derived_claim": "用户通常上午到办公室",
                    "evidence_refs": cluster["evidence_refs"],
                    "confidence": 0.95,
                    "importance": 0.8,
                    "profile_value": 0.9,
                    "sensitive": False,
                }
            )
        return SimpleNamespace(completion_text=json.dumps({"decisions": decisions}))


@pytest.mark.asyncio
async def test_behavior_semantic_overlap_uses_contract_correction():
    base = 2_000_000_000.0
    evidence = [
        UserProfileFactSource(
            source_uid=f"source-{index}",
            timeline_uid=f"timeline-{index}",
            timeline_revision=1,
            fact_index=0,
            raw_fact="用户上午到办公室开始值班",
            actor_id="test:human:user-1",
            evidence_ended_at=base + days * 86400,
            metadata={"profile_signal": "behavior_evidence"},
        )
        for index, days in enumerate((0, 8, 16), start=1)
    ]
    provider = _OverlappingBehaviorCorrectionProvider()

    plan = await UserProfileBehaviorSynthesizer().synthesize(
        fact_namespace_uid="facts-1",
        evidence=evidence,
        existing_facts=[],
        settings=effective_user_profile_settings(
            {"user_profile.behavior_temporal_candidate_limit": 0}
        ),
        provider=provider,
    )

    assert len(plan.facts) == 1
    assert len(provider.prompts) == 3
    assert "used by multiple patterns" in provider.prompts[-1]
    assert plan.diagnostics["contract_correction_attempts"] == 1


def test_behavior_merge_refreshes_evidence_statistics_without_rewriting_claim():
    base = 2_000_000_000.0
    existing_uid = "fact-existing"
    sources = [
        UserProfileFactSource(
            source_uid=f"source-{index}",
            profile_fact_uid=existing_uid if index < 4 else None,
            timeline_uid=f"timeline-{index}",
            timeline_revision=1,
            fact_index=0,
            raw_fact="用户上午到办公室",
            actor_id="test:human:user-1",
            evidence_ended_at=base + days * 86400,
            metadata={"profile_signal": "behavior_evidence"},
        )
        for index, days in enumerate((0, 8, 16, 17), start=1)
    ]
    claim = "用户通常在上午10点前后到达公司"
    existing = {
        "profile_fact_uid": existing_uid,
        "fact_namespace_uid": "facts-1",
        "category": "habit",
        "status": "active",
        "representative_source_uid": sources[0].source_uid,
        "derived_claim": claim,
        "confidence": 0.88,
        "importance": 0.7,
        "inference_kind": "behavioral_inference",
        "first_seen_at": base,
        "last_confirmed_at": base + 18 * 86400,
        "metadata": {
            "profile_value": 0.8,
            "independent_timeline_count": 3,
            "evidence_span_days": 16.0,
            "observation_started_at": base,
            "observation_ended_at": base + 16 * 86400,
            "synthesis_revision": 1,
        },
    }

    plan = UserProfileBehaviorSynthesizer._validate(
        fact_namespace_uid="facts-1",
        payload={
            "patterns": [
                {
                    "operation": "merge_existing",
                    "profile_fact_uid": existing_uid,
                    "category": "habit",
                    "derived_claim": claim,
                    "source_uids": [sources[-1].source_uid],
                    "confidence": 0.92,
                    "importance": 0.75,
                    "profile_value": 0.9,
                    "sensitive": False,
                }
            ],
            "accumulating_clusters": [],
        },
        evidence=sources,
        existing_facts=[existing],
        settings=effective_user_profile_settings(),
    )

    fact = plan.facts[0]
    assert fact.profile_fact_uid == existing_uid
    assert fact.derived_claim == claim
    assert fact.confidence == 0.92
    assert fact.metadata["independent_timeline_count"] == 4
    assert fact.metadata["evidence_span_days"] == 17.0
    assert fact.metadata["profile_value"] == 0.9
    assert fact.metadata["synthesis_revision"] == 2
    assert fact.last_confirmed_at == base + 18 * 86400
    assert plan.source_assignments == {sources[-1].source_uid: existing_uid}


def test_adaptive_task_batch_respects_candidate_limit_without_dropping_timeline():
    manager = UserProfileMaintenanceManager(
        None,
        config=effective_user_profile_settings(
            {
                "user_profile.maintenance_batch_candidate_limit": 3,
                "user_profile.maintenance_prompt_max_chars": 200000,
            }
        ),
    )
    events = []
    for index in range(2):
        payload = _timeline_payload(
            facts=[
                f"Alice明确说自己喜欢饮品 {index}-A",
                f"Alice明确说自己喜欢饮品 {index}-B",
            ]
        )
        events.append({"operation": "upsert", "payload": payload})

    selected, diagnostics = manager._select_task_events(events, manager.config)

    assert selected == events[:1]
    assert diagnostics["batch_timeline_count"] == 1
    assert diagnostics["batch_candidate_count"] == 2
    assert diagnostics["batch_was_bounded"] is True


def test_fact_prompt_budget_trims_optional_context_before_candidates():
    settings = effective_user_profile_settings(
        {"user_profile.maintenance_prompt_max_chars": 5000}
    )
    candidate = UserProfileFactMaintainer.extract_candidates(
        _timeline_payload(), actor_id="test:human:user-1"
    )[0]
    existing = [
        {
            "profile_fact_uid": f"fact-{index}",
            "category": "preference",
            "status": "active",
            "raw_fact": "已有事实" + "x" * 500,
        }
        for index in range(10)
    ]

    fitted, evidence, prompt, diagnostics = (
        UserProfileFactMaintainer.fit_prompt_context(
            [candidate], [], existing, settings
        )
    )

    assert candidate.source_uid in prompt
    assert evidence == []
    assert len(fitted) < len(existing)
    assert len(prompt) <= 5000
    assert diagnostics["existing_fact_count_omitted"] > 0


@pytest.mark.asyncio
async def test_profile_request_disables_stacked_provider_retries():
    provider = _RetryBudgetProvider()

    result = await UserProfileFactMaintainer._request_with_retries(
        provider,
        "bounded prompt",
        effective_user_profile_settings(),
    )

    assert result == "ok"
    assert provider.request_max_retries == 1


@pytest.mark.asyncio
async def test_profile_request_surfaces_provider_timeout_to_durable_task():
    provider = _RetryBudgetProvider(fail_with_timeout=True)

    with pytest.raises(
        RuntimeError, match="User-profile Provider request exceeded 180 seconds"
    ):
        await UserProfileFactMaintainer._request_with_retries(
            provider,
            "bounded prompt",
            effective_user_profile_settings(),
        )

    assert provider.request_max_retries == 1


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
