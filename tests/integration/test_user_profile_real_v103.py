"""Opt-in migration validation against a copied real v10.3 plugin data set."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.models.user_profile import (
    UserProfileFact,
    UserProfileFactCategory,
    UserProfileFactSource,
    UserRelationshipState,
)
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.managers.user_profile_history_manager import (
    UserProfileHistoryManager,
)
from astrbot_plugin_livingmemory.core.models.memory_identity import resolve_memory_space
from astrbot_plugin_livingmemory.storage.db_migration import DBMigration
from astrbot_plugin_livingmemory.storage.user_profile_store import UserProfileStore


def _source_dir() -> Path:
    configured = os.environ.get("LIVINGMEMORY_V103_DATA_DIR", "").strip()
    if not configured:
        pytest.skip("LIVINGMEMORY_V103_DATA_DIR is not configured")
    source = Path(configured).resolve()
    if not (source / "livingmemory.db").is_file():
        pytest.skip("configured v10.3 data directory has no livingmemory.db")
    return source


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _table_counts(db_path: Path) -> dict[str, int]:
    async with aiosqlite.connect(db_path) as db:
        rows = await (
            await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ).fetchall()
        counts: dict[str, int] = {}
        for (name,) in rows:
            if name == "db_version":
                continue
            quoted = str(name).replace('"', '""')
            row = await (
                await db.execute(f'SELECT COUNT(*) FROM "{quoted}"')
            ).fetchone()
            counts[str(name)] = int(row[0])
        return counts


@pytest.mark.asyncio
async def test_real_v103_copy_migrates_without_rewriting_existing_rows(tmp_path):
    source = _source_dir()
    source_db = source / "livingmemory.db"
    source_hash = _sha256(source_db)
    copied_dir = tmp_path / "plugin_data"
    shutil.copytree(source, copied_dir)
    copied_db = copied_dir / "livingmemory.db"

    migration = DBMigration(str(copied_db))
    assert await migration.get_db_version() == "10.3"
    before_counts = await _table_counts(copied_db)

    first = await migration.migrate()
    assert first["success"] is True
    assert first["to_version"] == "10.5"
    assert await migration.get_db_version() == "10.5"
    after_counts = await _table_counts(copied_db)
    assert {table: after_counts[table] for table in before_counts} == before_counts
    assert _sha256(source_db) == source_hash

    second = await migration.migrate()
    assert second["success"] is True
    assert await migration.get_db_version() == "10.5"

    profile_tables = {
        "user_profile_users",
        "user_profile_accounts",
        "user_profile_scopes",
        "user_profile_facts",
        "user_profile_fact_sources",
        "user_profile_conflicts",
        "user_relationship_states",
        "user_relationship_revisions",
        "user_profile_projection_events",
        "user_profile_tasks",
        "user_profile_timeline_identities",
    }
    async with aiosqlite.connect(copied_db) as db:
        integrity = await (await db.execute("PRAGMA integrity_check")).fetchone()
        foreign_keys = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
        table_rows = await (
            await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).fetchall()
        existing_tables = {str(row[0]) for row in table_rows}
        assert integrity is not None and integrity[0] == "ok"
        assert foreign_keys == []
        assert profile_tables <= existing_tables
        for table in profile_tables:
            count = await (
                await db.execute(f'SELECT COUNT(*) FROM "{table}"')
            ).fetchone()
            assert count is not None and count[0] == 0

    store = UserProfileStore(str(copied_db))
    await store.initialize()
    actor_id = "integration:human:livingmemory-v104-test-user"
    scope = await store.ensure_private_scope(
        actor_id=actor_id,
        bot_account="integration-bot",
        persona_id="integration-persona",
        display_name="Migration Test User",
    )
    assert scope is not None
    source_fact = UserProfileFactSource(
        timeline_uid="integration-timeline-v104",
        timeline_revision=1,
        fact_index=0,
        raw_fact="The migration test user prefers concise answers.",
        actor_id=actor_id,
    )
    await store.save_fact_sources([source_fact])
    fact = UserProfileFact(
        fact_namespace_uid=scope.fact_namespace_uid,
        category=UserProfileFactCategory.COMMUNICATION_PREFERENCE,
        representative_source_uid=source_fact.source_uid,
    )
    await store.publish_fact_changes(
        fact_namespace_uid=scope.fact_namespace_uid,
        upserts=[fact],
        source_assignments={source_fact.source_uid: fact.profile_fact_uid},
        expected_revision=0,
    )
    relationship = await store.publish_relationship(
        UserRelationshipState(
            profile_scope_uid=scope.profile_scope_uid,
            familiarity=0.2,
            subjective_summary="I have only met this test user once.",
        ),
        expected_revision=0,
    )
    serving = await store.list_serving_facts(scope.fact_namespace_uid)
    assert [item["raw_fact"] for item in serving] == [source_fact.raw_fact]
    assert relationship.revision == 1
    assert _sha256(source_db) == source_hash


@pytest.mark.asyncio
async def test_real_v103_legacy_private_timelines_auto_resolve_without_rewrite(
    tmp_path,
):
    source = _source_dir()
    source_db = source / "livingmemory.db"
    source_hash = _sha256(source_db)
    copied_dir = tmp_path / "plugin_data"
    shutil.copytree(source, copied_dir)
    copied_db = copied_dir / "livingmemory.db"
    migration = await DBMigration(str(copied_db)).migrate()
    assert migration["success"] is True
    store = UserProfileStore(str(copied_db))
    await store.initialize()

    selected = None
    async with aiosqlite.connect(copied_db) as db:
        rows = await (
            await db.execute("SELECT metadata FROM documents ORDER BY id")
        ).fetchall()
    for (raw_metadata,) in rows:
        metadata = json.loads(raw_metadata or "{}")
        space = resolve_memory_space(
            metadata.get("session_id"), metadata.get("persona_id")
        )
        actor_id, display_name = MemoryEngine._profile_actor_from_metadata(
            metadata, space.target_id
        )
        if space.chat_type == "private" and actor_id:
            selected = (actor_id, display_name, space)
            break
    assert selected is not None
    actor_id, display_name, space = selected
    scope = await store.ensure_private_scope(
        actor_id=actor_id,
        bot_account=space.bot_account,
        persona_id=space.persona_id,
        display_name=display_name,
    )
    assert scope is not None
    manager = UserProfileHistoryManager(
        str(copied_db),
        store,
        actor_resolver=MemoryEngine._profile_actor_from_metadata,
    )

    preview = await manager.preview(scope.profile_scope_uid)

    assert preview["eligible_timeline_count"] == 71
    assert preview["native_identity_count"] == 31
    assert preview["legacy_auto_resolved_count"] == 40
    assert preview["pending_review_count"] == 0
    assert _sha256(source_db) == source_hash
