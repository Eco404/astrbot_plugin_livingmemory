"""Persistent, per-user ordered maintenance for derived user profiles."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from astrbot.api import logger

from ...storage.user_profile_store import UserProfileStore
from ..models.user_profile import UserProfileTask
from .user_profile_fact_maintainer import (
    UserProfileFactMaintainer,
    UserProfileFactMaintenancePlan,
)


class UserProfileMaintenanceManager:
    """Drain projection events serially per user and checkpoint each business stage."""

    def __init__(
        self,
        store: UserProfileStore,
        *,
        provider: Any = None,
        provider_resolver: Callable[..., Any] | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.store = store
        self.default_provider = provider
        self.provider_resolver = provider_resolver
        self.config = dict(config or {})
        self.fact_maintainer = UserProfileFactMaintainer(provider)
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._scheduled: dict[str, asyncio.Task] = {}
        self._retry_tasks: set[asyncio.Task] = set()
        self._closing = False
        self._semaphore = asyncio.Semaphore(self._concurrency())

    def apply_config(self, config: dict[str, Any]) -> None:
        old_concurrency = self._concurrency()
        self.config = dict(config)
        if self._concurrency() != old_concurrency:
            self._semaphore = asyncio.Semaphore(self._concurrency())

    async def start(self) -> None:
        """Resume durable tasks first, then any pending unbatched events."""
        limit = int(self.config.get("user_profile.startup_recovery_limit", 64))
        recoverable = await self.store.list_recoverable_tasks(limit=limit)
        for task in recoverable:
            scope_uid = str(task.get("profile_scope_uid") or "")
            if scope_uid:
                self._schedule_existing_task(scope_uid, task)
        pending = await self.store.list_pending_projection_events(limit=limit)
        for event in pending:
            scope_uid = str(event.get("profile_scope_uid") or "")
            if scope_uid:
                self.schedule_scope(scope_uid)

    async def close(self) -> None:
        self._closing = True
        tasks = [*self._scheduled.values(), *self._retry_tasks]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduled.clear()
        self._retry_tasks.clear()

    def schedule_scope(self, profile_scope_uid: str) -> None:
        if self._closing or not profile_scope_uid:
            return
        current = self._scheduled.get(profile_scope_uid)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._drain_scope(profile_scope_uid))
        self._scheduled[profile_scope_uid] = task
        task.add_done_callback(
            lambda finished, uid=profile_scope_uid: self._scheduled.pop(uid, None)
            if self._scheduled.get(uid) is finished
            else None
        )

    async def drain_scope(self, profile_scope_uid: str) -> None:
        """Synchronous entry used by tests and explicit maintenance actions."""
        await self._drain_scope(profile_scope_uid)

    def _schedule_existing_task(
        self, profile_scope_uid: str, task_payload: dict[str, Any]
    ) -> None:
        if self._closing or profile_scope_uid in self._scheduled:
            return
        task = asyncio.create_task(
            self._resume_then_drain(profile_scope_uid, task_payload)
        )
        self._scheduled[profile_scope_uid] = task
        task.add_done_callback(
            lambda finished, uid=profile_scope_uid: self._scheduled.pop(uid, None)
            if self._scheduled.get(uid) is finished
            else None
        )

    async def _resume_then_drain(
        self, profile_scope_uid: str, task_payload: dict[str, Any]
    ) -> None:
        lock = self._scope_locks.setdefault(profile_scope_uid, asyncio.Lock())
        async with self._semaphore, lock:
            succeeded = await self._process_task(task_payload)
            if not succeeded:
                return
        await self._drain_scope(profile_scope_uid)

    async def _drain_scope(self, profile_scope_uid: str) -> None:
        lock = self._scope_locks.setdefault(profile_scope_uid, asyncio.Lock())
        async with self._semaphore, lock:
            while not self._closing:
                scope = await self.store.get_scope(profile_scope_uid)
                if scope is None:
                    return
                if not scope.enabled:
                    await self.store.set_scope_state(profile_scope_uid, has_gap=True)
                    return
                events = await self.store.list_projection_events_for_scope(
                    profile_scope_uid,
                    limit=int(
                        self.config.get(
                            "user_profile.maintenance_batch_timeline_limit", 8
                        )
                    ),
                )
                if not events:
                    return
                provider = self._resolve_provider()
                task = UserProfileTask(
                    profile_scope_uid=profile_scope_uid,
                    settings_snapshot=dict(self.config),
                    provider_signature=self._provider_signature(provider),
                )
                await self.store.create_task(task, events)
                payload = await self.store.get_task(task.task_uid)
                if payload is None or not await self._process_task(payload, provider=provider):
                    return

    async def _process_task(
        self,
        task: dict[str, Any],
        *,
        provider: Any = None,
    ) -> bool:
        task_uid = str(task["task_uid"])
        scope_uid = str(task["profile_scope_uid"])
        settings = dict(task.get("settings_snapshot") or self.config)
        scope = await self.store.get_scope(scope_uid)
        if scope is None or not scope.enabled:
            await self.store.update_task(
                task_uid,
                status="cancelled",
                error="Profile scope is disabled or no longer exists",
                clear_persona_prompt=True,
            )
            await self.store.finish_task_events(
                task_uid, status="pending", error="Profile scope is disabled"
            )
            if scope is not None:
                await self.store.set_scope_state(scope_uid, has_gap=True)
            return False

        result_summary = dict(task.get("result_summary") or {})
        try:
            if not result_summary.get("facts_checkpoint"):
                await self.store.update_task(task_uid, status="running_facts")
                projections: list[dict[str, Any]] = []
                candidates = []
                for item in task.get("items") or []:
                    event_payload = dict(item.get("event_payload") or {})
                    operation = str(item.get("event_operation") or "upsert")
                    timeline_uid = str(item.get("timeline_uid") or "")
                    sources = []
                    if operation in {"upsert", "restore"}:
                        source_metadata = dict(event_payload.get("metadata") or {})
                        source_metadata["memory_uid"] = timeline_uid
                        source_metadata["revision"] = max(
                            1, int(item.get("timeline_revision") or 1)
                        )
                        event_payload["metadata"] = source_metadata
                        sources = self.fact_maintainer.extract_candidates(
                            event_payload,
                            actor_id=str(event_payload.get("profile_actor_id") or ""),
                        )
                        for source in sources:
                            source.timeline_uid = timeline_uid or source.timeline_uid
                            source.timeline_revision = max(
                                1, int(item.get("timeline_revision") or 1)
                            )
                    projections.append(
                        {
                            "timeline_uid": timeline_uid,
                            "timeline_revision": int(item.get("timeline_revision") or 1),
                            "operation": operation,
                            "sources": sources,
                        }
                    )
                    candidates.extend(sources)

                existing = await self.store.list_facts_for_maintenance(
                    scope.fact_namespace_uid
                )
                selected_provider = provider or self._resolve_provider()
                if candidates:
                    plan = await self.fact_maintainer.maintain(
                        fact_namespace_uid=scope.fact_namespace_uid,
                        candidates=candidates,
                        existing_facts=existing,
                        settings=settings,
                        provider=selected_provider,
                    )
                else:
                    plan = UserProfileFactMaintenancePlan()
                self._apply_lifecycle(plan, candidates, settings)
                expected_revision = await self.store.get_fact_namespace_revision(
                    scope.fact_namespace_uid
                )
                fact_revision = await self.store.apply_fact_projection_batch(
                    fact_namespace_uid=scope.fact_namespace_uid,
                    projections=projections,
                    facts=plan.facts,
                    source_assignments=plan.source_assignments,
                    conflicts=plan.conflicts,
                    expected_revision=expected_revision,
                    checkpoint_task_uid=task_uid,
                )
                result_summary.update(
                    {
                        "facts_checkpoint": True,
                        "fact_revision": fact_revision,
                        "fact_diagnostics": plan.diagnostics,
                    }
                )
                await self.store.update_task_items(
                    task_uid,
                    status="facts_completed",
                    result={"fact_revision": fact_revision},
                )
                await self.store.update_task(
                    task_uid,
                    status="facts_completed",
                    result_summary=result_summary,
                )

            # The relationship stage is attached in Phase 3. Keeping this boundary
            # explicit makes restart recovery skip an already published fact revision.
            await self.store.update_task(
                task_uid,
                status="completed",
                result_summary=result_summary,
                clear_persona_prompt=True,
            )
            cursor = await self.store.finish_task_events(
                task_uid, status="completed", error=None
            )
            await self.store.set_scope_state(
                scope_uid,
                projection_cursor=cursor,
                has_gap=False,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retries = int(task.get("retries") or 0) + 1
            base = float(
                settings.get("user_profile.maintenance_retry_base_seconds", 60)
            )
            maximum = float(
                settings.get("user_profile.maintenance_retry_max_seconds", 3600)
            )
            delay = min(maximum, base * (2 ** max(0, retries - 1)))
            await self.store.update_task(
                task_uid,
                status="facts_failed",
                error=str(exc)[:4000],
                result_summary=result_summary,
                retries=retries,
                next_retry_at=time.time() + delay,
                clear_persona_prompt=True,
            )
            await self.store.finish_task_events(
                task_uid, status="pending", error=str(exc)[:4000]
            )
            await self.store.set_scope_state(scope_uid, has_gap=True)
            logger.error(
                "[UserProfile] 客观事实维护失败 (scope=%s, task=%s): %s",
                scope_uid,
                task_uid,
                exc,
                exc_info=True,
            )
            self._schedule_retry(scope_uid, task_uid, delay)
            return False

    def _resolve_provider(self) -> Any:
        provider_id = str(self.config.get("user_profile.provider_id") or "")
        if self.provider_resolver is not None:
            try:
                resolved = self.provider_resolver(provider_id)
            except TypeError:
                resolved = self.provider_resolver()
            if isinstance(resolved, dict):
                resolved = resolved.get("llm_provider")
            if resolved is not None:
                return resolved
        return self.default_provider

    def _schedule_retry(
        self, profile_scope_uid: str, task_uid: str, delay: float
    ) -> None:
        if self._closing:
            return

        async def wake() -> None:
            await asyncio.sleep(max(0.0, delay))
            task_payload = await self.store.get_task(task_uid)
            if task_payload is not None:
                self._schedule_existing_task(profile_scope_uid, task_payload)
            else:
                self.schedule_scope(profile_scope_uid)

        task = asyncio.create_task(wake())
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    def _concurrency(self) -> int:
        return max(
            1,
            min(
                16,
                int(self.config.get("user_profile.maintenance_concurrency", 1)),
            ),
        )

    @staticmethod
    def _provider_signature(provider: Any) -> dict[str, Any]:
        if provider is None:
            return {}
        config = getattr(provider, "provider_config", {}) or {}
        if not isinstance(config, dict):
            config = {}
        return {
            "provider_id": str(config.get("id") or type(provider).__name__),
            "model": str(config.get("model") or config.get("model_name") or ""),
        }

    @staticmethod
    def _apply_lifecycle(
        plan: UserProfileFactMaintenancePlan,
        candidates: list[Any],
        settings: dict[str, Any],
    ) -> None:
        source_by_uid = {item.source_uid: item for item in candidates}
        now = time.time()
        day = 86400.0
        windows = {
            "preference": (
                "user_profile.preference_fixed_days",
                "user_profile.preference_review_days",
            ),
            "communication_preference": (
                "user_profile.communication_fixed_days",
                "user_profile.communication_review_days",
            ),
            "habit": (
                "user_profile.habit_fixed_days",
                "user_profile.habit_review_days",
            ),
            "current_state": (
                "user_profile.current_state_fixed_days",
                "user_profile.current_state_review_days",
            ),
        }
        for fact in plan.facts:
            category = str(getattr(fact.category, "value", fact.category))
            source = source_by_uid.get(fact.representative_source_uid)
            confirmed = (
                source.evidence_ended_at if source is not None else None
            ) or fact.last_confirmed_at or now
            if category in windows:
                fixed_key, review_key = windows[category]
                fact.fixed_injection_until = confirmed + int(settings[fixed_key]) * day
                fact.review_after = confirmed + int(settings[review_key]) * day
            elif category == "plan_commitment":
                temporal = source.metadata.get("fact_temporal", {}) if source else {}
                end_at = temporal.get("end_at") or temporal.get("ended_at")
                try:
                    end_timestamp = float(end_at)
                except (TypeError, ValueError):
                    end_timestamp = None
                if end_timestamp is not None:
                    fact.fixed_injection_until = end_timestamp
                    fact.review_after = end_timestamp + int(
                        settings["user_profile.dated_plan_grace_days"]
                    ) * day
                else:
                    fact.review_after = confirmed + int(
                        settings["user_profile.undated_plan_review_days"]
                    ) * day
            if str(getattr(fact.status, "value", fact.status)) == "pending":
                fact.review_after = min(
                    fact.review_after or float("inf"),
                    confirmed
                    + int(settings["user_profile.pending_retention_days"]) * day,
                )


__all__ = ["UserProfileMaintenanceManager"]
