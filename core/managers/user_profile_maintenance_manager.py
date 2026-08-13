"""Persistent, per-user ordered maintenance for derived user profiles."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any

from astrbot.api import logger

from ...storage.user_profile_store import (
    UserProfileRevisionConflict,
    UserProfileStore,
)
from ..models.user_profile import (
    UserProfileFactStatus,
    UserProfileTask,
    UserRelationshipState,
)
from .user_profile_fact_maintainer import (
    UserProfileFactMaintainer,
    UserProfileFactMaintenancePlan,
)
from .user_profile_behavior_synthesizer import UserProfileBehaviorSynthesizer
from .user_relationship_maintainer import UserRelationshipMaintainer


class _AsyncNoopContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False


class UserProfileMaintenanceManager:
    """Drain projection events serially per user and checkpoint each business stage."""

    def __init__(
        self,
        store: UserProfileStore,
        *,
        provider: Any = None,
        provider_resolver: Callable[..., Any] | None = None,
        persona_resolver: Callable[[str], Any] | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.store = store
        self.default_provider = provider
        self.provider_resolver = provider_resolver
        self.persona_resolver = persona_resolver
        self.config = dict(config or {})
        self.fact_maintainer = UserProfileFactMaintainer(provider)
        self.behavior_synthesizer = UserProfileBehaviorSynthesizer()
        self.relationship_maintainer = UserRelationshipMaintainer(provider)
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._namespace_locks: dict[str, asyncio.Lock] = {}
        self._scheduled: dict[str, asyncio.Task] = {}
        self._retry_tasks: set[asyncio.Task] = set()
        self._retry_by_task_uid: dict[str, asyncio.Task] = {}
        self._lifecycle_task: asyncio.Task | None = None
        self._settings_changed = asyncio.Event()
        self._closing = False
        self._semaphore = asyncio.Semaphore(self._concurrency())

    def apply_config(self, config: dict[str, Any]) -> None:
        old_concurrency = self._concurrency()
        self.config = dict(config)
        if self._concurrency() != old_concurrency:
            self._semaphore = asyncio.Semaphore(self._concurrency())
        self._settings_changed.set()

    async def start(self) -> None:
        """Resume durable tasks first, then any pending unbatched events."""
        await self.run_lifecycle_maintenance()
        if self._lifecycle_task is None or self._lifecycle_task.done():
            self._lifecycle_task = asyncio.create_task(self._lifecycle_loop())
        limit = int(self.config.get("user_profile.startup_recovery_limit", 64))
        recoverable = await self.store.list_recoverable_tasks(
            limit=limit, include_future=True
        )
        for task in recoverable:
            scope_uid = str(task.get("profile_scope_uid") or "")
            if scope_uid:
                retry_at = float(task.get("next_retry_at") or 0)
                delay = max(0.0, retry_at - time.time())
                if delay:
                    self._schedule_retry(scope_uid, str(task["task_uid"]), delay)
                else:
                    self._schedule_existing_task(scope_uid, task)
        pending = await self.store.list_pending_projection_events(limit=limit)
        for event in pending:
            scope_uid = str(event.get("profile_scope_uid") or "")
            if scope_uid:
                self.schedule_scope(scope_uid)

    async def close(self) -> None:
        self._closing = True
        tasks = [*self._scheduled.values(), *self._retry_tasks]
        if self._lifecycle_task is not None:
            tasks.append(self._lifecycle_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduled.clear()
        self._retry_tasks.clear()
        self._retry_by_task_uid.clear()
        self._lifecycle_task = None

    async def run_lifecycle_maintenance(self) -> dict[str, int]:
        """Advance time-based states and compact fully derivable maintenance data."""
        return await self.store.run_profile_lifecycle_maintenance(
            completed_task_retention_days=int(
                self.config.get("user_profile.completed_task_retention_days", 30)
            ),
            projection_compaction_days=int(
                self.config.get("user_profile.projection_compaction_days", 30)
            ),
            stale_retention_days=int(
                self.config.get("user_profile.stale_retention_days", 180)
            ),
        )

    async def _lifecycle_loop(self) -> None:
        while not self._closing:
            hours = max(
                1,
                int(self.config.get("user_profile.lifecycle_scan_interval_hours", 24)),
            )
            try:
                await asyncio.wait_for(
                    self._settings_changed.wait(), timeout=hours * 3600.0
                )
                self._settings_changed.clear()
                continue
            except TimeoutError:
                pass
            try:
                await self.run_lifecycle_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[UserProfile] 周期性生命周期维护失败", exc_info=True)

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

    async def resume_task(self, task_uid: str) -> None:
        """Resume the original durable task after an explicit administrator retry."""
        task = await self.store.get_task(task_uid)
        if task is None:
            raise ValueError("Unknown user-profile task")
        scope_uid = str(task.get("profile_scope_uid") or "")
        if not scope_uid:
            raise ValueError("User-profile task has no scope")
        retry_wake = self._retry_by_task_uid.pop(task_uid, None)
        if retry_wake is not None and not retry_wake.done():
            retry_wake.cancel()
        self._schedule_existing_task(scope_uid, task)

    async def cancel_task(self, task_uid: str) -> dict[str, Any]:
        """Stop the scope worker and cancel the task's complete build group."""
        task = await self.store.get_task(task_uid)
        if task is None:
            raise ValueError("Unknown user-profile task")
        scope_uid = str(task.get("profile_scope_uid") or "")
        retry_wakes = []
        for candidate_uid, retry_wake in list(self._retry_by_task_uid.items()):
            candidate = await self.store.get_task(candidate_uid)
            if str((candidate or {}).get("profile_scope_uid") or "") != scope_uid:
                continue
            self._retry_by_task_uid.pop(candidate_uid, None)
            if not retry_wake.done():
                retry_wake.cancel()
                retry_wakes.append(retry_wake)
        if retry_wakes:
            await asyncio.gather(*retry_wakes, return_exceptions=True)
        scheduled = self._scheduled.get(scope_uid)
        if scheduled is not None and not scheduled.done():
            scheduled.cancel()
            await asyncio.gather(scheduled, return_exceptions=True)
        result = await self.store.cancel_profile_task_group(task_uid)
        for cancelled_uid in result.get("task_uids", []):
            retry_wake = self._retry_by_task_uid.pop(str(cancelled_uid), None)
            if retry_wake is not None and not retry_wake.done():
                retry_wake.cancel()
        return result

    async def continue_gap(self, profile_scope_uid: str) -> dict[str, Any]:
        """Resume interrupted events without replaying completed fact batches."""
        result = await self.store.continue_profile_gap(profile_scope_uid)
        task_uid = str(result.get("resume_task_uid") or "")
        if task_uid:
            await self.resume_task(task_uid)
        elif int(result.get("event_count") or 0):
            self.schedule_scope(profile_scope_uid)
        return result

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
        _lock_held: bool = False,
        _settings: dict[str, Any] | None = None,
        _provider: Any = None,
        _history_through_sequence: int | None = None,
        _checkpoint_task_uid: str | None = None,
    ) -> UserRelationshipState | None:
        """Replace the current relationship from durable projection history."""
        lock = self._scope_locks.setdefault(profile_scope_uid, asyncio.Lock())
        context = _AsyncNoopContext() if _lock_held else lock
        config = dict(_settings or self.config)
        async with context:
            scope = await self.store.get_scope(profile_scope_uid)
            if scope is None:
                raise ValueError("Unknown user-profile scope")
            history = await self.store.list_projection_history(profile_scope_uid)
            if _history_through_sequence is not None:
                history = [
                    event
                    for event in history
                    if int(event.get("sequence") or 0)
                    <= int(_history_through_sequence)
                ]
            latest_by_timeline: dict[str, dict[str, Any]] = {}
            for event in history:
                latest_by_timeline[str(event.get("timeline_uid") or "")] = event
            active_events = [
                event
                for event in latest_by_timeline.values()
                if str(event.get("operation") or "") in {"upsert", "restore"}
            ]
            active_events.sort(key=lambda event: int(event.get("sequence") or 0))
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
                        "identity_resolution": event.get("payload", {}).get(
                            "identity_resolution"
                        )
                        or {},
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
                    checkpoint_task_uid=_checkpoint_task_uid,
                )
            persona = await self._resolve_current_persona(scope.persona_id)
            objective_facts = await self.store.list_serving_facts(
                scope.fact_namespace_uid,
                include_sensitive=False,
                limit=int(
                    config.get("user_profile.fact_maintenance_context_limit", 200)
                ),
            )
            sensitivity = str(
                scope.relationship_sensitivity_override
                or config.get(
                    "user_profile.relationship_sensitivity", "balanced"
                )
            )
            behavior_mode = str(
                scope.relationship_behavior_override
                or config.get(
                    "user_profile.relationship_behavior_mode", "natural"
                )
            )
            rebuilt: UserRelationshipState | None = None
            diagnostics: dict[str, Any] = {}
            summaries: list[str] = []
            source_timeline_uids: list[str] = []
            provider = _provider or self._resolve_provider()
            batch_limit = max(
                1,
                int(
                    config.get(
                        "user_profile.relationship_rebuild_batch_limit", 32
                    )
                ),
            )
            for offset in range(0, len(timelines), batch_limit):
                maintained = await self.relationship_maintainer.maintain(
                    profile_scope_uid=profile_scope_uid,
                    timelines=timelines[offset : offset + batch_limit],
                    current_state=rebuilt,
                    current_persona=persona,
                    objective_facts=objective_facts,
                    sensitivity=sensitivity,
                    behavior_mode=behavior_mode,
                    settings=config,
                    provider=provider,
                )
                if maintained is None:
                    continue
                rebuilt = maintained.state
                diagnostics = maintained.diagnostics
                for timeline_uid in maintained.state.source_timeline_uids:
                    if timeline_uid not in source_timeline_uids:
                        source_timeline_uids.append(timeline_uid)
                if maintained.change_summary:
                    summaries.append(maintained.change_summary)
            if rebuilt is None:
                return None
            await self._assert_persona_unchanged(scope.persona_id, persona)
            diagnostics["persona_basis"] = "current_config"
            diagnostics["history_event_count"] = len(history)
            diagnostics["meaningful_timeline_count"] = len(timelines)
            diagnostics["history_batch_count"] = (
                len(timelines) + batch_limit - 1
            ) // batch_limit
            rebuilt.relationship_uid = (
                current.relationship_uid
                if current is not None
                else rebuilt.relationship_uid
            )
            rebuilt.created_at = (
                current.created_at if current is not None else rebuilt.created_at
            )
            rebuilt.source_timeline_uids = source_timeline_uids
            return await self.store.publish_relationship(
                rebuilt,
                expected_revision=(current.revision if current is not None else 0),
                operation="history_rebuild",
                reason=reason,
                change_summary="; ".join(summaries)[-1000:],
                diagnostics=diagnostics,
                full_revision_limit=int(
                    config.get(
                        "user_profile.relationship_full_revision_limit", 100
                    )
                ),
                checkpoint_task_uid=_checkpoint_task_uid,
            )

    def _schedule_existing_task(
        self, profile_scope_uid: str, task_payload: dict[str, Any]
    ) -> None:
        current = self._scheduled.get(profile_scope_uid)
        if self._closing or (current is not None and not current.done()):
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
                if await self.store.get_blocking_failed_task(profile_scope_uid):
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
                events, batch_diagnostics = self._select_task_events(
                    events, self.config
                )
                provider = self._resolve_provider()
                task = UserProfileTask(
                    profile_scope_uid=profile_scope_uid,
                    settings_snapshot=dict(self.config),
                    provider_signature=self._provider_signature(provider),
                    result_summary=batch_diagnostics,
                )
                await self.store.create_task(task, events)
                payload = await self.store.get_task(task.task_uid)
                if payload is None or not await self._process_task(payload, provider=provider):
                    return

    def _select_task_events(
        self,
        events: list[dict[str, Any]],
        settings: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Bound a task by Timeline count, candidate count, and mandatory prompt size."""
        if not events:
            return [], {}
        candidate_limit = max(
            1,
            int(
                settings.get(
                    "user_profile.maintenance_batch_candidate_limit", 16
                )
            ),
        )
        legacy_limit = max(
            1,
            int(settings.get("user_profile.legacy_review_batch_candidate_limit", 64)),
        )
        prompt_limit = max(
            4000,
            int(settings.get("user_profile.maintenance_prompt_max_chars", 16000)),
        )
        selected: list[dict[str, Any]] = []
        candidates = []
        prompt_chars = 0
        selected_mode = self._event_projection_mode(events[0])
        selected_build_uid = self._event_build_operation_uid(events[0])
        for event in events:
            if selected and self._event_projection_mode(event) != selected_mode:
                break
            if selected and self._event_build_operation_uid(event) != selected_build_uid:
                break
            payload = dict(event.get("payload") or {})
            sources = []
            if str(event.get("operation") or "upsert") in {"upsert", "restore"}:
                sources = self.fact_maintainer.extract_candidates(
                    payload,
                    actor_id=str(payload.get("profile_actor_id") or ""),
                    legacy_attribution_confidence=float(
                        settings.get(
                            "user_profile.legacy_summary_candidate_confidence", 0.45
                        )
                    ),
                )
            behavior = [
                source
                for source in sources
                if str(source.metadata.get("profile_signal") or "")
                in {"behavior_evidence", "behavior_pattern"}
            ]
            proposed_candidates, proposed_legacy_groups = (
                self.fact_maintainer.group_legacy_candidates(
                    [*candidates, *[source for source in sources if source not in behavior]]
                )
            )
            ordinary_count = sum(
                1
                for source in proposed_candidates
                if not self.fact_maintainer._is_legacy_summary_source(source)
            )
            legacy_count = len(proposed_legacy_groups)
            proposed_prompt_chars = len(
                self.fact_maintainer._build_prompt(
                    proposed_candidates, [], [], settings
                )
            )
            exceeds = (
                ordinary_count > candidate_limit
                or legacy_count > legacy_limit
                or proposed_prompt_chars > prompt_limit
            )
            if selected and exceeds:
                break
            selected.append(event)
            candidates = proposed_candidates
            prompt_chars = proposed_prompt_chars

        return selected, {
            "batch_timeline_count": len(selected),
            "batch_candidate_count": len(candidates),
            "batch_prompt_estimate_chars": prompt_chars,
            "batch_candidate_limit": candidate_limit,
            "batch_legacy_candidate_limit": legacy_limit,
            "batch_prompt_target_chars": prompt_limit,
            "batch_was_bounded": len(selected) < len(events),
            "projection_mode": selected_mode or "incremental",
            "build_operation_uid": selected_build_uid,
            "batch_single_timeline_exceeded_target": bool(
                len(selected) == 1
                and (
                    len(candidates) > candidate_limit
                    or prompt_chars > prompt_limit
                )
            ),
        }

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
            )
            await self.store.finish_task_events(
                task_uid, status="pending", error="Profile scope is disabled"
            )
            if scope is not None:
                await self.store.set_scope_state(scope_uid, has_gap=True)
            return False

        result_summary = dict(task.get("result_summary") or {})
        if (
            not result_summary.get("facts_checkpoint")
            and result_summary.get("relationship_checkpoint")
        ):
            # Tasks created by older development builds could publish a relationship
            # after facts failed. A successful fact retry must always recompute it.
            result_summary = {
                key: value
                for key, value in result_summary.items()
                if not key.startswith("relationship_")
            }
            await self.store.update_task(
                task_uid,
                status=str(task.get("status") or "pending"),
                result_summary=result_summary,
            )
        if result_summary.get("facts_checkpoint") and not result_summary.get(
            "behavior_checkpoint"
        ) and result_summary.get("relationship_checkpoint"):
            # Development tasks produced before behavior had an independent
            # checkpoint need one synthesis pass before relationship serving.
            result_summary = {
                key: value
                for key, value in result_summary.items()
                if not key.startswith("relationship_")
            }
            await self.store.update_task(
                task_uid,
                status=str(task.get("status") or "pending"),
                result_summary=result_summary,
            )
        selected_provider = provider or self._resolve_provider()
        result_summary["automatic_retry_pending"] = False
        result_summary.pop("failed_stage", None)
        result_summary.pop("request_elapsed_seconds", None)
        fact_error: Exception | None = None
        behavior_error: Exception | None = None
        relationship_error: Exception | None = None

        if not result_summary.get("facts_checkpoint"):
            started = time.monotonic()
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
                result_summary.pop("facts_error", None)
                result_summary["facts_elapsed_seconds"] = round(
                    time.monotonic() - started, 3
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                fact_error = exc
                result_summary["facts_error"] = str(exc)[:4000]
                result_summary["request_elapsed_seconds"] = round(
                    time.monotonic() - started, 3
                )
                logger.error(
                    "[UserProfile] 客观事实维护失败 (scope=%s, task=%s): %s",
                    scope_uid,
                    task_uid,
                    exc,
                    exc_info=True,
                )

        if fact_error is not None:
            await self._record_task_failure(
                task=task,
                settings=settings,
                result_summary=result_summary,
                stage="facts",
                error=fact_error,
            )
            return False

        if result_summary.get("facts_checkpoint") and not result_summary.get(
            "behavior_checkpoint"
        ):
            started = time.monotonic()
            try:
                namespace_lock = self._namespace_locks.setdefault(
                    str(scope.fact_namespace_uid), asyncio.Lock()
                )
                async with namespace_lock:
                    behavior_result = await self._run_behavior_stage(
                        task=task,
                        scope=scope,
                        settings=settings,
                        provider=selected_provider,
                    )
                result_summary.update(behavior_result)
                result_summary.pop("behavior_error", None)
                result_summary["behavior_elapsed_seconds"] = round(
                    time.monotonic() - started, 3
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                behavior_error = exc
                result_summary["behavior_error"] = str(exc)[:4000]
                result_summary["request_elapsed_seconds"] = round(
                    time.monotonic() - started, 3
                )
                logger.error(
                    "[UserProfile] 行为归纳失败 (scope=%s, task=%s): %s",
                    scope_uid,
                    task_uid,
                    exc,
                    exc_info=True,
                )

        if behavior_error is not None:
            await self._record_task_failure(
                task=task,
                settings=settings,
                result_summary=result_summary,
                stage="behavior",
                error=behavior_error,
            )
            return False

        if result_summary.get("behavior_checkpoint") and not result_summary.get(
            "relationship_checkpoint"
        ):
            started = time.monotonic()
            try:
                relationship_result = await self._run_relationship_stage(
                    task=task,
                    scope=scope,
                    settings=settings,
                    provider=selected_provider,
                )
                result_summary.update(relationship_result)
                result_summary.pop("relationship_error", None)
                result_summary["relationship_elapsed_seconds"] = round(
                    time.monotonic() - started, 3
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                relationship_error = exc
                result_summary["relationship_error"] = str(exc)[:4000]
                result_summary["request_elapsed_seconds"] = round(
                    time.monotonic() - started, 3
                )
                logger.error(
                    "[UserProfile] 人格关系维护失败 (scope=%s, task=%s): %s",
                    scope_uid,
                    task_uid,
                    exc,
                    exc_info=True,
                )

        if relationship_error is not None:
            await self._record_task_failure(
                task=task,
                settings=settings,
                result_summary=result_summary,
                stage="relationship",
                error=relationship_error,
            )
            return False

        await self.store.update_task(
            task_uid,
            status="completed",
            result_summary=result_summary,
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

    async def _record_task_failure(
        self,
        *,
        task: dict[str, Any],
        settings: dict[str, Any],
        result_summary: dict[str, Any],
        stage: str,
        error: Exception,
    ) -> None:
        task_uid = str(task["task_uid"])
        scope_uid = str(task["profile_scope_uid"])
        retries = int(task.get("retries") or 0) + 1
        retry_limit = max(
            0, int(settings.get("user_profile.maintenance_max_retries", 3))
        )
        base = float(
            settings.get("user_profile.maintenance_retry_base_seconds", 60)
        )
        maximum = float(
            settings.get("user_profile.maintenance_retry_max_seconds", 3600)
        )
        delay = min(maximum, base * (2 ** max(0, retries - 1)))
        will_retry = retries <= retry_limit
        status = (
            "facts_failed"
            if will_retry and stage in {"facts", "behavior"}
            else "facts_completed"
            if will_retry
            else "failed"
        )
        error_text = str(error)[:4000]
        result_summary.update(
            {
                "failed_stage": stage,
                "retry_limit": retry_limit,
                "automatic_retry_pending": will_retry,
            }
        )
        await self.store.update_task(
            task_uid,
            status=status,
            error=error_text,
            result_summary=result_summary,
            retries=retries,
            next_retry_at=time.time() + delay if will_retry else None,
        )
        await self.store.finish_task_events(
            task_uid,
            status="queued" if will_retry else "failed",
            error=error_text,
        )
        await self.store.set_scope_state(scope_uid, has_gap=True)
        if will_retry:
            self._schedule_retry(scope_uid, task_uid, delay)

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
        extracted_candidates = []
        for event in self._normalized_task_events(task):
            event_payload = event["payload"]
            operation = event["operation"]
            timeline_uid = event["timeline_uid"]
            sources = []
            if operation in {"upsert", "restore"}:
                sources = self.fact_maintainer.extract_candidates(
                    event_payload,
                    actor_id=str(event_payload.get("profile_actor_id") or ""),
                    legacy_attribution_confidence=float(
                        settings.get(
                            "user_profile.legacy_summary_candidate_confidence", 0.45
                        )
                    ),
                )
            projections.append(
                {
                    "timeline_uid": timeline_uid,
                    "timeline_revision": event["timeline_revision"],
                    "operation": operation,
                    "sources": sources,
                }
            )
            extracted_candidates.extend(sources)

        behavior_sources = [
            source
            for source in extracted_candidates
            if str(source.metadata.get("profile_signal") or "")
            in {"behavior_evidence", "behavior_pattern"}
        ]
        candidates = [
            source for source in extracted_candidates if source not in behavior_sources
        ]
        candidates, legacy_groups = self.fact_maintainer.group_legacy_candidates(
            candidates
        )

        existing = await self.store.list_facts_for_maintenance(
            scope.fact_namespace_uid,
            statuses=("conflict", "active", "pending", "stale"),
            limit=int(
                settings.get("user_profile.fact_maintenance_context_limit", 200)
            ),
        )
        legacy_preassignments: dict[str, str] = {}
        unmatched_candidates = []
        for candidate in candidates:
            if not self.fact_maintainer._is_legacy_summary_source(candidate):
                unmatched_candidates.append(candidate)
                continue
            target_uid = self.fact_maintainer.match_legacy_pending_fact(
                candidate, existing
            )
            if not target_uid:
                unmatched_candidates.append(candidate)
                continue
            for source in legacy_groups.get(candidate.source_uid, [candidate]):
                legacy_preassignments[source.source_uid] = target_uid
        candidates = unmatched_candidates
        if candidates:
            plan = await self.fact_maintainer.maintain(
                fact_namespace_uid=scope.fact_namespace_uid,
                candidates=candidates,
                supporting_evidence=[],
                existing_facts=existing,
                settings=settings,
                provider=provider,
            )
        else:
            plan = UserProfileFactMaintenancePlan()
        plan.source_assignments.update(legacy_preassignments)
        for representative_uid, group in legacy_groups.items():
            target_uid = plan.source_assignments.get(representative_uid)
            if not target_uid:
                continue
            for source in group:
                plan.source_assignments[source.source_uid] = target_uid
        if legacy_groups:
            plan.diagnostics.update(
                {
                    "legacy_source_count": sum(len(group) for group in legacy_groups.values()),
                    "legacy_group_count": len(legacy_groups),
                    "legacy_preassigned_source_count": len(legacy_preassignments),
                }
            )
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
            "behavior_sources_collected": len(behavior_sources),
        }
        await self.store.update_task_items(
            task_uid,
            status="facts_completed",
            result={"fact_revision": fact_revision},
        )
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

    async def _run_behavior_stage(
        self,
        *,
        task: dict[str, Any],
        scope: Any,
        settings: dict[str, Any],
        provider: Any,
    ) -> dict[str, Any]:
        task_uid = str(task["task_uid"])
        current = await self.store.get_task(task_uid)
        await self.store.update_task(
            task_uid,
            status="running_behavior",
            result_summary=dict((current or task).get("result_summary") or {}),
        )
        behavior_result = await self._run_behavior_synthesis(
            task=task,
            scope=scope,
            settings=settings,
            provider=provider,
        )
        result = {
            "behavior_checkpoint": True,
            "behavior_diagnostics": behavior_result,
        }
        if behavior_result.get("fact_revision") is not None:
            result["fact_revision"] = int(behavior_result["fact_revision"])
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

    async def _run_behavior_synthesis(
        self,
        *,
        task: dict[str, Any],
        scope: Any,
        settings: dict[str, Any],
        provider: Any,
    ) -> dict[str, Any]:
        evidence = await self.store.list_unassigned_behavior_evidence(
            scope.profile_scope_uid,
            limit=int(settings["user_profile.behavior_evidence_pool_limit"]),
            retention_days=int(settings["user_profile.pending_retention_days"]),
            include_assigned_patterns=True,
        )
        evidence = self.behavior_synthesizer.eligible_evidence(evidence)
        source_uids = [item.source_uid for item in evidence]
        fingerprint = self.behavior_synthesizer.evidence_fingerprint(evidence)
        projection_mode = self._task_projection_mode(task)
        final_history_batch = False
        if projection_mode == "history_rebuild":
            final_history_batch = not bool(
                await self.store.count_pending_projection_events(
                    scope.profile_scope_uid,
                    projection_mode="history_rebuild",
                )
            )
        previous_uids = set(scope.behavior_synthesis_evidence_uids or [])
        new_evidence_count = len(set(source_uids) - previous_uids)
        elapsed_hours = (
            (time.time() - float(scope.behavior_synthesis_last_at)) / 3600.0
            if scope.behavior_synthesis_last_at is not None
            else float("inf")
        )
        minimum_new = int(
            settings.get("user_profile.behavior_synthesis_min_new_evidence", 3)
        )
        cooldown = float(
            settings.get("user_profile.behavior_synthesis_cooldown_hours", 24)
        )
        should_run = bool(evidence) and (
            final_history_batch
            or (
                projection_mode != "history_rebuild"
                and new_evidence_count >= minimum_new
                and elapsed_hours >= cooldown
            )
        )
        diagnostics: dict[str, Any] = {
            "eligible_evidence_count": len(evidence),
            "new_evidence_count": new_evidence_count,
            "final_history_batch": final_history_batch,
            "model_called": False,
        }
        if not should_run:
            diagnostics["skipped"] = (
                "history_rebuild_deferred"
                if projection_mode == "history_rebuild" and not final_history_batch
                else "trigger_threshold_or_cooldown"
            )
            return diagnostics

        existing = await self.store.list_facts_for_maintenance(
            scope.fact_namespace_uid,
            statuses=("conflict", "active", "pending", "stale"),
            limit=int(settings.get("user_profile.fact_maintenance_context_limit", 200)),
        )
        synthesis = await self.behavior_synthesizer.synthesize(
            fact_namespace_uid=scope.fact_namespace_uid,
            evidence=evidence,
            existing_facts=existing,
            settings=settings,
            provider=provider,
        )
        self._apply_lifecycle(synthesis, evidence, settings)
        if synthesis.facts or synthesis.source_assignments or synthesis.conflicts:
            fact_revision = await self.store.apply_fact_projection_batch(
                fact_namespace_uid=scope.fact_namespace_uid,
                projections=[],
                facts=synthesis.facts,
                source_assignments=synthesis.source_assignments,
                conflicts=synthesis.conflicts,
                expected_revision=await self.store.get_fact_namespace_revision(
                    scope.fact_namespace_uid
                ),
            )
            diagnostics["fact_revision"] = fact_revision
        remaining = await self.store.list_unassigned_behavior_evidence(
            scope.profile_scope_uid,
            limit=int(settings["user_profile.behavior_evidence_pool_limit"]),
            retention_days=int(settings["user_profile.pending_retention_days"]),
            include_assigned_patterns=True,
        )
        remaining = self.behavior_synthesizer.eligible_evidence(remaining)
        await self.store.set_scope_state(
            scope.profile_scope_uid,
            behavior_synthesis_last_at=time.time(),
            behavior_synthesis_evidence_fingerprint=(
                self.behavior_synthesizer.evidence_fingerprint(remaining)
            ),
            behavior_synthesis_evidence_uids=[item.source_uid for item in remaining],
        )
        diagnostics.update(synthesis.diagnostics)
        diagnostics["evidence_fingerprint"] = fingerprint
        diagnostics["tracked_evidence_count"] = len(remaining)
        return diagnostics

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
        if self._task_projection_mode(task) == "history_rebuild":
            remaining = await self.store.count_pending_projection_events(
                scope.profile_scope_uid,
                projection_mode="history_rebuild",
            )
            if remaining:
                result = await self._checkpoint_skipped_relationship(
                    task, "history_rebuild_deferred"
                )
                result["relationship_deferred_pending_timelines"] = remaining
                return result
            await self.store.update_task(task_uid, status="running_relationship")
            rebuilt = await self.rebuild_relationship_from_projection_history(
                scope.profile_scope_uid,
                use_all_history=True,
                reason="Automatic relationship rebuild after historical facts",
                _lock_held=True,
                _settings=settings,
                _provider=provider,
                _history_through_sequence=max(
                    int(item.get("event_sequence") or 0)
                    for item in (task.get("items") or [])
                ),
                _checkpoint_task_uid=task_uid,
            )
            if rebuilt is None:
                return await self._checkpoint_skipped_relationship(
                    task, "history_rebuild_no_meaningful_interaction"
                )
            result = {
                "relationship_checkpoint": True,
                "relationship_revision": rebuilt.revision,
                "relationship_history_rebuilt": True,
            }
            await self.store.update_task_items(
                task_uid,
                status="relationship_completed",
                result={"relationship_revision": rebuilt.revision},
            )
            return result
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
                    "identity_resolution": event["payload"].get(
                        "identity_resolution"
                    )
                    or {},
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
            limit=int(settings.get("user_profile.fact_maintenance_context_limit", 200)),
        )
        persona = await self._resolve_current_persona(scope.persona_id)
        persona_changed = bool(
            current is not None
            and self._persona_digest(current.persona_signature)
            and self._persona_digest(current.persona_signature)
            != self._persona_digest(persona.get("signature"))
        )
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
            current_persona=persona,
            persona_changed=persona_changed,
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
        await self._assert_persona_unchanged(scope.persona_id, persona)
        maintained.diagnostics.setdefault("persona_basis", "current_config")
        maintained.diagnostics.setdefault("persona_reconciled", persona_changed)
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
    def _event_projection_mode(event: dict[str, Any]) -> str:
        payload = event.get("payload") or event.get("event_payload") or {}
        return str(payload.get("projection_mode") or "")

    @staticmethod
    def _event_build_operation_uid(event: dict[str, Any]) -> str:
        payload = event.get("payload") or event.get("event_payload") or {}
        return str(payload.get("build_operation_uid") or "")

    @classmethod
    def _task_projection_mode(cls, task: dict[str, Any]) -> str:
        modes = {
            cls._event_projection_mode(item)
            for item in (task.get("items") or [])
        }
        return modes.pop() if len(modes) == 1 else ""

    async def _resolve_current_persona(self, persona_id: str) -> dict[str, Any]:
        if self.persona_resolver is None:
            raise RuntimeError("Current persona resolver is unavailable")
        resolved = self.persona_resolver(str(persona_id or ""))
        if inspect.isawaitable(resolved):
            resolved = await resolved
        persona = dict(resolved) if isinstance(resolved, dict) else {}
        signature = persona.get("signature")
        if not persona.get("persona_id") or not isinstance(signature, dict):
            raise RuntimeError(f"Current persona is unavailable: {persona_id}")
        if not self._persona_digest(signature):
            raise RuntimeError(f"Current persona has no valid signature: {persona_id}")
        persona["basis"] = "current_config"
        return persona

    async def _assert_persona_unchanged(
        self, persona_id: str, expected: dict[str, Any]
    ) -> None:
        current = await self._resolve_current_persona(persona_id)
        if self._persona_digest(current.get("signature")) != self._persona_digest(
            expected.get("signature")
        ):
            raise RuntimeError("Current persona changed during relationship maintenance")

    @staticmethod
    def _persona_digest(signature: Any) -> str:
        return str(signature.get("digest") or "") if isinstance(signature, dict) else ""

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
        existing = self._retry_by_task_uid.get(task_uid)
        if existing is not None and not existing.done():
            return

        async def wake() -> None:
            await asyncio.sleep(max(0.0, delay))
            task_payload = await self.store.get_task(task_uid)
            if (
                task_payload is not None
                and str(task_payload.get("status") or "")
                in {"pending", "facts_failed", "facts_completed"}
                and bool(
                    (task_payload.get("result_summary") or {}).get(
                        "automatic_retry_pending"
                    )
                )
            ):
                self._schedule_existing_task(profile_scope_uid, task_payload)

        task = asyncio.create_task(wake())
        self._retry_tasks.add(task)
        self._retry_by_task_uid[task_uid] = task

        def discard(finished: asyncio.Task) -> None:
            self._retry_tasks.discard(finished)
            if self._retry_by_task_uid.get(task_uid) is finished:
                self._retry_by_task_uid.pop(task_uid, None)

        task.add_done_callback(discard)

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
                end_at = (
                    temporal.get("event_ended_at")
                    or temporal.get("event_started_at")
                    or temporal.get("end_at")
                    or temporal.get("ended_at")
                )
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
            current_status = str(getattr(fact.status, "value", fact.status))
            if (
                fact.review_after is not None
                and fact.review_after <= now
                and not fact.pinned
                and current_status in {"active", "pending"}
            ):
                fact.status = (
                    UserProfileFactStatus.ARCHIVED
                    if current_status == "pending"
                    else UserProfileFactStatus.STALE
                )


__all__ = ["UserProfileMaintenanceManager"]
