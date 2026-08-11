from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.models.user_profile import (
    UserProfileFact,
    UserProfileFactCategory,
    UserProfileFactSource,
    UserProfileProjectionEvent,
    UserProfileTask,
    UserRelationshipState,
)
from astrbot_plugin_livingmemory.storage.db_migration import DBMigration
from astrbot_plugin_livingmemory.storage.user_profile_store import (
    UserProfileRevisionConflict,
    UserProfileStore,
)


@pytest.mark.asyncio
async def test_user_profile_schema_and_sparse_settings(tmp_path):
    db_path = str(tmp_path / "profile.db")
    store = UserProfileStore(db_path)
    await store.initialize()

    with sqlite3.connect(db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "user_profile_users",
        "user_profile_accounts",
        "user_profile_scopes",
        "user_profile_facts",
        "user_profile_fact_sources",
        "user_relationship_states",
        "user_relationship_revisions",
        "user_profile_projection_events",
        "user_profile_tasks",
    } <= tables

    assert await store.get_setting_overrides() == {}
    await store.update_setting_overrides(
        {"user_profile.injection_max_chars": 900}, settings_revision=1
    )
    assert await store.get_setting_overrides() == {
        "user_profile.injection_max_chars": 900
    }
    await store.update_setting_overrides({}, reset_all=True)
    assert await store.get_setting_overrides() == {}


@pytest.mark.asyncio
async def test_private_scope_requires_stable_actor_and_preserves_names(tmp_path):
    store = UserProfileStore(str(tmp_path / "identity.db"))
    await store.initialize()

    assert await store.ensure_private_scope(
        actor_id="temporary-session",
        bot_account="bot-1",
        persona_id="persona-1",
    ) is None
    first = await store.ensure_private_scope(
        actor_id="qq:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
        display_name="甲",
    )
    second = await store.ensure_private_scope(
        actor_id="qq:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
        display_name="甲的新昵称",
    )

    assert first is not None and second is not None
    assert first.profile_scope_uid == second.profile_scope_uid
    detail = await store.profile_detail(first.profile_scope_uid)
    assert detail is not None
    assert detail["accounts"][0]["observed_names"] == ["甲", "甲的新昵称"]
    assert detail["accounts"][0]["last_observed_name"] == "甲的新昵称"


@pytest.mark.asyncio
async def test_fact_sources_publish_atomically_and_keep_raw_timeline_text(tmp_path):
    store = UserProfileStore(str(tmp_path / "facts.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="qq:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    assert scope is not None
    source = UserProfileFactSource(
        timeline_uid="timeline-1",
        timeline_revision=2,
        fact_index=0,
        raw_fact="用户明确说自己喜欢简洁回答",
        actor_id="qq:human:user-1",
    )
    await store.save_fact_sources([source])
    fact = UserProfileFact(
        fact_namespace_uid=scope.fact_namespace_uid,
        category=UserProfileFactCategory.COMMUNICATION_PREFERENCE,
        representative_source_uid=source.source_uid,
        confidence=0.95,
        importance=0.8,
    )
    revision = await store.publish_fact_changes(
        fact_namespace_uid=scope.fact_namespace_uid,
        upserts=[fact],
        source_assignments={source.source_uid: fact.profile_fact_uid},
        expected_revision=0,
    )

    assert revision == 1
    facts = await store.list_serving_facts(scope.fact_namespace_uid)
    assert len(facts) == 1
    assert facts[0]["raw_fact"] == source.raw_fact
    assert facts[0]["timeline_revision"] == 2
    with pytest.raises(UserProfileRevisionConflict):
        await store.publish_fact_changes(
            fact_namespace_uid=scope.fact_namespace_uid,
            upserts=[],
            expected_revision=0,
        )


@pytest.mark.asyncio
async def test_relationship_revisions_are_clamped_and_auditable(tmp_path):
    store = UserProfileStore(str(tmp_path / "relationship.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="qq:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    assert scope is not None
    state = UserRelationshipState(
        profile_scope_uid=scope.profile_scope_uid,
        familiarity=1.5,
        trust=-0.5,
        warmth=0.6,
        subjective_summary="我逐渐熟悉这名用户。",
        source_timeline_uids=["timeline-1"],
    )
    saved = await store.publish_relationship(state, expected_revision=0)

    assert saved.revision == 1
    loaded = await store.get_relationship(scope.profile_scope_uid)
    assert loaded is not None
    assert loaded.familiarity == 1.0
    assert loaded.trust == 0.0
    revisions = await store.list_relationship_revisions(loaded.relationship_uid)
    assert revisions[0]["after_state"]["subjective_summary"] == state.subjective_summary
    with pytest.raises(UserProfileRevisionConflict):
        await store.publish_relationship(state, expected_revision=0)


@pytest.mark.asyncio
async def test_projection_event_is_idempotent_and_task_keeps_order(tmp_path):
    store = UserProfileStore(str(tmp_path / "tasks.db"))
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="qq:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    assert scope is not None
    event = UserProfileProjectionEvent(
        timeline_uid="timeline-1",
        timeline_revision=1,
        operation="upsert",
        memory_space_id="space-1",
        profile_scope_uid=scope.profile_scope_uid,
    )
    first_uid = await store.enqueue_projection_event(event)
    duplicate_uid = await store.enqueue_projection_event(
        UserProfileProjectionEvent(
            timeline_uid="timeline-1",
            timeline_revision=1,
            operation="upsert",
            memory_space_id="space-1",
            profile_scope_uid=scope.profile_scope_uid,
        )
    )
    assert duplicate_uid == first_uid
    pending = await store.list_pending_projection_events()
    task = UserProfileTask(profile_scope_uid=scope.profile_scope_uid)
    await store.create_task(task, pending)
    loaded = await store.get_task(task.task_uid)

    assert loaded is not None
    assert [item["event_uid"] for item in loaded["items"]] == [first_uid]


@pytest.mark.asyncio
async def test_migration_v10_3_to_v10_4_creates_profile_schema(tmp_path):
    db_path = str(tmp_path / "migration.db")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE db_version (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL,
                description TEXT,
                migrated_at TEXT NOT NULL,
                migration_duration_seconds REAL
            );
            INSERT INTO db_version(version, description, migrated_at)
            VALUES ('v10.3', 'fixture', '2026-08-11T00:00:00+00:00');
            """
        )
        await db.commit()

    result = await DBMigration(db_path).migrate()

    assert result["success"] is True
    assert result["to_version"] == "10.4"
    assert await DBMigration(db_path).get_db_version() == "10.4"
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'user_profile_users'"
            )
        ).fetchone()
    assert row is not None

