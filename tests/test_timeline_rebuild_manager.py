import asyncio
import json
import time

import aiosqlite
import pytest

from astrbot_plugin_livingmemory.core.managers.timeline_rebuild_manager import (
    TimelineRebuildManager,
)
from astrbot_plugin_livingmemory.core.models.conversation_models import Message


class FakeConversationManager:
    def __init__(self, messages):
        self.messages = messages

    async def get_messages_by_id_span(
        self, session_id, first_message_id, last_message_id, *, limit=100
    ):
        return [
            item
            for item in self.messages
            if item.session_id == session_id
            and first_message_id <= item.id <= last_message_id
        ][:limit]


class FakeProcessor:
    async def process_conversation(self, messages, is_group_chat=False, persona_id=None):
        return "重构后的 Timeline", {"key_facts": ["事实一"], "topics": ["测试"]}, 0.7

    def classify_atoms_from_metadata(self, **kwargs):
        return []


class FlakyProcessor(FakeProcessor):
    def __init__(self):
        self.fail = True

    async def process_conversation(self, messages, is_group_chat=False, persona_id=None):
        if self.fail:
            raise ValueError("质量校验未通过")
        return await super().process_conversation(
            messages, is_group_chat=is_group_chat, persona_id=persona_id
        )


class FakeTopicBuilder:
    def __init__(self):
        self.calls = []

    async def build_space(self, memory_space_id, **kwargs):
        self.calls.append((memory_space_id, kwargs))
        return {"status": "completed", "memory_space_id": memory_space_id}


class FakeMemoryEngine:
    def __init__(self, memories):
        self.memories = memories
        self.topic_build_manager = FakeTopicBuilder()
        self.rewrites = []
        self.updates = []
        self.maintenance_calls = []

    async def get_memory(self, memory_id):
        return self.memories.get(memory_id)

    async def rewrite_memory_in_place(self, memory_id, **kwargs):
        current = self.memories[memory_id]
        old = dict(current["metadata"])
        metadata = dict(kwargs["metadata"])
        for key in (
            "memory_uid", "memory_space_id", "session_id", "persona_id", "source_window"
        ):
            metadata[key] = old[key]
        metadata["revision"] = int(old["revision"]) + 1
        self.memories[memory_id] = {
            "id": memory_id,
            "text": kwargs["content"],
            "metadata": metadata,
        }
        self.rewrites.append((memory_id, kwargs))
        return memory_id

    async def update_memory(self, memory_id, updates):
        if memory_id not in self.memories:
            return False
        self.memories[memory_id]["metadata"].update(updates.get("metadata", {}))
        self.updates.append((memory_id, updates))
        return True

    def _schedule_topic_maintenance(self, memory_space_id, **kwargs):
        self.maintenance_calls.append((memory_space_id, kwargs))


class BlockingMemoryEngine(FakeMemoryEngine):
    def __init__(self, memories):
        super().__init__(memories)
        self.write_started = asyncio.Event()
        self.release_write = asyncio.Event()

    async def rewrite_memory_in_place(self, memory_id, **kwargs):
        self.write_started.set()
        await self.release_write.wait()
        return await super().rewrite_memory_in_place(memory_id, **kwargs)


