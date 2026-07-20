"""Weighted multi-branch Topic recall with provenance-aware filtering."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .recall_pipeline import RecallPipeline, RecallQueryBranch
from .topic_retriever import TopicRecallResult, TopicRetriever


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


__all__ = ["TopicRecallOutcome", "TopicRecallPipeline"]
