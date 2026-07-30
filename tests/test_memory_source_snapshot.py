import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.memory_source import (
    restore_source_messages,
    serialize_source_messages,
)
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.storage.db_migration import DBMigration
from astrbot_plugin_livingmemory.storage.memory_identity_store import (
    MemoryIdentityStore,
)


@pytest.mark.asyncio
async def test_source_snapshot_follows_stable_memory_uid_and_cascades(tmp_path):
    db_path = tmp_path / "memory.db"
    store = MemoryIdentityStore(str(db_path))
    await store.initialize()
    await store.upsert_memory(
        memory_uid="timeline-1",
        document_id=7,
        memory_layer="timeline",
        memory_space_id="space-1",
        revision=1,
        created_at=1.0,
    )
    await store.save_source_snapshot(
        "timeline-1",
        [{"id": 1, "role": "user", "content": "hello"}],
        source_revision=1,
        retention_reason="importance_threshold",
    )

    await store.upsert_memory(
        memory_uid="timeline-1",
        document_id=9,
        memory_layer="timeline",
        memory_space_id="space-1",
        revision=2,
        created_at=1.0,
    )
    snapshot = await store.get_source_snapshot_by_document_id(9)
    assert snapshot is not None
    assert snapshot["messages"][0]["content"] == "hello"

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(
            "DELETE FROM memory_registry WHERE memory_uid = ?", ("timeline-1",)
        )
        await db.commit()
    assert await store.get_source_snapshot("timeline-1") is None


def test_source_message_snapshot_keeps_reconstruction_fields_only():
    messages = [
        Message(
            3,
            "qq:FriendMessage:user",
            "assistant",
            "structured source",
            "bot",
            "Persona",
            platform="qq",
            metadata={
                "is_bot_message": True,
                "persona_name": "Persona",
                "unrelated": "discarded",
            },
        )
    ]
    serialized = serialize_source_messages(messages)
    assert serialized[0]["metadata"] == {
        "is_bot_message": True,
        "persona_name": "Persona",
        "proactive_message": False,
    }
    restored = restore_source_messages(serialized)
    assert restored[0].content == "structured source"
    assert restored[0].role == "assistant"


@pytest.mark.asyncio
async def test_v10_1_migration_creates_source_snapshot_table(tmp_path):
    db_path = tmp_path / "migration.db"
    migration = DBMigration(str(db_path))
    await migration._migrate_v10_to_v10_1(None)
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'memory_source_snapshots'"
            )
        ).fetchone()
    assert row == ("memory_source_snapshots",)
