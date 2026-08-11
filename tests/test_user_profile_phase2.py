from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from astrbot_plugin_livingmemory.core.managers.user_profile_fact_maintainer import (
    UserProfileFactMaintainer,
    UserProfileFactValidationError,
)
from astrbot_plugin_livingmemory.core.managers.user_profile_maintenance_manager import (
    UserProfileMaintenanceManager,
)
from astrbot_plugin_livingmemory.core.models.user_profile import (
    UserProfileFactSource,
    UserProfileProjectionEvent,
)
from astrbot_plugin_livingmemory.core.user_profile_settings import (
    effective_user_profile_settings,
)
from astrbot_plugin_livingmemory.storage.user_profile_store import UserProfileStore


class _AcceptingProvider:
    provider_config = {"id": "profile-test", "model": "deterministic"}

    async def text_chat(self, *, prompt, system_prompt, **kwargs):
        match = re.search(r"Candidate sources: (.*?)\nOutput shape:", prompt, re.S)
        candidates = json.loads(match.group(1))
        operations = [
            {
                "source_uid": item["source_uid"],
                "operation": "accept_new",
                "category": "preference",
                "confidence": 0.95,
                "importance": 0.8,
                "inference_kind": "explicit",
                "sensitive": False,
                "reason": "explicit self report",
            }
            for item in candidates
        ]
        return SimpleNamespace(
            completion_text=json.dumps({"operations": operations})
        )


def _timeline_payload(*, revision: int = 1, facts: list[str] | None = None):
    facts = ["Alice明确说自己喜欢无糖茶"] if facts is None else facts
    actor_id = "test:human:user-1"
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
            "create_time": 1000.0 + revision,
            "updated_at": 1000.0 + revision,
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


@pytest.mark.asyncio
async def test_fact_contract_rejects_secret_even_when_model_accepts():
    source = UserProfileFactSource(
        timeline_uid="timeline-secret",
        timeline_revision=1,
        fact_index=0,
        raw_fact="我的 API key 是 sk-test-placeholder",
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


def test_behavioral_inference_uses_actual_sources_not_reported_counts():
    source = UserProfileFactSource(
        timeline_uid="timeline-1",
        timeline_revision=1,
        fact_index=0,
        raw_fact="Alice本次要求回答简短",
        actor_id="test:human:user-1",
        evidence_started_at=1000,
        evidence_ended_at=1000,
    )
    maintainer = UserProfileFactMaintainer()
    with pytest.raises(UserProfileFactValidationError, match="too few Timelines"):
        maintainer._validate_plan(
            fact_namespace_uid="facts-1",
            payload={
                "operations": [
                    {
                        "source_uid": source.source_uid,
                        "operation": "accept_new",
                        "category": "communication_preference",
                        "confidence": 0.99,
                        "importance": 0.8,
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
