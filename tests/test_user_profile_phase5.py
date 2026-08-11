from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.models.user_profile import (
    UserProfileFact,
    UserProfileFactCategory,
    UserProfileFactSource,
    UserProfileFactStatus,
    UserProfileProjectionEvent,
    UserRelationshipState,
)
from astrbot_plugin_livingmemory.core.page_api_modules import PageApiUtils
from astrbot_plugin_livingmemory.core.page_api_modules.user_profile_handler import (
    UserProfileHandler,
)
from astrbot_plugin_livingmemory.storage.user_profile_store import (
    UserProfileRevisionConflict,
    UserProfileStore,
)


async def _new_scope(
    store: UserProfileStore,
    actor_id: str,
    *,
    bot_account: str = "bot-1",
    persona_id: str = "persona-1",
):
    scope = await store.ensure_private_scope(
        actor_id=actor_id,
        bot_account=bot_account,
        persona_id=persona_id,
        display_name=actor_id.rsplit(":", 1)[-1],
    )
    assert scope is not None
    return scope


async def _publish_fact(
    store: UserProfileStore,
    scope,
    *,
    actor_id: str,
    timeline_uid: str,
    raw_fact: str,
    status: UserProfileFactStatus | str = UserProfileFactStatus.ACTIVE,
):
    source = UserProfileFactSource(
        timeline_uid=timeline_uid,
        timeline_revision=1,
        fact_index=0,
        raw_fact=raw_fact,
        actor_id=actor_id,
    )
    await store.save_fact_sources([source])
    fact = UserProfileFact(
        fact_namespace_uid=scope.fact_namespace_uid,
        category=UserProfileFactCategory.PREFERENCE,
        representative_source_uid=source.source_uid,
        status=status,
        confidence=0.95,
        importance=0.8,
    )
    revision = await store.publish_fact_changes(
        fact_namespace_uid=scope.fact_namespace_uid,
        upserts=[fact],
        source_assignments={source.source_uid: fact.profile_fact_uid},
        expected_revision=await store.get_fact_namespace_revision(
            scope.fact_namespace_uid
        ),
    )
    return source, fact, revision