async def create_source_db(path, *, complete=True, linked=True):
    now = time.time()
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE memory_registry (
                memory_uid TEXT PRIMARY KEY, document_id INTEGER,
                memory_layer TEXT, memory_space_id TEXT, revision INTEGER,
                status TEXT, created_at REAL, updated_at REAL
            );
            CREATE TABLE memory_source_spans (
                memory_uid TEXT PRIMARY KEY, session_id TEXT,
                first_message_id INTEGER, last_message_id INTEGER,
                start_index INTEGER, end_index INTEGER, traceability TEXT,
                metadata TEXT
            );
            CREATE TABLE topic_timeline_links (
                topic_uid TEXT, timeline_uid TEXT, status TEXT
            );
            """
        )
        await db.execute(
            "INSERT INTO memory_registry VALUES (?, ?, 'timeline', ?, 1, 'active', ?, ?)",
            ("memory-1", 1, "space-1", now, now),
        )
        source = {
            "session_id": "bot:FriendMessage:user",
            "first_message_id": 10,
            "last_message_id": 11,
            "message_count": 2,
            "start_index": 0,
            "end_index": 2,
        }
        await db.execute(
            "INSERT INTO memory_source_spans VALUES (?, ?, ?, ?, 0, 2, ?, ?)",
            (
                "memory-1", source["session_id"], 10, 11,
                "full" if complete else "partial", json.dumps(source),
            ),
        )
        if linked:
            await db.execute(
                "INSERT INTO topic_timeline_links VALUES ('topic-1', 'memory-1', 'active')"
            )
        await db.commit()


async def add_second_timeline(path, *, linked=True):
    now = time.time()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO memory_registry VALUES (?, ?, 'timeline', ?, 1, 'active', ?, ?)",
            ("memory-2", 2, "space-1", now, now),
        )
        if linked:
            await db.execute(
                "INSERT INTO topic_timeline_links VALUES ('topic-2', 'memory-2', 'active')"
            )
        await db.commit()


def source_messages():
    return [
        Message(10, "bot:FriendMessage:user", "user", "第一条", "user", "用户", platform="qq"),
        Message(
            11, "bot:FriendMessage:user", "assistant", "第二条", "bot", "人格",
            platform="qq", metadata={"actor_type": "assistant"},
        ),
    ]


def source_memory():
    source = {
        "session_id": "bot:FriendMessage:user",
        "first_message_id": 10,
        "last_message_id": 11,
        "message_count": 2,
        "start_index": 0,
        "end_index": 2,
    }
    return {
        "id": 1,
        "text": "旧 Timeline",
        "metadata": {
            "memory_uid": "memory-1",
            "memory_space_id": "space-1",
            "session_id": source["session_id"],
            "persona_id": "persona-1",
            "revision": 1,
            "source_window": source,
        },
    }


def second_source_memory():
    memory = source_memory()
    memory["id"] = 2
    memory["text"] = "第二条旧 Timeline"
    memory["metadata"] = dict(memory["metadata"])
    memory["metadata"]["memory_uid"] = "memory-2"
    return memory


def staged_payload(memory, content):
    metadata = dict(memory["metadata"])
    return {
        "content": content,
        "metadata": metadata,
        "importance": 0.8,
        "session_id": metadata["session_id"],
        "persona_id": metadata["persona_id"],
    }


async def wait_task(manager, task_uid):
    for _ in range(100):
        task = await manager.get_task(task_uid)
        if task["status"] in manager.TERMINAL_STATUSES:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("task did not finish")


@pytest.mark.asyncio
async def test_preview_blocks_partial_source_span(tmp_path):
    db_path = tmp_path / "memory.db"
    await create_source_db(db_path, complete=False)
    manager = TimelineRebuildManager(
        str(db_path), FakeConversationManager(source_messages()),
        FakeMemoryEngine({1: source_memory()}), FakeProcessor(),
    )
    await manager.initialize()
    preview = await manager.preview([1])
    assert preview["reconstructable_count"] == 0
    assert "完整的原始消息 ID 边界" in preview["items"][0]["blocked_reasons"][0]


@pytest.mark.asyncio
async def test_inactive_timeline_listing_and_restore_are_space_scoped(tmp_path):
    db_path = tmp_path / "inactive.db"
    now = time.time()
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT);
            CREATE TABLE memory_registry (
                memory_uid TEXT PRIMARY KEY, document_id INTEGER,
                memory_layer TEXT, memory_space_id TEXT, revision INTEGER,
                status TEXT, created_at REAL, updated_at REAL
            );
            CREATE TABLE topic_timeline_links (
                topic_uid TEXT, timeline_uid TEXT, status TEXT
            );
            """
        )
        await db.executemany(
            "INSERT INTO documents VALUES (?, ?, ?)",
            [
                (1, "已归档 Timeline", '{"summary_quality":"low"}'),
                (2, "其他空间 Timeline", '{}'),
                (3, "仍活跃 Timeline", '{}'),
            ],
        )
        await db.executemany(
            "INSERT INTO memory_registry VALUES (?, ?, 'timeline', ?, 1, ?, ?, ?)",
            [
                ("memory-1", 1, "space-1", "archived", now, now),
                ("memory-2", 2, "space-2", "archived", now, now),
                ("memory-3", 3, "space-1", "active", now, now),
            ],
        )
        await db.execute(
            "INSERT INTO topic_timeline_links VALUES ('topic-1', 'memory-1', 'active')"
        )
        await db.commit()
    memory = source_memory()
    memory["text"] = "已归档 Timeline"
    memory["metadata"]["status"] = "archived"
    engine = FakeMemoryEngine({1: memory})
    manager = TimelineRebuildManager(
        str(db_path), FakeConversationManager([]), engine, FakeProcessor()
    )
    await manager.initialize()

    items = await manager.list_inactive_timelines("space-1")

    assert [item["memory_id"] for item in items] == [1]
    assert items[0]["topic_count"] == 1
    assert items[0]["summary_quality"] == "low"
    restored = await manager.restore_inactive_timelines("space-1", [1, 2])
    assert restored["restored_count"] == 1
    assert restored["failed_ids"] == [2]
    assert engine.updates == [(1, {"metadata": {"status": "active"}})]
    assert engine.maintenance_calls == [
        ("space-1", {"full": False, "timeline_uids": ["memory-1"]})
    ]


