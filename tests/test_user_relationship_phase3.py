from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from astrbot_plugin_livingmemory.core.managers.user_profile_maintenance_manager import (
    UserProfileMaintenanceManager,
)
from astrbot_plugin_livingmemory.core.managers.user_relationship_maintainer import (
    UserRelationshipMaintenanceResult,
    UserRelationshipMaintainer,
)
from astrbot_plugin_livingmemory.core.models.user_profile import (
    UserProfileProjectionEvent,
    UserRelationshipState,
)
from astrbot_plugin_livingmemory.core.user_profile_settings import (
    effective_user_profile_settings,
)
from astrbot_plugin_livingmemory.storage.user_profile_store import UserProfileStore


class _RelationshipProvider:
    provider_config = {"id": "relationship-test", "model": "subjective"}

    def __init__(self, *, fail_facts: bool = False):
        self.fail_facts = fail_facts
        self.fact_calls = 0
        self.relationship_calls = 0

    async def text_chat(self, *, prompt, system_prompt, **kwargs):
        if prompt.startswith("Maintain an objective"):
            self.fact_calls += 1
            if self.fail_facts:
                raise RuntimeError("fact stage unavailable")
            match = re.search(
                r"Candidate sources: (.*?)\nHistorical behavior evidence:",
                prompt,
                re.S,
            )
            candidates = json.loads(match.group(1))
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "operations": [
                            {
                                "source_uid": item["source_uid"],
                                "operation": "ignore",
                            }
                            for item in candidates
                        ]
                    }
                )
            )
        self.relationship_calls += 1
        return SimpleNamespace(
            completion_text=json.dumps(
                {
                    "cited_timeline_refs": ["T1"],
                    "dimensions": {
                        "familiarity": 0.8,
                        "trust": 0.75,
                        "warmth": 0.7,
                        "ease": 0.65,
                        "tension": 0.15,
                        "concern": 0.6,
                    },
                    "stance_tags": ["愿意倾听", "重视承诺"],
                    "subjective_summary": "我觉得她愿意坦诚交流，也会认真看待彼此的承诺。",
                    "recent_aftereffect": "仍有些担心",
                    "aftereffect_days": 99,
                    "major_event": False,
                    "change_summary": "对方主动表达感谢和信任",
                },
                ensure_ascii=False,
            )
        )


class _RelationshipCorrectionProvider(_RelationshipProvider):
    def __init__(self, invalid_responses: int):
        super().__init__()
        self.invalid_responses = invalid_responses
        self.prompts = []

    async def text_chat(self, *, prompt, system_prompt, **kwargs):
        self.prompts.append(prompt)
        response = await super().text_chat(
            prompt=prompt, system_prompt=system_prompt, **kwargs
        )
        if len(self.prompts) <= self.invalid_responses:
            payload = json.loads(response.completion_text)
            payload["cited_timeline_refs"] = ["T999"]
            return SimpleNamespace(completion_text=json.dumps(payload))
        return response


def _persona_resolver(persona_id: str):
    return {
        "persona_id": persona_id,
        "name": "Companion",
        "prompt": "你重视真诚，也会保留自己的判断。",
        "signature": {"algorithm": "sha256", "digest": "persona-digest"},
    }


