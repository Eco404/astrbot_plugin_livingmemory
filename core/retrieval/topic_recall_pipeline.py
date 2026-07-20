"""Weighted multi-branch Topic recall with provenance-aware filtering."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

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

    def diagnostics(self) -> dict[str, Any]:
        selected = {item.fragment_uid for item in self.results}
        return {
            "available_count": self.available_count,
            "candidate_count": len(self.candidates),
            "selected_count": len(self.results),
            "applied_threshold": round(self.applied_threshold, 6),
            "context_suppressed": self.context_suppressed,
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

    def diagnostics(self) -> dict[str, Any]:
        selected_uids = {item.topic_uid for item in self.results}
        return {
            "candidate_count": len(self.candidates),
            "selected_count": len(self.results),
            "applied_threshold": round(self.applied_threshold, 6),
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
        track_access: bool = True,
    ) -> TopicRecallOutcome:
        if not branches or not memory_space_id or final_k <= 0:
            return TopicRecallOutcome([], [], 0.0)
        multiplier = max(1, min(10, int(self.config.get("recall_candidate_multiplier", 4))))
        candidate_k = min(50, max(final_k, final_k * multiplier))
        scan_limit = max(
            100, min(5000, int(self.config.get("recall_scan_limit", 2000)))
        )
        payloads = await self.retriever.store.list_topic_recall_payloads(
            memory_space_id,
            limit=scan_limit,
        )
        if not payloads:
            return TopicRecallOutcome([], [], 0.0)
        query_vectors = await self.retriever._get_embeddings(
            [branch.text for branch in branches]
        )
        branch_results = await asyncio.gather(
            *(
                self.retriever.search(
                    branch.text,
                    memory_space_id=memory_space_id,
                    k=candidate_k,
                    payloads=payloads,
                    query_vector=query_vector,
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
                if candidate is None:
                    candidate = result
                    candidate.branch_scores = {}
                    candidate_map[result.topic_uid] = candidate
                candidate.branch_scores[branch.name] = result.relevance_score

        for candidate in candidate_map.values():
            miss_probability = 1.0
            for branch in branches:
                branch_score = candidate.branch_scores.get(branch.name)
                if branch_score is not None:
                    miss_probability *= 1.0 - min(1.0, branch.weight * branch_score)
            candidate.relevance_score = 1.0 - miss_probability
            candidate.final_score = self.retriever._rank_score(
                candidate.topic, candidate.relevance_score
            )

        candidates = sorted(
            candidate_map.values(), key=lambda item: item.final_score, reverse=True
        )
        overlap_threshold = max(
            0.0,
            min(1.0, float(self.config.get("recall_context_overlap_threshold", 0.8))),
        )
        context_suppressed = 0
        visible: list[TopicRecallResult] = []
        for candidate in candidates:
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
                continue
            if coverage > 0:
                candidate.relevance_score *= 1.0 - 0.25 * coverage
                candidate.final_score = self.retriever._rank_score(
                    candidate.topic, candidate.relevance_score
                )
            visible.append(candidate)
        visible.sort(key=lambda item: item.final_score, reverse=True)

        minimum = max(
            0.0, min(1.0, float(self.config.get("recall_min_relevance", 0.32)))
        )
        relative = max(
            0.0, min(1.0, float(self.config.get("recall_relative_floor", 0.7)))
        )
        best = max((item.relevance_score for item in visible), default=0.0)
        threshold = max(minimum, best * relative)
        eligible = [
            item for item in visible if item.relevance_score >= threshold
        ]
        selected = self._select_mmr(
            eligible,
            final_k,
            max(0.0, min(1.0, float(self.config.get("recall_mmr_lambda", 0.78)))),
        )
        if track_access and selected:
            await self.retriever.store.record_topic_access(
                [item.topic_uid for item in selected]
            )
        return TopicRecallOutcome(
            selected,
            candidates,
            threshold,
            context_suppressed=context_suppressed,
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
    ) -> TopicFragmentRecallOutcome:
        """Recall formal third-person fragments owned by the selected Topics."""
        if not topic_results or limit <= 0:
            return TopicFragmentRecallOutcome([], [], 0, 0.0)
        rows = await self.retriever.store.list_active_fragments_for_topics(
            [item.topic_uid for item in topic_results]
        )
        safe_rows = [
            row
            for row in rows
            if row["fragment"].metadata.get("narrative_schema_version")
            == "third_person_roles_v1"
        ]
        available_count = len(safe_rows)
        if not safe_rows or not branches:
            return TopicFragmentRecallOutcome([], [], available_count, 0.0)

        query_vectors = await self.retriever._get_embeddings(
            [branch.text for branch in branches]
        )
        parent_scores = {
            item.topic_uid: item.relevance_score for item in topic_results
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
            miss_probability = 1.0
            best_embedding = 0.0
            best_keyword = 0.0
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
                miss_probability *= 1.0 - min(
                    1.0, branch.weight * branch_relevance
                )
                best_embedding = max(best_embedding, embedding)
                best_keyword = max(best_keyword, keyword)
            own_relevance = 1.0 - miss_probability
            parent_relevance = parent_scores.get(str(row["topic_uid"]), 0.0)
            relevance = min(
                1.0, own_relevance * 0.78 + parent_relevance * 0.22
            )
            final_score = min(
                1.0,
                relevance * 0.90
                + float(fragment.importance) * 0.06
                + float(fragment.confidence) * 0.04,
            )
            candidate = TopicFragmentRecallResult(
                fragment=fragment,
                topic_uid=str(row["topic_uid"]),
                relevance_score=relevance,
                final_score=final_score,
                embedding_score=best_embedding,
                keyword_score=best_keyword,
                parent_topic_relevance=parent_relevance,
                sources=list(row.get("sources") or []),
            )
            previous = candidates_by_uid.get(fragment.fragment_uid)
            if previous is None or candidate.final_score > previous.final_score:
                candidates_by_uid[fragment.fragment_uid] = candidate

        candidates = sorted(
            candidates_by_uid.values(),
            key=lambda item: item.final_score,
            reverse=True,
        )
        rerank_enabled = bool(self.config.get("recall_use_rerank", True))
        rerank_weight = max(
            0.0,
            min(1.0, float(self.config.get("recall_rerank_weight", 0.35))),
        )
        if (
            candidates
            and rerank_enabled
            and rerank_weight > 0.0
            and self.retriever.rerank_provider is not None
        ):
            query = max(branches, key=lambda item: item.weight).text
            try:
                documents = [item.content for item in candidates]
                reranked = await self.retriever.rerank_provider.rerank(
                    query, documents, top_n=len(documents)
                )
                rerank_map = {
                    int(getattr(item, "index", -1)): float(
                        getattr(item, "relevance_score", 0.0)
                    )
                    for item in reranked
                }
                for index, candidate in enumerate(candidates):
                    score = rerank_map.get(index)
                    if score is None or not math.isfinite(score):
                        continue
                    score = max(0.0, min(1.0, score))
                    candidate.rerank_score = score
                    candidate.relevance_score = (
                        candidate.relevance_score * (1.0 - rerank_weight)
                        + score * rerank_weight
                    )
                    candidate.final_score = min(
                        1.0,
                        candidate.relevance_score * 0.90
                        + candidate.fragment.importance * 0.06
                        + candidate.fragment.confidence * 0.04,
                    )
            except Exception:
                logger.warning(
                    "[TopicRecall] 片段 Rerank 失败，保留向量与关键词排序",
                    exc_info=True,
                )

        overlap_threshold = max(
            0.0,
            min(1.0, float(self.config.get("recall_context_overlap_threshold", 0.8))),
        )
        visible: list[TopicFragmentRecallResult] = []
        context_suppressed = 0
        for candidate in candidates:
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
        best = max((item.relevance_score for item in visible), default=0.0)
        threshold = max(minimum, best * relative)
        eligible = [
            item for item in visible if item.relevance_score >= threshold
        ]
        selected = self._select_fragment_mmr(
            eligible,
            limit,
            max(
                0.0,
                min(1.0, float(self.config.get("recall_mmr_lambda", 0.78))),
            ),
        )
        return TopicFragmentRecallOutcome(
            selected,
            candidates,
            available_count,
            threshold,
            context_suppressed=context_suppressed,
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
