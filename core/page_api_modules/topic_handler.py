"""Read-only Topic-memory inspection and manual maintenance endpoints."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from quart import request

from astrbot.api import logger

from ..models.memory_identity import resolve_memory_space
from ..models.topic_memory import TopicMaintenanceMode
from ..timeline_settings import (
    SHARED_QUERY_SETTING_KEYS,
    TIMELINE_SETTING_DEFINITIONS,
    validate_timeline_setting,
)
from ..topic_settings import TOPIC_SETTING_DEFINITIONS, validate_topic_setting

if TYPE_CHECKING:
    from .utils import PageApiUtils


class TopicHandler:
    _PROGRESS_DETAIL_FIELDS = {
        "activity",
        "item_kind",
        "item_index",
        "item_total",
        "timeline_count",
        "group_timeline_count",
        "fragment_count",
        "batch_fragment_count",
        "batch_index",
        "batch_total",
        "synthesis_level",
        "llm_call_current",
        "llm_call_total",
        "llm_concurrency",
        "completed_groups",
        "active_group_count",
        "group_concurrency",
        "rerank_call_current",
        "rerank_call_total",
        "active_rerank_count",
        "rerank_concurrency",
        "reviewed_components",
        "active_component_review_count",
        "component_review_concurrency",
        "review_output_groups",
    }
    _STAGE_RANGES: dict[str, tuple[float, float]] = {
        "pending": (0.0, 0.0),
        "candidate_scan": (0.0, 15.0),
        "candidate_scan_completed": (15.0, 15.0),
        "fragment_extraction": (15.0, 45.0),
        "embedding": (45.0, 60.0),
        "fragment_matching": (60.0, 72.0),
        "component_review": (72.0, 82.0),
        "topic_synthesis": (82.0, 92.0),
        "materialization": (92.0, 100.0),
        "completed": (100.0, 100.0),
    }

    def __init__(self, utils: "PageApiUtils"):
        self.utils = utils
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task] = set()

    async def get_overview(self, memory_engine) -> dict[str, Any]:
        memory_space_id = self.utils.optional_text(request.args.get("memory_space_id"))
        try:
            store = memory_engine.topic_memory_store
            overview = await store.get_overview(memory_space_id)
            overview["enabled"] = bool(memory_engine.topic_memory_enabled)
            overview["auto_maintenance"] = bool(memory_engine.topic_auto_maintenance)
            overview["rerank_available"] = memory_engine.rerank_provider is not None
            rerank_config = getattr(
                memory_engine.rerank_provider, "provider_config", {}
            ) or {}
            overview["rerank_backend"] = (
                str(rerank_config.get("id") or "configured")
                if isinstance(rerank_config, dict)
                else type(memory_engine.rerank_provider).__name__
                if memory_engine.rerank_provider is not None
                else None
            )
            overview["memory_spaces"] = await store.list_memory_spaces()
            runs = await store.list_maintenance_runs(
                memory_space_id, limit=20
            )
            overview["runs"] = runs
            active_jobs = self._active_jobs()
            overview["active_jobs"] = active_jobs
            overview["active_job"] = next(
                (
                    job
                    for job in active_jobs
                    if job["memory_space_id"] == memory_space_id
                ),
                None,
            )
            overview["resumable_run"] = next(
                (
                    self._resumable_run_payload(run)
                    for run in runs[:1]
                    if str(run.get("status") or "")
                    in {"failed", "pending", "cancelled"}
                ),
                None,
            )
            return self.utils.ok(overview)
        except Exception as exc:
            logger.error("[PageAPI] 获取 Topic 概览失败", exc_info=True)
            return self.utils.error(str(exc))

    async def list_topics(self, memory_engine) -> dict[str, Any]:
        memory_space_id = self.utils.optional_text(request.args.get("memory_space_id"))
        if not memory_space_id:
            return self.utils.ok({"items": [], "memory_space_id": None})
        status = self.utils.optional_text(request.args.get("status"))
        try:
            limit = max(1, min(int(request.args.get("limit", 100)), 500))
            offset = max(0, int(request.args.get("offset", 0)))
            topics = await memory_engine.topic_memory_store.list_topics(
                memory_space_id,
                status=None if status in (None, "all") else status,
                limit=limit,
                offset=offset,
            )
            items = [self._topic_payload(item) for item in topics]
            for item in items:
                item["support"] = await memory_engine.topic_memory_store.get_topic_support_metrics(
                    item["topic_uid"]
                )
            return self.utils.ok(
                {"items": items, "memory_space_id": memory_space_id}
            )
        except Exception as exc:
            logger.error("[PageAPI] 获取 Topic 列表失败", exc_info=True)
            return self.utils.error(str(exc))

    async def _combined_settings(self, memory_engine, initializer) -> dict[str, Any]:
        payload = await memory_engine.get_topic_runtime_settings()
        timeline = await initializer.get_timeline_runtime_settings()
        for key in SHARED_QUERY_SETTING_KEYS:
            definition = dict(TIMELINE_SETTING_DEFINITIONS[key])
            definition["shared_query_setting"] = True
            definition["description"] = (
                f"{definition.get('description', '')} "
                "与 Timeline 参数面板共享，在任一页修改都会同步生效。"
            ).strip()
            payload["definitions"][key] = definition
            payload["effective"][key] = timeline["effective"][key]
            if key in timeline["overrides"]:
                payload["overrides"][key] = timeline["overrides"][key]
        payload["build_active"] = bool(
            self.has_active_jobs()
            or memory_engine.topic_build_manager.has_active_builds()
        )
        return payload

    async def get_settings(self, memory_engine, initializer) -> dict[str, Any]:
        try:
            payload = await self._combined_settings(memory_engine, initializer)
            return self.utils.ok(payload)
        except Exception as exc:
            logger.error("[PageAPI] 获取 Topic 参数失败", exc_info=True)
            return self.utils.error(str(exc))

    async def update_settings(self, memory_engine, initializer) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        changes = payload.get("changes", {})
        reset_keys = payload.get("reset_keys", [])
        reset_all = payload.get("reset_all", False)
        if not isinstance(changes, dict):
            return self.utils.error("changes 必须是对象")
        if not isinstance(reset_keys, list):
            return self.utils.error("reset_keys 必须是数组")
        if not isinstance(reset_all, bool):
            return self.utils.error("reset_all 必须是布尔值")
        if self.has_active_jobs() or memory_engine.topic_build_manager.has_active_builds():
            return self.utils.error("Topic 构建正在运行，暂时不能修改参数")
        try:
            known = set(TOPIC_SETTING_DEFINITIONS) | set(SHARED_QUERY_SETTING_KEYS)
            unknown = (set(changes) | {str(key) for key in reset_keys}) - known
            if unknown:
                raise ValueError("未知 Topic 参数: " + ", ".join(sorted(unknown)))
            topic_changes = {
                key: validate_topic_setting(key, value)
                for key, value in changes.items()
                if key in TOPIC_SETTING_DEFINITIONS
            }
            shared_changes = {
                key: validate_timeline_setting(key, value)
                for key, value in changes.items()
                if key in SHARED_QUERY_SETTING_KEYS
            }
            topic_reset_keys = [
                str(key) for key in reset_keys if str(key) in TOPIC_SETTING_DEFINITIONS
            ]
            shared_reset_keys = [
                str(key) for key in reset_keys if str(key) in SHARED_QUERY_SETTING_KEYS
            ]
            if topic_changes or topic_reset_keys or reset_all:
                await memory_engine.update_topic_runtime_settings(
                    topic_changes,
                    reset_keys=topic_reset_keys,
                    reset_all=reset_all,
                )
            if shared_changes or shared_reset_keys or reset_all:
                await initializer.update_timeline_runtime_settings(
                    shared_changes,
                    reset_keys=(
                        list(SHARED_QUERY_SETTING_KEYS)
                        if reset_all
                        else shared_reset_keys
                    ),
                    reset_all=False,
                )
            result = await self._combined_settings(memory_engine, initializer)
            return self.utils.ok(result)
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 保存 Topic 参数失败", exc_info=True)
            return self.utils.error(str(exc))

    async def get_topic_detail(self, memory_engine) -> dict[str, Any]:
        topic_uid = self.utils.optional_text(request.args.get("topic_uid"))
        if not topic_uid:
            return self.utils.error("topic_uid 不能为空")
        try:
            topic = await memory_engine.topic_memory_store.get_topic(topic_uid)
            if topic is None:
                return self.utils.error("Topic 不存在")
            provenance = await memory_engine.topic_memory_store.get_topic_provenance(
                topic_uid
            )
            related_topics = await memory_engine.topic_memory_store.list_topic_relations(
                topic_uid
            )
            return self.utils.ok(
                {
                    "topic": self._topic_payload(topic),
                    "provenance": provenance,
                    "support": await memory_engine.topic_memory_store.get_topic_support_metrics(
                        topic_uid
                    ),
                    "related_topics": related_topics,
                    "read_only": True,
                }
            )
        except Exception as exc:
            logger.error("[PageAPI] 获取 Topic 详情失败", exc_info=True)
            return self.utils.error(str(exc))

    async def list_unindexed_timelines(self, memory_engine) -> dict[str, Any]:
        memory_space_id = self.utils.optional_text(request.args.get("memory_space_id"))
        if not memory_space_id:
            return self.utils.error("memory_space_id 不能为空")
        try:
            candidate_manager = memory_engine.topic_build_manager.candidate_manager
            items = await candidate_manager.list_unindexed_timelines(memory_space_id)
            return self.utils.ok(
                {
                    "memory_space_id": memory_space_id,
                    "total": len(items),
                    "items": items,
                }
            )
        except Exception as exc:
            logger.error("[PageAPI] 检测未索引 Timeline 失败", exc_info=True)
            return self.utils.error(str(exc))

    async def recompute_relations(self, memory_engine) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        memory_space_id = self.utils.optional_text(payload.get("memory_space_id"))
        if not memory_space_id:
            return self.utils.error("memory_space_id 不能为空")
        if self.has_active_jobs() or memory_engine.topic_build_manager.has_active_builds():
            return self.utils.error("Topic 构建正在运行，暂时不能重算相关话题")
        try:
            result = await memory_engine.topic_build_manager.recompute_topic_relations(
                memory_space_id
            )
            return self.utils.ok(result)
        except (TypeError, ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 重算 Topic 关系失败", exc_info=True)
            return self.utils.error(str(exc))

    async def start_build(self, memory_engine) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        if not memory_engine.topic_memory_enabled:
            return self.utils.error("请先在插件配置中启用 Topic 记忆")
        resume_run_uid = self.utils.optional_text(payload.get("resume_run_uid"))
        memory_space_id = self.utils.optional_text(payload.get("memory_space_id"))
        reset_topics = payload.get("reset_topics", False)
        if not isinstance(reset_topics, bool):
            return self.utils.error("reset_topics 必须是布尔值")
        raw_timeline_uids = payload.get("timeline_uids")
        timeline_uids: list[str] | None = None
        if raw_timeline_uids is not None:
            if not isinstance(raw_timeline_uids, list):
                return self.utils.error("timeline_uids 必须是数组")
            timeline_uids = list(
                dict.fromkeys(
                    str(value or "").strip()
                    for value in raw_timeline_uids
                    if str(value or "").strip()
                )
            )
            if not timeline_uids:
                return self.utils.error("请至少选择一条 Timeline")
            if any(len(value) > 512 for value in timeline_uids):
                return self.utils.error("timeline_uid 长度无效")
        resume_run = None
        if resume_run_uid:
            if timeline_uids is not None:
                return self.utils.error("断点续建不能重新指定 Timeline")
            if reset_topics:
                return self.utils.error("断点续建不能同时清空 Topic")
            resume_run = await memory_engine.topic_memory_store.get_maintenance_run(
                resume_run_uid
            )
            if resume_run is None:
                return self.utils.error("要继续的 Topic 构建任务不存在")
            if str(resume_run.get("status") or "") not in {
                "failed",
                "pending",
                "cancelled",
            }:
                return self.utils.error("该 Topic 构建任务当前不可继续")
            run_space_id = str(resume_run.get("memory_space_id") or "")
            if memory_space_id and memory_space_id != run_space_id:
                return self.utils.error("继续任务与当前记忆空间不一致")
            memory_space_id = run_space_id
        if not memory_space_id:
            session_id = self.utils.optional_text(payload.get("session_id"))
            persona_id = self.utils.optional_text(payload.get("persona_id"))
            if not session_id and not persona_id:
                return self.utils.error(
                    "memory_space_id、session_id、persona_id 至少填写一项"
                )
            memory_space_id = resolve_memory_space(
                session_id, persona_id
            ).memory_space_id
        try:
            mode = TopicMaintenanceMode(
                str(
                    resume_run.get("mode")
                    if resume_run is not None
                    else payload.get("mode") or "full"
                )
            )
            since = payload.get("since")
            if mode is TopicMaintenanceMode.INCREMENTAL:
                since = (
                    None
                    if timeline_uids is not None
                    else float(since)
                    if since is not None
                    else time.time() - 86400.0
                )
        except (TypeError, ValueError):
            return self.utils.error("构建模式或 since 参数无效")
        if reset_topics and mode is not TopicMaintenanceMode.FULL:
            return self.utils.error("只有全量构建可以先清空 Topic")
        if timeline_uids is not None and mode is not TopicMaintenanceMode.INCREMENTAL:
            return self.utils.error("指定 Timeline 仅适用于增量补建")

        active_jobs = self._active_jobs()
        if active_jobs:
            active_job = active_jobs[0]
            if active_job.get("memory_space_id") == memory_space_id:
                return self.utils.ok({**active_job, "already_running": True})
            return self.utils.error(
                "已有 Topic 构建任务正在运行，请等待完成后再启动新任务"
            )

        job_uid = str(uuid.uuid4())
        self._jobs[job_uid] = {
            "job_uid": job_uid,
            "memory_space_id": memory_space_id,
            "mode": mode.value,
            "status": "pending",
            "stage": "pending",
            "current": 0,
            "total": 0,
            "overall_percent": 0.0,
            "created_at": time.time(),
            "stage_started_at": time.time(),
            "last_progress_at": time.time(),
            "run_uid": resume_run_uid,
            "resumed": bool(resume_run_uid),
            "reset_topics": reset_topics,
        }

        async def progress(event: dict[str, Any]) -> None:
            job = self._jobs.get(job_uid)
            if job is None:
                return
            event_stage = str(event.get("stage") or "").strip()
            event_status = str(event.get("status") or "").strip()
            if not event_stage:
                event_stage = (
                    "candidate_scan_completed"
                    if event_status == "completed"
                    else "candidate_scan"
                )
            now = time.time()
            previous_stage = str(job.get("stage") or "")
            if event_stage != previous_stage:
                job["stage_started_at"] = now
            for key in self._PROGRESS_DETAIL_FIELDS:
                job.pop(key, None)
            event_current = int(
                event.get("current", event.get("processed_items", 0)) or 0
            )
            event_total = int(
                event.get("total", event.get("total_items", 0)) or 0
            )
            if event_stage == previous_stage:
                event_current = max(int(job.get("current") or 0), event_current)
                event_total = max(int(job.get("total") or 0), event_total)
            job.update(
                {
                    "status": "running",
                    "stage": event_stage,
                    "current": event_current,
                    "total": event_total,
                    "last_progress_at": now,
                }
            )
            for key in self._PROGRESS_DETAIL_FIELDS:
                if key in event and event[key] is not None:
                    job[key] = event[key]
            overall_percent = self._overall_percent(
                event_stage,
                int(job["current"]),
                int(job["total"]),
            )
            job["overall_percent"] = (
                max(float(job.get("overall_percent") or 0.0), overall_percent)
                if event_stage == previous_stage
                else overall_percent
            )
            if event.get("run_uid"):
                job["run_uid"] = str(event["run_uid"])

        async def run() -> None:
            self._jobs[job_uid]["status"] = "running"
            try:
                if resume_run_uid:
                    result = await memory_engine.topic_build_manager.resume_run(
                        resume_run_uid,
                        progress_callback=progress,
                    )
                else:
                    result = await memory_engine.topic_build_manager.build_space(
                        memory_space_id,
                        mode=mode,
                        since=since,
                        timeline_uids=timeline_uids,
                        reset_topics=reset_topics,
                        progress_callback=progress,
                    )
                self._jobs[job_uid].update(
                    {
                        "status": "completed",
                        "stage": "completed",
                        "overall_percent": 100.0,
                        "result": result,
                        "completed_at": time.time(),
                        "last_progress_at": time.time(),
                    }
                )
            except asyncio.CancelledError:
                self._jobs[job_uid].update(
                    {
                        "status": "cancelled",
                        "stage": "cancelled",
                        "completed_at": time.time(),
                        "last_progress_at": time.time(),
                    }
                )
                raise
            except Exception as exc:
                failed_stage = str(self._jobs[job_uid].get("stage") or "")
                if not self._jobs[job_uid].get("run_uid"):
                    failed_runs = await memory_engine.topic_memory_store.list_maintenance_runs(
                        memory_space_id,
                        limit=1,
                    )
                    if failed_runs and str(failed_runs[0].get("status") or "") in {
                        "failed",
                        "pending",
                        "cancelled",
                    }:
                        self._jobs[job_uid]["run_uid"] = str(
                            failed_runs[0].get("run_uid") or ""
                        )
                self._jobs[job_uid].update(
                    {
                        "status": "failed",
                        "stage": "failed",
                        "failed_stage": failed_stage,
                        "error": str(exc),
                        "completed_at": time.time(),
                        "last_progress_at": time.time(),
                    }
                )
                logger.error("[PageAPI] Topic 构建失败", exc_info=True)

        task = asyncio.create_task(run(), name=f"livingmemory-topic-ui-{job_uid[:8]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self.utils.ok(dict(self._jobs[job_uid]))

    async def get_build_progress(self) -> dict[str, Any]:
        job_uid = self.utils.optional_text(request.args.get("job_uid"))
        if not job_uid or job_uid not in self._jobs:
            return self.utils.error("构建任务不存在或插件已重启")
        return self.utils.ok(dict(self._jobs[job_uid]))

    async def discard_build(self, memory_engine) -> dict[str, Any]:
        """Cancel a persisted breakpoint task by discarding its saved progress."""
        payload = await request.get_json(silent=True) or {}
        run_uid = self.utils.optional_text(payload.get("run_uid"))
        memory_space_id = self.utils.optional_text(payload.get("memory_space_id"))
        if not run_uid:
            return self.utils.error("run_uid 不能为空")
        active_job = next(
            (
                job
                for job in self._active_jobs()
                if job.get("run_uid") == run_uid
                or (
                    memory_space_id
                    and job.get("memory_space_id") == memory_space_id
                )
            ),
            None,
        )
        if active_job is not None:
            return self.utils.error("该记忆空间仍有 Topic 构建正在运行，不能清除断点")
        try:
            result = await memory_engine.topic_memory_store.discard_maintenance_run(
                run_uid,
                memory_space_id=memory_space_id,
            )
            for job_uid, job in list(self._jobs.items()):
                if job.get("run_uid") == run_uid:
                    self._jobs.pop(job_uid, None)
            return self.utils.ok(result)
        except ValueError as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 清除 Topic 构建断点失败", exc_info=True)
            return self.utils.error(str(exc))

    def _active_jobs(self) -> list[dict[str, Any]]:
        jobs = [
            dict(job)
            for job in self._jobs.values()
            if job.get("status") in {"pending", "running"}
        ]
        jobs.sort(key=lambda job: float(job.get("created_at") or 0), reverse=True)
        return jobs

    def has_active_jobs(self) -> bool:
        return bool(self._active_jobs())

    async def shutdown(self) -> None:
        """Cancel and drain WebUI build tasks before plugin-owned stores close."""
        tasks = [task for task in self._tasks if not task.done()]
        if not tasks:
            self._tasks.clear()
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    @classmethod
    def _resumable_run_payload(cls, run: dict[str, Any]) -> dict[str, Any]:
        stage = str(run.get("stage") or "candidate_scan")
        current = int(
            run.get("current_group_index")
            or run.get("processed_items")
            or 0
        )
        total = int(run.get("total_groups") or run.get("total_items") or 0)
        return {
            "job_uid": f"persisted:{run.get('run_uid')}",
            "run_uid": str(run.get("run_uid") or ""),
            "memory_space_id": str(run.get("memory_space_id") or ""),
            "mode": str(run.get("mode") or "full"),
            "status": str(run.get("status") or "failed"),
            "stage": "failed" if str(run.get("status")) == "failed" else stage,
            "failed_stage": stage if str(run.get("status")) == "failed" else None,
            "current": current,
            "total": total,
            "overall_percent": cls._overall_percent(stage, current, total),
            "created_at": float(run.get("created_at") or time.time()),
            "last_progress_at": float(run.get("updated_at") or time.time()),
            "error": str(run.get("error") or ""),
            "resumable": True,
        }

    @classmethod
    def _overall_percent(cls, stage: str, current: int, total: int) -> float:
        start, end = cls._STAGE_RANGES.get(stage, (0.0, 0.0))
        if end <= start:
            return round(end, 1)
        fraction = max(0.0, min(1.0, current / total)) if total > 0 else 0.0
        return round(start + (end - start) * fraction, 1)

    @staticmethod
    def _topic_payload(topic) -> dict[str, Any]:
        payload = asdict(topic)
        payload["status"] = topic.status.value
        return payload


__all__ = ["TopicHandler"]