@pytest.mark.asyncio
async def test_fact_governance_conflicts_and_later_evidence(tmp_path):
    store = UserProfileStore(str(tmp_path / "fact-governance.db"))
    await store.initialize()
    scope = await _new_scope(store, "test:human:user-1")
    _source, pending, revision = await _publish_fact(
        store,
        scope,
        actor_id="test:human:user-1",
        timeline_uid="timeline-pending",
        raw_fact="用户明确说喜欢短回答",
        status=UserProfileFactStatus.PENDING,
    )

    confirmed = await store.apply_fact_admin_action(
        scope.profile_scope_uid,
        pending.profile_fact_uid,
        action="confirm",
        expected_revision=revision,
    )
    with pytest.raises(UserProfileRevisionConflict):
        await store.apply_fact_admin_action(
            scope.profile_scope_uid,
            pending.profile_fact_uid,
            action="pin",
            expected_revision=revision,
        )
    paused = await store.apply_fact_admin_action(
        scope.profile_scope_uid,
        pending.profile_fact_uid,
        action="pause",
        expected_revision=confirmed["fact_revision"],
    )
    resumed = await store.apply_fact_admin_action(
        scope.profile_scope_uid,
        pending.profile_fact_uid,
        action="resume",
        expected_revision=paused["fact_revision"],
    )
    facts = await store.list_facts_for_maintenance(scope.fact_namespace_uid)
    governed = next(item for item in facts if item["profile_fact_uid"] == pending.profile_fact_uid)
    assert governed["status"] == "active"
    assert governed["admin_confirmed"] is True

    sources = [
        UserProfileFactSource(
            timeline_uid=f"timeline-conflict-{index}",
            timeline_revision=1,
            fact_index=0,
            raw_fact=text,
            actor_id="test:human:user-1",
        )
        for index, text in enumerate(("用户住在北京", "用户住在上海"), start=1)
    ]
    conflict_facts = [
        UserProfileFact(
            fact_namespace_uid=scope.fact_namespace_uid,
            category=UserProfileFactCategory.STABLE_INFO,
            representative_source_uid=source.source_uid,
            status=UserProfileFactStatus.CONFLICT,
        )
        for source in sources
    ]
    conflict_uid = "conflict-residence"
    conflict_revision = await store.apply_fact_projection_batch(
        fact_namespace_uid=scope.fact_namespace_uid,
        projections=[
            {"timeline_uid": source.timeline_uid, "sources": [source]}
            for source in sources
        ],
        facts=conflict_facts,
        source_assignments={
            source.source_uid: fact.profile_fact_uid
            for source, fact in zip(sources, conflict_facts, strict=True)
        },
        conflicts=[
            {
                "conflict_uid": conflict_uid,
                "conflict_key": "residence",
                "fact_uids": [fact.profile_fact_uid for fact in conflict_facts],
            }
        ],
        expected_revision=resumed["fact_revision"],
    )
    with pytest.raises(UserProfileRevisionConflict):
        await store.resolve_profile_conflict(
            scope.profile_scope_uid,
            conflict_uid,
            resolution="select",
            selected_fact_uid=conflict_facts[0].profile_fact_uid,
            expected_revision=conflict_revision - 1,
        )
    resolution = await store.resolve_profile_conflict(
        scope.profile_scope_uid,
        conflict_uid,
        resolution="select",
        selected_fact_uid=conflict_facts[0].profile_fact_uid,
        expected_revision=conflict_revision,
    )
    detail = await store.profile_detail(scope.profile_scope_uid)
    assert detail is not None
    conflict = next(item for item in detail["conflicts"] if item["conflict_uid"] == conflict_uid)
    assert conflict["status"] == "resolved"
    assert conflict["resolution_kind"] == "select"
    statuses = {
        item["profile_fact_uid"]: item["status"] for item in detail["facts"]
    }
    assert statuses[conflict_facts[0].profile_fact_uid] == "active"
    assert statuses[conflict_facts[1].profile_fact_uid] == "superseded"

    later_sources = [
        UserProfileFactSource(
            timeline_uid=f"timeline-later-{index}",
            timeline_revision=1,
            fact_index=0,
            raw_fact=text,
            actor_id="test:human:user-1",
        )
        for index, text in enumerate(("用户喜欢茶", "用户不喜欢茶"), start=1)
    ]
    later_facts = [
        UserProfileFact(
            fact_namespace_uid=scope.fact_namespace_uid,
            category=UserProfileFactCategory.PREFERENCE,
            representative_source_uid=source.source_uid,
            status=UserProfileFactStatus.CONFLICT,
        )
        for source in later_sources
    ]
    later_uid = "conflict-tea"
    later_revision = await store.apply_fact_projection_batch(
        fact_namespace_uid=scope.fact_namespace_uid,
        projections=[
            {"timeline_uid": source.timeline_uid, "sources": [source]}
            for source in later_sources
        ],
        facts=later_facts,
        source_assignments={
            source.source_uid: fact.profile_fact_uid
            for source, fact in zip(later_sources, later_facts, strict=True)
        },
        conflicts=[
            {
                "conflict_uid": later_uid,
                "conflict_key": "tea",
                "fact_uids": [fact.profile_fact_uid for fact in later_facts],
            }
        ],
        expected_revision=resolution["fact_revision"],
    )
    later_facts[0].status = UserProfileFactStatus.ACTIVE
    later_facts[1].status = UserProfileFactStatus.SUPERSEDED
    await store.apply_fact_projection_batch(
        fact_namespace_uid=scope.fact_namespace_uid,
        projections=[],
        facts=later_facts,
        expected_revision=later_revision,
    )
    detail = await store.profile_detail(scope.profile_scope_uid)
    assert detail is not None
    later_conflict = next(item for item in detail["conflicts"] if item["conflict_uid"] == later_uid)
    assert later_conflict["status"] == "auto_resolved"
    assert later_conflict["resolution_kind"] == "new_evidence"


