from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from astrbot_plugin_livingmemory.core.managers.user_profile_maintenance_manager import (
    UserProfileMaintenanceManager,
)
from astrbot_plugin_livingmemory.core.managers.user_relationship_maintainer import (
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
            match = re.search(r"Candidate sources: (.*?)\nOutput shape:", prompt, re.S)
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


def _relationship_event_payload(*, assistant_only: bool = False):
    user_actor = "test:human:user-1"
    assistant_actor = "test:assistant:bot-1"
    evidence_actor = assistant_actor if assistant_only else user_actor
    subject_actor = assistant_actor if assistant_only else user_actor
    return {
        "profile_actor_id": user_actor,
        "persona_snapshot": {
            "persona_id": "persona-1",
            "name": "Companion",
            "prompt": "你重视真诚，也会保留自己的判断。",
            "signature": {"algorithm": "sha256", "digest": "persona-digest"},
        },
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


def test_relationship_trigger_rejects_assistant_only_evidence():
    user_actor = "test:human:user-1"
    meaningful = UserRelationshipMaintainer.meaningful_timelines(
        [
            {
                "operation": "upsert",
                "metadata": _relationship_event_payload(assistant_only=True)["metadata"],
            }
        ],
        actor_id=user_actor,
    )
    assert meaningful == []


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
        persona_snapshot={"signature": {"digest": "persona-digest"}},
        objective_facts=[],
        sensitivity="balanced",
        behavior_mode="natural",
        settings=settings,
    )
    assert result is not None
    assert result.state.familiarity == pytest.approx(0.57)
    assert result.state.tension == pytest.approx(0.43)
    assert result.diagnostics["soft_limited"]
    remaining_days = (result.state.aftereffect_expires_at - __import__("time").time()) / 86400
    assert 13.9 <= remaining_days <= 14.0


@pytest.mark.asyncio
async def test_relationship_stage_publishes_and_clears_persona_prompt(tmp_path):
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
        store, provider=provider, config=settings
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
                "SELECT persona_prompt, result_summary FROM user_profile_tasks LIMIT 1"
            )
        ).fetchone()
        event_row = await (
            await db.execute(
                "SELECT status FROM user_profile_projection_events WHERE event_uid = ?",
                (event_uid,),
            )
        ).fetchone()
    assert task_row["persona_prompt"] == ""
    assert json.loads(task_row["result_summary"])["relationship_checkpoint"] is True
    assert event_row["status"] == "completed"
    assert provider.fact_calls == 1
    assert provider.relationship_calls == 1
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
        store, provider=provider, config=settings
    )
    await store.enqueue_projection_event(
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
    async with store._connect() as db:
        row = await (
            await db.execute(
                "SELECT task_uid FROM user_profile_tasks ORDER BY created_at LIMIT 1"
            )
        ).fetchone()
    task = await store.get_task(str(row["task_uid"]))
    assert task["status"] == "facts_failed"
    assert task["result_summary"]["relationship_checkpoint"] is True
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