def _relationship_event_payload(
    *, assistant_only: bool = False, include_objective_fact: bool = False
):
    user_actor = "test:human:user-1"
    assistant_actor = "test:assistant:bot-1"
    evidence_actor = assistant_actor if assistant_only else user_actor
    subject_actor = assistant_actor if assistant_only else user_actor
    payload = {
        "profile_actor_id": user_actor,
        "metadata": {
            "memory_uid": "relationship-timeline-1",
            "revision": 1,
            "session_id": "bot-1:private:user-1",
            "persona_id": "persona-1",
            "memory_space_id": "space-relationship-1",
            "memory_layer": "timeline",
            "create_time": 1000.0,
            "updated_at": 1001.0,
            "canonical_summary": "Alice感谢我一直认真听她说，并表示很信任我。",
            "sentiment": "positive",
            "key_facts": ["Alice感谢我一直认真听她说，并表示很信任我"],
            "key_fact_profiles": [
                {
                    "fact_index": 0,
                    "fact_type": "relationship_interaction",
                    "durability": "high",
                    "selection_reason": "relationship_significance",
                }
            ],
            "key_fact_evidence": [{"fact_index": 0, "message_refs": ["M1"]}],
            "key_fact_attributions": [
                {
                    "fact_index": 0,
                    "subject_refs": [{"actor_id": subject_actor}],
                    "claim_type": "speaker_self",
                    "confidence": 1.0,
                    "attribution_status": "verified",
                }
            ],
            "role_bindings": {
                "message_actor_ids": {"M1": evidence_actor},
                "actors": [
                    {
                        "actor_id": user_actor,
                        "actor_type": "human",
                        "sender_id": "user-1",
                        "observed_names": ["Alice"],
                    },
                    {
                        "actor_id": assistant_actor,
                        "actor_type": "assistant",
                        "sender_id": "bot-1",
                        "observed_names": ["Companion"],
                    },
                ],
            },
        },
    }
    if include_objective_fact:
        metadata = payload["metadata"]
        metadata["key_facts"].append("Alice明确表示她更喜欢简洁的回答")
        metadata["key_fact_profiles"].append(
            {
                "fact_index": 1,
                "fact_type": "preference",
                "durability": "high",
                "selection_reason": "stable_personal_fact",
            }
        )
        metadata["key_fact_evidence"].append({"fact_index": 1, "message_refs": ["M1"]})
        metadata["key_fact_attributions"].append(
            {
                "fact_index": 1,
                "subject_refs": [{"actor_id": user_actor}],
                "claim_type": "speaker_self",
                "confidence": 1.0,
                "attribution_status": "verified",
            }
        )
    return payload


def test_relationship_trigger_rejects_assistant_only_evidence():
    user_actor = "test:human:user-1"
    meaningful = UserRelationshipMaintainer.meaningful_timelines(
        [
            {
                "operation": "upsert",
                "metadata": _relationship_event_payload(assistant_only=True)[
                    "metadata"
                ],
            }
        ],
        actor_id=user_actor,
    )
    assert meaningful == []


@pytest.mark.asyncio
async def test_legacy_summary_relationship_is_weak_and_initially_capped():
    timelines = UserRelationshipMaintainer.meaningful_timelines(
        [
            {
                "operation": "upsert",
                "timeline_uid": "legacy-relationship",
                "timeline_revision": 1,
                "identity_resolution": {"evidence_basis": "timeline_summary_only"},
                "metadata": {
                    "memory_uid": "legacy-relationship",
                    "create_time": 1000,
                    "canonical_summary": "旧摘要记录了一次积极互动。",
                    "key_facts": ["对话气氛友好"],
                },
            }
        ],
        actor_id="test:human:user-1",
    )
    assert timelines[0]["weak_history"] is True
    settings = effective_user_profile_settings(
        {"user_profile.maintenance_max_retries": 0}
    )
    result = await UserRelationshipMaintainer(_RelationshipProvider()).maintain(
        profile_scope_uid="scope-legacy",
        timelines=timelines,
        current_state=None,
        current_persona={"signature": {}},
        objective_facts=[],
        sensitivity="balanced",
        behavior_mode="natural",
        settings=settings,
    )
    assert result is not None
    assert max(result.state.dimensions().values()) <= 0.35
    assert result.diagnostics["legacy_summary_only"] is True


def test_relationship_prompt_minimizes_private_detail_repetition():
    prompt, _refs = UserRelationshipMaintainer._build_prompt(
        timelines=[
            {
                "summary": "grounded interaction",
                "facts": ["relationship signal"],
                "sentiment": "neutral",
            }
        ],
        current_state=None,
        current_persona={"persona_id": "persona-1", "name": "Companion"},
        objective_facts=[],
        sensitivity="balanced",
        behavior_mode="natural",
    )
    assert "Do not restate concrete private details" in prompt
    assert "It is not evidence that the user reciprocates intimacy" in prompt