@pytest.mark.asyncio
async def test_reset_delete_disable_and_stale_rebuild_preview(tmp_path):
    store = UserProfileStore(str(tmp_path / "lifecycle.db"))
    await store.initialize()
    scope = await _new_scope(store, "test:human:user-1")
    await _publish_fact(
        store,
        scope,
        actor_id="test:human:user-1",
        timeline_uid="timeline-1",
        raw_fact="用户喜欢无糖茶",
    )
    event = UserProfileProjectionEvent(
        timeline_uid="timeline-1",
        timeline_revision=1,
        operation="upsert",
        memory_space_id="space-1",
        profile_scope_uid=scope.profile_scope_uid,
    )
    await store.enqueue_projection_event(event)
    await store.publish_relationship(
        UserRelationshipState(
            profile_scope_uid=scope.profile_scope_uid,
            trust=0.7,
            subjective_summary="我信任这名用户。",
        ),
        expected_revision=0,
    )

    stale_fingerprint = await store.profile_fingerprint(scope.profile_scope_uid)
    await store.set_profile_enabled(scope.profile_scope_uid, False)
    with pytest.raises(UserProfileRevisionConflict):
        await store.prepare_profile_rebuild(
            scope.profile_scope_uid,
            clear_overrides=False,
            expected_fingerprint=stale_fingerprint,
        )
    await store.set_profile_enabled(scope.profile_scope_uid, True)
    reset = await store.reset_objective_profile(scope.profile_scope_uid)
    assert reset["projection_cursor"] > 0
    assert await store.list_facts_for_maintenance(scope.fact_namespace_uid) == []
    assert await store.get_relationship(scope.profile_scope_uid) is not None

    deleted = await store.delete_and_disable_profile(scope.profile_scope_uid)
    assert deleted["enabled"] is False
    assert deleted["auto_enable_blocked"] is True
    assert await store.get_relationship(scope.profile_scope_uid) is None
    deleted_scope = await store.get_scope(scope.profile_scope_uid)
    assert deleted_scope is not None
    assert deleted_scope.enabled is False
    assert deleted_scope.auto_enable_blocked is True


@pytest.mark.asyncio
async def test_manual_account_binding_and_unbinding_reprojects_sources(tmp_path):
    store = UserProfileStore(str(tmp_path / "accounts.db"))
    await store.initialize()
    actor_a = "test:human:user-a"
    actor_b = "other:human:user-b"
    scope_a = await _new_scope(store, actor_a)
    scope_b = await _new_scope(store, actor_b)
    await _publish_fact(
        store,
        scope_a,
        actor_id=actor_a,
        timeline_uid="timeline-a",
        raw_fact="A 喜欢红茶",
    )
    await _publish_fact(
        store,
        scope_b,
        actor_id=actor_b,
        timeline_uid="timeline-b",
        raw_fact="B 喜欢咖啡",
    )
    for scope, actor_id, timeline_uid in (
        (scope_a, actor_a, "timeline-a"),
        (scope_b, actor_b, "timeline-b"),
    ):
        await store.enqueue_projection_event(
            UserProfileProjectionEvent(
                timeline_uid=timeline_uid,
                timeline_revision=1,
                operation="upsert",
                memory_space_id="space-1",
                profile_scope_uid=scope.profile_scope_uid,
                payload={"profile_actor_id": actor_id},
            )
        )
    await store.publish_relationship(
        UserRelationshipState(profile_scope_uid=scope_a.profile_scope_uid, trust=0.2),
        expected_revision=0,
    )
    await store.publish_relationship(
        UserRelationshipState(profile_scope_uid=scope_b.profile_scope_uid, trust=0.8),
        expected_revision=0,
    )

    preview = await store.preview_account_binding(
        target_actor_id=actor_a, actor_ids=[actor_b]
    )
    result = await store.bind_accounts(
        target_actor_id=actor_a,
        actor_ids=[actor_b],
        expected_fingerprint=preview["fingerprint"],
    )
    assert result["requires_rebuild"] is True
    bound_a = await store.get_scope_by_actor(
        actor_id=actor_a,
        bot_account="bot-1",
        persona_id="persona-1",
        include_disabled=True,
    )
    bound_b = await store.get_scope_by_actor(
        actor_id=actor_b,
        bot_account="bot-1",
        persona_id="persona-1",
        include_disabled=True,
    )
    assert bound_a is not None and bound_b is not None
    assert bound_a.profile_scope_uid == bound_b.profile_scope_uid
    facts = await store.list_facts_for_maintenance(bound_a.fact_namespace_uid)
    assert {item["raw_fact"] for item in facts} == {"A 喜欢红茶", "B 喜欢咖啡"}
    b_fact = next(item for item in facts if item["raw_fact"] == "B 喜欢咖啡")
    async with aiosqlite.connect(store.db_path) as db:
        source_actor = await (
            await db.execute(
                "SELECT source_account_actor_id FROM user_profile_fact_sources "
                "WHERE profile_fact_uid = ?",
                (b_fact["profile_fact_uid"],),
            )
        ).fetchone()
    assert source_actor[0] == actor_b
    relationship = await store.get_relationship(bound_a.profile_scope_uid)
    assert relationship is not None and relationship.trust == pytest.approx(0.2)

    unbind_preview = await store.preview_account_unbind(actor_b)
    unbound = await store.unbind_account(
        actor_b, expected_fingerprint=unbind_preview["fingerprint"]
    )
    assert unbound["created_scope_uids"]
    separate_a = await store.get_scope_by_actor(
        actor_id=actor_a,
        bot_account="bot-1",
        persona_id="persona-1",
        include_disabled=True,
    )
    separate_b = await store.get_scope_by_actor(
        actor_id=actor_b,
        bot_account="bot-1",
        persona_id="persona-1",
        include_disabled=True,
    )
    assert separate_a is not None and separate_b is not None
    assert separate_a.logical_user_uid != separate_b.logical_user_uid
    assert separate_b.has_gap is True
    assert await store.list_facts_for_maintenance(separate_b.fact_namespace_uid) == []
    history = await store.list_projection_history(separate_b.profile_scope_uid)
    assert [item["timeline_uid"] for item in history] == ["timeline-b"]


