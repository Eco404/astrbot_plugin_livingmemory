from __future__ import annotations

import json

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.managers.user_profile_history_manager import (
    UserProfileHistoryChangedError,
    UserProfileHistoryManager,
)
from astrbot_plugin_livingmemory.storage.user_profile_store import UserProfileStore


def _actor_resolver(metadata, private_target_id):
    actors = (metadata.get("role_bindings") or {}).get("actors") or []
    matches = [
        item
        for item in actors
        if item.get("actor_type") == "human"
        and item.get("sender_id") == private_target_id
    ]
    if len(matches) != 1:
        return "", None
    names = matches[0].get("observed_names") or []
    return str(matches[0]["actor_id"]), str(names[-1]) if names else None


def _metadata(
    timeline_uid: str,
    *,
    actor_id: str = "test:human:user-1",
    persona_id: str = "persona-1",
    include_binding: bool = True,
    revision: int = 1,
):
    bindings = {}
    if include_binding:
        bindings = {
            "actors": [
                {
                    "actor_id": actor_id,
                    "actor_type": "human",
                    "sender_id": "user-1",
                    "observed_names": ["User One"],
                }
            ]
        }
    return {
        "memory_uid": timeline_uid,
        "revision": revision,
        "memory_layer": "timeline",
        "status": "active",
        "session_id": "bot-1:private:user-1",
        "persona_id": persona_id,
        "create_time": 1000 + revision,
        "role_bindings": bindings,
        "key_facts": [],
    }


async def _insert_document(db_path, metadata):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metadata TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "INSERT INTO documents (metadata) VALUES (?)",
            (json.dumps(metadata, ensure_ascii=False),),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_history_preview_backfill_is_exact_idempotent_and_persona_aware(tmp_path):
    db_path = str(tmp_path / "history.db")
    store = UserProfileStore(db_path)
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    assert scope is not None
    await _insert_document(db_path, _metadata("timeline-eligible"))
    await _insert_document(
        db_path, _metadata("timeline-other-persona", persona_id="persona-2")
    )
    await _insert_document(
        db_path, _metadata("timeline-ambiguous", include_binding=False)
    )

    manager = UserProfileHistoryManager(
        db_path,
        store,
        actor_resolver=_actor_resolver,
        persona_resolver=lambda persona_id: {
            "persona_id": persona_id,
            "prompt": "current persona prompt",
            "signature": {"revision": 2},
        },
    )
    preview = await manager.preview(scope.profile_scope_uid)
    assert preview["eligible_timeline_count"] == 1
    assert preview["missing_timeline_count"] == 1
    assert preview["ambiguous_identity_count"] == 1
    assert preview["out_of_scope_count"] == 1

    result = await manager.backfill(
        scope.profile_scope_uid,
        expected_history_fingerprint=preview["history_fingerprint"],
    )
    assert result["inserted_event_count"] == 1
    events = await store.list_projection_history(scope.profile_scope_uid)
    assert [item["timeline_uid"] for item in events] == ["timeline-eligible"]
    assert events[0]["payload"]["persona_snapshot"]["signature"] == {
        "revision": 2
    }
    assert events[0]["payload"]["persona_snapshot"]["basis"] == "current_config"

    second_preview = await manager.preview(scope.profile_scope_uid)
    assert second_preview["missing_timeline_count"] == 0
    second = await manager.backfill(
        scope.profile_scope_uid,
        expected_history_fingerprint=second_preview["history_fingerprint"],
    )
    assert second["inserted_event_count"] == 0
    assert second["refreshed_event_count"] == 1
    assert len(await store.list_projection_history(scope.profile_scope_uid)) == 1


@pytest.mark.asyncio
async def test_history_backfill_rejects_stale_preview(tmp_path):
    db_path = str(tmp_path / "history-stale.db")
    store = UserProfileStore(db_path)
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    assert scope is not None
    await _insert_document(db_path, _metadata("timeline-1"))
    manager = UserProfileHistoryManager(
        db_path, store, actor_resolver=_actor_resolver
    )
    preview = await manager.preview(scope.profile_scope_uid)
    await _insert_document(db_path, _metadata("timeline-2"))
    with pytest.raises(UserProfileHistoryChangedError):
        await manager.backfill(
            scope.profile_scope_uid,
            expected_history_fingerprint=preview["history_fingerprint"],
        )


@pytest.mark.asyncio
async def test_history_preview_handles_database_without_documents_table(tmp_path):
    db_path = str(tmp_path / "history-empty.db")
    store = UserProfileStore(db_path)
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    manager = UserProfileHistoryManager(
        db_path, store, actor_resolver=_actor_resolver
    )

    preview = await manager.preview(scope.profile_scope_uid)

    assert preview["eligible_timeline_count"] == 0
    assert preview["missing_timeline_count"] == 0
    assert preview["document_table_missing_count"] == 1
