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
