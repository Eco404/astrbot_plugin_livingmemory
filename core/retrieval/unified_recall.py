"""One retrieval coordinator for passive injection and active Agent recall."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ..models.memory_identity import resolve_memory_space
from .recall_pipeline import RecallPipeline, RecallPipelineResult, RecallQueryBranch
from .topic_recall_pipeline import TopicRecallPipeline


@dataclass(slots=True)
class UnifiedRecallRequest:
    query: str
    final_k: int
    session_id: str
    persona_id: str | None
    recall_session_id: str | None
    recall_persona_id: str | None
    session_scope: list[str] = field(default_factory=list)
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    expansion_enabled: bool = False
    assistant_mode: str = "exclude"
    visible_message_start_index: int | None = None
    visible_message_end_index: int | None = None
    current_actor_ids: set[str] = field(default_factory=set)
    topic_enabled: bool | None = None


@dataclass(slots=True)
class UnifiedRecallOutcome:
    timeline_outcome: RecallPipelineResult
    topic_outcome: Any | None = None
    fragment_outcome: Any | None = None
    timeline_results: list[Any] = field(default_factory=list)
    topic_results: list[Any] = field(default_factory=list)
    fragment_results: list[Any] = field(default_factory=list)

    def diagnostics(self) -> dict[str, Any]:
        def build(value: Any) -> dict[str, Any] | None:
            diagnostics = getattr(value, "diagnostics", None)
            return diagnostics() if callable(diagnostics) else None

        return {
            "timeline": self.timeline_outcome.diagnostics(),
            "topic": build(self.topic_outcome),
            "topic_fragments": build(self.fragment_outcome),
        }


class UnifiedRecallCoordinator:
    """Apply the same qualification and supplement policy at every entry point."""

    def __init__(self, memory_engine: Any, config_manager: Any) -> None:
        self.memory_engine = memory_engine
        self.config_manager = config_manager
        self.timeline_pipeline = RecallPipeline(memory_engine, config_manager)

    def topic_enabled(self) -> bool:
        return bool(
            getattr(self.memory_engine, "topic_memory_enabled", False) is True
            and self.config_manager.get("topic_memory.recall_enabled", True)
        )

    async def search(self, request: UnifiedRecallRequest) -> UnifiedRecallOutcome:
        timeline_search = self.timeline_pipeline.search(
            current_query=request.query,
            final_k=max(1, int(request.final_k)),
            session_id=request.recall_session_id,
            persona_id=request.recall_persona_id,
            recent_messages=request.recent_messages,
            expansion_enabled=request.expansion_enabled,
            assistant_mode=request.assistant_mode,
            context_session_id=request.session_id,
            visible_message_start_index=request.visible_message_start_index,
            visible_message_end_index=request.visible_message_end_index,
            track_access=False,
        )
        use_topic = (
            self.topic_enabled()
            if request.topic_enabled is None
            else bool(request.topic_enabled)
        )
        topic_search = None
        branches: list[RecallQueryBranch] = []
        if use_topic:
            topic_config = getattr(
                self.memory_engine.topic_recall_pipeline, "config", {}
            ) or {}
            branches = self.timeline_pipeline.build_query_branches(
                request.query,
                request.recent_messages,
                expansion_enabled=request.expansion_enabled,
                assistant_mode=request.assistant_mode,
            )
            scope = request.session_scope or [request.session_id]
            memory_space_ids = list(
                dict.fromkeys(
                    resolve_memory_space(session_id, request.persona_id).memory_space_id
                    for session_id in scope
                )
            )
            topic_pipeline = self.memory_engine.topic_recall_pipeline
            search_spaces = getattr(topic_pipeline, "search_spaces", None)
            if callable(search_spaces):
                search_coro = search_spaces(
                    branches=branches,
                    memory_space_ids=memory_space_ids,
                    final_k=min(
                        max(1, int(request.final_k)),
                        int(topic_config.get("recall_top_k", 3)),
                    ),
                    context_session_id=request.session_id,
                    visible_message_start_index=request.visible_message_start_index,
                    visible_message_end_index=request.visible_message_end_index,
                    current_actor_ids=request.current_actor_ids,
                )
            else:
                search_coro = topic_pipeline.search(
                    branches=branches,
                    memory_space_id=memory_space_ids[0],
                    final_k=min(
                        max(1, int(request.final_k)),
                        int(topic_config.get("recall_top_k", 3)),
                    ),
                    context_session_id=request.session_id,
                    visible_message_start_index=request.visible_message_start_index,
                    visible_message_end_index=request.visible_message_end_index,
                    current_actor_ids=request.current_actor_ids,
                )
            topic_search = self._safe_topic_recall(search_coro, request.session_id)
        if topic_search is not None:
            timeline_outcome, topic_outcome = await asyncio.gather(
                timeline_search, topic_search
            )
        else:
            timeline_outcome = await timeline_search
            topic_outcome = None

        topic_results = topic_outcome.results if topic_outcome else []
        fragment_outcome = None
        fragment_results: list[Any] = []
        if topic_results:
            topic_config = getattr(
                self.memory_engine.topic_recall_pipeline, "config", {}
            ) or {}
            supplement_limit = min(
                int(topic_config.get("timeline_supplement_k", 2)),
                max(0, int(request.final_k) - len(topic_results)),
            )
            fragment_outcome = await self._safe_topic_recall(
                self.memory_engine.topic_recall_pipeline.search_fragment_supplements(
                    branches=branches,
                    topic_results=topic_results,
                    limit=supplement_limit,
                    context_session_id=request.session_id,
                    visible_message_start_index=request.visible_message_start_index,
                    visible_message_end_index=request.visible_message_end_index,
                    query_vectors=getattr(topic_outcome, "query_vectors", None),
                ),
                request.session_id,
            )
            if fragment_outcome is not None:
                fragment_results = fragment_outcome.results
            all_fragments_duplicate_parent = bool(
                fragment_outcome is not None
                and fragment_outcome.available_count > 0
                and int(getattr(fragment_outcome, "duplicate_parent_count", 0))
                == fragment_outcome.available_count
            )
            if fragment_results or all_fragments_duplicate_parent:
                timeline_results = []
            else:
                timeline_results = TopicRecallPipeline.select_timeline_supplements(
                    timeline_outcome.results,
                    topic_results,
                    supplement_limit,
                )
        else:
            timeline_results = timeline_outcome.results
        return UnifiedRecallOutcome(
            timeline_outcome=timeline_outcome,
            topic_outcome=topic_outcome,
            fragment_outcome=fragment_outcome,
            timeline_results=timeline_results,
            topic_results=topic_results,
            fragment_results=fragment_results,
        )

    async def record_access(self, outcome: UnifiedRecallOutcome) -> None:
        """Record only results that were actually returned or injected."""
        if outcome.topic_results:
            await self.memory_engine.topic_recall_pipeline.record_topic_access(
                outcome.topic_results
            )
        source_resolver = getattr(
            self.memory_engine.topic_recall_pipeline,
            "source_timeline_document_ids",
            None,
        )
        source_document_ids = (
            await source_resolver(
                outcome.topic_results,
                outcome.fragment_results,
            )
            if callable(source_resolver)
            and (outcome.topic_results or outcome.fragment_results)
            else []
        )
        document_ids = list(
            dict.fromkeys(
                [item.doc_id for item in outcome.timeline_results]
                + list(source_document_ids)
            )
        )
        if document_ids:
            self.memory_engine.record_memory_access(document_ids)

    @staticmethod
    async def _safe_topic_recall(search_coro: Any, session_id: str) -> Any | None:
        try:
            return await search_coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[%s] Topic 派生层召回失败，本次保留 Timeline 召回: %s",
                session_id,
                exc,
                exc_info=True,
            )
            return None


__all__ = [
    "UnifiedRecallCoordinator",
    "UnifiedRecallOutcome",
    "UnifiedRecallRequest",
]
