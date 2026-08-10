"""Automatic, source-grounded construction of Topic memories."""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from astrbot.api import logger

from ...storage.topic_memory_store import TopicMemoryStore
from ..embedding_signature import (
    SUPPORTED_TOPIC_EMBEDDING_FORMATS,
    TOPIC_CENTROID_EMBEDDING_FORMAT,
    TOPIC_DIRECT_EMBEDDING_FORMAT,
    TOPIC_FRAGMENT_EMBEDDING_FORMAT,
    make_embedding_signature,
    signature_mismatch_reason,
)
from ..models.identity_profile import (
    SupplementalIdentityProfile,
    SupplementalIdentityStore,
    parse_supplemental_identity_profiles,
)
from ..models.topic_memory import (
    TopicFragmentDraft,
    TopicMaintenanceMode,
    TopicMaintenanceRun,
    TopicMaintenanceStatus,
    TopicMemory,
    TopicMemoryStatus,
)
from ..topic_runtime import TopicBuildRunContext
from ..topic_settings import TOPIC_SETTINGS_REVISION
from ..topic_vector_index import TopicVectorIndex
from .topic_build_contracts import (
    _RELATION_ALGORITHM_VERSION,
    TopicBuildContractMixin,
    TopicBuildValidationError,
)
from .topic_build_identity import TopicBuildIdentityMixin
from .topic_build_provider import TopicBuildProviderMixin
from .topic_build_support import TopicBuildSupportMixin
from .topic_component_matcher import TopicComponentMatcherMixin
from .topic_component_synthesizer import TopicComponentSynthesizerMixin
from .topic_fragment_extractor import TopicFragmentExtractorMixin
from .topic_maintenance_manager import TopicMaintenanceManager
from .topic_snapshot_publisher import TopicSnapshotPublisherMixin