@pytest.mark.asyncio
async def test_relationship_soft_limit_and_aftereffect_clamp():
    provider = _RelationshipProvider()
    settings = effective_user_profile_settings(
        {"user_profile.maintenance_max_retries": 0}
    )
    current = UserRelationshipState(
        profile_scope_uid="scope-1",
        familiarity=0.5,
        trust=0.5,
        warmth=0.5,
        ease=0.5,
        tension=0.5,
        concern=0.5,
    )
    result = await UserRelationshipMaintainer(provider).maintain(
        profile_scope_uid="scope-1",
        timelines=[
            {
                "timeline_uid": "timeline-1",
                "timeline_revision": 1,
                "summary": "meaningful",
                "facts": ["Alice表达信任"],
                "sentiment": "positive",
                "major_event_eligible": False,
                "updated_at": 1000,
            }
        ],
        current_state=current,
        current_persona={"signature": {"digest": "persona-digest"}},
        objective_facts=[],
        sensitivity="balanced",
        behavior_mode="natural",
        settings=settings,
    )
    assert result is not None
    assert result.state.familiarity == pytest.approx(0.57)
    assert result.state.tension == pytest.approx(0.43)
    assert result.diagnostics["soft_limited"]
    remaining_days = (
        result.state.aftereffect_expires_at - __import__("time").time()
    ) / 86400
    assert 13.9 <= remaining_days <= 14.0


@pytest.mark.asyncio
async def test_relationship_contract_correction_repeats_allowed_refs():
    provider = _RelationshipCorrectionProvider(invalid_responses=2)
    result = await UserRelationshipMaintainer(provider).maintain(
        profile_scope_uid="scope-correction",
        timelines=[
            {
                "timeline_uid": "timeline-1",
                "timeline_revision": 1,
                "summary": "meaningful",
                "facts": ["Alice表达信任"],
                "sentiment": "positive",
                "major_event_eligible": False,
                "updated_at": 1000,
            }
        ],
        current_state=None,
        current_persona={"signature": {"digest": "persona-digest"}},
        objective_facts=[],
        sensitivity="balanced",
        behavior_mode="natural",
        settings=effective_user_profile_settings(
            {
                "user_profile.maintenance_max_retries": 0,
                "user_profile.contract_correction_retries": 2,
            }
        ),
    )

    assert result is not None
    assert len(provider.prompts) == 3
    assert result.diagnostics["contract_correction_used"] is True
    assert result.diagnostics["contract_correction_attempts"] == 2
    assert all(
        'Allowed cited_timeline_refs: ["T1"]' in prompt
        for prompt in provider.prompts[1:]
    )


