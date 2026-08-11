"""Persistent, per-user ordered maintenance for derived user profiles."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from astrbot.api import logger

from ...storage.user_profile_store import (
    UserProfileRevisionConflict,
    UserProfileStore,
)
from ..models.user_profile import UserProfileTask, UserRelationshipState
from .user_profile_fact_maintainer import (
    UserProfileFactMaintainer,
    UserProfileFactMaintenancePlan,
)
from .user_relationship_maintainer import UserRelationshipMaintainer


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
        self.relationship_maintainer = UserRelationshipMaintainer(provider)
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._namespace_locks: dict[str, asyncio.Lock] = {}
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

    async def set_relationship_frozen(
        self, profile_scope_uid: str, frozen: bool
    ) -> Any:
        lock = self._scope_locks.setdefault(profile_scope_uid, asyncio.Lock())
        async with lock:
            return await self.store.set_scope_state(
                profile_scope_uid, relationship_frozen=bool(frozen)
            )

    async def update_relationship_manually(
        self,
        profile_scope_uid: str,
        *,
        changes: dict[str, Any],
        reason: str | None = None,
        expected_revision: int | None = None,
        sensitivity_override: str | None | object = ...,
        behavior_override: str | None | object = ...,
    ) -> UserRelationshipState:
        lock = self._scope_locks.setdefault(profile_scope_uid, asyncio.Lock())
        async with lock:
            scope = await self.store.get_scope(profile_scope_uid)
            if scope is None:
                raise ValueError("Unknown user-profile scope")
            current = await self.store.get_relationship(profile_scope_uid)
            current_revision = current.revision if current is not None else 0
            if expected_revision is not None and current_revision != int(
                expected_revision
            ):
                raise UserProfileRevisionConflict(
                    f"Expected relationship revision {expected_revision}, "
                    f"got {current_revision}"
                )
            state = self._relationship_copy(current, profile_scope_uid)
            for key in (
                "familiarity",
                "trust",
                "warmth",
                "ease",
                "tension",
                "concern",
            ):
                if key in changes:
                    state_value = float(changes[key])
                    if state_value > 1.0:
                        state_value /= 100.0
                    setattr(state, key, max(0.0, min(1.0, state_value)))
            if "stance_tags" in changes:
                tags = []
                for value in changes.get("stance_tags") or []:
                    tag = str(value).strip()[:40]
                    if tag and tag not in tags:
                        tags.append(tag)
                    if len(tags) >= 8:
                        break
                state.stance_tags = tags
            if "subjective_summary" in changes:
                limit = int(
                    self.config.get(
                        "user_profile.relationship_narrative_max_chars", 500
                    )
                )
                state.subjective_summary = str(
                    changes.get("subjective_summary") or ""
                ).strip()[:limit]
            if sensitivity_override is not ... and sensitivity_override not in {
                None,
                "very_slow",
                "slow",
                "balanced",
                "fast",
                "very_fast",
            }:
                raise ValueError("Unsupported relationship sensitivity override")
            if behavior_override is not ... and behavior_override not in {
                None,
                "restrained",
                "natural",
                "high_autonomy",
                "unrestricted",
            }:
                raise ValueError("Unsupported relationship behavior override")
            published = await self.store.publish_relationship(
                state,
                expected_revision=current_revision,
                operation="manual_update",
                reason=reason,
                change_summary="Administrator edited relationship state",
                full_revision_limit=int(
                    self.config.get(
                        "user_profile.relationship_full_revision_limit", 100
                    )
                ),
            )
            if sensitivity_override is not ... or behavior_override is not ...:
                await self.store.set_scope_state(
                    profile_scope_uid,
                    sensitivity_override=sensitivity_override,
                    behavior_override=behavior_override,
                )
            return published

    async def reset_relationship(
        self, profile_scope_uid: str, *, reason: str | None = None
    ) -> UserRelationshipState | None:
        lock = self._scope_locks.setdefault(profile_scope_uid, asyncio.Lock())
        async with lock:
            scope = await self.store.get_scope(profile_scope_uid)
            if scope is None:
                raise ValueError("Unknown user-profile scope")
            current = await self.store.get_relationship(profile_scope_uid)
            reset_at = time.time()
            published = None
            if current is not None:
                published = await self.store.publish_relationship(
                    UserRelationshipState(
                        relationship_uid=current.relationship_uid,
                        profile_scope_uid=profile_scope_uid,
                        source_timeline_uids=[],
                        persona_signature=current.persona_signature,
                        created_at=current.created_at,
                    ),
                    expected_revision=current.revision,
                    operation="reset",
                    reason=reason,
                    change_summary="Relationship state reset",
                    full_revision_limit=int(
                        self.config.get(
                            "user_profile.relationship_full_revision_limit", 100
                        )
                    ),
                )
            await self.store.set_scope_state(
                profile_scope_uid, relationship_reset_after=reset_at
            )
            return published

    async def rollback_relationship(
        self,
        profile_scope_uid: str,
        revision: int,
        *,
        reason: str | None = None,
    ) -> UserRelationshipState:
        lock = self._scope_locks.setdefault(profile_scope_uid, asyncio.Lock())
        async with lock:
            current = await self.store.get_relationship(profile_scope_uid)
            if current is None:
                raise ValueError("Relationship state does not exist")
            historical = await self.store.get_relationship_revision(
                current.relationship_uid, int(revision)
            )
            if historical is None or not historical.get("full_snapshot"):
                raise ValueError("Relationship revision is unavailable or compacted")
            snapshot = dict(historical.get("after_state") or {})
            state = self._relationship_from_snapshot(
                snapshot, profile_scope_uid, current.relationship_uid
            )
            state.created_at = current.created_at
            return await self.store.publish_relationship(
                state,
                expected_revision=current.revision,
                operation="rollback",
                reason=reason,
                change_summary=f"Rolled back to relationship revision {revision}",
                full_revision_limit=int(
                    self.config.get(
                        "user_profile.relationship_full_revision_limit", 100
                    )
                ),
            )

    async def rebuild_relationship_from_projection_history(
        self,
        profile_scope_uid: str,
        *,
        use_all_history: bool = True,
        reason: str | None = None,
    ) -> UserRelationshipState | None:
        """Replace the current relationship from durable projection history."""
        lock = self._scope_locks.setdefault(profile_scope_uid, asyncio.Lock())
        async with lock:
            scope = await self.store.get_scope(profile_scope_uid)
            if scope is None:
                raise ValueError("Unknown user-profile scope")
            history = await self.store.list_projection_history(profile_scope_uid)
            latest_by_timeline: dict[str, dict[str, Any]] = {}
            for event in history:
                latest_by_timeline[str(event.get("timeline_uid") or "")] = event
            active_events = [
                event
                for event in latest_by_timeline.values()
                if str(event.get("operation") or "") in {"upsert", "restore"}
            ]
            actor_id = next(
                (
                    str(event.get("payload", {}).get("profile_actor_id") or "")
                    for event in reversed(active_events)
                    if event.get("payload", {}).get("profile_actor_id")
                ),
                "",
            )
            timelines = self.relationship_maintainer.meaningful_timelines(
                [
                    {
                        "operation": event.get("operation"),
                        "timeline_uid": event.get("timeline_uid"),
                        "timeline_revision": event.get("timeline_revision"),
                        "metadata": event.get("payload", {}).get("metadata") or {},
                    }
                    for event in active_events
                ],
                actor_id=actor_id,
                reset_after=(
                    None if use_all_history else scope.relationship_reset_after
                ),
            )
            current = await self.store.get_relationship(profile_scope_uid)
            if not timelines:
                if current is None:
                    return None
                return await self.store.publish_relationship(
                    UserRelationshipState(
                        relationship_uid=current.relationship_uid,
                        profile_scope_uid=profile_scope_uid,
                        persona_signature=current.persona_signature,
                        created_at=current.created_at,
                    ),
                    expected_revision=current.revision,
                    operation="history_rebuild",
                    reason=reason,
                    change_summary="Relationship history rebuild produced no state",
                    full_revision_limit=int(
                        self.config.get(
                            "user_profile.relationship_full_revision_limit", 100
                        )
                    ),
                )
            persona_snapshot = self._persona_snapshot_from_events(active_events)
            objective_facts = await self.store.list_serving_facts(
                scope.fact_namespace_uid, include_sensitive=False
            )
            sensitivity = str(
                scope.relationship_sensitivity_override
                or self.config.get(
                    "user_profile.relationship_sensitivity", "balanced"
                )
            )
            behavior_mode = str(
                scope.relationship_behavior_override
                or self.config.get(
                    "user_profile.relationship_behavior_mode", "natural"
                )
            )
            maintained = await self.relationship_maintainer.maintain(
                profile_scope_uid=profile_scope_uid,
                timelines=timelines,
                current_state=None,
                persona_snapshot=persona_snapshot,
                objective_facts=objective_facts,
                sensitivity=sensitivity,
                behavior_mode=behavior_mode,
                settings=self.config,
                provider=self._resolve_provider(),
            )
            if maintained is None:
                return None
            maintained.diagnostics["persona_basis"] = (
                "timeline_snapshot" if persona_snapshot.get("prompt") else "unavailable"
            )
            maintained.state.relationship_uid = (
                current.relationship_uid
                if current is not None
                else maintained.state.relationship_uid
            )
            maintained.state.created_at = (
                current.created_at if current is not None else maintained.state.created_at
            )
            return await self.store.publish_relationship(
                maintained.state,
                expected_revision=(current.revision if current is not None else 0),
                operation="history_rebuild",
                reason=reason,
                change_summary=maintained.change_summary,
                diagnostics=maintained.diagnostics,
                full_revision_limit=int(
                    self.config.get(
                        "user_profile.relationship_full_revision_limit", 100
                    )
                ),
            )

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
                persona_snapshot = self._persona_snapshot_from_events(events)
                task = UserProfileTask(
                    profile_scope_uid=profile_scope_uid,
                    settings_snapshot=dict(self.config),
                    provider_signature=self._provider_signature(provider),
                    persona_signature=dict(
                        persona_snapshot.get("signature") or {}
                    ),
                    persona_prompt=str(persona_snapshot.get("prompt") or ""),
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
        selected_provider = provider or self._resolve_provider()
        fact_error: Exception | None = None
        relationship_error: Exception | None = None

        if not result_summary.get("facts_checkpoint"):
            try:
                namespace_lock = self._namespace_locks.setdefault(
                    str(scope.fact_namespace_uid), asyncio.Lock()
                )
                async with namespace_lock:
                    fact_result = await self._run_fact_stage(
                        task=task,
                        scope=scope,
                        settings=settings,
                        provider=selected_provider,
                    )
                result_summary.update(fact_result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                fact_error = exc
                result_summary["facts_error"] = str(exc)[:4000]
                logger.error(
                    "[UserProfile] 客观事实维护失败 (scope=%s, task=%s): %s",
                    scope_uid,
                    task_uid,
                    exc,
                    exc_info=True,
                )

        if not result_summary.get("relationship_checkpoint"):
            try:
                relationship_result = await self._run_relationship_stage(
                    task=task,
                    scope=scope,
                    settings=settings,
                    provider=selected_provider,
                )
                result_summary.update(relationship_result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                relationship_error = exc
                result_summary["relationship_error"] = str(exc)[:4000]
                logger.error(
                    "[UserProfile] 人格关系维护失败 (scope=%s, task=%s): %s",
                    scope_uid,
                    task_uid,
                    exc,
                    exc_info=True,
                )

        if fact_error is not None or relationship_error is not None:
            retries = int(task.get("retries") or 0) + 1
            base = float(
                settings.get("user_profile.maintenance_retry_base_seconds", 60)
            )
            maximum = float(
                settings.get("user_profile.maintenance_retry_max_seconds", 3600)
            )
            delay = min(maximum, base * (2 ** max(0, retries - 1)))
            failure_status = "facts_failed" if fact_error is not None else "facts_completed"
            error_text = str(fact_error or relationship_error)[:4000]
            await self.store.update_task(
                task_uid,
                status=failure_status,
                error=error_text,
                result_summary=result_summary,
                retries=retries,
                next_retry_at=time.time() + delay,
            )
            await self.store.finish_task_events(
                task_uid, status="pending", error=error_text
            )
            await self.store.set_scope_state(scope_uid, has_gap=True)
            self._schedule_retry(scope_uid, task_uid, delay)
            return False

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

    async def _run_fact_stage(
        self,
        *,
        task: dict[str, Any],
        scope: Any,
        settings: dict[str, Any],
        provider: Any,
    ) -> dict[str, Any]:
        task_uid = str(task["task_uid"])
        if not bool(settings.get("user_profile.enabled", True)):
            result = {"facts_checkpoint": True, "facts_skipped": "profile_disabled"}
            current = await self.store.get_task(task_uid)
            await self.store.update_task(
                task_uid,
                status="facts_completed",
                result_summary={
                    **dict((current or task).get("result_summary") or {}),
                    **result,
                },
            )
            return result
        await self.store.update_task(task_uid, status="running_facts")
        projections: list[dict[str, Any]] = []
        candidates = []
        for event in self._normalized_task_events(task):
            event_payload = event["payload"]
            operation = event["operation"]
            timeline_uid = event["timeline_uid"]
            sources = []
            if operation in {"upsert", "restore"}:
                sources = self.fact_maintainer.extract_candidates(
                    event_payload,
                    actor_id=str(event_payload.get("profile_actor_id") or ""),
                )
            projections.append(
                {
                    "timeline_uid": timeline_uid,
                    "timeline_revision": event["timeline_revision"],
                    "operation": operation,
                    "sources": sources,
                }
            )
            candidates.extend(sources)

        existing = await self.store.list_facts_for_maintenance(
            scope.fact_namespace_uid
        )
        if candidates:
            plan = await self.fact_maintainer.maintain(
                fact_namespace_uid=scope.fact_namespace_uid,
                candidates=candidates,
                existing_facts=existing,
                settings=settings,
                provider=provider,
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
        result = {
            "facts_checkpoint": True,
            "fact_revision": fact_revision,
            "fact_diagnostics": plan.diagnostics,
        }
        await self.store.update_task_items(
            task_uid,
            status="facts_completed",
            result={"fact_revision": fact_revision},
        )
        await self.store.update_task(
            task_uid,
            status="facts_completed",
            result_summary={**dict(task.get("result_summary") or {}), **result},
        )
        return result

    async def _run_relationship_stage(
        self,
        *,
        task: dict[str, Any],
        scope: Any,
        settings: dict[str, Any],
        provider: Any,
    ) -> dict[str, Any]:
        task_uid = str(task["task_uid"])
        if not bool(settings.get("user_profile.relationship_enabled", True)):
            return await self._checkpoint_skipped_relationship(
                task, "relationship_disabled"
            )
        if scope.relationship_frozen:
            return await self._checkpoint_skipped_relationship(
                task, "relationship_frozen"
            )
        events = self._normalized_task_events(task)
        actor_id = next(
            (
                str(event["payload"].get("profile_actor_id") or "")
                for event in events
                if event["payload"].get("profile_actor_id")
            ),
            "",
        )
        timelines = self.relationship_maintainer.meaningful_timelines(
            [
                {
                    "operation": event["operation"],
                    "timeline_uid": event["timeline_uid"],
                    "timeline_revision": event["timeline_revision"],
                    "metadata": event["payload"].get("metadata") or {},
                }
                for event in events
            ],
            actor_id=actor_id,
            reset_after=scope.relationship_reset_after,
        )
        if not timelines:
            return await self._checkpoint_skipped_relationship(
                task, "no_meaningful_user_interaction"
            )

        await self.store.update_task(task_uid, status="running_relationship")
        current = await self.store.get_relationship(scope.profile_scope_uid)
        objective_facts = await self.store.list_serving_facts(
            scope.fact_namespace_uid,
            include_sensitive=False,
        )
        persona_snapshot = self._persona_snapshot_from_task(task)
        sensitivity = str(
            scope.relationship_sensitivity_override
            or settings.get("user_profile.relationship_sensitivity", "balanced")
        )
        behavior_mode = str(
            scope.relationship_behavior_override
            or settings.get("user_profile.relationship_behavior_mode", "natural")
        )
        maintained = await self.relationship_maintainer.maintain(
            profile_scope_uid=scope.profile_scope_uid,
            timelines=timelines,
            current_state=current,
            persona_snapshot=persona_snapshot,
            objective_facts=objective_facts,
            sensitivity=sensitivity,
            behavior_mode=behavior_mode,
            settings=settings,
            provider=provider,
        )
        if maintained is None:
            return await self._checkpoint_skipped_relationship(
                task, "maintainer_declined"
            )
        published = await self.store.publish_relationship(
            maintained.state,
            expected_revision=(current.revision if current is not None else 0),
            operation="automatic",
            change_summary=maintained.change_summary,
            diagnostics=maintained.diagnostics,
            provider_signature=dict(task.get("provider_signature") or {}),
            full_revision_limit=int(
                settings.get("user_profile.relationship_full_revision_limit", 100)
            ),
            checkpoint_task_uid=task_uid,
        )
        result = {
            "relationship_checkpoint": True,
            "relationship_revision": published.revision,
            "relationship_diagnostics": maintained.diagnostics,
        }
        await self.store.update_task_items(
            task_uid,
            status="relationship_completed",
            result={"relationship_revision": published.revision},
        )
        return result

    async def _checkpoint_skipped_relationship(
        self, task: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        result = {
            "relationship_checkpoint": True,
            "relationship_skipped": reason,
        }
        task_uid = str(task["task_uid"])
        current = await self.store.get_task(task_uid)
        await self.store.update_task(
            task_uid,
            status="facts_completed",
            result_summary={
                **dict((current or task).get("result_summary") or {}),
                **result,
            },
        )
        return result

    @staticmethod
    def _normalized_task_events(task: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in task.get("items") or []:
            payload = dict(item.get("event_payload") or {})
            metadata = dict(payload.get("metadata") or {})
            timeline_uid = str(item.get("timeline_uid") or "")
            revision = max(1, int(item.get("timeline_revision") or 1))
            metadata["memory_uid"] = timeline_uid
            metadata["revision"] = revision
            payload["metadata"] = metadata
            result.append(
                {
                    "timeline_uid": timeline_uid,
                    "timeline_revision": revision,
                    "operation": str(item.get("event_operation") or "upsert"),
                    "payload": payload,
                }
            )
        return result

    @staticmethod
    def _persona_snapshot_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in reversed(events):
            payload = event.get("payload") or {}
            snapshot = payload.get("persona_snapshot")
            if isinstance(snapshot, dict):
                return dict(snapshot)
        return {}

    @classmethod
    def _persona_snapshot_from_task(cls, task: dict[str, Any]) -> dict[str, Any]:
        event_snapshots = [
            {
                "payload": dict(item.get("event_payload") or {})
            }
            for item in task.get("items") or []
        ]
        snapshot = cls._persona_snapshot_from_events(event_snapshots)
        snapshot["prompt"] = str(task.get("persona_prompt") or snapshot.get("prompt") or "")
        snapshot["signature"] = dict(
            task.get("persona_signature") or snapshot.get("signature") or {}
        )
        return snapshot

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
    def _relationship_copy(
        current: UserRelationshipState | None, profile_scope_uid: str
    ) -> UserRelationshipState:
        if current is None:
            return UserRelationshipState(profile_scope_uid=profile_scope_uid)
        return UserRelationshipState(
            relationship_uid=current.relationship_uid,
            profile_scope_uid=profile_scope_uid,
            revision=current.revision,
            **current.dimensions(),
            stance_tags=list(current.stance_tags),
            subjective_summary=current.subjective_summary,
            recent_aftereffect=current.recent_aftereffect,
            aftereffect_expires_at=current.aftereffect_expires_at,
            persona_signature=dict(current.persona_signature),
            source_timeline_uids=list(current.source_timeline_uids),
            created_at=current.created_at,
            updated_at=current.updated_at,
        )

    @staticmethod
    def _relationship_from_snapshot(
        snapshot: dict[str, Any],
        profile_scope_uid: str,
        relationship_uid: str,
    ) -> UserRelationshipState:
        return UserRelationshipState(
            relationship_uid=relationship_uid,
            profile_scope_uid=profile_scope_uid,
            **{
                key: float(snapshot.get(key, 0.0) or 0.0)
                for key in (
                    "familiarity",
                    "trust",
                    "warmth",
                    "ease",
                    "tension",
                    "concern",
                )
            },
            stance_tags=[str(item) for item in snapshot.get("stance_tags") or []],
            subjective_summary=str(snapshot.get("subjective_summary") or ""),
            recent_aftereffect=str(snapshot.get("recent_aftereffect") or ""),
            aftereffect_expires_at=snapshot.get("aftereffect_expires_at"),
            persona_signature=dict(snapshot.get("persona_signature") or {}),
            source_timeline_uids=[
                str(item) for item in snapshot.get("source_timeline_uids") or []
            ],
        )

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