@pytest.mark.asyncio
async def test_empty_selection_never_expands_to_all_timelines(tmp_path):
    db_path = tmp_path / "memory.db"
    await create_source_db(db_path)
    manager = TimelineRebuildManager(
        str(db_path),
        FakeConversationManager(source_messages()),
        FakeMemoryEngine({1: source_memory()}),
        FakeProcessor(),
    )
    await manager.initialize()

    preview = await manager.preview([])

    assert preview["total_count"] == 0
    with pytest.raises(ValueError, match="至少选择"):
        await manager.start_task([])


@pytest.mark.asyncio
async def test_preview_can_filter_low_quality_timelines(tmp_path):
    db_path = tmp_path / "memory.db"
    await create_source_db(db_path)
    memory = source_memory()
    memory["metadata"]["summary_quality"] = "low"
    engine = FakeMemoryEngine({1: memory})
    manager = TimelineRebuildManager(
        str(db_path),
        FakeConversationManager(source_messages()),
        engine,
        FakeProcessor(),
    )
    await manager.initialize()

    low_only = await manager.preview(quality_filter="low")
    assert low_only["total_count"] == 1
    assert low_only["items"][0]["summary_quality"] == "low"

    engine.memories[1]["metadata"]["summary_quality"] = "normal"
    normal_only = await manager.preview(quality_filter="low")
    assert normal_only["total_count"] == 0


@pytest.mark.asyncio
async def test_rebuild_preserves_id_and_runs_local_topic_sync(tmp_path):
    db_path = tmp_path / "memory.db"
    await create_source_db(db_path)
    engine = FakeMemoryEngine({1: source_memory()})
    manager = TimelineRebuildManager(
        str(db_path), FakeConversationManager(source_messages()), engine, FakeProcessor()
    )
    await manager.initialize()
    started = await manager.start_task([1], topic_mode="local")
    finished = await wait_task(manager, started["task_uid"])
    assert finished["status"] == "completed"
    assert [item[0] for item in engine.rewrites] == [1]
    assert engine.memories[1]["metadata"]["revision"] == 2
    assert engine.memories[1]["metadata"]["memory_uid"] == "memory-1"
    assert engine.rewrites[0][1]["schedule_topic_maintenance"] is False
    assert engine.topic_build_manager.calls[0][1]["timeline_uids"] == ["memory-1"]


@pytest.mark.asyncio
async def test_local_sync_skips_unlinked_timeline(tmp_path):
    db_path = tmp_path / "memory.db"
    await create_source_db(db_path, linked=False)
    engine = FakeMemoryEngine({1: source_memory()})
    manager = TimelineRebuildManager(
        str(db_path), FakeConversationManager(source_messages()), engine, FakeProcessor()
    )
    await manager.initialize()
    started = await manager.start_task([1], topic_mode="local")
    finished = await wait_task(manager, started["task_uid"])
    assert finished["status"] == "completed"
    assert engine.topic_build_manager.calls == []
    assert finished["result"]["topic_results"]["space-1"]["status"] == "not_required"


