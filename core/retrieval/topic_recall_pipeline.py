"""Weighted multi-branch Topic recall with provenance-aware filtering."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..affect_memory import (
    affect_similarity,
    extract_query_affect,
    select_affect_events,
)
from .recall_pipeline import RecallPipeline, RecallQueryBranch
from .topic_retriever import (
    TopicFragmentRecallResult,
    TopicRecallResult,
    TopicRetriever,
)


@dataclass(slots=True)
class TopicFragmentRecallOutcome:
    results: list[TopicFragmentRecallResult]
    candidates: list[TopicFragmentRecallResult]
    available_count: int
    applied_threshold: float
    context_suppressed: int = 0
    selection_threshold: float = 0.0
    duplicate_parent_count: int = 0

    def diagnostics(self) -> dict[str, Any]:
        selected = {item.fragment_uid for item in self.results}
        return {
            "available_count": self.available_count,
            "candidate_count": len(self.candidates),
            "selected_count": len(self.results),
            "applied_threshold": round(self.applied_threshold, 6),
            "selection_threshold": round(self.selection_threshold, 6),
            "context_suppressed": self.context_suppressed,
            "duplicate_parent_count": self.duplicate_parent_count,
            "candidates": [
                {**item.to_dict(), "selected": item.fragment_uid in selected}
                for item in self.candidates
            ],
        }


@dataclass(slots=True)
class TopicRecallOutcome:
    results: list[TopicRecallResult]
    candidates: list[TopicRecallResult]
    applied_threshold: float
    context_suppressed: int = 0
    selection_threshold: float = 0.0
    query_vectors: list[list[float]] | None = None

    def diagnostics(self) -> dict[str, Any]:
        selected_uids = {item.topic_uid for item in self.results}
        return {
            "candidate_count": len(self.candidates),
            "selected_count": len(self.results),
            "applied_threshold": round(self.applied_threshold, 6),
            "selection_threshold": round(self.selection_threshold, 6),
            "context_suppressed": self.context_suppressed,
            "candidates": [
                {
                    **item.to_dict(),
                    "selected": item.topic_uid in selected_uids,
                }
                for item in self.candidates
            ],
        }


class TopicRecallPipeline:
    def __init__(self, retriever: TopicRetriever, config: dict[str, Any] | None = None):
        self.retriever = retriever
        self.config = config or {}

    async def search(
        self,
        *,
        branches: list[RecallQueryBranch],
        memory_space_id: str,
        final_k: int,
        context_session_id: str | None = None,
        visible_message_start_index: int | None = None,
        visible_message_end_index: int | None = None,
        current_actor_ids: set[str] | None = None,
    ) -> TopicRecallOutcome:
        if not branches or not memory_space_id or final_k <= 0:
            return TopicRecallOutcome([], [], 0.0)
        multiplier = max(1, min(10, int(self.config.get("recall_candidate_multiplier", 4))))
        candidate_k = min(50, max(final_k, final_k * multiplier))
        primary_branch = self._primary_branch(branches)
        query_affect = extract_query_affect(primary_branch.text)
        query_vectors = await self.retriever._get_embeddings(
            [branch.text for branch in branches]
        )
        branch_results = await asyncio.gather(
            *(
                self.retriever.search(
                    branch.text,
                    memory_space_id=memory_space_id,
                    k=candidate_k,
                    query_vector=query_vector,
                    use_rerank=False,
                )
                for branch, query_vector in zip(
                    branches, query_vectors, strict=True
                )
            )
        )
        candidate_map: dict[str, TopicRecallResult] = {}
        for branch, results in zip(branches, branch_results, strict=True):
            for result in results:
                candidate = candidate_map.get(result.topic_uid)
                if candidate is None or branch.name == primary_branch.name:
                    previous_scores = (
                        dict(candidate.branch_scores) if candidate is not None else {}
                    )
                    candidate = result
                    candidate.branch_scores = previous_scores
                    candidate_map[result.topic_uid] = candidate
                candidate.branch_scores[branch.name] = result.relevance_score

        for candidate in candidate_map.values():
            current_relevance = float(
                candidate.branch_scores.get(primary_branch.name, 0.0)
            )
            candidate.current_relevance = current_relevance
            candidate.relevance_score = current_relevance
            candidate.base_relevance_score = current_relevance
            candidate.context_support = self._bounded_context_support(
                candidate.branch_scores,
                branches,
                primary_branch.name,
            )
            available_actor_ids = {
                str(actor.get("actor_id") or "")
                for actor in candidate.actors
                if str(actor.get("actor_id") or "")
                and str(actor.get("resolution_status") or "") != "unresolved"
            }
            candidate.matched_actor_ids = sorted(
                available_actor_ids & set(current_actor_ids or set())
            )
            if candidate.matched_actor_ids:
                candidate.actor_match_boost = max(
                    0.0,
                    min(
                        0.2,
                        float(self.config.get("recall_actor_match_boost", 0.04)),
                    ),
                )
            if bool(self.config.get("recall_affect_enabled", True)):
                candidate.affect_match_score = affect_similarity(
                    query_affect,
                    candidate.topic.affect_profile,
                )
                candidate.affect_match_boost = self._affect_boost(
                    candidate.affect_match_score,
                    query_affect,
                )
                candidate.selected_affect_events = select_affect_events(
                    candidate.topic.affect_profile,
                    query_affect,
                    limit=int(self.config.get("recall_affect_event_limit", 1)),
                    min_confidence=float(
                        self.config.get("recall_affect_min_confidence", 0.65)
                    ),
                )
            candidate.ranking_score = self._topic_ranking_score(candidate)
            candidate.final_score = candidate.ranking_score

        candidates = sorted(
            candidate_map.values(), key=lambda item: item.final_score, reverse=True
        )
        overlap_threshold = max(
            0.0,
            min(
                1.0,
                float(self.config.get("recall_context_overlap_threshold", 0.8)),
            )
        )
        context_suppressed = 0
        visible: list[TopicRecallResult] = []
        for candidate in candidates:
            if candidate.current_relevance is None or candidate.current_relevance <= 0:
                candidate.filter_reason = "missing_current_query_match"
                continue
            coverage = (
                self._context_coverage(
                    candidate.sources,
                    session_id=context_session_id,
                    visible_start=visible_message_start_index,
                    visible_end=visible_message_end_index,
                )
                if overlap_threshold > 0
                else 0.0
            )
            candidate.context_coverage = coverage
            if coverage >= overlap_threshold and overlap_threshold > 0:
                context_suppressed += 1
                candidate.filter_reason = "current_context_overlap"
                continue
            if coverage > 0:
                candidate.current_relevance *= 1.0 - 0.25 * coverage
                candidate.relevance_score = candidate.current_relevance
                candidate.ranking_score = self._topic_ranking_score(candidate)
                candidate.final_score = candidate.ranking_score
            visible.append(candidate)
        visible.sort(key=lambda item: item.final_score, reverse=True)

        minimum = max(
            0.0, min(1.0, float(self.config.get("recall_min_relevance", 0.32)))
        )
        relative = max(
            0.0, min(1.0, float(self.config.get("recall_relative_floor", 0.7)))
        )
        best = max((item.current_relevance or 0.0 for item in visible), default=0.0)
        threshold = max(minimum, best * relative)
        eligible: list[TopicRecallResult] = []
        for item in visible:
            current_relevance = float(item.current_relevance or 0.0)
            if current_relevance < minimum:
                item.filter_reason = "below_min_relevance"
                continue
            if current_relevance < threshold:
                item.filter_reason = "below_relative_floor"
                continue
            eligible.append(item)

        selection_threshold = self._selection_threshold(
            eligible,
            threshold,
        )
        selection_pool: list[TopicRecallResult] = []
        for item in eligible:
            if float(item.current_relevance or 0.0) < selection_threshold:
                item.filter_reason = "below_selection_floor"
                continue
            selection_pool.append(item)

        if (
            selection_pool
            and bool(self.config.get("recall_use_rerank", True))
            and self.retriever._rerank_weight() > 0.0
            and self.retriever.rerank_provider is not None
        ):
            try:
                rerank_confidence = await self.retriever.assign_rerank_ranks(
                    primary_branch.text,
                    selection_pool,
                )
                for item in selection_pool:
                    if item.rerank_percentile is not None:
                        item.rerank_rank_boost = self.retriever._rerank_rank_boost(
                            item.rerank_percentile,
                            rerank_confidence,
                        )
                    item.ranking_score = self._topic_ranking_score(item)
                    item.final_score = item.ranking_score
            except Exception:
                logger.warning(
                    "[TopicRecall] Topic Rerank 失败，保留当前消息基础排序",
                    exc_info=True,
                )
        selection_pool.sort(key=lambda item: item.final_score, reverse=True)
        selected = self._select_mmr(
            selection_pool,
            final_k,
            max(0.0, min(1.0, float(self.config.get("recall_mmr_lambda", 0.78)))),
        )
        selected_uids = {item.topic_uid for item in selected}
        for item in selection_pool:
            item.selected = item.topic_uid in selected_uids
            item.filter_reason = (
                None if item.selected else "diversity_or_result_limit"
            )
        candidates.sort(key=lambda item: item.final_score, reverse=True)
        return TopicRecallOutcome(
            selected,
            candidates,
            threshold,
            context_suppressed=context_suppressed,
            selection_threshold=selection_threshold,
            query_vectors=query_vectors,
        )

    async def search_spaces(
        self,
        *,
        branches: list[RecallQueryBranch],
        memory_space_ids: list[str],
        final_k: int,
        context_session_id: str | None = None,
        visible_message_start_index: int | None = None,
        visible_message_end_index: int | None = None,
        current_actor_ids: set[str] | None = None,
    ) -> TopicRecallOutcome:
        """Search an explicit canonical/alias space group and merge by Topic UID."""
        spaces = list(dict.fromkeys(item for item in memory_space_ids if item))
        if not spaces:
            return TopicRecallOutcome([], [], 0.0)
        if len(spaces) == 1:
            return await self.search(
                branches=branches,
                memory_space_id=spaces[0],
                final_k=final_k,
                context_session_id=context_session_id,
                visible_message_start_index=visible_message_start_index,
                visible_message_end_index=visible_message_end_index,
                current_actor_ids=current_actor_ids,
            )
        outcomes = await asyncio.gather(
            *(
                self.search(
                    branches=branches,
                    memory_space_id=memory_space_id,
                    final_k=final_k,
                    context_session_id=context_session_id,
                    visible_message_start_index=visible_message_start_index,
                    visible_message_end_index=visible_message_end_index,
                    current_actor_ids=current_actor_ids,
                )
                for memory_space_id in spaces
            )
        )
        candidates: dict[str, TopicRecallResult] = {}
        for outcome in outcomes:
            for candidate in outcome.candidates:
                previous = candidates.get(candidate.topic_uid)
                if previous is None or candidate.final_score > previous.final_score:
                    candidates[candidate.topic_uid] = candidate
        ordered = sorted(candidates.values(), key=lambda item: item.final_score, reverse=True)
        selected = ordered[:final_k]
        selected_uids = {item.topic_uid for item in selected}
        for candidate in ordered:
            candidate.selected = candidate.topic_uid in selected_uids
            if not candidate.selected and not candidate.filter_reason:
                candidate.filter_reason = "diversity_or_result_limit"
        return TopicRecallOutcome(
            selected,
            ordered,
            min((item.applied_threshold for item in outcomes), default=0.0),
            context_suppressed=sum(item.context_suppressed for item in outcomes),
            selection_threshold=min(
                (item.selection_threshold for item in outcomes), default=0.0
            ),
            query_vectors=outcomes[0].query_vectors if outcomes else None,
        )

    async def record_topic_access(
        self, topic_results: list[TopicRecallResult]
    ) -> int:
        """Record access only after the caller confirms results were consumed."""
        topic_uids = list(
            dict.fromkeys(item.topic_uid for item in topic_results if item.topic_uid)
        )
        if not topic_uids:
            return 0
        return await self.retriever.store.record_topic_access(topic_uids)

    async def source_timeline_document_ids(
        self,
        topic_results: list[TopicRecallResult],
        fragment_results: list[TopicFragmentRecallResult],
        *,
        limit_per_result: int = 3,
    ) -> list[int]:
        """Resolve a bounded set of source Timelines for consumed Topic output."""
        limit = max(1, min(int(limit_per_result), 10))
        timeline_uids: list[str] = []
        for result in fragment_results:
            timeline_uids.extend(
                str(uid) for uid in result.fragment.timeline_uids[:limit] if str(uid)
            )
        for result in topic_results:
            ordered = sorted(
                result.sources,
                key=lambda item: (
                    float(item.get("contribution_weight") or 0.0),
                    float(item.get("semantic_similarity") or 0.0),
                ),
                reverse=True,
            )
            timeline_uids.extend(
                str(item.get("timeline_uid") or "")
                for item in ordered[:limit]
                if item.get("timeline_uid")
            )
        return await self.retriever.store.timeline_document_ids(
            list(dict.fromkeys(timeline_uids))
        )

    def _topic_ranking_score(self, candidate: TopicRecallResult) -> float:
        current = float(candidate.current_relevance or 0.0)
        base = self.retriever._rank_score(candidate.topic, current)
        return min(
            1.0,
            base
            + candidate.context_support
            + candidate.actor_match_boost
            + candidate.affect_match_boost
            + candidate.rerank_rank_boost,
        )

    def _affect_boost(
        self,
        similarity: float,
        query_affect: dict[str, Any],
    ) -> float:
        if not query_affect.get("explicit"):
            return 0.0
        cap = max(
            0.0,
            min(0.12, float(self.config.get("recall_affect_boost_cap", 0.04))),
        )
        return cap * max(0.0, min(1.0, similarity)) * max(
            0.0, min(1.0, float(query_affect.get("confidence") or 0.0))
        )

    def _bounded_context_support(
        self,
        branch_scores: dict[str, float],
        branches: list[RecallQueryBranch],
        primary_name: str,
    ) -> float:
        miss_probability = 1.0
        has_context = False
        for branch in branches:
            if branch.name == primary_name:
                continue
            score = branch_scores.get(branch.name)
            if score is None:
                continue
            has_context = True
            miss_probability *= 1.0 - min(
                1.0,
                max(0.0, float(branch.weight)) * max(0.0, float(score)),
            )
        if not has_context:
            return 0.0
        cap = max(
            0.0,
            min(
                0.25,
                float(self.config.get("recall_context_support_cap", 0.08)),
            ),
        )
        return cap * (1.0 - miss_probability)

    def _selection_threshold(
        self,
        candidates: list[TopicRecallResult | TopicFragmentRecallResult],
        eligibility_threshold: float,
    ) -> float:
        relative = max(
            0.0,
            min(
                1.0,
                float(self.config.get("recall_selection_relative_floor", 0.90)),
            ),
        )
        best = max(
            (float(item.current_relevance or 0.0) for item in candidates),
            default=0.0,
        )
        return max(eligibility_threshold, best * relative)

    @staticmethod
    def _primary_branch(branches: list[RecallQueryBranch]) -> RecallQueryBranch:
        return next(
            (branch for branch in branches if branch.name == "current"),
            max(branches, key=lambda branch: branch.weight),
        )

    async def search_fragment_supplements(
        self,
        *,
        branches: list[RecallQueryBranch],
        topic_results: list[TopicRecallResult],
        limit: int,
        context_session_id: str | None = None,
        visible_message_start_index: int | None = None,
        visible_message_end_index: int | None = None,
        query_vectors: list[list[float]] | None = None,
    ) -> TopicFragmentRecallOutcome:
        """Recall formal role-anchored fragments owned by selected Topics."""
        if not topic_results or limit <= 0:
            return TopicFragmentRecallOutcome([], [], 0, 0.0)
        rows = await self.retriever.store.list_active_fragments_for_topics(
            [item.topic_uid for item in topic_results]
        )
        safe_rows = [
            row
            for row in rows
            if row["fragment"].metadata.get("narrative_schema_version")
            in {
                "first_person_assistant_roles_affect_v4",
                "first_person_assistant_roles_v3",
                "first_person_assistant_roles_v2",
                "third_person_roles_v1",
            }
        ]
        available_count = len(safe_rows)
        if not safe_rows or not branches:
            return TopicFragmentRecallOutcome([], [], available_count, 0.0)

        primary_branch = self._primary_branch(branches)
        query_affect = extract_query_affect(primary_branch.text)
        if query_vectors is None or len(query_vectors) != len(branches):
            query_vectors = await self.retriever._get_embeddings(
                [branch.text for branch in branches]
            )
        if query_vectors:
            self.retriever.validate_fragment_embeddings(
                [row["fragment"] for row in safe_rows],
                query_vectors[0],
            )
        parents = {item.topic_uid: item for item in topic_results}
        fragment_counts: dict[str, int] = {}
        for row in safe_rows:
            topic_uid = str(row["topic_uid"])
            fragment_counts[topic_uid] = fragment_counts.get(topic_uid, 0) + 1
        parent_scores = {
            item.topic_uid: float(
                item.current_relevance
                if item.current_relevance is not None
                else item.relevance_score
            )
            for item in topic_results
        }
        candidates_by_uid: dict[str, TopicFragmentRecallResult] = {}
        for row in safe_rows:
            fragment = row["fragment"]
            if not fragment.embedding:
                continue
            fragment_text = "\n".join(
                [
                    fragment.label,
                    fragment.summary,
                    " ".join(
                        str(fact.get("content") or "")
                        for fact in fragment.facts
                    ),
                    " ".join(fragment.keywords),
                ]
            )
            features = self.retriever._text_features(fragment_text)
            branch_scores: dict[str, float] = {}
            primary_embedding = 0.0
            primary_keyword = 0.0
            for branch, query_vector in zip(
                branches, query_vectors, strict=True
            ):
                embedding = max(
                    0.0,
                    self.retriever._cosine(query_vector, fragment.embedding),
                )
                query_features = self.retriever._text_features(branch.text)
                keyword = len(query_features & features) / max(
                    1, len(query_features)
                )
                branch_relevance = min(1.0, embedding * 0.85 + keyword * 0.15)
                branch_scores[branch.name] = branch_relevance
                if branch.name == primary_branch.name:
                    primary_embedding = embedding
                    primary_keyword = keyword
            own_relevance = branch_scores.get(primary_branch.name, 0.0)
            parent_relevance = parent_scores.get(str(row["topic_uid"]), 0.0)
            current_relevance = min(
                1.0, own_relevance * 0.78 + parent_relevance * 0.22
            )
            candidate = TopicFragmentRecallResult(
                fragment=fragment,
                topic_uid=str(row["topic_uid"]),
                relevance_score=current_relevance,
                final_score=0.0,
                embedding_score=primary_embedding,
                keyword_score=primary_keyword,
                parent_topic_relevance=parent_relevance,
                sources=list(row.get("sources") or []),
                branch_scores=branch_scores,
                current_relevance=current_relevance,
            )
            candidate.context_support = self._bounded_context_support(
                branch_scores,
                branches,
                primary_branch.name,
            )
            if bool(self.config.get("recall_affect_enabled", True)):
                candidate.affect_match_score = affect_similarity(
                    query_affect,
                    fragment.affect_events,
                )
                candidate.affect_match_boost = self._affect_boost(
                    candidate.affect_match_score,
                    query_affect,
                )
                candidate.selected_affect_events = select_affect_events(
                    fragment.affect_events,
                    query_affect,
                    limit=int(self.config.get("recall_affect_event_limit", 1)),
                    min_confidence=float(
                        self.config.get("recall_affect_min_confidence", 0.65)
                    ),
                )
            candidate.ranking_score = self._fragment_ranking_score(candidate)
            candidate.final_score = candidate.ranking_score
            parent = parents.get(candidate.topic_uid)
            if parent is not None and self._fragment_body_duplicates_parent(
                candidate,
                parent,
                fragment_counts.get(candidate.topic_uid, 0),
            ):
                candidate.body_suppressed = True
                if not candidate.fact_contents:
                    candidate.filter_reason = "duplicate_parent_without_facts"
            previous = candidates_by_uid.get(fragment.fragment_uid)
            if previous is None or candidate.final_score > previous.final_score:
                candidates_by_uid[fragment.fragment_uid] = candidate

        candidates = sorted(
            candidates_by_uid.values(),
            key=lambda item: item.final_score,
            reverse=True,
        )
        overlap_threshold = max(
            0.0,
            min(1.0, float(self.config.get("recall_context_overlap_threshold", 0.8))),
        )
        visible: list[TopicFragmentRecallResult] = []
        context_suppressed = 0
        duplicate_parent_count = 0
        for candidate in candidates:
            if candidate.body_suppressed:
                duplicate_parent_count += 1
            if candidate.filter_reason == "duplicate_parent_without_facts":
                continue
            candidate.context_coverage = self._context_coverage(
                candidate.sources,
                session_id=context_session_id,
                visible_start=visible_message_start_index,
                visible_end=visible_message_end_index,
            )
            if (
                overlap_threshold > 0.0
                and candidate.context_coverage >= overlap_threshold
            ):
                context_suppressed += 1
                candidate.filter_reason = "current_context_overlap"
                continue
            visible.append(candidate)
        visible.sort(key=lambda item: item.final_score, reverse=True)
        minimum = max(
            0.0,
            min(1.0, float(self.config.get("fragment_min_relevance", 0.28))),
        )
        relative = max(
            0.0,
            min(1.0, float(self.config.get("fragment_relative_floor", 0.65))),
        )
        best = max((item.current_relevance or 0.0 for item in visible), default=0.0)
        threshold = max(minimum, best * relative)
        eligible: list[TopicFragmentRecallResult] = []
        for item in visible:
            current_relevance = float(item.current_relevance or 0.0)
            if current_relevance < minimum:
                item.filter_reason = "below_min_relevance"
                continue
            if current_relevance < threshold:
                item.filter_reason = "below_relative_floor"
                continue
            eligible.append(item)

        selection_threshold = self._selection_threshold(
            eligible,
            threshold,
        )
        selection_pool: list[TopicFragmentRecallResult] = []
        for item in eligible:
            if float(item.current_relevance or 0.0) < selection_threshold:
                item.filter_reason = "below_selection_floor"
                continue
            selection_pool.append(item)

        if (
            selection_pool
            and bool(self.config.get("recall_use_rerank", True))
            and self.retriever._rerank_weight() > 0.0
            and self.retriever.rerank_provider is not None
        ):
            try:
                rerank_confidence = await self.retriever.assign_rerank_ranks(
                    primary_branch.text,
                    selection_pool,
                )
                for item in selection_pool:
                    if item.rerank_percentile is not None:
                        item.rerank_rank_boost = self.retriever._rerank_rank_boost(
                            item.rerank_percentile,
                            rerank_confidence,
                        )
                    item.ranking_score = self._fragment_ranking_score(item)
                    item.final_score = item.ranking_score
            except Exception:
                logger.warning(
                    "[TopicRecall] 片段 Rerank 失败，保留当前消息基础排序",
                    exc_info=True,
                )
        selection_pool.sort(key=lambda item: item.final_score, reverse=True)
        selected = self._select_fragment_mmr(
            selection_pool,
            limit,
            max(
                0.0,
                min(1.0, float(self.config.get("recall_mmr_lambda", 0.78))),
            ),
        )
        selected_uids = {item.fragment_uid for item in selected}
        for item in selection_pool:
            item.selected = item.fragment_uid in selected_uids
            item.filter_reason = (
                None if item.selected else "diversity_or_result_limit"
            )
        selected_event_uids_by_topic: dict[str, set[str]] = {}
        for item in selected:
            selected_event_uids_by_topic.setdefault(item.topic_uid, set()).update(
                str(event.get("event_uid") or "")
                for event in item.selected_affect_events
                if str(event.get("event_uid") or "")
            )
        for parent in topic_results:
            duplicate_event_uids = selected_event_uids_by_topic.get(
                parent.topic_uid, set()
            )
            if duplicate_event_uids:
                parent.selected_affect_events = [
                    event
                    for event in parent.selected_affect_events
                    if str(event.get("event_uid") or "") not in duplicate_event_uids
                ]
        candidates.sort(key=lambda item: item.final_score, reverse=True)
        return TopicFragmentRecallOutcome(
            selected,
            candidates,
            available_count,
            threshold,
            context_suppressed=context_suppressed,
            selection_threshold=selection_threshold,
            duplicate_parent_count=duplicate_parent_count,
        )

    @staticmethod
    def _fragment_body_duplicates_parent(
        candidate: TopicFragmentRecallResult,
        parent: TopicRecallResult,
        sibling_count: int,
    ) -> bool:
        fragment_summary = RecallPipeline._text_features(candidate.fragment.summary)
        parent_summary = RecallPipeline._text_features(parent.topic.summary)
        summary_similarity = RecallPipeline._jaccard(
            fragment_summary,
            parent_summary,
        )
        if sibling_count == 1 and summary_similarity >= 0.82:
            return True
        fragment_content = RecallPipeline._text_features(candidate.body_content)
        parent_content = RecallPipeline._text_features(parent.content)
        return RecallPipeline._jaccard(fragment_content, parent_content) >= 0.90

    @staticmethod
    def _fragment_ranking_score(candidate: TopicFragmentRecallResult) -> float:
        current = float(candidate.current_relevance or 0.0)
        return min(
            1.0,
            current * 0.90
            + float(candidate.fragment.importance) * 0.06
            + float(candidate.fragment.confidence) * 0.04
            + candidate.context_support
            + candidate.affect_match_boost
            + candidate.rerank_rank_boost,
        )

    @staticmethod
    def _select_fragment_mmr(
        candidates: list[TopicFragmentRecallResult],
        limit: int,
        mmr_lambda: float,
    ) -> list[TopicFragmentRecallResult]:
        remaining = list(candidates)
        selected: list[TopicFragmentRecallResult] = []
        features = {
            item.fragment_uid: RecallPipeline._text_features(item.content)
            for item in remaining
        }
        while remaining and len(selected) < limit:
            if not selected:
                selected.append(remaining.pop(0))
                continue
            index = max(
                range(len(remaining)),
                key=lambda value: (
                    mmr_lambda * remaining[value].final_score
                    - (1.0 - mmr_lambda)
                    * max(
                        RecallPipeline._jaccard(
                            features[remaining[value].fragment_uid],
                            features[item.fragment_uid],
                        )
                        for item in selected
                    )
                ),
            )
            selected.append(remaining.pop(index))
        return selected

    @staticmethod
    def _select_mmr(
        candidates: list[TopicRecallResult],
        limit: int,
        mmr_lambda: float,
    ) -> list[TopicRecallResult]:
        remaining = list(candidates)
        selected: list[TopicRecallResult] = []
        features = {
            item.topic_uid: RecallPipeline._text_features(item.content)
            for item in remaining
        }
        while remaining and len(selected) < limit:
            if not selected:
                selected.append(remaining.pop(0))
                continue
            best_index = max(
                range(len(remaining)),
                key=lambda index: (
                    mmr_lambda * remaining[index].final_score
                    - (1.0 - mmr_lambda)
                    * max(
                        RecallPipeline._jaccard(
                            features[remaining[index].topic_uid],
                            features[item.topic_uid],
                        )
                        for item in selected
                    )
                ),
            )
            selected.append(remaining.pop(best_index))
        return selected

    @staticmethod
    def select_timeline_supplements(
        timeline_results,
        topic_results: list[TopicRecallResult],
        limit: int,
    ) -> list:
        if limit <= 0:
            return []
        linked_uids = {
            str(source.get("timeline_uid") or "")
            for topic in topic_results
            for source in topic.sources
        }
        topic_features = [
            RecallPipeline._text_features(topic.content) for topic in topic_results
        ]

        def rank(item):
            metadata = item.metadata if isinstance(item.metadata, dict) else {}
            linked = str(metadata.get("memory_uid") or "") in linked_uids
            return (1 if linked else 0, float(item.final_score))

        selected = []
        for item in sorted(timeline_results, key=rank, reverse=True):
            features = RecallPipeline._text_features(item.content)
            if topic_features and max(
                RecallPipeline._jaccard(features, value)
                for value in topic_features
            ) >= 0.82:
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected

    @staticmethod
    def _context_coverage(
        sources: list[dict[str, Any]],
        *,
        session_id: str | None,
        visible_start: int | None,
        visible_end: int | None,
    ) -> float:
        if (
            not session_id
            or visible_start is None
            or visible_end is None
            or visible_end <= visible_start
            or not sources
        ):
            return 0.0
        overlapping = 0
        for source in sources:
            if source.get("session_id") != session_id:
                continue
            try:
                start = int(source.get("start_index"))
                end = int(source.get("end_index"))
            except (TypeError, ValueError):
                continue
            if start < visible_end and end > visible_start:
                overlapping += 1
        return overlapping / max(1, len(sources))


__all__ = [
    "TopicFragmentRecallOutcome",
    "TopicRecallOutcome",
    "TopicRecallPipeline",
]
