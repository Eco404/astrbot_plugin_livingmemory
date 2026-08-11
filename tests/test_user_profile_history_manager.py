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
    target_id: str = "user-1",
):
    bindings = {}
    if include_binding:
        bindings = {
            "actors": [
                {
                    "actor_id": actor_id,
                    "actor_type": "human",
                    "sender_id": target_id,
                    "observed_names": ["User One"],
                }
            ]
        }
    return {
        "memory_uid": timeline_uid,
        "revision": revision,
        "memory_layer": "timeline",
        "status": "active",
        "session_id": f"bot-1:private:{target_id}",
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
        db_path, _metadata("timeline-legacy-resolved", include_binding=False)
    )
    await _insert_document(
        db_path,
        _metadata(
            "timeline-pending-review",
            include_binding=False,
            target_id="unbound-user",
        ),
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
    assert preview["eligible_timeline_count"] == 2
    assert preview["missing_timeline_count"] == 2
    assert preview["ambiguous_identity_count"] == 1
    assert preview["legacy_auto_resolved_count"] == 1
    assert preview["pending_review_count"] == 1
    assert preview["out_of_scope_count"] == 1

    result = await manager.backfill(
        scope.profile_scope_uid,
        expected_history_fingerprint=preview["history_fingerprint"],
    )
    assert result["inserted_event_count"] == 2
    events = await store.list_projection_history(scope.profile_scope_uid)
    assert [item["timeline_uid"] for item in events] == [
        "timeline-eligible",
        "timeline-legacy-resolved",
    ]
    assert events[0]["payload"]["persona_snapshot"]["signature"] == {"revision": 2}
    assert events[0]["payload"]["persona_snapshot"]["basis"] == "current_config"

    second_preview = await manager.preview(scope.profile_scope_uid)
    assert second_preview["missing_timeline_count"] == 0
    second = await manager.backfill(
        scope.profile_scope_uid,
        expected_history_fingerprint=second_preview["history_fingerprint"],
    )
    assert second["inserted_event_count"] == 0
    assert second["refreshed_event_count"] == 2
    assert len(await store.list_projection_history(scope.profile_scope_uid)) == 2

    reviews = await store.list_timeline_identity_resolutions(
        statuses=("pending_review",)
    )
    assert [item["timeline_uid"] for item in reviews] == ["timeline-pending-review"]


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
    manager = UserProfileHistoryManager(db_path, store, actor_resolver=_actor_resolver)
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
    manager = UserProfileHistoryManager(db_path, store, actor_resolver=_actor_resolver)

    preview = await manager.preview(scope.profile_scope_uid)

    assert preview["eligible_timeline_count"] == 0
    assert preview["missing_timeline_count"] == 0
    assert preview["document_table_missing_count"] == 1


@pytest.mark.asyncio
async def test_conversation_identity_evidence_batches_and_tolerates_legacy_columns(
    tmp_path,
):
    db_path = str(tmp_path / "history-conversation-compat.db")
    store = UserProfileStore(db_path)
    await store.initialize()
    conversation_path = tmp_path / "conversations.db"
    session_ids = {f"session-{index}" for index in range(1001)}
    async with aiosqlite.connect(conversation_path) as db:
        await db.executescript(
            """
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY);
            CREATE TABLE messages (
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                sender_id TEXT
            );
            """
        )
        await db.executemany(
            "INSERT INTO sessions(session_id) VALUES (?)",
            [(session_id,) for session_id in session_ids],
        )
        await db.executemany(
            "INSERT INTO messages(session_id, role, sender_id) VALUES (?, ?, ?)",
            [
                ("session-0", "user", "user-1"),
                ("session-0", "assistant", "bot-1"),
                ("session-1000", "human", "user-2"),
            ],
        )
        await db.commit()
    manager = UserProfileHistoryManager(db_path, store, actor_resolver=_actor_resolver)

    evidence = await manager._conversation_identity_evidence(session_ids)

    assert evidence["session-0"]["platform"] == ""
    assert evidence["session-0"]["human_sender_ids"] == {"user-1"}
    assert evidence["session-0"]["stable_actor_ids"] == set()
    assert evidence["session-1000"]["human_sender_ids"] == {"user-2"}


@pytest.mark.asyncio
async def test_new_exact_account_candidate_refreshes_pending_identity_fingerprint(
    tmp_path,
):
    db_path = str(tmp_path / "history-candidate-refresh.db")
    store = UserProfileStore(db_path)
    await store.initialize()
    first_scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    assert first_scope is not None
    await _insert_document(
        db_path,
        _metadata("timeline-new-candidate", include_binding=False, target_id="user-2"),
    )
    async with aiosqlite.connect(tmp_path / "conversations.db") as db:
        await db.executescript(
            """
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, platform TEXT);
            CREATE TABLE messages (
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                sender_id TEXT,
                metadata TEXT
            );
            INSERT INTO sessions(session_id, platform)
            VALUES ('bot-1:private:user-2', 'test');
            INSERT INTO messages(session_id, role, sender_id, metadata)
            VALUES ('bot-1:private:user-2', 'user', 'user-2', '{}');
            """
        )
        await db.commit()
    manager = UserProfileHistoryManager(db_path, store, actor_resolver=_actor_resolver)

    first = await manager.preview(first_scope.profile_scope_uid)
    assert first["pending_review_count"] == 1
    pending = (
        await store.list_timeline_identity_resolutions(statuses=("pending_review",))
    )[0]

    second_scope = await store.ensure_private_scope(
        actor_id="test:human:user-2",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    assert second_scope is not None
    second = await manager.preview(first_scope.profile_scope_uid)
    resolved = await store.get_timeline_identity_resolution(
        pending["timeline_uid"],
        pending["timeline_revision"],
        pending["memory_space_id"],
    )

    assert second["pending_review_count"] == 0
    assert resolved is not None
    assert resolved["status"] == "resolved"
    assert resolved["actor_id"] == "test:human:user-2"
    assert resolved["evidence_fingerprint"] != pending["evidence_fingerprint"]


@pytest.mark.asyncio
async def test_pending_legacy_identity_can_be_ignored_restored_and_bound(tmp_path):
    db_path = str(tmp_path / "history-review.db")
    store = UserProfileStore(db_path)
    await store.initialize()
    scope = await store.ensure_private_scope(
        actor_id="test:human:user-1",
        bot_account="bot-1",
        persona_id="persona-1",
    )
    assert scope is not None
    await _insert_document(
        db_path,
        _metadata(
            "timeline-review",
            include_binding=False,
            target_id="legacy-unbound",
        ),
    )
    manager = UserProfileHistoryManager(db_path, store, actor_resolver=_actor_resolver)

    preview = await manager.preview(scope.profile_scope_uid)
    assert preview["pending_review_count"] == 1
    pending = (
        await store.list_timeline_identity_resolutions(statuses=("pending_review",))
    )[0]

    ignored = await store.resolve_timeline_identity_review(
        timeline_uid=pending["timeline_uid"],
        timeline_revision=pending["timeline_revision"],
        memory_space_id=pending["memory_space_id"],
        action="ignore",
        expected_evidence_fingerprint=pending["evidence_fingerprint"],
    )
    assert ignored["status"] == "ignored"
    assert (await manager.preview(scope.profile_scope_uid))[
        "ignored_identity_count"
    ] == 1

    restored = await store.resolve_timeline_identity_review(
        timeline_uid=ignored["timeline_uid"],
        timeline_revision=ignored["timeline_revision"],
        memory_space_id=ignored["memory_space_id"],
        action="restore",
        expected_evidence_fingerprint=ignored["evidence_fingerprint"],
    )
    assert restored["status"] == "pending_review"
    rescanned = await manager.preview(scope.profile_scope_uid)
    assert rescanned["pending_review_count"] == 1
    pending = (
        await store.list_timeline_identity_resolutions(statuses=("pending_review",))
    )[0]

    bound = await store.resolve_timeline_identity_review(
        timeline_uid=pending["timeline_uid"],
        timeline_revision=pending["timeline_revision"],
        memory_space_id=pending["memory_space_id"],
        action="bind",
        expected_evidence_fingerprint=pending["evidence_fingerprint"],
        profile_scope_uid=scope.profile_scope_uid,
        actor_id="test:human:user-1",
    )
    assert bound["status"] == "resolved"
    assert bound["identity_basis"] == "admin_binding"
    final = await manager.preview(scope.profile_scope_uid)
    assert final["eligible_timeline_count"] == 1
    assert final["pending_review_count"] == 0