@pytest.mark.asyncio
async def test_relationship_stage_uses_current_persona_without_persisting_prompt(tmp_path):
    store = UserProfileStore(str(tmp_path / "relationship.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
        display_name="Alice",
    )
    provider = _RelationshipProvider()
    settings = effective_user_profile_settings(
        {"user_profile.maintenance_max_retries": 0}
    )
    manager = UserProfileMaintenanceManager(
        store,
        provider=provider,
        persona_resolver=_persona_resolver,
        config=settings,
    )
    event_uid = await store.enqueue_projection_event(
        UserProfileProjectionEvent(
            timeline_uid="relationship-timeline-1",
            timeline_revision=1,
            operation="upsert",
            memory_space_id="space-relationship-1",
            profile_scope_uid=scope.profile_scope_uid,
            payload=_relationship_event_payload(),
        )
    )
    await manager.drain_scope(scope.profile_scope_uid)

    relationship = await store.get_relationship(scope.profile_scope_uid)
    assert relationship is not None
    assert relationship.revision == 1
    assert relationship.persona_signature["digest"] == "persona-digest"
    assert relationship.source_timeline_uids == ["relationship-timeline-1"]
    tasks = await store.list_recoverable_tasks(limit=10)
    assert tasks == []
    async with store._connect() as db:
        task_row = await (
            await db.execute(
                "SELECT result_summary FROM user_profile_tasks LIMIT 1"
            )
        ).fetchone()
        event_row = await (
            await db.execute(
                "SELECT status FROM user_profile_projection_events WHERE event_uid = ?",
                (event_uid,),
            )
        ).fetchone()
    assert json.loads(task_row["result_summary"])["relationship_checkpoint"] is True
    assert (
        json.loads(task_row["result_summary"])["relationship_diagnostics"][
            "persona_basis"
        ]
        == "current_config"
    )
    assert event_row["status"] == "completed"
    assert provider.fact_calls == 0
    assert provider.relationship_calls == 1
    await manager.close()


@pytest.mark.asyncio
async def test_relationship_ignores_obsolete_event_persona_and_rejects_midrun_change(
    tmp_path,
):
    store = UserProfileStore(str(tmp_path / "relationship-current-persona.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    payload = _relationship_event_payload()
    payload["persona_snapshot"] = {
        "persona_id": "persona-1",
        "prompt": "obsolete prompt",
        "signature": {"digest": "obsolete"},
    }
    await store.enqueue_projection_event(
        UserProfileProjectionEvent(
            timeline_uid="relationship-timeline-1",
            timeline_revision=1,
            operation="upsert",
            memory_space_id="space-relationship-1",
            profile_scope_uid=scope.profile_scope_uid,
            payload=payload,
        )
    )
    provider = _RelationshipProvider()
    calls = 0

    async def changing_persona(persona_id: str):
        nonlocal calls
        calls += 1
        digest = "current-v1" if calls == 1 else "current-v2"
        return {
            "persona_id": persona_id,
            "name": "Companion",
            "prompt": f"current prompt {digest}",
            "signature": {"algorithm": "sha256", "digest": digest},
        }

    manager = UserProfileMaintenanceManager(
        store,
        provider=provider,
        persona_resolver=changing_persona,
        config=effective_user_profile_settings(
            {
                "user_profile.maintenance_retry_base_seconds": 5,
                "user_profile.maintenance_retry_max_seconds": 60,
            }
        ),
    )

    await manager.drain_scope(scope.profile_scope_uid)

    assert await store.get_relationship(scope.profile_scope_uid) is None
    async with store._connect() as db:
        task_row = await (
            await db.execute(
                "SELECT status, error FROM user_profile_tasks ORDER BY created_at LIMIT 1"
            )
        ).fetchone()
        columns = {
            str(row[1])
            for row in await (
                await db.execute("PRAGMA table_info(user_profile_tasks)")
            ).fetchall()
        }
    assert task_row["status"] == "facts_completed"
    assert "changed during relationship maintenance" in task_row["error"]
    assert "persona_prompt" not in columns
    assert "persona_signature" not in columns
    assert provider.relationship_calls == 1
    await manager.close()


@pytest.mark.asyncio
async def test_relationship_history_rebuild_batches_all_latest_events_with_current_persona(
    tmp_path,
):
    store = UserProfileStore(str(tmp_path / "relationship-history-batches.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    event_specs = [
        ("timeline-1", 1, 1001.0),
        ("timeline-2", 1, 1002.0),
        ("timeline-1", 2, 1003.0),
        ("timeline-3", 1, 1004.0),
        ("timeline-4", 1, 1005.0),
        ("timeline-5", 1, 1006.0),
    ]
    for timeline_uid, revision, updated_at in event_specs:
        payload = _relationship_event_payload()
        payload["metadata"].update(
            {
                "memory_uid": timeline_uid,
                "revision": revision,
                "updated_at": updated_at,
                "canonical_summary": f"relationship event {timeline_uid} r{revision}",
            }
        )
        await store.enqueue_projection_event(
            UserProfileProjectionEvent(
                timeline_uid=timeline_uid,
                timeline_revision=revision,
                operation="upsert",
                memory_space_id="space-relationship-1",
                profile_scope_uid=scope.profile_scope_uid,
                status="completed",
                payload=payload,
            )
        )

    persona_calls = 0
    persona_objects = []

    async def current_persona(persona_id: str):
        nonlocal persona_calls
        persona_calls += 1
        return {
            "persona_id": persona_id,
            "name": "Companion",
            "prompt": "current persona prompt",
            "signature": {"algorithm": "sha256", "digest": "current-digest"},
        }

    manager = UserProfileMaintenanceManager(
        store,
        provider=object(),
        persona_resolver=current_persona,
        config=effective_user_profile_settings(
            {"user_profile.relationship_rebuild_batch_limit": 2}
        ),
    )
    batches = []
    prior_states = []

    async def maintain_batch(**kwargs):
        timelines = kwargs["timelines"]
        batches.append(
            [(item["timeline_uid"], item["timeline_revision"]) for item in timelines]
        )
        prior_states.append(kwargs["current_state"])
        persona_objects.append(kwargs["current_persona"])
        state = UserRelationshipState(
            profile_scope_uid=scope.profile_scope_uid,
            familiarity=0.1 * len(batches),
            persona_signature=dict(kwargs["current_persona"]["signature"]),
            source_timeline_uids=[item["timeline_uid"] for item in timelines],
        )
        return UserRelationshipMaintenanceResult(
            state=state,
            change_summary=f"batch {len(batches)}",
            diagnostics={"batch": len(batches)},
        )

    manager.relationship_maintainer.maintain = maintain_batch
    rebuilt = await manager.rebuild_relationship_from_projection_history(
        scope.profile_scope_uid
    )

    assert rebuilt is not None
    assert batches == [
        [("timeline-2", 1), ("timeline-1", 2)],
        [("timeline-3", 1), ("timeline-4", 1)],
        [("timeline-5", 1)],
    ]
    assert prior_states[0] is None
    assert prior_states[1] is not None and prior_states[1].familiarity == 0.1
    assert prior_states[2] is not None and prior_states[2].familiarity == 0.2
    assert persona_calls == 2
    assert len({id(item) for item in persona_objects}) == 1
    assert rebuilt.source_timeline_uids == [
        "timeline-2",
        "timeline-1",
        "timeline-3",
        "timeline-4",
        "timeline-5",
    ]
    revision = (await store.list_relationship_revisions(rebuilt.relationship_uid))[0]
    assert revision["diagnostics"]["history_event_count"] == 6
    assert revision["diagnostics"]["meaningful_timeline_count"] == 5
    assert revision["diagnostics"]["history_batch_count"] == 3
    assert revision["persona_signature"]["digest"] == "current-digest"
    await manager.close()


@pytest.mark.asyncio
async def test_relationship_runs_when_fact_stage_fails(tmp_path):
    store = UserProfileStore(str(tmp_path / "relationship-partial.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    provider = _RelationshipProvider(fail_facts=True)
    settings = effective_user_profile_settings(
        {
            "user_profile.maintenance_max_retries": 0,
            "user_profile.maintenance_retry_base_seconds": 5,
            "user_profile.maintenance_retry_max_seconds": 60,
        }
    )
    manager = UserProfileMaintenanceManager(
        store,
        provider=provider,
        persona_resolver=_persona_resolver,
        config=settings,
    )
    await store.enqueue_projection_event(
        UserProfileProjectionEvent(
            timeline_uid="relationship-timeline-1",
            timeline_revision=1,
            operation="upsert",
            memory_space_id="space-relationship-1",
            profile_scope_uid=scope.profile_scope_uid,
            payload=_relationship_event_payload(include_objective_fact=True),
        )
    )
    await manager.drain_scope(scope.profile_scope_uid)

    relationship = await store.get_relationship(scope.profile_scope_uid)
    assert relationship is not None
    async with store._connect() as db:
        row = await (
            await db.execute(
                "SELECT task_uid FROM user_profile_tasks ORDER BY created_at LIMIT 1"
            )
        ).fetchone()
    task = await store.get_task(str(row["task_uid"]))
    assert task["status"] == "facts_failed"
    assert task["result_summary"]["relationship_checkpoint"] is True
    assert provider.fact_calls == 1
    assert provider.relationship_calls == 1
    await manager.close()


@pytest.mark.asyncio
async def test_manual_relationship_edit_freeze_reset_and_rollback(tmp_path):
    store = UserProfileStore(str(tmp_path / "relationship-admin.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    manager = UserProfileMaintenanceManager(
        store, config=effective_user_profile_settings()
    )
    first = await manager.update_relationship_manually(
        scope.profile_scope_uid,
        changes={
            "familiarity": 60,
            "trust": 75,
            "subjective_summary": "我愿意认真听她说话。",
            "stance_tags": ["耐心"],
        },
        sensitivity_override="slow",
        behavior_override="high_autonomy",
    )
    second = await manager.update_relationship_manually(
        scope.profile_scope_uid,
        changes={"trust": 40, "tension": 55},
    )
    assert first.revision == 1
    assert second.revision == 2
    await manager.set_relationship_frozen(scope.profile_scope_uid, True)
    frozen_scope = await store.get_scope(scope.profile_scope_uid)
    assert frozen_scope.relationship_frozen is True
    assert frozen_scope.relationship_sensitivity_override == "slow"
    assert frozen_scope.relationship_behavior_override == "high_autonomy"

    rolled_back = await manager.rollback_relationship(
        scope.profile_scope_uid, 1, reason="restore earlier baseline"
    )
    assert rolled_back.revision == 3
    assert rolled_back.trust == pytest.approx(0.75)
    reset = await manager.reset_relationship(scope.profile_scope_uid)
    assert reset.revision == 4
    assert reset.subjective_summary == ""
    reset_scope = await store.get_scope(scope.profile_scope_uid)
    assert reset_scope.relationship_reset_after is not None
    revisions = await store.list_relationship_revisions(
        reset.relationship_uid, limit=10
    )
    assert [item["operation"] for item in revisions[:2]] == ["reset", "rollback"]
    await manager.close()