@pytest.mark.asyncio
async def test_failed_rebuild_is_resumable_without_changing_id(tmp_path):
    db_path = tmp_path / "memory.db"
    await create_source_db(db_path)
    engine = FakeMemoryEngine({1: source_memory()})
    processor = FlakyProcessor()
    manager = TimelineRebuildManager(
        str(db_path), FakeConversationManager(source_messages()), engine, processor
    )
    await manager.initialize()

    started = await manager.start_task([1], topic_mode="local")
    failed = await wait_task(manager, started["task_uid"])

    assert failed["status"] == "completed_with_errors"
    assert engine.rewrites == []
    assert engine.memories[1]["text"] == "旧 Timeline"

    processor.fail = False
    await manager.resume_task(started["task_uid"])
    completed = await wait_task(manager, started["task_uid"])

    assert completed["status"] == "completed"
    assert engine.memories[1]["id"] == 1
    assert engine.memories[1]["text"] == "重构后的 Timeline"


@pytest.mark.asyncio
async def test_staged_edits_do_not_write_until_batch_apply_and_sync_once(tmp_path):
    db_path = tmp_path / "memory.db"
    await create_source_db(db_path)
    await add_second_timeline(db_path)
    memories = {1: source_memory(), 2: second_source_memory()}
    engine = FakeMemoryEngine(memories)
    manager = TimelineRebuildManager(
        str(db_path), FakeConversationManager(source_messages()), engine, FakeProcessor()
    )
    await manager.initialize()

    first = await manager.stage_edit(
        memory_id=1,
        prepared_payload=staged_payload(memories[1], "暂存后的 Timeline 一"),
    )
    second = await manager.stage_edit(
        memory_id=2,
        prepared_payload=staged_payload(memories[2], "暂存后的 Timeline 二"),
    )

    assert engine.rewrites == []
    assert engine.memories[1]["text"] == "旧 Timeline"
    assert {item["edit_uid"] for item in await manager.list_staged_edits()} == {
        first["edit_uid"],
        second["edit_uid"],
    }

    started = await manager.start_staged_task(
        [first["edit_uid"], second["edit_uid"]], topic_mode="local"
    )
    finished = await wait_task(manager, started["task_uid"])

    assert finished["status"] == "completed"
    assert [memory_id for memory_id, _ in engine.rewrites] == [1, 2]
    assert engine.memories[1]["text"] == "暂存后的 Timeline 一"
    assert engine.memories[2]["text"] == "暂存后的 Timeline 二"
    assert len(engine.topic_build_manager.calls) == 1
    assert engine.topic_build_manager.calls[0][1]["timeline_uids"] == [
        "memory-1",
        "memory-2",
    ]
    assert await manager.list_staged_edits() == []


@pytest.mark.asyncio
async def test_staged_edit_can_be_deleted_without_writing(tmp_path):
    db_path = tmp_path / "memory.db"
    await create_source_db(db_path)
    memory = source_memory()
    engine = FakeMemoryEngine({1: memory})
    manager = TimelineRebuildManager(
        str(db_path), FakeConversationManager(source_messages()), engine, FakeProcessor()
    )
    await manager.initialize()
    staged = await manager.stage_edit(
        memory_id=1,
        prepared_payload=staged_payload(memory, "不会应用的内容"),
    )

    assert await manager.delete_staged_edits([staged["edit_uid"]]) == 1
    assert await manager.list_staged_edits() == []
    assert engine.rewrites == []


@pytest.mark.asyncio
async def test_cancelling_after_staged_timeline_write_stops_before_topic_sync(tmp_path):
    db_path = tmp_path / "memory.db"
    await create_source_db(db_path)
    memory = source_memory()
    engine = BlockingMemoryEngine({1: memory})
    manager = TimelineRebuildManager(
        str(db_path), FakeConversationManager(source_messages()), engine, FakeProcessor()
    )
    await manager.initialize()
    staged = await manager.stage_edit(
        memory_id=1,
        prepared_payload=staged_payload(memory, "已应用但尚未同步 Topic"),
    )

    started = await manager.start_staged_task([staged["edit_uid"]])
    await asyncio.wait_for(engine.write_started.wait(), timeout=2)
    with pytest.raises(ValueError, match="正在执行维护任务"):
        await manager.stage_edit(
            memory_id=1,
            prepared_payload=staged_payload(memory, "不应在任务中重复暂存"),
        )
    assert await manager.cancel_task(started["task_uid"]) is True
    engine.release_write.set()
    finished = await wait_task(manager, started["task_uid"])

    assert finished["status"] == "cancelled"
    assert engine.memories[1]["text"] == "已应用但尚未同步 Topic"
    assert engine.topic_build_manager.calls == []
