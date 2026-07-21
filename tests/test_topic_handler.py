"""Tests for Topic WebUI build-job state reporting."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot_plugin_livingmemory.core.page_api_modules.topic_handler import TopicHandler
from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils


@pytest.mark.asyncio
async def test_failed_build_overrides_completed_scan_stage(monkeypatch):
    class FailingBuildManager:
        async def build_space(self, _memory_space_id, **kwargs):
            await kwargs["progress_callback"](
                {
                    "run_uid": "run-1",
                    "status": "completed",
                    "processed_items": 39,
                    "total_items": 39,
                }
            )
            raise RuntimeError("fact contains an unknown atom fingerprint")

    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={"memory_space_id": "space-1", "mode": "full"}
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    handler = TopicHandler(PageApiUtils())
    engine = SimpleNamespace(
        topic_memory_enabled=True,
        topic_build_manager=FailingBuildManager(),
    )

    response = await handler.start_build(engine)
    job_uid = response["data"]["job_uid"]
    task = next(iter(handler._tasks))
    await task

    job = handler._jobs[job_uid]
    assert job["status"] == "failed"
    assert job["stage"] == "failed"
    assert job["failed_stage"] == "candidate_scan_completed"
    assert job["current"] == 39
    assert job["total"] == 39
    assert job["overall_percent"] == 15.0
    assert "unknown atom fingerprint" in job["error"]


@pytest.mark.parametrize(
    ("stage", "current", "total", "expected"),
    [
        ("candidate_scan", 5, 10, 7.5),
        ("fragment_extraction", 1, 2, 30.0),
        ("embedding", 1, 2, 52.5),
        ("fragment_matching", 1, 2, 66.0),
        ("component_review", 1, 2, 77.0),
        ("topic_synthesis", 1, 2, 87.0),
        ("materialization", 1, 2, 96.0),
        ("completed", 0, 0, 100.0),
    ],
)
def test_overall_progress_uses_all_build_stages(
    stage, current, total, expected
):
    assert TopicHandler._overall_percent(stage, current, total) == expected


@pytest.mark.asyncio
async def test_lists_unindexed_timelines_for_selected_space(monkeypatch):
    request = MagicMock()
    request.args = {"memory_space_id": "space-1"}
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    candidate_manager = SimpleNamespace(
        list_unindexed_timelines=AsyncMock(
            return_value=[
                {
                    "timeline_uid": "timeline-1",
                    "revision": 2,
                    "summary": "尚未建立 Topic 索引",
                }
            ]
        )
    )
    engine = SimpleNamespace(
        topic_build_manager=SimpleNamespace(candidate_manager=candidate_manager)
    )

    response = await TopicHandler(PageApiUtils()).list_unindexed_timelines(engine)

    assert response["status"] == "ok"
    assert response["data"]["total"] == 1
    assert response["data"]["items"][0]["timeline_uid"] == "timeline-1"
    candidate_manager.list_unindexed_timelines.assert_awaited_once_with("space-1")


@pytest.mark.asyncio
async def test_recomputes_only_topic_relations(monkeypatch):
    request = MagicMock()
    request.get_json = AsyncMock(return_value={"memory_space_id": "space-1"})
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    manager = SimpleNamespace(
        has_active_builds=lambda: False,
        recompute_topic_relations=AsyncMock(
            return_value={
                "memory_space_id": "space-1",
                "topic_count": 64,
                "relation_count": 42,
                "algorithm_version": 5,
            }
        ),
    )
    engine = SimpleNamespace(topic_build_manager=manager)

    response = await TopicHandler(PageApiUtils()).recompute_relations(engine)

    assert response["status"] == "ok"
    assert response["data"]["relation_count"] == 42
    manager.recompute_topic_relations.assert_awaited_once_with("space-1")


@pytest.mark.asyncio
async def test_job_progress_is_monotonic_within_a_concurrent_stage(monkeypatch):
    release = asyncio.Event()

    class OutOfOrderBuildManager:
        async def build_space(self, memory_space_id, **kwargs):
            callback = kwargs["progress_callback"]
            await callback(
                {"stage": "topic_synthesis", "current": 3, "total": 10}
            )
            await callback(
                {"stage": "topic_synthesis", "current": 2, "total": 10}
            )
            await release.wait()
            return {"status": "completed", "memory_space_id": memory_space_id}

    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={"memory_space_id": "space-1", "mode": "full"}
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    handler = TopicHandler(PageApiUtils())
    engine = SimpleNamespace(
        topic_memory_enabled=True,
        topic_build_manager=OutOfOrderBuildManager(),
    )

    response = await handler.start_build(engine)
    job_uid = response["data"]["job_uid"]
    await asyncio.sleep(0)

    assert handler._jobs[job_uid]["current"] == 3
    assert handler._jobs[job_uid]["overall_percent"] == 85.0

    release.set()
    await next(iter(handler._tasks))


@pytest.mark.asyncio
async def test_duplicate_manual_build_returns_existing_job(monkeypatch):
    release = asyncio.Event()

    class BlockingBuildManager:
        def __init__(self):
            self.calls = 0

        async def build_space(self, memory_space_id, **_kwargs):
            self.calls += 1
            await release.wait()
            return {"status": "completed", "memory_space_id": memory_space_id}

    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={"memory_space_id": "space-1", "mode": "full"}
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    manager = BlockingBuildManager()
    engine = SimpleNamespace(
        topic_memory_enabled=True,
        topic_build_manager=manager,
    )
    handler = TopicHandler(PageApiUtils())

    first = await handler.start_build(engine)
    second = await handler.start_build(engine)

    assert second["status"] == "ok"
    assert second["data"]["job_uid"] == first["data"]["job_uid"]
    assert second["data"]["already_running"] is True
    assert len(handler._jobs) == 1
    await asyncio.sleep(0)
    assert manager.calls == 1

    release.set()
    await next(iter(handler._tasks))


@pytest.mark.asyncio
async def test_reset_full_build_is_forwarded_to_build_manager(monkeypatch):
    class ResetBuildManager:
        def __init__(self):
            self.kwargs = None

        async def build_space(self, memory_space_id, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed", "memory_space_id": memory_space_id}

    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={
            "memory_space_id": "space-1",
            "mode": "full",
            "reset_topics": True,
        }
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    manager = ResetBuildManager()
    handler = TopicHandler(PageApiUtils())
    engine = SimpleNamespace(
        topic_memory_enabled=True,
        topic_build_manager=manager,
    )

    response = await handler.start_build(engine)
    job_uid = response["data"]["job_uid"]
    await next(iter(handler._tasks))

    assert manager.kwargs["reset_topics"] is True
    assert handler._jobs[job_uid]["reset_topics"] is True
    assert handler._jobs[job_uid]["status"] == "completed"


@pytest.mark.asyncio
async def test_selected_unindexed_timelines_are_forwarded_without_time_window(
    monkeypatch,
):
    class SelectedBuildManager:
        def __init__(self):
            self.kwargs = None

        async def build_space(self, memory_space_id, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed", "memory_space_id": memory_space_id}

    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={
            "memory_space_id": "space-1",
            "mode": "incremental",
            "timeline_uids": ["timeline-2", "timeline-1", "timeline-2"],
        }
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    manager = SelectedBuildManager()
    handler = TopicHandler(PageApiUtils())
    engine = SimpleNamespace(
        topic_memory_enabled=True,
        topic_build_manager=manager,
    )

    response = await handler.start_build(engine)
    job_uid = response["data"]["job_uid"]
    await next(iter(handler._tasks))

    assert manager.kwargs["timeline_uids"] == ["timeline-2", "timeline-1"]
    assert manager.kwargs["since"] is None
    assert handler._jobs[job_uid]["status"] == "completed"


@pytest.mark.asyncio
async def test_reset_is_rejected_for_incremental_build(monkeypatch):
    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={
            "memory_space_id": "space-1",
            "mode": "incremental",
            "reset_topics": True,
        }
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    handler = TopicHandler(PageApiUtils())
    engine = SimpleNamespace(topic_memory_enabled=True)

    response = await handler.start_build(engine)

    assert response["status"] == "error"
    assert "全量构建" in response["message"]


@pytest.mark.asyncio
async def test_job_exposes_detailed_llm_progress(monkeypatch):
    class DetailedBuildManager:
        async def build_space(self, memory_space_id, **kwargs):
            await kwargs["progress_callback"](
                {
                    "run_uid": "run-1",
                    "stage": "topic_synthesis",
                    "current": 1,
                    "total": 3,
                    "activity": "llm_call",
                    "item_kind": "topic_component",
                    "item_index": 2,
                    "item_total": 3,
                    "fragment_count": 27,
                    "batch_fragment_count": 12,
                    "synthesis_level": 1,
                    "llm_call_current": 2,
                    "llm_call_total": 4,
                    "completed_groups": 5,
                    "active_group_count": 3,
                    "group_concurrency": 8,
                }
            )
            await asyncio.sleep(0)
            return {"status": "completed", "memory_space_id": memory_space_id}

    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={"memory_space_id": "space-1", "mode": "full"}
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    handler = TopicHandler(PageApiUtils())
    engine = SimpleNamespace(
        topic_memory_enabled=True,
        topic_build_manager=DetailedBuildManager(),
    )

    response = await handler.start_build(engine)
    job_uid = response["data"]["job_uid"]
    await asyncio.sleep(0)
    job = handler._jobs[job_uid]

    assert job["stage"] == "topic_synthesis"
    assert job["fragment_count"] == 27
    assert job["batch_fragment_count"] == 12
    assert job["llm_call_current"] == 2
    assert job["llm_call_total"] == 4
    assert job["completed_groups"] == 5
    assert job["active_group_count"] == 3
    assert job["group_concurrency"] == 8
    await next(iter(handler._tasks))


@pytest.mark.asyncio
async def test_failed_persisted_run_can_be_resumed(monkeypatch):
    class ResumeManager:
        def __init__(self):
            self.resumed_run_uid = None

        async def resume_run(self, run_uid, **_kwargs):
            self.resumed_run_uid = run_uid
            return {"status": "completed", "run_uid": run_uid}

    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={
            "memory_space_id": "space-1",
            "resume_run_uid": "run-failed",
        }
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    manager = ResumeManager()
    store = SimpleNamespace(
        get_maintenance_run=AsyncMock(
            return_value={
                "run_uid": "run-failed",
                "memory_space_id": "space-1",
                "mode": "full",
                "status": "failed",
                "stage": "topic_synthesis",
            }
        )
    )
    engine = SimpleNamespace(
        topic_memory_enabled=True,
        topic_memory_store=store,
        topic_build_manager=manager,
    )
    handler = TopicHandler(PageApiUtils())

    response = await handler.start_build(engine)
    job_uid = response["data"]["job_uid"]
    await next(iter(handler._tasks))

    assert manager.resumed_run_uid == "run-failed"
    assert handler._jobs[job_uid]["status"] == "completed"
    assert handler._jobs[job_uid]["resumed"] is True


def test_resumable_run_payload_preserves_failed_stage():
    payload = TopicHandler._resumable_run_payload(
        {
            "run_uid": "run-1",
            "memory_space_id": "space-1",
            "mode": "full",
            "status": "failed",
            "stage": "topic_synthesis",
            "current_group_index": 3,
            "total_groups": 8,
            "created_at": 10.0,
            "updated_at": 20.0,
            "error": "timeout",
        }
    )

    assert payload["resumable"] is True
    assert payload["failed_stage"] == "topic_synthesis"
    assert payload["overall_percent"] == 85.8


@pytest.mark.asyncio
async def test_persisted_breakpoint_task_can_be_discarded(monkeypatch):
    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={
            "run_uid": "run-failed",
            "memory_space_id": "space-1",
        }
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    store = SimpleNamespace(
        discard_maintenance_run=AsyncMock(
            return_value={
                "run_uid": "run-failed",
                "memory_space_id": "space-1",
                "deleted_intermediate_items": 7,
            }
        )
    )
    handler = TopicHandler(PageApiUtils())
    handler._jobs["old-job"] = {
        "job_uid": "old-job",
        "run_uid": "run-failed",
        "memory_space_id": "space-1",
        "status": "failed",
    }

    response = await handler.discard_build(
        SimpleNamespace(topic_memory_store=store)
    )

    assert response["status"] == "ok"
    assert response["data"]["deleted_intermediate_items"] == 7
    store.discard_maintenance_run.assert_awaited_once_with(
        "run-failed",
        memory_space_id="space-1",
    )
    assert "old-job" not in handler._jobs


@pytest.mark.asyncio
async def test_active_topic_build_cannot_be_discarded(monkeypatch):
    request = MagicMock()
    request.get_json = AsyncMock(
        return_value={
            "run_uid": "run-active",
            "memory_space_id": "space-1",
        }
    )
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.page_api_modules.topic_handler.request",
        request,
    )
    store = SimpleNamespace(discard_maintenance_run=AsyncMock())
    handler = TopicHandler(PageApiUtils())
    handler._jobs["active-job"] = {
        "job_uid": "active-job",
        "run_uid": "run-active",
        "memory_space_id": "space-1",
        "status": "running",
    }

    response = await handler.discard_build(
        SimpleNamespace(topic_memory_store=store)
    )

    assert response["status"] == "error"
    assert "正在运行" in response["message"]
    store.discard_maintenance_run.assert_not_awaited()