class TopicBuildManager(
    TopicFragmentExtractorMixin,
    TopicComponentMatcherMixin,
    TopicComponentSynthesizerMixin,
    TopicSnapshotPublisherMixin,
    TopicBuildIdentityMixin,
    TopicBuildProviderMixin,
    TopicBuildContractMixin,
    TopicBuildSupportMixin,
):
    """Turn deterministic preview windows into maintained Topic snapshots."""

    def __init__(
        self,
        db_path: str,
        store: TopicMemoryStore,
        candidate_manager: TopicMaintenanceManager,
        *,
        llm_provider: Any = None,
        embedding_provider: Any = None,
        rerank_provider: Any = None,
        config: dict[str, Any] | None = None,
        identity_profile_store: SupplementalIdentityStore | None = None,
        conversation_store: Any = None,
        provider_resolver: Callable[[], dict[str, Any]] | None = None,
        vector_index: Any = None,
    ):
        self.db_path = db_path
        self.store = store
        self.candidate_manager = candidate_manager
        self._default_llm_provider = llm_provider
        self._default_embedding_provider = embedding_provider
        self._default_rerank_provider = rerank_provider
        self._default_config = dict(config or {})
        self._runtime_context: contextvars.ContextVar[TopicBuildRunContext | None] = (
            contextvars.ContextVar("topic_build_runtime_context", default=None)
        )
        self.provider_resolver = provider_resolver
        self.identity_profile_store = (
            identity_profile_store or SupplementalIdentityStore()
        )
        self.conversation_store = conversation_store
        self.vector_index = vector_index or TopicVectorIndex(store)
        self.llm_concurrency = max(
            1,
            int(self.config.get("llm_concurrency", 1)),
        )
        self.rerank_concurrency = max(
            1,
            min(32, int(self.config.get("rerank_concurrency", 1))),
        )
        self._llm_semaphore = asyncio.Semaphore(self.llm_concurrency)
        self._rerank_semaphore = asyncio.Semaphore(self.rerank_concurrency)
        self._space_locks: dict[str, asyncio.Lock] = {}
        self._structured_output_capabilities: dict[tuple[str, str, int], bool] = {}
        self._scheduled: dict[str, asyncio.Task] = {}
        self._scheduled_requests: dict[str, dict[str, Any]] = {}
        self._scheduled_wakeups: dict[str, asyncio.Event] = {}

    @property
    def config(self) -> Mapping[str, Any]:
        context = self._runtime_context.get()
        return context.config if context is not None else self._default_config

    @config.setter
    def config(self, value: dict[str, Any]) -> None:
        self._default_config = dict(value)

    @property
    def llm_provider(self) -> Any:
        context = self._runtime_context.get()
        return (
            context.llm_provider if context is not None else self._default_llm_provider
        )

    @llm_provider.setter
    def llm_provider(self, value: Any) -> None:
        self._default_llm_provider = value

    @property
    def embedding_provider(self) -> Any:
        context = self._runtime_context.get()
        return (
            context.embedding_provider
            if context is not None
            else self._default_embedding_provider
        )

    @embedding_provider.setter
    def embedding_provider(self, value: Any) -> None:
        self._default_embedding_provider = value

    @property
    def rerank_provider(self) -> Any:
        context = self._runtime_context.get()
        return (
            context.rerank_provider
            if context is not None
            else self._default_rerank_provider
        )

    @rerank_provider.setter
    def rerank_provider(self, value: Any) -> None:
        self._default_rerank_provider = value

    @property
    def llm_concurrency(self) -> int:
        context = self._runtime_context.get()
        return (
            context.llm_concurrency
            if context is not None
            else self._default_llm_concurrency
        )

    @llm_concurrency.setter
    def llm_concurrency(self, value: int) -> None:
        self._default_llm_concurrency = int(value)

    @property
    def rerank_concurrency(self) -> int:
        context = self._runtime_context.get()
        return (
            context.rerank_concurrency
            if context is not None
            else self._default_rerank_concurrency
        )

    @rerank_concurrency.setter
    def rerank_concurrency(self, value: int) -> None:
        self._default_rerank_concurrency = int(value)

    @property
    def _llm_semaphore(self) -> asyncio.Semaphore:
        context = self._runtime_context.get()
        return (
            context.llm_semaphore
            if context is not None
            else self._default_llm_semaphore
        )

    @_llm_semaphore.setter
    def _llm_semaphore(self, value: asyncio.Semaphore) -> None:
        self._default_llm_semaphore = value

    @property
    def _rerank_semaphore(self) -> asyncio.Semaphore:
        context = self._runtime_context.get()
        return (
            context.rerank_semaphore
            if context is not None
            else self._default_rerank_semaphore
        )

    @_rerank_semaphore.setter
    def _rerank_semaphore(self, value: asyncio.Semaphore) -> None:
        self._default_rerank_semaphore = value

    def _resolved_providers(self) -> dict[str, Any]:
        resolved = self.provider_resolver() if self.provider_resolver else {}
        return {
            "llm_provider": resolved.get("llm_provider", self._default_llm_provider),
            "embedding_provider": resolved.get(
                "embedding_provider", self._default_embedding_provider
            ),
            "rerank_provider": resolved.get(
                "rerank_provider", self._default_rerank_provider
            ),
        }

    def _make_run_context(
        self,
        *,
        memory_space_id: str,
        run_uid: str,
        config: dict[str, Any],
        supplemental_identity_profiles: list[SupplementalIdentityProfile] | None = None,
    ) -> TopicBuildRunContext:
        providers = self._resolved_providers()
        return TopicBuildRunContext.create(
            memory_space_id=memory_space_id,
            run_uid=run_uid,
            config=config,
            supplemental_identity_profiles=(
                supplemental_identity_profiles
                if supplemental_identity_profiles is not None
                else self.identity_profile_store.profiles
            ),
            **providers,
        )

    def _supplemental_profile_payload(self) -> list[dict[str, Any]]:
        return [
            profile.to_storage_dict()
            for profile in self.identity_profile_store.profiles
        ]

    def _profiles_from_run_config(
        self, run_config: Mapping[str, Any]
    ) -> list[SupplementalIdentityProfile]:
        raw = run_config.get("supplemental_identity_profiles")
        if isinstance(raw, list):
            return parse_supplemental_identity_profiles(raw)
        # Compatibility for tasks created before profile snapshots were persisted.
        return self.identity_profile_store.profiles

    def apply_config(self, config: dict[str, Any]) -> None:
        """Apply settings between builds; callers must reject active mutations."""
        normalized = dict(config)
        self._default_config = normalized
        self._default_llm_concurrency = max(
            1, min(64, int(normalized.get("llm_concurrency", 1)))
        )
        self._default_rerank_concurrency = max(
            1, min(32, int(normalized.get("rerank_concurrency", 1)))
        )
        self._default_llm_semaphore = asyncio.Semaphore(self._default_llm_concurrency)
        self._default_rerank_semaphore = asyncio.Semaphore(
            self._default_rerank_concurrency
        )

    def schedule_space(
        self,
        memory_space_id: str,
        *,
        full: bool = False,
        since: float | None = None,
        timeline_uids: Iterable[str] | None = None,
        immediate: bool = False,
    ) -> None:
        """Debounce automatic maintenance and preserve the broadest request."""
        if not memory_space_id:
            return
        request = self._scheduled_requests.setdefault(
            memory_space_id,
            {"full": False, "since": since, "timeline_uids": set(), "immediate": False},
        )
        request["full"] = bool(request["full"] or full)
        if since is not None:
            previous = request.get("since")
            request["since"] = (
                min(float(previous), float(since)) if previous else float(since)
            )
        request.setdefault("timeline_uids", set()).update(
            str(uid).strip() for uid in (timeline_uids or []) if str(uid).strip()
        )
        request["immediate"] = bool(request.get("immediate") or immediate)
        wakeup = self._scheduled_wakeups.setdefault(memory_space_id, asyncio.Event())
        if immediate:
            wakeup.set()
        task = self._scheduled.get(memory_space_id)
        if task is None or task.done():
            self._scheduled[memory_space_id] = asyncio.create_task(
                self._run_scheduled(memory_space_id),
                name=f"livingmemory-topic-{memory_space_id[:24]}",
            )

    async def _run_scheduled(self, memory_space_id: str) -> None:
        try:
            wakeup = self._scheduled_wakeups.setdefault(
                memory_space_id, asyncio.Event()
            )
            delay = max(0.0, float(self.config.get("auto_debounce_seconds", 60.0)))
            if not self._scheduled_requests.get(memory_space_id, {}).get("immediate"):
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=delay)
                except TimeoutError:
                    pass
            request = self._scheduled_requests.pop(memory_space_id, {})
            full = bool(request.get("full"))
            since = request.get("since")
            timeline_uids = sorted(request.get("timeline_uids") or [])
            if not full and since is None and not timeline_uids:
                since = time.time() - 300.0
            await self.build_space(
                memory_space_id,
                mode=(
                    TopicMaintenanceMode.FULL
                    if full
                    else TopicMaintenanceMode.INCREMENTAL
                ),
                since=(None if full or since is None else float(since)),
                timeline_uids=None if full else timeline_uids or None,
                automatic=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                f"[TopicMemory] 自动维护失败 (memory_space_id={memory_space_id})",
                exc_info=True,
            )
        finally:
            self._scheduled.pop(memory_space_id, None)
            self._scheduled_wakeups.pop(memory_space_id, None)
            if memory_space_id in self._scheduled_requests:
                self.schedule_space(memory_space_id)

    async def close(self) -> None:
        tasks = list(self._scheduled.values())
        self._scheduled.clear()
        self._scheduled_requests.clear()
        self._scheduled_wakeups.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def has_active_builds(self) -> bool:
        """Return whether a build currently holds one of this manager's space locks."""
        return any(lock.locked() for lock in self._space_locks.values())

    async def recompute_topic_relations(
        self,
        memory_space_id: str,
        *,
        progress_callback=None,
    ) -> dict[str, Any]:
        """Replace only the derived relation graph using persisted Topic data."""
        if not memory_space_id:
            raise ValueError("memory_space_id is required")
        lock = self._space_locks.setdefault(memory_space_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Topic build is already running for this memory space")
        async with lock:
            if progress_callback is not None:
                result = progress_callback(
                    {"stage": "relation_loading", "current": 0, "total": 1}
                )
                if asyncio.iscoroutine(result):
                    await result
            topics = await self.store.list_all_topics(
                memory_space_id,
                status=TopicMemoryStatus.ACTIVE,
            )
            if progress_callback is not None:
                result = progress_callback(
                    {
                        "stage": "relation_deriving",
                        "current": 0,
                        "total": max(1, len(topics)),
                    }
                )
                if asyncio.iscoroutine(result):
                    await result
            run_uid = f"relation-recompute:{uuid.uuid4()}"
            # Relation derivation is CPU-bound. Keep it off the event loop so
            # WebUI progress polling remains responsive for large Topic sets.
            relations = await asyncio.to_thread(
                self._derive_topic_relations,
                run_uid,
                topics,
            )
            if progress_callback is not None:
                result = progress_callback(
                    {
                        "stage": "relation_publishing",
                        "current": 0,
                        "total": max(1, len(relations)),
                    }
                )
                if asyncio.iscoroutine(result):
                    await result
            relation_count = await self.store.replace_topic_relations(
                memory_space_id,
                relations,
            )
            if progress_callback is not None:
                result = progress_callback(
                    {
                        "stage": "relation_publishing",
                        "current": max(1, relation_count),
                        "total": max(1, relation_count),
                    }
                )
                if asyncio.iscoroutine(result):
                    await result
            return {
                "memory_space_id": memory_space_id,
                "topic_count": len(topics),
                "relation_count": relation_count,
                "algorithm_version": _RELATION_ALGORITHM_VERSION,
            }

    async def resolve_maintenance_review(
        self,
        review_uid: str,
        *,
        action: str,
        target_topic_uid: str | None = None,
        preview_token: str | None = None,
        progress_callback=None,
    ) -> dict[str, Any]:
        """Apply one user decision without rerunning fragment extraction."""
        action = str(action or "").strip().lower()
        if action == "ignore":
            changed = await self.store.set_maintenance_review_status(
                review_uid,
                status="ignored",
                action="ignore",
                payload={},
            )
            if not changed:
                raise ValueError("Topic review is no longer pending")
            return {"review_uid": review_uid, "status": "ignored", "action": action}
        if action == "sync_sources":
            review = await self.store.get_maintenance_review_context(review_uid)
            if review is None or str(review.get("status")) != "pending":
                raise ValueError("Topic review is missing or no longer pending")
            if str(review.get("review_type")) != "deleted_timeline_source_repair":
                raise ValueError("This review is not a Timeline source repair")
            return await self.repair_deleted_timeline_sources(
                str(review["memory_space_id"]),
                affected_topic_uids=[
                    str(uid) for uid in review.get("topic_uids", []) if str(uid)
                ],
                deleted_timeline_uids=[
                    str(uid) for uid in review.get("timeline_uids", []) if str(uid)
                ],
                review_uid=review_uid,
                progress_callback=progress_callback,
            )
        if action not in {"merge", "new"}:
            raise ValueError("Unsupported Topic review action")
        review = await self.store.get_maintenance_review_context(review_uid)
        if review is None or str(review.get("status")) != "pending":
            raise ValueError("Topic review is missing or no longer pending")
        if str(review.get("review_type")) != "ambiguous_topic_match":
            raise ValueError("This review type cannot materialize a Topic directly")
        memory_space_id = str(review["memory_space_id"])
        if action == "merge" and not str(target_topic_uid or "").strip():
            raise ValueError("target_topic_uid is required for merge")
        lock = self._space_locks.setdefault(memory_space_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Topic build is already running for this memory space")
        async with lock:
            await self.store.rebase_maintenance_review(
                review_uid,
                preview_token=str(preview_token or ""),
            )
            context = await self.store.get_maintenance_review_context(review_uid)
            if context is None or str(context.get("status")) != "pending":
                raise ValueError("Topic review changed; refresh before applying")
            details = dict(context.get("details") or {})
            source_run_uid = str(details.get("run_uid") or "").strip()
            fragments = (
                await self.store.list_fragments(run_uid=source_run_uid)
                if source_run_uid
                else []
            )
            fragment_uids = {
                str(value) for value in details.get("fragment_uids", []) if str(value)
            }
            review_timelines = {
                str(value) for value in context.get("timeline_uids", []) if str(value)
            }
            fragments = [
                item
                for item in fragments
                if (
                    item.fragment_uid in fragment_uids
                    if fragment_uids
                    else bool(set(item.timeline_uids) & review_timelines)
                )
            ]
            if not fragments:
                raise ValueError(
                    "The reusable fragments for this review are unavailable"
                )
            existing = None
            if action == "merge":
                existing = await self.store.get_topic(str(target_topic_uid))
                if (
                    existing is None
                    or existing.memory_space_id != memory_space_id
                    or existing.status != TopicMemoryStatus.ACTIVE
                ):
                    raise ValueError("The selected target Topic is unavailable")
                if existing.topic_uid not in set(context.get("topic_uids") or []):
                    raise ValueError(
                        "The target Topic is not one of the reviewed candidates"
                    )
                existing_rows = await self.store.list_active_fragments_for_topics(
                    [existing.topic_uid]
                )
                fragments = [
                    *(row["fragment"] for row in existing_rows),
                    *fragments,
                ]
            return await self._publish_governance_groups(
                memory_space_id,
                [fragments],
                retained_topics=[existing],
                affected_topic_uids={existing.topic_uid} if existing else set(),
                operation="review_merge" if existing else "review_new",
                review_resolution={
                    "review_uid": review_uid,
                    "action": action,
                    "payload": {
                        "target_topic_uid": existing.topic_uid if existing else None
                    },
                },
                progress_callback=progress_callback,
            )

    async def merge_topics(
        self,
        memory_space_id: str,
        *,
        topic_uids: list[str],
        main_topic_uid: str,
    ) -> dict[str, Any]:
        """Merge existing Topics from their formal fragments, retaining one UID."""
        normalized = list(
            dict.fromkeys(str(uid).strip() for uid in topic_uids if str(uid).strip())
        )
        main_topic_uid = str(main_topic_uid or "").strip()
        if len(normalized) < 2:
            raise ValueError("Select at least two Topics to merge")
        if main_topic_uid not in normalized:
            raise ValueError("The retained Topic must be included in the merge")
        lock = self._space_locks.setdefault(memory_space_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Topic build is already running for this memory space")
        async with lock:
            topics = await self.store.get_topics_by_uids(memory_space_id, normalized)
            if {topic.topic_uid for topic in topics} != set(normalized):
                raise ValueError("One or more selected Topics are unavailable")
            main = next(topic for topic in topics if topic.topic_uid == main_topic_uid)
            rows = await self.store.list_active_fragments_for_topics(normalized)
            fragments = list(
                {row["fragment"].fragment_uid: row["fragment"] for row in rows}.values()
            )
            if not fragments:
                raise ValueError(
                    "Selected Topics do not have reusable formal fragments"
                )
            return await self._publish_governance_groups(
                memory_space_id,
                [fragments],
                retained_topics=[main],
                affected_topic_uids=set(normalized),
                operation="manual_merge",
                operation_payload={
                    "topic_uids": normalized,
                    "main_topic_uid": main_topic_uid,
                },
            )

    async def split_topic(
        self,
        memory_space_id: str,
        *,
        topic_uid: str,
        fragment_groups: list[list[str]],
    ) -> dict[str, Any]:
        """Split one Topic by exhaustive formal-fragment groups."""
        topic = await self.store.get_topic(str(topic_uid or "").strip())
        if (
            topic is None
            or topic.memory_space_id != memory_space_id
            or topic.status != TopicMemoryStatus.ACTIVE
        ):
            raise ValueError("Topic is unavailable")
        rows = await self.store.list_active_fragments_for_topics([topic.topic_uid])
        fragments_by_uid = {
            row["fragment"].fragment_uid: row["fragment"] for row in rows
        }
        normalized_groups = [
            list(dict.fromkeys(str(uid).strip() for uid in group if str(uid).strip()))
            for group in fragment_groups
            if isinstance(group, list) and group
        ]
        flattened = [uid for group in normalized_groups for uid in group]
        if len(normalized_groups) < 2:
            raise ValueError("A split requires at least two fragment groups")
        if len(flattened) != len(set(flattened)):
            raise ValueError("A formal fragment cannot belong to two split groups")
        if set(flattened) != set(fragments_by_uid):
            raise ValueError("Every formal fragment must be assigned exactly once")
        lock = self._space_locks.setdefault(memory_space_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Topic build is already running for this memory space")
        async with lock:
            current = await self.store.get_topic(topic.topic_uid)
            if current is None or current.revision != topic.revision:
                raise RuntimeError("Topic changed; refresh the split preview")
            return await self._publish_governance_groups(
                memory_space_id,
                [
                    [fragments_by_uid[uid] for uid in group]
                    for group in normalized_groups
                ],
                retained_topics=[current, *([None] * (len(normalized_groups) - 1))],
                affected_topic_uids={current.topic_uid},
                operation="manual_split",
                operation_payload={
                    "topic_uid": current.topic_uid,
                    "fragment_groups": normalized_groups,
                },
            )

    async def _publish_governance_groups(
        self,
        memory_space_id: str,
        fragment_groups: list[list[TopicFragmentDraft]],
        *,
        retained_topics: list[TopicMemory | None],
        affected_topic_uids: set[str],
        operation: str,
        operation_payload: dict[str, Any] | None = None,
        review_resolution: dict[str, Any] | None = None,
        progress_callback=None,
    ) -> dict[str, Any]:
        """Synthesize selected fragments and publish every governance result atomically."""
        supplemental_profiles = self._supplemental_profile_payload()
        run = TopicMaintenanceRun(
            memory_space_id=memory_space_id,
            mode=TopicMaintenanceMode.REPAIR,
            status=TopicMaintenanceStatus.RUNNING,
            total_items=sum(len(group) for group in fragment_groups),
            config={
                "topic_settings": dict(self._default_config),
                "supplemental_identity_profiles": supplemental_profiles,
            },
            metadata={
                "governance_operation": operation,
                **dict(operation_payload or {}),
            },
        )
        await self.store.create_maintenance_run(run)
        await self._emit(
            progress_callback,
            run.run_uid,
            "component_review",
            1,
            1,
            activity=operation,
        )
        context = self._make_run_context(
            memory_space_id=memory_space_id,
            run_uid=run.run_uid,
            config=dict(self._default_config),
            supplemental_identity_profiles=parse_supplemental_identity_profiles(
                supplemental_profiles
            ),
        )
        token = self._runtime_context.set(context)
        try:
            all_timeline_uids = sorted(
                {
                    uid
                    for fragments in fragment_groups
                    for fragment in fragments
                    for uid in fragment.timeline_uids
                }
            )
            candidates = await self.candidate_manager.load_candidates(
                memory_space_id,
                timeline_uids=all_timeline_uids,
            )
            candidate_map = {item.memory_uid: item for item in candidates}
            if set(candidate_map) != set(all_timeline_uids):
                raise ValueError("One or more source Timelines are no longer available")
            snapshots: list[dict[str, Any]] = []
            materialized_topics: list[TopicMemory] = []
            for index, fragments in enumerate(fragment_groups):
                existing = (
                    retained_topics[index] if index < len(retained_topics) else None
                )
                await self._emit(
                    progress_callback,
                    run.run_uid,
                    "topic_synthesis",
                    index,
                    len(fragment_groups),
                    activity=operation,
                )
                synthesis = await self._synthesize_component_checkpointed(
                    run.run_uid, fragments
                )
                await self._emit(
                    progress_callback,
                    run.run_uid,
                    "topic_synthesis",
                    index + 1,
                    len(fragment_groups),
                    activity=operation,
                )
                await self._emit(
                    progress_callback,
                    run.run_uid,
                    "materialization",
                    index,
                    len(fragment_groups),
                    activity=operation,
                )
                topic, atoms, links, sources, actor_links, atom_actor_links = (
                    self._materialize_snapshot(
                        run.run_uid,
                        memory_space_id,
                        synthesis,
                        fragments,
                        candidate_map,
                        existing,
                    )
                )
                topic.metadata["governance"] = {
                    "operation": operation,
                    "run_uid": run.run_uid,
                    "source_topic_uids": sorted(affected_topic_uids),
                }
                materialized_topics.append(topic)
                snapshots.append(
                    {
                        "topic": topic,
                        "atoms": atoms,
                        "links": links,
                        "atom_sources": sources,
                        "actor_links": actor_links,
                        "atom_actor_links": atom_actor_links,
                        "fragments": fragments,
                        "expected_revision": existing.revision if existing else None,
                        "decision": {
                            "decision_uid": str(uuid.uuid4()),
                            "action": operation,
                            "fragment_uids": [item.fragment_uid for item in fragments],
                            "metadata": dict(operation_payload or {}),
                            "llm_output": synthesis,
                        },
                    }
                )
                await self._emit(
                    progress_callback,
                    run.run_uid,
                    "materialization",
                    index + 1,
                    len(fragment_groups),
                    activity=operation,
                )
            active_topics = await self.store.list_all_topics(
                memory_space_id, status=TopicMemoryStatus.ACTIVE
            )
            relation_topics = [
                topic
                for topic in active_topics
                if topic.topic_uid not in affected_topic_uids
            ] + materialized_topics
            relations = self._derive_topic_relations(run.run_uid, relation_topics)
            await self._emit(
                progress_callback,
                run.run_uid,
                "publication",
                0,
                1,
                activity=operation,
            )
            publication = await self.store.publish_topic_build(
                run_uid=run.run_uid,
                memory_space_id=memory_space_id,
                mode=TopicMaintenanceMode.REPAIR,
                snapshots=snapshots,
                relations=relations,
                affected_topic_uids=affected_topic_uids,
                relation_scope_topic_uids=None,
                review_resolution=review_resolution,
            )
            await self._emit(
                progress_callback,
                run.run_uid,
                "publication",
                1,
                1,
                activity=operation,
            )
            if self.vector_index is not None:
                self.vector_index.invalidate(memory_space_id)
            return {
                "run_uid": run.run_uid,
                "operation": operation,
                "status": "completed",
                "topic_uids": [topic.topic_uid for topic in publication["topics"]],
                "archived_topics": int(publication.get("archived_topics") or 0),
                "relation_count": int(publication.get("relation_count") or 0),
            }
        except Exception as exc:
            await self.store.update_maintenance_run(
                run.run_uid,
                status=TopicMaintenanceStatus.FAILED,
                error=str(exc),
            )
            raise
        finally:
            self._runtime_context.reset(token)

    async def get_embedding_health(self, memory_space_id: str) -> dict[str, Any]:
        """Inspect persisted vector signatures without making model calls."""
        embedding_provider = self._resolved_providers()["embedding_provider"]
        topics = await self.store.list_all_topics(
            memory_space_id,
            status=TopicMemoryStatus.ACTIVE,
        )
        fragments = await self.store.list_formal_fragments(memory_space_id)
        reasons: Counter[str] = Counter()
        if embedding_provider is None and (topics or fragments):
            reasons["provider_unavailable"] = len(topics) + len(fragments)
        else:
            for topic in topics:
                vector = topic.metadata.get("embedding", [])
                reason = (
                    "missing_embedding"
                    if not vector
                    else signature_mismatch_reason(
                        topic.embedding_signature,
                        embedding_provider,
                        expected_formats=SUPPORTED_TOPIC_EMBEDDING_FORMATS,
                    )
                )
                if reason:
                    reasons[reason] += 1
            for fragment in fragments:
                reason = (
                    "missing_embedding"
                    if not fragment.embedding
                    else signature_mismatch_reason(
                        fragment.embedding_signature,
                        embedding_provider,
                        expected_formats={TOPIC_FRAGMENT_EMBEDDING_FORMAT},
                    )
                )
                if reason:
                    reasons[reason] += 1
        return {
            "needs_revectorization": bool(reasons),
            "topic_count": len(topics),
            "fragment_count": len(fragments),
            "incompatible_count": sum(reasons.values()),
            "reasons": dict(sorted(reasons.items())),
        }

    async def revectorize_space(
        self,
        memory_space_id: str,
        *,
        progress_callback=None,
    ) -> dict[str, Any]:
        """Regenerate Topic vectors and relations without invoking the LLM."""
        memory_space_id = str(memory_space_id or "").strip()
        if not memory_space_id:
            raise ValueError("memory_space_id is required")
        lock = self._space_locks.setdefault(memory_space_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Topic build is already running for this memory space")
        async with lock:
            operation_uid = f"revectorize:{uuid.uuid4()}"
            context = self._make_run_context(
                memory_space_id=memory_space_id,
                run_uid=operation_uid,
                config=dict(self._default_config),
            )
            token = self._runtime_context.set(context)
            try:
                return await self._revectorize_space_impl(
                    memory_space_id,
                    operation_uid=operation_uid,
                    progress_callback=progress_callback,
                )
            finally:
                self._runtime_context.reset(token)

    async def _revectorize_space_impl(
        self,
        memory_space_id: str,
        *,
        operation_uid: str,
        progress_callback=None,
    ) -> dict[str, Any]:
        if self.embedding_provider is None:
            raise RuntimeError("重新向量化需要可用的 Embedding Provider")
        topics = await self.store.list_all_topics(
            memory_space_id,
            status=TopicMemoryStatus.ACTIVE,
        )
        if not topics:
            return {
                "memory_space_id": memory_space_id,
                "topic_count": 0,
                "fragment_count": 0,
                "relation_count": 0,
            }
        formal_fragments = await self.store.list_formal_fragments(memory_space_id)
        batch_size = max(1, min(64, int(self.config.get("embedding_batch_size", 8))))
        fragment_updates: list[dict[str, Any]] = []
        fragment_vectors: dict[str, list[float]] = {}
        for start in range(0, len(formal_fragments), batch_size):
            batch = formal_fragments[start : start + batch_size]
            vectors = await self._get_embeddings(
                [self._fragment_embedding_text(item) for item in batch]
            )
            if len(vectors) != len(batch):
                raise RuntimeError(
                    "Embedding Provider returned an unexpected vector count"
                )
            for fragment, vector in zip(batch, vectors, strict=True):
                normalized = [float(value) for value in vector]
                signature = make_embedding_signature(
                    self.embedding_provider,
                    dimension=len(normalized),
                    input_format_version=TOPIC_FRAGMENT_EMBEDDING_FORMAT,
                )
                fragment_vectors[fragment.fragment_uid] = normalized
                fragment_updates.append(
                    {
                        "fragment_uid": fragment.fragment_uid,
                        "embedding": normalized,
                        "embedding_signature": signature,
                    }
                )
            await self._emit(
                progress_callback,
                operation_uid,
                "revector_fragments",
                min(start + len(batch), len(formal_fragments)),
                len(formal_fragments),
                activity="embedding",
            )

        link_rows = await self.store.list_active_fragments_for_topics(
            [topic.topic_uid for topic in topics]
        )
        linked_fragment_uids: dict[str, list[str]] = {}
        for row in link_rows:
            linked_fragment_uids.setdefault(str(row["topic_uid"]), []).append(
                row["fragment"].fragment_uid
            )
        direct_topics = [
            topic for topic in topics if not linked_fragment_uids.get(topic.topic_uid)
        ]
        direct_vectors: dict[str, list[float]] = {}
        for start in range(0, len(direct_topics), batch_size):
            batch = direct_topics[start : start + batch_size]
            vectors = await self._get_embeddings(
                [self._topic_embedding_text(topic) for topic in batch]
            )
            if len(vectors) != len(batch):
                raise RuntimeError(
                    "Embedding Provider returned an unexpected vector count"
                )
            for topic, vector in zip(batch, vectors, strict=True):
                direct_vectors[topic.topic_uid] = [float(value) for value in vector]

        topic_updates: list[dict[str, Any]] = []
        topics_with_vectors: list[TopicMemory] = []
        for position, topic in enumerate(topics, 1):
            linked_vectors = [
                fragment_vectors[uid]
                for uid in linked_fragment_uids.get(topic.topic_uid, [])
                if uid in fragment_vectors
            ]
            if linked_vectors:
                vector = self._average_vectors(linked_vectors)
                input_format = TOPIC_CENTROID_EMBEDDING_FORMAT
            else:
                vector = direct_vectors.get(topic.topic_uid, [])
                input_format = TOPIC_DIRECT_EMBEDDING_FORMAT
            if not vector:
                raise RuntimeError(f"无法为 Topic {topic.topic_uid} 生成有效向量")
            signature = make_embedding_signature(
                self.embedding_provider,
                dimension=len(vector),
                input_format_version=input_format,
            )
            topic.metadata = {**topic.metadata, "embedding": vector}
            topic.embedding_signature = signature
            topic_updates.append(
                {
                    "topic_uid": topic.topic_uid,
                    "expected_revision": topic.revision,
                    "embedding": vector,
                    "embedding_signature": signature,
                }
            )
            topics_with_vectors.append(topic)
            await self._emit(
                progress_callback,
                operation_uid,
                "revector_topics",
                position,
                len(topics),
                activity="centroid",
            )

        await self._emit(
            progress_callback,
            operation_uid,
            "revector_relations",
            0,
            1,
            activity="relations",
        )
        relations = self._derive_topic_relations(operation_uid, topics_with_vectors)
        published = await self.store.replace_embeddings_and_relations(
            memory_space_id=memory_space_id,
            topic_updates=topic_updates,
            fragment_updates=fragment_updates,
            relations=relations,
        )
        if self.vector_index is not None:
            self.vector_index.invalidate(memory_space_id)
        await self._emit(
            progress_callback,
            operation_uid,
            "revector_relations",
            1,
            1,
            activity="publication",
        )
        return {
            "memory_space_id": memory_space_id,
            "topic_count": published["topics"],
            "fragment_count": published["fragments"],
            "relation_count": published["relations"],
        }

    async def clear_topic_space(self, memory_space_id: str) -> dict[str, int]:
        """Clear one space while excluding builds and other Topic maintenance."""
        memory_space_id = str(memory_space_id or "").strip()
        if not memory_space_id:
            raise ValueError("memory_space_id is required")
        lock = self._space_locks.setdefault(memory_space_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Topic build is already running for this memory space")
        async with lock:
            result = await self.store.clear_space(memory_space_id)
            if self.vector_index is not None:
                self.vector_index.invalidate(memory_space_id)
            return result

    async def repair_deleted_timeline_sources(
        self,
        memory_space_id: str,
        *,
        affected_topic_uids: list[str],
        deleted_timeline_uids: list[str],
        review_uid: str | None = None,
        progress_callback=None,
    ) -> dict[str, Any]:
        """Rebuild affected Topics only from their remaining authoritative sources."""
        normalized_topics = sorted(
            {str(uid).strip() for uid in affected_topic_uids if str(uid).strip()}
        )
        deleted = {
            str(uid).strip() for uid in deleted_timeline_uids if str(uid).strip()
        }
        if not memory_space_id or not normalized_topics:
            return {"status": "completed", "topic_uids": [], "archived_topics": 0}
        lock = self._space_locks.setdefault(memory_space_id, asyncio.Lock())
        async with lock:
            topics = await self.store.get_topics_by_uids(
                memory_space_id,
                normalized_topics,
                status=None,
            )
            repair_run_uid = str(uuid.uuid4())
            fragment_groups: list[list[TopicFragmentDraft]] = []
            retained_topics: list[TopicMemory | None] = []
            for topic in topics:
                fragment = await self._existing_topic_fragment(
                    repair_run_uid,
                    topic,
                    exclude_timeline_uids=deleted,
                )
                if fragment is None:
                    continue
                fragment_groups.append([fragment])
                retained_topics.append(topic)
            result = await self._publish_governance_groups(
                memory_space_id,
                fragment_groups,
                retained_topics=retained_topics,
                affected_topic_uids=set(normalized_topics),
                operation="deleted_timeline_source_repair",
                operation_payload={
                    "deleted_timeline_uids": sorted(deleted),
                    "review_uid": review_uid,
                },
                progress_callback=progress_callback,
            )
            if review_uid:
                await self.store.set_maintenance_review_status(
                    review_uid,
                    status="resolved",
                    action="automatic_source_repair",
                    payload=result,
                )
            return result

    async def build_space(
        self,
        memory_space_id: str,
        *,
        mode: TopicMaintenanceMode = TopicMaintenanceMode.FULL,
        since: float | None = None,
        timeline_uids: list[str] | None = None,
        reset_topics: bool = False,
        progress_callback=None,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Scan and build one memory space while serializing concurrent runs."""
        mode = TopicMaintenanceMode(mode)
        if reset_topics and mode is not TopicMaintenanceMode.FULL:
            raise ValueError("Topic reset is only available for full builds")
        lock = self._space_locks.setdefault(memory_space_id, asyncio.Lock())
        async with lock:
            selected = list(dict.fromkeys(timeline_uids or []))
            batch_limit = max(1, int(self.config.get("incremental_max_timelines", 120)))
            if mode is TopicMaintenanceMode.INCREMENTAL and timeline_uids is None:
                existing = await self.store.list_topics(
                    memory_space_id,
                    status=TopicMemoryStatus.ACTIVE,
                    limit=1,
                )
                if existing:
                    selected = await self.candidate_manager.list_candidate_uids(
                        memory_space_id,
                        since=since,
                        only_unindexed=True,
                    )
                    timeline_uids = selected
                    since = None
                    if not selected:
                        return {
                            "status": "completed",
                            "memory_space_id": memory_space_id,
                            "batch_count": 0,
                            "run_uids": [],
                            "timeline_count": 0,
                            "fragment_count": 0,
                            "topic_count": 0,
                            "topics": [],
                            "pipeline": "bounded_delta_batches",
                        }
            if mode is TopicMaintenanceMode.INCREMENTAL and automatic:
                auto_limit = max(
                    1,
                    int(self.config.get("incremental_auto_max_timelines", 240)),
                )
                if len(selected) > auto_limit:
                    review_uid = await self.store.enqueue_maintenance_review(
                        memory_space_id=memory_space_id,
                        review_type="incremental_scope_too_large",
                        timeline_uids=selected,
                        topic_uids=[],
                        details={
                            "timeline_count": len(selected),
                            "automatic_limit": auto_limit,
                        },
                    )
                    logger.warning(
                        "[TopicMemory] 自动增量范围超过上限，已等待用户确认 "
                        "(memory_space_id=%s, timelines=%s, limit=%s)",
                        memory_space_id,
                        len(selected),
                        auto_limit,
                    )
                    return {
                        "status": "pending_confirmation",
                        "memory_space_id": memory_space_id,
                        "review_uid": review_uid,
                        "timeline_count": len(selected),
                        "automatic_limit": auto_limit,
                        "pipeline": "bounded_delta_batches",
                    }
            if mode is TopicMaintenanceMode.INCREMENTAL and len(selected) > batch_limit:
                results: list[dict[str, Any]] = []
                total_batches = (len(selected) + batch_limit - 1) // batch_limit
                for batch_index, offset in enumerate(
                    range(0, len(selected), batch_limit), 1
                ):
                    batch = selected[offset : offset + batch_limit]
                    batch_progress = self._batched_progress_callback(
                        progress_callback,
                        batch_index=batch_index,
                        total_batches=total_batches,
                    )
                    result = await self._build_space_locked(
                        memory_space_id,
                        mode=mode,
                        since=None,
                        timeline_uids=batch,
                        reset_topics=False,
                        progress_callback=batch_progress,
                        batch_index=batch_index,
                        total_batches=total_batches,
                    )
                    results.append(result)
                return {
                    "status": (
                        "completed_with_review"
                        if any(
                            item.get("status") == "completed_with_review"
                            for item in results
                        )
                        else "completed"
                    ),
                    "memory_space_id": memory_space_id,
                    "batch_count": total_batches,
                    "run_uids": [item["run_uid"] for item in results],
                    "timeline_count": sum(
                        item.get("timeline_count", 0) for item in results
                    ),
                    "fragment_count": sum(
                        item.get("fragment_count", 0) for item in results
                    ),
                    "topic_count": sum(item.get("topic_count", 0) for item in results),
                    "topics": [
                        topic for item in results for topic in item.get("topics", [])
                    ],
                    "component_outcomes": {
                        component_uid: outcome
                        for item in results
                        for component_uid, outcome in (
                            item.get("component_outcomes") or {}
                        ).items()
                    },
                    "pipeline": "bounded_delta_batches",
                }
            return await self._build_space_locked(
                memory_space_id,
                mode=mode,
                since=since,
                timeline_uids=timeline_uids,
                reset_topics=reset_topics,
                progress_callback=progress_callback,
                batch_index=1,
                total_batches=1,
            )

    @staticmethod
    def _batched_progress_callback(
        progress_callback,
        *,
        batch_index: int,
        total_batches: int,
    ):
        if progress_callback is None:
            return None

        async def emit(event: dict[str, Any]) -> None:
            await progress_callback(
                {
                    **event,
                    "maintenance_batch_index": batch_index,
                    "maintenance_batch_total": total_batches,
                }
            )

        return emit

    async def _build_space_locked(
        self,
        memory_space_id: str,
        *,
        mode: TopicMaintenanceMode,
        since: float | None,
        timeline_uids: list[str] | None,
        reset_topics: bool,
        progress_callback,
        batch_index: int,
        total_batches: int,
    ) -> dict[str, Any]:
        incremental_scope: dict[str, Any] | None = None
        scan_only_unindexed = False
        if mode is TopicMaintenanceMode.INCREMENTAL:
            existing = await self.store.list_topics(
                memory_space_id,
                status=TopicMemoryStatus.ACTIVE,
                limit=1,
            )
            if not existing:
                if timeline_uids is None:
                    mode = TopicMaintenanceMode.FULL
                    since = None
                else:
                    scan_only_unindexed = True
            else:
                selected_only = timeline_uids is not None
                seeds = await self.candidate_manager.load_candidates(
                    memory_space_id,
                    since=since,
                    timeline_uids=timeline_uids,
                    only_unindexed=selected_only,
                )
                incremental_scope = (
                    await self.candidate_manager.prepare_incremental_scope(
                        memory_space_id,
                        seeds,
                        time_gap_seconds=float(self.config.get("time_gap_hours", 6.0))
                        * 3600.0,
                        max_timelines=int(
                            self.config.get("incremental_max_timelines", 120)
                        ),
                    )
                )
                timeline_uids = list(incremental_scope["timeline_uids"])
                since = None
        scan = await self.candidate_manager.start_scan(
            memory_space_id,
            mode=mode,
            since=since,
            timeline_uids=timeline_uids,
            only_unindexed=scan_only_unindexed,
            batch_size=int(self.config.get("candidate_batch_size", 100)),
            time_gap_seconds=float(self.config.get("time_gap_hours", 6.0)) * 3600.0,
            similarity_threshold=float(
                self.config.get("candidate_similarity_threshold", 0.52)
            ),
            progress_callback=progress_callback,
            run_config={
                "topic_settings": dict(self.config),
                "topic_settings_revision": TOPIC_SETTINGS_REVISION,
                "supplemental_identity_profiles": (
                    self._supplemental_profile_payload()
                ),
                "time_cluster_keys": dict(
                    (incremental_scope or {}).get("time_cluster_keys", {})
                ),
            },
            run_metadata={
                "incremental_scope": incremental_scope or {},
                "pipeline": "bounded_delta_full_pipeline",
                "reset_topics": bool(reset_topics),
                "batch_index": batch_index,
                "total_batches": total_batches,
            },
        )
        return await self.build_from_scan(
            scan["run_uid"], progress_callback=progress_callback
        )

    async def resume_run(
        self, run_uid: str, *, progress_callback=None
    ) -> dict[str, Any]:
        """Resume a persisted run from its latest durable stage boundary."""
        run = await self.store.get_maintenance_run(run_uid)
        if run is None:
            raise ValueError(f"Topic maintenance run not found: {run_uid}")
        memory_space_id = str(run.get("memory_space_id") or "").strip()
        if not memory_space_id:
            raise ValueError(f"Topic maintenance run has no memory space: {run_uid}")
        lock = self._space_locks.setdefault(memory_space_id, asyncio.Lock())
        async with lock:
            run = await self.store.get_maintenance_run(run_uid)
            if run is None:
                raise ValueError(f"Topic maintenance run not found: {run_uid}")
            status = str(run.get("status") or "")
            if status in {
                TopicMaintenanceStatus.COMPLETED.value,
                TopicMaintenanceStatus.COMPLETED_WITH_REVIEW.value,
            }:
                raise ValueError("Completed Topic maintenance runs cannot be resumed")
            stage = str(run.get("stage") or "candidate_scan")
            groups = await self.store.list_candidate_groups(run_uid)
            if (
                stage in {"pending", "candidate_scan", "candidate_scan_completed"}
                or not groups
            ):
                scan = await self.candidate_manager.resume_scan(
                    run_uid,
                    progress_callback=progress_callback,
                )
                run_uid = str(scan["run_uid"])
            return await self.build_from_scan(
                run_uid,
                progress_callback=progress_callback,
            )

    async def build_from_scan(
        self, run_uid: str, *, progress_callback=None
    ) -> dict[str, Any]:
        """Run one build with the immutable settings captured at task creation."""
        run = await self.store.get_maintenance_run(run_uid)
        if run is None:
            raise ValueError(f"Topic maintenance run not found: {run_uid}")
        run_config = run.get("config") or {}
        snapshot = run_config.get("topic_settings")
        merged = dict(self._default_config)
        if isinstance(snapshot, dict):
            merged.update(snapshot)
        context = self._make_run_context(
            memory_space_id=str(run["memory_space_id"]),
            run_uid=run_uid,
            config=merged,
            supplemental_identity_profiles=self._profiles_from_run_config(run_config),
        )
        token = self._runtime_context.set(context)
        try:
            return await self._build_from_scan_impl(
                run_uid, progress_callback=progress_callback
            )
        finally:
            self._runtime_context.reset(token)

    async def _build_from_scan_impl(
        self, run_uid: str, *, progress_callback=None
    ) -> dict[str, Any]:
        run = await self.store.get_maintenance_run(run_uid)
        if run is None:
            raise ValueError(f"Topic maintenance run not found: {run_uid}")
        groups = await self.store.list_candidate_groups(run_uid)
        candidates = await self.store.get_scan_items(run_uid)
        candidate_map = {item.memory_uid: item for item in candidates}
        memory_space_id = str(run["memory_space_id"])
        run_mode = TopicMaintenanceMode(str(run["mode"]))
        reset_topics = bool((run.get("metadata") or {}).get("reset_topics", False))
        await self.store.update_maintenance_run(
            run_uid,
            status=TopicMaintenanceStatus.RUNNING,
            stage="fragment_extraction",
            current_group_index=0,
            total_groups=len(groups),
            error="",
        )
        try:
            await self._extract_groups_concurrently(
                run_uid,
                groups,
                candidate_map,
                progress_callback=progress_callback,
            )

            fragments = await self.store.list_fragments(run_uid=run_uid)
            if not fragments and candidates:
                raise TopicBuildValidationError(
                    "LLM did not produce any Topic fragments"
                )
            await self.store.update_maintenance_run(
                run_uid,
                stage="embedding",
                current_group_index=0,
                total_groups=len(fragments),
            )
            fragments = await self._embed_fragments(fragments, progress_callback)
            components, scores = await self._match_fragments_checkpointed(
                run_uid,
                fragments,
                progress_callback=progress_callback,
            )
            matched_component_count = len(components)
            await self.store.update_maintenance_run(
                run_uid,
                stage="component_review",
                current_group_index=0,
                total_groups=len(components),
            )
            components = await self._review_components_checkpointed(
                run_uid,
                fragments,
                components,
                progress_callback=progress_callback,
            )
            reviewed_component_count = len(components)
            await self.store.update_maintenance_run(
                run_uid,
                stage="topic_synthesis",
                current_group_index=0,
                total_groups=len(components),
            )

            built: list[dict[str, Any]] = []
            plans: list[dict[str, Any]] = []
            incremental_scope = (
                (run.get("metadata") or {}).get("incremental_scope", {})
                if run_mode is TopicMaintenanceMode.INCREMENTAL
                else {}
            )
            affected_topic_uids = {
                str(uid)
                for uid in incremental_scope.get("affected_topic_uids", [])
                if str(uid)
            }
            all_existing = (
                await self.store.list_all_topics(
                    memory_space_id,
                    status=TopicMemoryStatus.ACTIVE,
                )
                if run_mode is TopicMaintenanceMode.FULL
                else await self.store.get_topics_by_uids(
                    memory_space_id,
                    sorted(affected_topic_uids),
                )
            )
            existing = [] if reset_topics else list(all_existing)
            seed_timeline_uids = {
                str(uid)
                for uid in incremental_scope.get("seed_timeline_uids", [])
                if str(uid)
            }
            seed_topic_uids = {
                str(uid)
                for uid in incremental_scope.get("seed_topic_uids", [])
                if str(uid)
            }
            if (
                run_mode is TopicMaintenanceMode.INCREMENTAL
                and seed_timeline_uids
                and "seed_topic_uids" not in incremental_scope
            ):
                for timeline_uid in seed_timeline_uids:
                    for row in await self.store.get_topics_for_timeline(timeline_uid):
                        if (
                            str(row.get("status") or "")
                            == TopicMemoryStatus.ACTIVE.value
                            and str(row.get("link_status") or "") == "active"
                        ):
                            seed_topic_uids.add(str(row["topic_uid"]))
            scoped_incremental_publish = bool(
                run_mode is TopicMaintenanceMode.INCREMENTAL and seed_timeline_uids
            )
            used_existing: set[str] = set()
            updated_existing_topic_uids: set[str] = set()
            component_outcomes: dict[str, dict[str, Any]] = {}
            pending_review_topic_uids: set[str] = set()
            component_fragment_sets = [
                [fragments[index] for index in component] for component in components
            ]
            synthesis_progress_lock = asyncio.Lock()
            completed_initial_syntheses = 0

            def synthesis_progress_for(
                position: int,
                component_fragments: list[TopicFragmentDraft],
                *,
                stage_current: int | None = None,
            ):
                async def synthesis_progress(
                    current: int,
                    total: int,
                    batch_fragment_count: int,
                    level: int,
                ) -> None:
                    await self._emit(
                        progress_callback,
                        run_uid,
                        "topic_synthesis",
                        (
                            completed_initial_syntheses
                            if stage_current is None
                            else stage_current
                        ),
                        len(components),
                        activity="llm_call",
                        item_kind="topic_component",
                        item_index=position,
                        item_total=len(components),
                        fragment_count=len(component_fragments),
                        batch_fragment_count=batch_fragment_count,
                        synthesis_level=level,
                        llm_call_current=current,
                        llm_call_total=total,
                        llm_concurrency=self.llm_concurrency,
                    )

                return synthesis_progress

            async def synthesize_initial_component(
                position: int,
                component_fragments: list[TopicFragmentDraft],
            ) -> dict[str, Any]:
                nonlocal completed_initial_syntheses
                synthesis = await self._synthesize_component_checkpointed(
                    run_uid,
                    component_fragments,
                    progress_callback=synthesis_progress_for(
                        position,
                        component_fragments,
                    ),
                )
                async with synthesis_progress_lock:
                    completed_initial_syntheses += 1
                    await self._emit(
                        progress_callback,
                        run_uid,
                        "topic_synthesis",
                        completed_initial_syntheses,
                        len(components),
                        activity="stage_progress",
                        item_kind="topic_component",
                        item_index=position,
                        item_total=len(components),
                        fragment_count=len(component_fragments),
                        llm_concurrency=self.llm_concurrency,
                    )
                return synthesis

            initial_syntheses = await self._gather_cancel_on_error(
                [
                    synthesize_initial_component(
                        position,
                        component_fragments,
                    )
                    for position, component_fragments in enumerate(
                        component_fragment_sets,
                        1,
                    )
                ]
            )

            assignments: list[dict[str, Any]] = []
            assignment_proposals: list[dict[str, Any]] = []
            for position, (initial_fragments, synthesis) in enumerate(
                zip(component_fragment_sets, initial_syntheses, strict=True),
                1,
            ):
                component_fragments = list(initial_fragments)
                component_uid = self._component_uid(component_fragments)
                match_pool = existing
                if run_mode is TopicMaintenanceMode.INCREMENTAL:
                    match_pool = await self._incremental_existing_candidates(
                        memory_space_id,
                        component_fragments,
                        existing,
                        affected_topic_uids,
                    )
                match_diagnostics: dict[str, dict[str, Any]] = {}
                (
                    matched,
                    match_scores,
                    ambiguous,
                ) = await self._match_existing_topic_decision(
                    synthesis,
                    component_fragments,
                    match_pool,
                    (
                        used_existing
                        if run_mode is not TopicMaintenanceMode.INCREMENTAL
                        else set()
                    ),
                    require_source_overlap=False,
                    incremental=(run_mode is TopicMaintenanceMode.INCREMENTAL),
                    diagnostics=match_diagnostics,
                )
                proposal = {
                    "position": position,
                    "component_uid": component_uid,
                    "fragments": component_fragments,
                    "synthesis": synthesis,
                    "matched": matched,
                    "match_scores": match_scores,
                    "match_diagnostics": match_diagnostics,
                    "ambiguous": ambiguous,
                }
                assignment_proposals.append(proposal)
                if matched and run_mode is not TopicMaintenanceMode.INCREMENTAL:
                    used_existing.add(matched.topic_uid)

            if run_mode is TopicMaintenanceMode.INCREMENTAL:
                self._refine_incremental_event_assignments(assignment_proposals)

            for proposal in assignment_proposals:
                position = int(proposal["position"])
                component_uid = str(proposal["component_uid"])
                component_fragments = list(proposal["fragments"])
                synthesis = proposal["synthesis"]
                matched = proposal["matched"]
                match_scores = list(proposal["match_scores"])
                match_diagnostics = dict(proposal["match_diagnostics"])
                ambiguous = bool(proposal["ambiguous"])
                if ambiguous:
                    review_topic_uids = [item[1].topic_uid for item in match_scores[:2]]
                    pending_review_topic_uids.update(review_topic_uids)
                    await self.store.enqueue_maintenance_review(
                        memory_space_id=memory_space_id,
                        review_type="ambiguous_topic_match",
                        timeline_uids=sorted(
                            {
                                uid
                                for fragment in component_fragments
                                for uid in fragment.timeline_uids
                            }
                        ),
                        topic_uids=review_topic_uids,
                        component_uid=component_uid,
                        details={
                            "component_uid": component_uid,
                            "run_uid": run_uid,
                            "fragment_uids": [
                                fragment.fragment_uid
                                for fragment in component_fragments
                            ],
                            "proposed_title": str(synthesis.get("title") or ""),
                            "proposed_summary": str(synthesis.get("summary") or ""),
                            "scores": [
                                {"topic_uid": item[1].topic_uid, "score": item[0]}
                                for item in match_scores[:3]
                            ],
                            "match_diagnostics": match_diagnostics,
                        },
                    )
                    component_outcomes[component_uid] = {
                        "status": "pending_review",
                        "fragment_uids": self._component_fragment_uids(
                            component_fragments
                        ),
                        "topic_uids": review_topic_uids,
                    }
                    # Do not publish a duplicate Topic merely because the local
                    # match is ambiguous. With no active link the Timeline stays
                    # eligible for a later confirmed/full maintenance run.
                    continue
                component_timeline_uids = {
                    uid
                    for fragment in component_fragments
                    for uid in fragment.timeline_uids
                }
                if (
                    scoped_incremental_publish
                    and not component_timeline_uids & seed_timeline_uids
                    and not (
                        matched is not None and matched.topic_uid in seed_topic_uids
                    )
                ):
                    component_outcomes[component_uid] = {
                        "status": "retained_old",
                        "reason": "outside_incremental_seed_scope",
                        "fragment_uids": self._component_fragment_uids(
                            component_fragments
                        ),
                    }
                    continue
                assignments.append(
                    {
                        "position": position,
                        "component_uid": component_uid,
                        "fragments": component_fragments,
                        "synthesis": synthesis,
                        "matched": matched,
                        "match_scores": match_scores,
                        "match_diagnostics": match_diagnostics,
                    }
                )

            # Incremental components are assigned before any Topic is materialized.
            # This removes order dependence and lets several coherent deltas extend
            # one existing Topic through a single synthesis and atomic snapshot.
            assignment_groups: dict[str, list[dict[str, Any]]] = {}
            for assignment in assignments:
                matched = assignment["matched"]
                key = (
                    f"topic:{matched.topic_uid}"
                    if run_mode is TopicMaintenanceMode.INCREMENTAL
                    and matched is not None
                    else f"component:{assignment['component_uid']}"
                )
                assignment_groups.setdefault(key, []).append(assignment)

            completed_materializations = 0
            for grouped_assignments in assignment_groups.values():
                first_assignment = grouped_assignments[0]
                matched = first_assignment["matched"]
                position = int(first_assignment["position"])
                initial_fragments = list(
                    {
                        fragment.fragment_uid: fragment
                        for assignment in grouped_assignments
                        for fragment in assignment["fragments"]
                    }.values()
                )
                component_fragments = list(initial_fragments)
                synthesis = first_assignment["synthesis"]
                component_uids = [
                    str(assignment["component_uid"])
                    for assignment in grouped_assignments
                ]
                if matched is not None and run_mode is TopicMaintenanceMode.INCREMENTAL:
                    existing_fragment = await self._existing_topic_fragment(
                        run_uid,
                        matched,
                        exclude_timeline_uids=seed_timeline_uids,
                    )
                    if existing_fragment is not None:
                        component_fragments = [existing_fragment, *component_fragments]
                    if existing_fragment is not None or len(grouped_assignments) > 1:
                        synthesis = await self._synthesize_component_checkpointed(
                            run_uid,
                            component_fragments,
                            progress_callback=synthesis_progress_for(
                                position,
                                component_fragments,
                                stage_current=len(components),
                            ),
                        )
                (
                    topic,
                    atoms,
                    links,
                    sources,
                    actor_links,
                    atom_actor_links,
                ) = self._materialize_snapshot(
                    run_uid,
                    memory_space_id,
                    synthesis,
                    component_fragments,
                    candidate_map,
                    matched,
                )
                plans.append(
                    {
                        "topic": topic,
                        "atoms": atoms,
                        "links": links,
                        "sources": sources,
                        "actor_links": actor_links,
                        "atom_actor_links": atom_actor_links,
                        "matched": matched,
                        "fragments": component_fragments,
                        "synthesis": synthesis,
                        "component_uid": self._component_uid(initial_fragments),
                        "component_uids": component_uids,
                        "match_diagnostics": {
                            str(assignment["component_uid"]): assignment[
                                "match_diagnostics"
                            ]
                            for assignment in grouped_assignments
                        },
                    }
                )
                if matched:
                    updated_existing_topic_uids.add(matched.topic_uid)
                for assignment in grouped_assignments:
                    component_outcomes[str(assignment["component_uid"])] = {
                        "status": (
                            "published_update" if matched else "published_create"
                        ),
                        "fragment_uids": self._component_fragment_uids(
                            assignment["fragments"]
                        ),
                        "topic_uids": [matched.topic_uid] if matched else [],
                        "match_diagnostics": assignment["match_diagnostics"],
                    }
                completed_materializations += len(grouped_assignments)
                await self.store.update_maintenance_run(
                    run_uid,
                    stage="topic_synthesis",
                    current_group_index=completed_materializations,
                    total_groups=len(components),
                )
                await self._emit(
                    progress_callback,
                    run_uid,
                    "topic_synthesis",
                    completed_materializations,
                    len(components),
                    activity="stage_progress",
                    item_kind="topic_component",
                    item_index=position,
                    item_total=len(components),
                    fragment_count=len(component_fragments),
                )

            if run_mode is TopicMaintenanceMode.INCREMENTAL:
                for affected_topic in all_existing:
                    if (
                        affected_topic.topic_uid not in affected_topic_uids
                        or affected_topic.topic_uid in updated_existing_topic_uids
                    ):
                        continue
                    retained_plan = await self._retained_affected_topic_plan(
                        run_uid=run_uid,
                        topic=affected_topic,
                        excluded_timeline_uids=seed_timeline_uids,
                        candidate_map=candidate_map,
                    )
                    if retained_plan is None:
                        continue
                    plans.append(retained_plan)
                    updated_existing_topic_uids.add(affected_topic.topic_uid)

            await self.store.update_maintenance_run(
                run_uid,
                stage="materialization",
                current_group_index=0,
                total_groups=len(plans),
            )
            snapshots: list[dict[str, Any]] = []
            for position, plan in enumerate(plans, 1):
                topic = plan["topic"]
                atoms = plan["atoms"]
                links = plan["links"]
                sources = plan["sources"]
                actor_links = plan["actor_links"]
                atom_actor_links = plan["atom_actor_links"]
                matched = plan["matched"]
                fragment_uids = [item.fragment_uid for item in plan["fragments"]]
                formal_fragments: list[TopicFragmentDraft] = []
                for fragment in plan["fragments"]:
                    existing_topic_uid = str(
                        fragment.metadata.get("existing_topic_uid") or ""
                    )
                    if not existing_topic_uid:
                        formal_fragments.append(fragment)
                        continue
                    existing_rows = await self.store.list_active_fragments_for_topics(
                        [existing_topic_uid]
                    )
                    formal_fragments.extend(
                        row["fragment"]
                        for row in existing_rows
                        if not (set(row["fragment"].timeline_uids) & seed_timeline_uids)
                    )
                formal_fragments = list(
                    {
                        fragment.fragment_uid: fragment for fragment in formal_fragments
                    }.values()
                )
                actor_links, atom_actor_links = (
                    self._normalize_published_actor_provenance(
                        actor_links,
                        atom_actor_links,
                        formal_fragments,
                    )
                )
                topic.metadata["fragment_uids"] = [
                    item.fragment_uid for item in formal_fragments
                ]
                decision_uid = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"livingmemory:topic-build:{run_uid}:{topic.topic_uid}",
                    )
                )
                snapshots.append(
                    {
                        "topic": topic,
                        "atoms": atoms,
                        "links": links,
                        "atom_sources": sources,
                        "actor_links": actor_links,
                        "atom_actor_links": atom_actor_links,
                        "fragments": formal_fragments,
                        "expected_revision": (
                            None
                            if reset_topics
                            else matched.revision
                            if matched
                            else None
                        ),
                        "decision": {
                            "decision_uid": decision_uid,
                            "action": "update" if matched else "create",
                            "fragment_uids": fragment_uids,
                            "candidate_scores": {
                                key: value
                                for key, value in scores.items()
                                if any(uid in key for uid in fragment_uids)
                            }
                            | {
                                f"existing:{component_uid}:{topic_uid}": float(
                                    details.get("score") or 0.0
                                )
                                for component_uid, component_details in (
                                    plan.get("match_diagnostics") or {}
                                ).items()
                                for topic_uid, details in component_details.items()
                                if topic_uid != "_decision"
                                and isinstance(details, dict)
                            },
                            "llm_output": plan["synthesis"],
                            "metadata": {
                                "component_uid": str(plan.get("component_uid") or ""),
                                "component_uids": list(
                                    plan.get("component_uids") or []
                                ),
                                "component_outcome": (
                                    "published_update"
                                    if matched
                                    else "published_create"
                                ),
                                "existing_topic_match": dict(
                                    plan.get("match_diagnostics") or {}
                                ),
                            },
                        },
                    },
                )
                await self.store.update_maintenance_run(
                    run_uid,
                    stage="materialization",
                    current_group_index=position,
                    total_groups=len(plans),
                )
                await self._emit(
                    progress_callback,
                    run_uid,
                    "materialization",
                    position,
                    len(plans),
                )

            expected_component_uids = {
                self._component_uid(items) for items in component_fragment_sets
            }
            missing_component_uids = sorted(
                expected_component_uids - component_outcomes.keys()
            )
            if missing_component_uids:
                raise TopicBuildValidationError(
                    "Topic component coverage incomplete: "
                    + ", ".join(missing_component_uids[:8])
                )

            publication_affected_topic_uids = set(affected_topic_uids)
            if run_mode is TopicMaintenanceMode.INCREMENTAL:
                # An unresolved component cannot authorize archiving any of its
                # candidate Topics. Keep their current snapshots until reviewed.
                publication_affected_topic_uids -= pending_review_topic_uids
            relation_scope_topic_uids: set[str] | None = None
            if run_mode is TopicMaintenanceMode.INCREMENTAL:
                relation_scope_topic_uids = {
                    *publication_affected_topic_uids,
                    *(snapshot["topic"].topic_uid for snapshot in snapshots),
                }
                relation_topics = await self._incremental_relation_topics(
                    memory_space_id,
                    [snapshot["topic"] for snapshot in snapshots],
                    {snapshot["topic"].topic_uid for snapshot in snapshots},
                )
            else:
                relation_topics = [snapshot["topic"] for snapshot in snapshots]
            relations = self._derive_topic_relations(run_uid, relation_topics)
            if relation_scope_topic_uids is not None:
                relations = [
                    relation
                    for relation in relations
                    if relation.left_topic_uid in relation_scope_topic_uids
                    or relation.right_topic_uid in relation_scope_topic_uids
                ]
            await self.store.update_maintenance_run(
                run_uid,
                stage="publication",
                current_group_index=0,
                total_groups=1,
            )
            await self._emit(
                progress_callback,
                run_uid,
                "publication",
                0,
                1,
                activity="atomic_publish",
                item_kind="topic_generation",
                topic_count=len(snapshots),
            )
            completion_status = (
                TopicMaintenanceStatus.COMPLETED_WITH_REVIEW
                if any(
                    outcome.get("status") == "pending_review"
                    for outcome in component_outcomes.values()
                )
                else TopicMaintenanceStatus.COMPLETED
            )
            additional_decisions = [
                {
                    "decision_uid": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"livingmemory:topic-build:{run_uid}:{component_uid}",
                        )
                    ),
                    "topic_uid": ((outcome.get("topic_uids") or [None])[0]),
                    "action": str(outcome.get("status") or "unknown"),
                    "fragment_uids": list(outcome.get("fragment_uids") or []),
                    "candidate_scores": {},
                    "llm_output": {},
                    "metadata": {
                        "component_uid": component_uid,
                        "reason": str(outcome.get("reason") or ""),
                        "topic_uids": list(outcome.get("topic_uids") or []),
                        "existing_topic_match": dict(
                            outcome.get("match_diagnostics") or {}
                        ),
                    },
                }
                for component_uid, outcome in component_outcomes.items()
                if not str(outcome.get("status") or "").startswith("published_")
            ]
            publication = await self.store.publish_topic_build(
                run_uid=run_uid,
                memory_space_id=memory_space_id,
                mode=run_mode,
                snapshots=snapshots,
                relations=relations,
                affected_topic_uids=publication_affected_topic_uids,
                reset_topics=reset_topics,
                relation_scope_topic_uids=relation_scope_topic_uids,
                sync_pending_topic_uids=pending_review_topic_uids,
                additional_decisions=additional_decisions,
                completion_status=completion_status,
            )
            published_component_uids = [
                component_uid
                for component_uid, outcome in component_outcomes.items()
                if str(outcome.get("status") or "").startswith("published_")
            ]
            if published_component_uids:
                await self._resolve_maintenance_reviews_safely(
                    memory_space_id,
                    component_uids=published_component_uids,
                )
            if self.vector_index is not None:
                self.vector_index.invalidate(memory_space_id)
            await self._emit(
                progress_callback,
                run_uid,
                "publication",
                1,
                1,
                activity="atomic_publish",
                item_kind="topic_generation",
                topic_count=len(snapshots),
            )
            for saved, snapshot in zip(publication["topics"], snapshots, strict=True):
                built.append(
                    {
                        "topic_uid": saved.topic_uid,
                        "revision": saved.revision,
                        "title": saved.title,
                        "timeline_count": len(snapshot["links"]),
                        "atom_count": len(snapshot["atoms"]),
                    }
                )
            return {
                "run_uid": run_uid,
                "status": completion_status.value,
                "memory_space_id": memory_space_id,
                "timeline_count": len(candidates),
                "fragment_count": len(fragments),
                "matched_component_count": matched_component_count,
                "reviewed_component_count": reviewed_component_count,
                "topic_count": len(built),
                "topics": built,
                "related_topic_count": publication["relation_count"],
                "rerank_used": self.rerank_provider is not None,
                "component_outcomes": component_outcomes,
                **(
                    {"reset": publication["reset"]}
                    if publication.get("reset") is not None
                    else {}
                ),
            }
        except asyncio.CancelledError:
            await asyncio.shield(
                self.store.update_maintenance_run(
                    run_uid,
                    status=TopicMaintenanceStatus.PENDING,
                )
            )
            raise
        except Exception as exc:
            await self.store.update_maintenance_run(
                run_uid,
                status=TopicMaintenanceStatus.FAILED,
                error=str(exc)[:1000],
            )
            raise


__all__ = ["TopicBuildManager", "TopicBuildValidationError"]