@pytest.mark.asyncio
async def test_objective_share_group_keeps_relationships_persona_isolated(tmp_path):
    store = UserProfileStore(str(tmp_path / "sharing.db"))
    await store.initialize()
    actor_id = "test:human:user-1"
    scope_one = await _new_scope(store, actor_id, persona_id="persona-1")
    scope_two = await _new_scope(store, actor_id, persona_id="persona-2")
    await _publish_fact(
        store,
        scope_one,
        actor_id=actor_id,
        timeline_uid="timeline-1",
        raw_fact="用户喜欢茶",
    )
    await _publish_fact(
        store,
        scope_two,
        actor_id=actor_id,
        timeline_uid="timeline-2",
        raw_fact="用户喜欢简洁回答",
    )
    await store.publish_relationship(
        UserRelationshipState(profile_scope_uid=scope_one.profile_scope_uid, warmth=0.2),
        expected_revision=0,
    )
    await store.publish_relationship(
        UserRelationshipState(profile_scope_uid=scope_two.profile_scope_uid, warmth=0.9),
        expected_revision=0,
    )

    preview = await store.preview_share_group(
        profile_scope_uids=[scope_one.profile_scope_uid, scope_two.profile_scope_uid]
    )
    shared = await store.save_share_group(
        name="same user objective facts",
        profile_scope_uids=[scope_one.profile_scope_uid, scope_two.profile_scope_uid],
        expected_fingerprint=preview["fingerprint"],
    )
    reloaded_one = await store.get_scope(scope_one.profile_scope_uid)
    reloaded_two = await store.get_scope(scope_two.profile_scope_uid)
    assert reloaded_one is not None and reloaded_two is not None
    assert reloaded_one.fact_namespace_uid == shared["fact_namespace_uid"]
    assert reloaded_two.fact_namespace_uid == shared["fact_namespace_uid"]
    shared_facts = await store.list_facts_for_maintenance(shared["fact_namespace_uid"])
    assert {item["raw_fact"] for item in shared_facts} == {
        "用户喜欢茶",
        "用户喜欢简洁回答",
    }
    relationship_one = await store.get_relationship(scope_one.profile_scope_uid)
    relationship_two = await store.get_relationship(scope_two.profile_scope_uid)
    assert relationship_one is not None and relationship_one.warmth == pytest.approx(0.2)
    assert relationship_two is not None and relationship_two.warmth == pytest.approx(0.9)
    assert relationship_one.relationship_uid != relationship_two.relationship_uid


@pytest.mark.asyncio
async def test_task_page_api_never_exposes_persona_prompt():
    store = MagicMock()
    store.list_profile_tasks = AsyncMock(
        return_value=[
            {
                "task_uid": "task-1",
                "status": "failed",
                "persona_prompt": "private persona instructions",
            }
        ]
    )
    engine = SimpleNamespace(
        user_profile_store=store,
        user_profile_maintenance_manager=None,
    )
    request = MagicMock()
    request.args.get.side_effect = lambda key, default=None: (
        "scope-1" if key == "profile_scope_uid" else default
    )
    import astrbot_plugin_livingmemory.core.page_api_modules.user_profile_handler as module

    previous = vars(module).get("request")
    vars(module)["request"] = request
    try:
        result = await UserProfileHandler(PageApiUtils()).list_tasks(engine)
    finally:
        vars(module)["request"] = previous

    assert result["status"] == "ok"
    assert result["data"]["items"] == [
        {"task_uid": "task-1", "status": "failed"}
    ]
