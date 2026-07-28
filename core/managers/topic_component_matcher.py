"""Topic fragment clustering, existing-topic matching, and relation derivation."""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from collections import Counter
from collections.abc import Iterable
from typing import (
    Any,
)

from astrbot.api import logger

from ..embedding_signature import SUPPORTED_TOPIC_EMBEDDING_FORMATS
from ..models.topic_memory import (
    TimelineTopicCandidate,
    TopicFragmentDraft,
    TopicMemory,
    TopicRelation,
)
from ..topic_similarity import lexical_tokens, weighted_jaccard_similarity
from .topic_build_contracts import (
    _RELATION_ALGORITHM_VERSION,
)
from .topic_relation_builder import vector_neighbor_rankings


class TopicComponentMatcherMixin:
    async def _match_fragments(
        self,
        fragments: list[TopicFragmentDraft],
        progress_callback=None,
    ) -> tuple[list[list[int]], dict[str, float]]:
        fragment_count = len(fragments)
        rerank_enabled = self.rerank_provider is not None and fragment_count > 1
        work_total = fragment_count * (2 if rerank_enabled else 1)

        threshold = float(self.config.get("fragment_similarity_threshold", 0.78))
        rerank_candidate_floor = float(self.config.get("rerank_candidate_floor", 0.63))
        component_min_pair = float(
            self.config.get("component_min_pair_similarity", 0.52)
        )
        component_cohesion = float(
            self.config.get("component_min_average_similarity", 0.65)
        )
        component_size_cohesion_penalty = float(
            self.config.get("component_size_cohesion_penalty", 0.005)
        )
        scores: dict[str, float] = {}
        embedding_scores: dict[tuple[int, int], float] = {}
        for left in range(len(fragments)):
            for right in range(left + 1, len(fragments)):
                cosine = self._cosine(
                    fragments[left].embedding, fragments[right].embedding
                )
                label_bonus = (
                    0.08
                    if self._norm(fragments[left].label)
                    == self._norm(fragments[right].label)
                    else 0.0
                )
                score = min(1.0, cosine + label_bonus)
                key = f"{fragments[left].fragment_uid}|{fragments[right].fragment_uid}"
                scores[key] = round(score, 6)
                embedding_scores[(left, right)] = score
            await self._emit(
                progress_callback,
                fragments[left].run_uid,
                "fragment_matching",
                left + 1,
                work_total,
            )

        rerank_passes: set[tuple[int, int]] = set()
        rerank_relevance: dict[tuple[int, int], float] = {}
        rerank_relative_ranks: dict[tuple[int, int], float] = {}
        rerank_failed = False
        if rerank_enabled:
            rerank_threshold = float(self.config.get("rerank_threshold", 0.55))
            top_n = max(1, int(self.config.get("rerank_top_n", 5)))
            documents = [self._fragment_embedding_text(item) for item in fragments]
            rerank_inputs: list[tuple[int, list[int]]] = []
            for index in range(fragment_count):
                candidate_indexes = sorted(
                    (
                        other
                        for other in range(fragment_count)
                        if other != index
                        and embedding_scores[tuple(sorted((index, other)))]
                        >= rerank_candidate_floor
                    ),
                    key=lambda other: (
                        -embedding_scores[tuple(sorted((index, other)))],
                        fragments[other].fragment_uid,
                    ),
                )[: max(top_n * 2, top_n)]
                rerank_inputs.append((index, candidate_indexes))

            progress_lock = asyncio.Lock()
            completed_queries = 0
            active_queries = 0

            async def emit_rerank_progress(
                *,
                active_delta: int = 0,
                completed_delta: int = 0,
                item_index: int,
            ) -> None:
                nonlocal active_queries, completed_queries
                async with progress_lock:
                    active_queries += active_delta
                    completed_queries += completed_delta
                    await self._emit(
                        progress_callback,
                        fragments[item_index].run_uid,
                        "fragment_matching",
                        fragment_count + completed_queries,
                        work_total,
                        activity="rerank_call",
                        item_kind="rerank_query",
                        item_index=item_index + 1,
                        item_total=fragment_count,
                        rerank_call_current=completed_queries,
                        rerank_call_total=fragment_count,
                        active_rerank_count=active_queries,
                        rerank_concurrency=self.rerank_concurrency,
                    )

            async def rerank_one(
                index: int,
                candidate_indexes: list[int],
            ) -> tuple[int, list[int], list[Any]]:
                if not candidate_indexes:
                    await emit_rerank_progress(
                        completed_delta=1,
                        item_index=index,
                    )
                    return index, candidate_indexes, []
                async with self._rerank_semaphore:
                    try:
                        await emit_rerank_progress(active_delta=1, item_index=index)
                        results = await self.rerank_provider.rerank(
                            documents[index],
                            [documents[item] for item in candidate_indexes],
                            # Request the complete candidate ordering.  Some
                            # rerankers expose a saturated score distribution,
                            # so truncating here would discard the reciprocal
                            # rank evidence needed for scale-independent matching.
                            top_n=len(candidate_indexes),
                        )
                        return index, candidate_indexes, list(results)
                    finally:
                        await emit_rerank_progress(
                            active_delta=-1,
                            completed_delta=1,
                            item_index=index,
                        )

            try:
                rerank_outputs = await self._gather_cancel_on_error(
                    [
                        rerank_one(index, candidates)
                        for index, candidates in rerank_inputs
                    ]
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if not bool(self.config.get("rerank_failure_fallback", True)):
                    raise
                rerank_failed = True
                logger.warning(
                    "[TopicMemory] Rerank 调用失败，本轮回退到 Embedding 匹配",
                    exc_info=True,
                )
                rerank_outputs = []

            if not rerank_failed:
                for index, candidate_indexes, results in rerank_outputs:
                    fragment = fragments[index]
                    ranked_results: list[tuple[int, float, Any]] = []
                    seen_candidates: set[int] = set()
                    for result in results:
                        local_index = int(getattr(result, "index", -1))
                        relevance = float(getattr(result, "relevance_score", 0.0))
                        if 0 <= local_index < len(candidate_indexes):
                            other = candidate_indexes[local_index]
                            if other in seen_candidates or not math.isfinite(relevance):
                                continue
                            seen_candidates.add(other)
                            ranked_results.append((other, relevance, result))
                    ranked_results.sort(
                        key=lambda item: (
                            -item[1],
                            fragments[item[0]].fragment_uid,
                        )
                    )
                    relative_rank_scores = self._relative_rank_scores(
                        [item[1] for item in ranked_results]
                    )
                    for (rank, (other, relevance, result)), relative_rank in zip(
                        enumerate(ranked_results, start=1),
                        relative_rank_scores,
                        strict=True,
                    ):
                        key = f"rerank:{fragment.fragment_uid}|{fragments[other].fragment_uid}"
                        scores[key] = round(relevance, 6)
                        rank_key = (
                            "rerank_rank:"
                            f"{fragment.fragment_uid}|{fragments[other].fragment_uid}"
                        )
                        scores[rank_key] = float(rank)
                        relative_key = (
                            "rerank_relative:"
                            f"{fragment.fragment_uid}|{fragments[other].fragment_uid}"
                        )
                        scores[relative_key] = round(relative_rank, 6)
                        rerank_relevance[(index, other)] = relevance
                        rerank_relative_ranks[(index, other)] = relative_rank
                        raw_score = getattr(result, "raw_score", None)
                        try:
                            raw_value = float(raw_score)
                        except (TypeError, ValueError):
                            raw_value = math.nan
                        if math.isfinite(raw_value):
                            raw_key = (
                                "rerank_raw:"
                                f"{fragment.fragment_uid}|{fragments[other].fragment_uid}"
                            )
                            scores[raw_key] = round(raw_value, 6)
                        if relevance >= rerank_threshold and rank <= top_n:
                            rerank_passes.add((index, other))

        if fragments and work_total:
            await self._emit(
                progress_callback,
                fragments[0].run_uid,
                "fragment_matching",
                work_total,
                work_total,
            )

        seed_edges: list[tuple[float, int, int]] = []
        reciprocal_rank_threshold = float(
            self.config.get("rerank_reciprocal_rank_threshold", 0.60)
        )
        for (left, right), embedding_score in embedding_scores.items():
            mutual_rerank = (
                not rerank_failed
                and (left, right) in rerank_passes
                and (right, left) in rerank_passes
            )
            directed_relevance = [
                rerank_relevance.get((left, right)),
                rerank_relevance.get((right, left)),
            ]
            directed_relative_ranks = [
                rerank_relative_ranks.get((left, right)),
                rerank_relative_ranks.get((right, left)),
            ]
            reciprocal_rerank = (
                not rerank_failed
                and all(value is not None for value in directed_relevance)
                and all(
                    float(value) >= rerank_threshold
                    for value in directed_relevance
                    if value is not None
                )
                and ((left, right) in rerank_passes or (right, left) in rerank_passes)
                and all(value is not None for value in directed_relative_ranks)
                and sum(
                    float(value)
                    for value in directed_relative_ranks
                    if value is not None
                )
                / 2.0
                >= reciprocal_rank_threshold
            )
            if embedding_score >= threshold or (
                embedding_score >= rerank_candidate_floor
                and (mutual_rerank or reciprocal_rerank)
            ):
                directed_scores = [
                    float(scores[key])
                    for key in (
                        f"rerank:{fragments[left].fragment_uid}|{fragments[right].fragment_uid}",
                        f"rerank:{fragments[right].fragment_uid}|{fragments[left].fragment_uid}",
                    )
                    if key in scores
                ]
                priority = (
                    (embedding_score + min(directed_scores)) / 2.0
                    if mutual_rerank and directed_scores
                    else (
                        embedding_score
                        + sum(
                            float(value)
                            for value in directed_relative_ranks
                            if value is not None
                        )
                        / 2.0
                    )
                    / 2.0
                    if reciprocal_rerank
                    else embedding_score
                )
                seed_edges.append((priority, left, right))

        components = self._cluster_fragment_edges(
            fragment_count,
            embedding_scores,
            seed_edges,
            minimum_pair_similarity=component_min_pair,
            minimum_average_similarity=component_cohesion,
            size_cohesion_penalty=component_size_cohesion_penalty,
        )
        return components, scores

    def _matching_audit(
        self,
        fragments: list[TopicFragmentDraft],
        components: list[list[int]],
        scores: dict[str, float],
    ) -> dict[str, Any]:
        """Explain singleton outcomes without treating the final partition as truth."""
        embedding_scores: dict[frozenset[str], float] = {}
        rerank_scores: dict[tuple[str, str], float] = {}
        rerank_raw_scores: dict[tuple[str, str], float] = {}
        rerank_ranks: dict[tuple[str, str], int] = {}
        rerank_relative_ranks: dict[tuple[str, str], float] = {}
        for key, value in scores.items():
            if key.startswith("rerank_relative:"):
                pair = key[16:].split("|", 1)
                if len(pair) == 2:
                    rerank_relative_ranks[(pair[0], pair[1])] = float(value)
            elif key.startswith("rerank_rank:"):
                pair = key[12:].split("|", 1)
                if len(pair) == 2:
                    rerank_ranks[(pair[0], pair[1])] = int(value)
            elif key.startswith("rerank_raw:"):
                pair = key[11:].split("|", 1)
                if len(pair) == 2:
                    rerank_raw_scores[(pair[0], pair[1])] = float(value)
            elif key.startswith("rerank:"):
                pair = key[7:].split("|", 1)
                if len(pair) == 2:
                    rerank_scores[(pair[0], pair[1])] = float(value)
            else:
                pair = key.split("|", 1)
                if len(pair) == 2:
                    embedding_scores[frozenset(pair)] = float(value)
        rerank_floor = float(self.config.get("rerank_candidate_floor", 0.63))
        merge_threshold = float(self.config.get("fragment_similarity_threshold", 0.78))
        rerank_threshold = float(self.config.get("rerank_threshold", 0.55))
        rerank_top_n = max(1, int(self.config.get("rerank_top_n", 5)))
        reciprocal_rank_threshold = float(
            self.config.get("rerank_reciprocal_rank_threshold", 0.60)
        )
        singleton_indexes = {
            component[0] for component in components if len(component) == 1
        }
        reasons = Counter()
        items = []
        for index in sorted(singleton_indexes):
            uid = fragments[index].fragment_uid
            neighbors = []
            for other_index, other in enumerate(fragments):
                if other_index == index:
                    continue
                other_uid = other.fragment_uid
                similarity = embedding_scores.get(frozenset((uid, other_uid)), 0.0)
                forward = rerank_scores.get((uid, other_uid))
                reverse = rerank_scores.get((other_uid, uid))
                mutual = (
                    forward is not None
                    and reverse is not None
                    and forward >= rerank_threshold
                    and reverse >= rerank_threshold
                    and rerank_ranks.get((uid, other_uid), rerank_top_n + 1)
                    <= rerank_top_n
                    and rerank_ranks.get((other_uid, uid), rerank_top_n + 1)
                    <= rerank_top_n
                )
                relative_values = [
                    rerank_relative_ranks.get((uid, other_uid)),
                    rerank_relative_ranks.get((other_uid, uid)),
                ]
                reciprocal = (
                    forward is not None
                    and reverse is not None
                    and forward >= rerank_threshold
                    and reverse >= rerank_threshold
                    and (
                        rerank_ranks.get((uid, other_uid), rerank_top_n + 1)
                        <= rerank_top_n
                        or rerank_ranks.get((other_uid, uid), rerank_top_n + 1)
                        <= rerank_top_n
                    )
                    and all(value is not None for value in relative_values)
                    and sum(
                        float(value) for value in relative_values if value is not None
                    )
                    / 2.0
                    >= reciprocal_rank_threshold
                )
                seed = similarity >= merge_threshold or (
                    similarity >= rerank_floor and (mutual or reciprocal)
                )
                neighbors.append((similarity, other_uid, mutual, reciprocal, seed))
            neighbors.sort(key=lambda item: (-item[0], item[1]))
            candidates = [item for item in neighbors if item[0] >= rerank_floor]
            seeds = [item for item in candidates if item[4]]
            if seeds:
                reason = "component_cohesion_rejected"
            elif candidates:
                reason = "no_mutual_rerank"
            else:
                reason = "below_rerank_candidate_floor"
            reasons[reason] += 1
            nearest = neighbors[0] if neighbors else (0.0, "", False, False, False)
            items.append(
                {
                    "fragment_uid": uid,
                    "label": fragments[index].label,
                    "reason": reason,
                    "nearest_fragment_uid": nearest[1],
                    "nearest_similarity": round(float(nearest[0]), 6),
                    "nearest_mutual_rerank": bool(nearest[2]),
                    "nearest_reciprocal_rerank": bool(nearest[3]),
                    "candidate_count": len(candidates),
                    "seed_count": len(seeds),
                }
            )
        mapped_values = sorted(rerank_scores.values())
        raw_values = sorted(rerank_raw_scores.values())
        return {
            "parameters": {
                "fragment_similarity_threshold": merge_threshold,
                "rerank_candidate_floor": rerank_floor,
                "rerank_threshold": rerank_threshold,
                "rerank_reciprocal_rank_threshold": reciprocal_rank_threshold,
                "component_min_pair_similarity": float(
                    self.config.get("component_min_pair_similarity", 0.52)
                ),
                "component_min_average_similarity": float(
                    self.config.get("component_min_average_similarity", 0.65)
                ),
                "component_size_cohesion_penalty": float(
                    self.config.get("component_size_cohesion_penalty", 0.005)
                ),
            },
            "singleton_reason_counts": dict(reasons),
            "singletons": items,
            "rerank_score_distribution": {
                "mapped": self._score_distribution(mapped_values),
                "raw": self._score_distribution(raw_values),
                "raw_score_available": bool(raw_values),
                "relative_rank": self._score_distribution(
                    sorted(rerank_relative_ranks.values())
                ),
                "provider_mapping": str(
                    getattr(self.rerank_provider, "score_mapping", "")
                    or (
                        "identity"
                        if getattr(self.rerank_provider, "score_domain", "") == "[0,1]"
                        else "provider_native"
                    )
                ),
                "provider_score_domain": str(
                    getattr(self.rerank_provider, "score_domain", "") or "unknown"
                ),
            },
        }

    @staticmethod
    def _relative_rank_scores(scores: list[float]) -> list[float]:
        """Map a descending score list to tie-aware percentiles in ``[0, 1]``."""
        result_count = len(scores)
        if result_count <= 1:
            return [1.0] * result_count

        relative_scores = [0.0] * result_count
        start = 0
        while start < result_count:
            end = start
            while end + 1 < result_count and math.isclose(
                scores[end + 1],
                scores[start],
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                end += 1
            average_rank = (start + end + 2) / 2.0
            percentile = 1.0 - (average_rank - 1.0) / (result_count - 1)
            for index in range(start, end + 1):
                relative_scores[index] = percentile
            start = end + 1
        return relative_scores

    def _derive_topic_relations(
        self,
        run_uid: str,
        topics: list[TopicMemory],
    ) -> list[TopicRelation]:
        """Build a bounded, undirected related-topic graph from multiple signals."""
        threshold = float(self.config.get("related_topic_similarity_threshold", 0.60))
        max_degree = max(1, int(self.config.get("related_topic_top_n", 3)))
        candidate_limit = max(8, max_degree * 4)
        rankings = vector_neighbor_rankings(
            topics,
            candidate_limit=candidate_limit,
            similarity_threshold=threshold,
        )
        topic_by_uid = {topic.topic_uid: topic for topic in topics}
        keyword_sets = {
            topic.topic_uid: self._topic_keyword_terms(topic) for topic in topics
        }
        text_token_sets = {
            topic.topic_uid: self._relation_text_terms(f"{topic.title} {topic.summary}")
            for topic in topics
        }
        keyword_document_frequency = Counter(
            term for terms in keyword_sets.values() for term in terms
        )
        text_document_frequency = Counter(
            token for tokens in text_token_sets.values() for token in tokens
        )
        rank_positions = {
            uid: {
                other_uid: index for index, (_, other_uid) in enumerate(candidates, 1)
            }
            for uid, candidates in rankings.items()
        }
        rank_margins: dict[str, dict[str, float]] = {}
        for uid, candidates in rankings.items():
            margins: dict[str, float] = {}
            for index, (similarity, other_uid) in enumerate(candidates):
                next_similarity = (
                    float(candidates[index + 1][0])
                    if index + 1 < len(candidates)
                    else threshold
                )
                margins[other_uid] = max(0.0, float(similarity) - next_similarity)
            rank_margins[uid] = margins
        pair_candidates: dict[tuple[str, str], float] = {}
        for left_uid, candidates in rankings.items():
            for similarity, right_uid in candidates[:candidate_limit]:
                pair = tuple(sorted((left_uid, right_uid)))
                pair_candidates[pair] = max(
                    float(similarity), pair_candidates.get(pair, 0.0)
                )

        eligible: list[dict[str, Any]] = []
        for (left_uid, right_uid), similarity in pair_candidates.items():
            left_rank = rank_positions[left_uid].get(right_uid, 10**9)
            right_rank = rank_positions[right_uid].get(left_uid, 10**9)
            reciprocal_core = left_rank <= max_degree and right_rank <= max_degree
            reciprocal_candidate = (
                left_rank <= candidate_limit and right_rank <= candidate_limit
            )
            mutual_nearest = left_rank == 1 and right_rank == 1
            distinctive_mutual = (
                len(topics) >= 4
                and mutual_nearest
                and (
                    similarity >= max(0.78, threshold + 0.12)
                    or (
                        similarity >= max(0.72, threshold + 0.10)
                        and rank_margins[left_uid].get(right_uid, 0.0) >= 0.025
                        and rank_margins[right_uid].get(left_uid, 0.0) >= 0.025
                    )
                )
            )
            context = self._topic_relation_context(
                topic_by_uid[left_uid],
                topic_by_uid[right_uid],
                topic_count=len(topics),
                keyword_document_frequency=keyword_document_frequency,
                text_document_frequency=text_document_frequency,
                left_keywords=keyword_sets[left_uid],
                right_keywords=keyword_sets[right_uid],
                left_text_tokens=text_token_sets[left_uid],
                right_text_tokens=text_token_sets[right_uid],
                semantic_similarity=similarity,
                strong_reciprocal=reciprocal_core,
                neighborhood_supported=reciprocal_candidate,
                distinctive_mutual=distinctive_mutual,
                relation_threshold=threshold,
            )
            if not context["contextual_match"]:
                continue
            evidence_bonus = {
                "multiple_discriminative_keywords": 0.040,
                "single_discriminative_keyword": 0.025,
                "shared_distinctive_identifier": 0.035,
                "shared_timeline_with_semantic_support": 0.010,
                "weighted_lexical_overlap": 0.025,
                "strong_reciprocal_semantics": 0.015,
                "strong_neighborhood_semantics": 0.010,
                "distinctive_mutual_semantics": 0.020,
            }.get(str(context["evidence_kind"]), 0.0)
            selection_score = (
                float(similarity)
                + (0.035 if reciprocal_core else 0.0)
                + (0.015 if reciprocal_candidate else 0.0)
                + evidence_bonus
                + min(0.010, float(context["source_overlap"]) * 0.01)
            )
            eligible.append(
                {
                    "left_uid": left_uid,
                    "right_uid": right_uid,
                    "similarity": float(similarity),
                    "selection_score": selection_score,
                    "left_rank": left_rank,
                    "right_rank": right_rank,
                    "reciprocal_core": reciprocal_core,
                    "reciprocal_candidate": reciprocal_candidate,
                    "mutual_nearest": mutual_nearest,
                    "left_margin": rank_margins[left_uid].get(right_uid, 0.0),
                    "right_margin": rank_margins[right_uid].get(left_uid, 0.0),
                    "context": context,
                }
            )

        eligible.sort(
            key=lambda item: (
                -float(item["selection_score"]),
                str(item["left_uid"]),
                str(item["right_uid"]),
            )
        )
        selected: dict[tuple[str, str], dict[str, Any]] = {}
        degree: Counter[str] = Counter()
        coverage_bonus = 0.10

        # Optimize one explicit objective instead of running several ordering-
        # sensitive greedy passes.  An uncovered endpoint is valuable, but a
        # weak edge cannot outrank a substantially stronger supported edge.
        while True:
            feasible: list[tuple[float, dict[str, Any]]] = []
            for item in eligible:
                pair = (str(item["left_uid"]), str(item["right_uid"]))
                if pair in selected:
                    continue
                if degree[pair[0]] >= max_degree or degree[pair[1]] >= max_degree:
                    continue
                marginal = float(item["selection_score"]) + coverage_bonus * sum(
                    degree[uid] == 0 for uid in pair
                )
                feasible.append((marginal, item))
            if not feasible:
                break
            _marginal, item = max(
                feasible,
                key=lambda entry: (
                    entry[0],
                    float(entry[1]["selection_score"]),
                    -max(int(entry[1]["left_rank"]), int(entry[1]["right_rank"])),
                    str(entry[1]["right_uid"]),
                    str(entry[1]["left_uid"]),
                ),
            )
            pair = (str(item["left_uid"]), str(item["right_uid"]))
            item["selection_reason"] = (
                "coverage_quality_objective"
                if degree[pair[0]] == 0 or degree[pair[1]] == 0
                else "quality_fill"
            )
            selected[pair] = item
            degree[pair[0]] += 1
            degree[pair[1]] += 1

        # If an eligible Topic is still isolated only because its neighbor is
        # saturated, globally choose the best safe edge swap.  The displaced
        # endpoint must remain connected, so coverage can improve but never
        # regress.  Re-evaluating all plans after every swap avoids UID/list
        # ordering deciding which orphan wins.
        while True:
            swap_plans: list[tuple[float, dict[str, Any], list[tuple[str, str]]]] = []
            for item in eligible:
                pair = (str(item["left_uid"]), str(item["right_uid"]))
                if pair in selected or not any(degree[uid] == 0 for uid in pair):
                    continue
                replacements: list[tuple[str, str]] = []
                valid = True
                for endpoint in (uid for uid in pair if degree[uid] >= max_degree):
                    choices = []
                    for old_pair, old_item in selected.items():
                        if endpoint not in old_pair:
                            continue
                        other_uid = (
                            old_pair[1] if old_pair[0] == endpoint else old_pair[0]
                        )
                        if degree[other_uid] <= 1:
                            continue
                        choices.append((old_pair, old_item))
                    if not choices:
                        valid = False
                        break
                    weakest_pair, _weakest_item = min(
                        choices,
                        key=lambda entry: (
                            float(entry[1]["selection_score"]),
                            entry[0],
                        ),
                    )
                    if weakest_pair not in replacements:
                        replacements.append(weakest_pair)
                if not valid:
                    continue
                projected = Counter(degree)
                removed_quality = 0.0
                for old_pair in replacements:
                    old_item = selected[old_pair]
                    projected[old_pair[0]] -= 1
                    projected[old_pair[1]] -= 1
                    removed_quality += float(old_item["selection_score"])
                if any(projected[uid] >= max_degree for uid in pair):
                    continue
                coverage_gain = sum(projected[uid] == 0 for uid in pair)
                quality_delta = float(item["selection_score"]) - removed_quality
                objective_delta = coverage_bonus * coverage_gain + quality_delta
                if objective_delta < 0.0:
                    continue
                swap_plans.append((objective_delta, item, replacements))
            if not swap_plans:
                break
            _delta, item, replacements = max(
                swap_plans,
                key=lambda plan: (
                    plan[0],
                    float(plan[1]["selection_score"]),
                    str(plan[1]["right_uid"]),
                    str(plan[1]["left_uid"]),
                ),
            )
            for old_pair in replacements:
                selected.pop(old_pair)
                degree[old_pair[0]] -= 1
                degree[old_pair[1]] -= 1
            pair = (str(item["left_uid"]), str(item["right_uid"]))
            item["selection_reason"] = "orphan_coverage_swap"
            selected[pair] = item
            degree[pair[0]] += 1
            degree[pair[1]] += 1

        relations = []
        for (left_uid, right_uid), item in sorted(selected.items()):
            similarity = float(item["similarity"])
            context = dict(item["context"])
            confidence = min(
                0.99,
                similarity
                + (0.03 if item["reciprocal_core"] else 0.0)
                + (
                    0.02
                    if context["evidence_kind"] != "strong_reciprocal_semantics"
                    else 0.0
                ),
            )
            relation_uid = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"livingmemory:topic-relation:{left_uid}:{right_uid}:related",
                )
            )
            relations.append(
                TopicRelation(
                    relation_uid=relation_uid,
                    memory_space_id=topic_by_uid[left_uid].memory_space_id,
                    left_topic_uid=left_uid,
                    right_topic_uid=right_uid,
                    confidence=round(float(confidence), 6),
                    semantic_similarity=round(float(similarity), 6),
                    build_run_uid=run_uid,
                    metadata={
                        "algorithm_version": _RELATION_ALGORITHM_VERSION,
                        "directionality": "undirected",
                        "hierarchical": False,
                        "left_rank": int(item["left_rank"]),
                        "right_rank": int(item["right_rank"]),
                        "reciprocal_top_n": bool(item["reciprocal_core"]),
                        "candidate_limit": candidate_limit,
                        "max_degree": max_degree,
                        "selection_score": round(float(item["selection_score"]), 6),
                        "selection_reason": str(item["selection_reason"]),
                        "mutual_nearest": bool(item["mutual_nearest"]),
                        "left_similarity_margin": round(float(item["left_margin"]), 6),
                        "right_similarity_margin": round(
                            float(item["right_margin"]), 6
                        ),
                        **context,
                    },
                )
            )
        return relations

    @classmethod
    def _topic_relation_context(
        cls,
        left: TopicMemory,
        right: TopicMemory,
        *,
        topic_count: int = 2,
        keyword_document_frequency: Counter[str] | None = None,
        text_document_frequency: Counter[str] | None = None,
        left_keywords: set[str] | None = None,
        right_keywords: set[str] | None = None,
        left_text_tokens: set[str] | None = None,
        right_text_tokens: set[str] | None = None,
        semantic_similarity: float = 0.0,
        strong_reciprocal: bool = False,
        neighborhood_supported: bool = False,
        distinctive_mutual: bool = False,
        relation_threshold: float = 0.60,
    ) -> dict[str, Any]:
        """Require multiple, corpus-aware signals for a related-topic edge."""
        left_sources = set(left.metadata.get("source_timeline_uids", []))
        right_sources = set(right.metadata.get("source_timeline_uids", []))
        shared_sources = sorted(left_sources & right_sources)
        source_overlap = len(shared_sources) / max(1, len(left_sources | right_sources))

        left_keywords = left_keywords or cls._topic_keyword_terms(left)
        right_keywords = right_keywords or cls._topic_keyword_terms(right)
        keyword_document_frequency = keyword_document_frequency or Counter(
            term for terms in (left_keywords, right_keywords) for term in terms
        )
        generic_frequency_limit = max(2, math.ceil(max(2, topic_count) * 0.15))
        shared_keywords = cls._collapse_redundant_relation_terms(
            term
            for term in left_keywords & right_keywords
            if keyword_document_frequency.get(term, 0) <= generic_frequency_limit
            and not cls._is_generic_relation_term(term)
        )
        keyword_rarities = {
            term: math.log(
                (max(2, topic_count) + 1.0)
                / (keyword_document_frequency.get(term, 0) + 1.0)
            )
            / math.log(max(2, topic_count) + 1.0)
            for term in shared_keywords
        }
        left_text_tokens = left_text_tokens or cls._relation_text_terms(
            f"{left.title} {left.summary}"
        )
        right_text_tokens = right_text_tokens or cls._relation_text_terms(
            f"{right.title} {right.summary}"
        )
        text_document_frequency = text_document_frequency or Counter(
            token
            for tokens in (left_text_tokens, right_text_tokens)
            for token in tokens
        )
        lexical_similarity = cls._weighted_jaccard(
            left_text_tokens,
            right_text_tokens,
            text_document_frequency,
            max(2, topic_count),
        )
        evidence_kind = ""
        strongest_keyword_rarity = max(keyword_rarities.values(), default=0.0)
        if len(shared_keywords) >= 2 and semantic_similarity >= max(
            0.62, relation_threshold + 0.02
        ):
            evidence_kind = "multiple_discriminative_keywords"
        elif (
            any(
                re.fullmatch(r"[a-z0-9_-]{2,}", term) and re.search(r"[a-z]", term)
                for term in shared_keywords
            )
            and semantic_similarity >= relation_threshold
        ):
            evidence_kind = "shared_distinctive_identifier"
        elif (
            shared_keywords
            and strongest_keyword_rarity >= 0.40
            and semantic_similarity
            >= (
                max(0.62, relation_threshold + 0.02)
                if strongest_keyword_rarity >= 0.70
                or (strongest_keyword_rarity >= 0.65 and neighborhood_supported)
                else max(0.68, relation_threshold + 0.08)
            )
        ):
            evidence_kind = "single_discriminative_keyword"
        elif lexical_similarity >= 0.08 and semantic_similarity >= max(
            0.68, relation_threshold + 0.08
        ):
            evidence_kind = "weighted_lexical_overlap"
        elif (
            shared_sources
            and semantic_similarity >= 0.74
            and (
                shared_keywords
                or lexical_similarity >= 0.025
                or (source_overlap >= 0.50 and semantic_similarity >= 0.78)
            )
        ):
            evidence_kind = "shared_timeline_with_semantic_support"
        elif strong_reciprocal and semantic_similarity >= 0.81:
            evidence_kind = "strong_reciprocal_semantics"
        elif (
            topic_count >= 8 and neighborhood_supported and semantic_similarity >= 0.78
        ):
            evidence_kind = "strong_neighborhood_semantics"
        elif distinctive_mutual:
            evidence_kind = "distinctive_mutual_semantics"
        contextual_match = bool(evidence_kind)
        return {
            "contextual_match": contextual_match,
            "evidence_kind": evidence_kind,
            "shared_timeline_uids": shared_sources,
            "source_overlap": round(float(source_overlap), 6),
            "shared_keywords": shared_keywords[:20],
            "shared_keyword_rarities": {
                key: round(float(value), 6)
                for key, value in sorted(keyword_rarities.items())[:20]
            },
            "lexical_similarity": round(float(lexical_similarity), 6),
            "generic_keyword_frequency_limit": generic_frequency_limit,
        }

    @classmethod
    def _topic_keyword_terms(cls, topic: TopicMemory) -> set[str]:
        terms: set[str] = set()
        for keyword in topic.metadata.get("keywords", []):
            raw_keyword = str(keyword or "").casefold()
            normalized = cls._norm(keyword)
            if normalized:
                terms.add(normalized)
                terms.update(re.findall(r"[a-z0-9_-]{2,}", raw_keyword))
        return {term for term in terms if not cls._is_structural_time_term(term)}

    @classmethod
    def _relation_text_terms(cls, value: str) -> set[str]:
        return {
            term
            for term in lexical_tokens(value)
            if not cls._is_structural_time_term(term)
            and not cls._is_generic_relation_term(term)
        }

    @classmethod
    def _collapse_redundant_relation_terms(cls, values: Iterable[str]) -> list[str]:
        """Count overlapping n-grams from one concept as one evidence signal."""
        result: list[str] = []
        for term in sorted(
            {str(value) for value in values}, key=lambda v: (-len(v), v)
        ):
            if any(term in existing or existing in term for existing in result):
                continue
            result.append(term)
        return sorted(result)

    @staticmethod
    def _is_generic_relation_term(value: str) -> bool:
        """Reject participant, discourse and abstract connector terms as evidence."""
        return str(value or "").strip().casefold() in {
            "我",
            "你",
            "他",
            "她",
            "它",
            "我们",
            "你们",
            "他们",
            "她们",
            "我的",
            "你的",
            "他的",
            "她的",
            "对方",
            "用户",
            "助手",
            "机器人",
            "名字",
            "身份",
            "确认",
            "相关",
            "有关",
            "内容",
            "事情",
            "情况",
            "问题",
            "状态",
            "安排",
            "计划",
            "记录",
            "日常",
            "近期",
            "近况",
            "交流",
            "互动",
            "持续",
            "需要",
            "需求",
            "方面",
            "现场",
            "边界",
            "项目",
        }

    @staticmethod
    def _is_structural_time_term(value: str) -> bool:
        """Exclude calendar/clock syntax without discarding named concepts.

        Relation evidence should not become stronger merely because two Topics
        mention the same date.  Alphanumeric names such as ``Expo2026`` remain
        usable; only standalone structural date and time forms are removed.
        """
        term = str(value or "").strip().casefold()
        if not term:
            return True
        if term in {"年", "月", "日", "号", "时", "分", "秒"}:
            return True
        if re.fullmatch(r"\d{1,2}", term):
            return True
        if re.fullmatch(r"(?:19|20|21)\d{2}", term):
            return True
        if re.fullmatch(r"\d{1,2}[:：]\d{2}(?::\d{2})?", term):
            return True
        if re.fullmatch(r"\d{1,2}时(?:\d{1,2}分?)?(?:\d{1,2}秒)?", term):
            return True

        chinese_date = re.fullmatch(
            r"((?:19|20|21)\d{2})年(\d{1,2})月(?:([0-3]?\d)日?)?",
            term,
        )
        if chinese_date:
            month = int(chinese_date.group(2))
            day = int(chinese_date.group(3) or 1)
            return 1 <= month <= 12 and 1 <= day <= 31

        date_match = re.fullmatch(
            r"((?:19|20|21)\d{2})[-_/.年](\d{1,2})"
            r"(?:[-_/.月](\d{1,2})日?)?",
            term,
        )
        if date_match:
            month = int(date_match.group(2))
            day = int(date_match.group(3) or 1)
            return 1 <= month <= 12 and 1 <= day <= 31
        month_day = re.fullmatch(r"(\d{1,2})[-_/.月](\d{1,2})日?", term)
        if month_day:
            return (
                1 <= int(month_day.group(1)) <= 12
                and 1 <= int(month_day.group(2)) <= 31
            )

        if term.isdigit() and len(term) in {6, 8, 12, 14}:
            year, month = int(term[:4]), int(term[4:6])
            if 1900 <= year <= 2199 and 1 <= month <= 12:
                if len(term) == 6:
                    return True
                day = int(term[6:8])
                if 1 <= day <= 31:
                    return True
        if term.isdigit() and len(term) == 4:
            hour, minute = int(term[:2]), int(term[2:])
            return 0 <= hour <= 23 and 0 <= minute <= 59
        return False

    @staticmethod
    def _weighted_jaccard(
        left: set[str],
        right: set[str],
        document_frequency: Counter[str],
        document_count: int,
    ) -> float:
        return weighted_jaccard_similarity(
            left,
            right,
            document_frequency,
            document_count,
        )

    @staticmethod
    def _cluster_fragment_edges(
        fragment_count: int,
        embedding_scores: dict[tuple[int, int], float],
        seed_edges: list[tuple[float, int, int]],
        *,
        minimum_pair_similarity: float,
        minimum_average_similarity: float,
        size_cohesion_penalty: float = 0.0,
    ) -> list[list[int]]:
        """Merge only components whose complete cross-section stays coherent.

        A plain connected-component union lets a chain of individually plausible
        edges collapse unrelated subjects into one Topic. Here every proposed
        component merge must also satisfy a minimum cross-pair score and an
        average-link cohesion score across all members.
        """
        parents = list(range(fragment_count))
        members: dict[int, set[int]] = {
            index: {index} for index in range(fragment_count)
        }

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        for _, left, right in sorted(
            seed_edges,
            key=lambda item: (-item[0], item[1], item[2]),
        ):
            left_root, right_root = find(left), find(right)
            if left_root == right_root:
                continue
            cross_scores = [
                embedding_scores[tuple(sorted((left_member, right_member)))]
                for left_member in members[left_root]
                for right_member in members[right_root]
            ]
            if not cross_scores:
                continue
            if min(cross_scores) < minimum_pair_similarity:
                continue
            required_average = minimum_average_similarity
            if min(len(members[left_root]), len(members[right_root])) > 1:
                combined_size = len(members[left_root]) + len(members[right_root])
                required_average += max(0.0, size_cohesion_penalty) * max(
                    0.0,
                    math.log2(max(1.0, combined_size / 2.0)),
                )
            required_average = min(1.0, required_average)
            if sum(cross_scores) / len(cross_scores) < required_average:
                continue
            parents[right_root] = left_root
            members[left_root].update(members.pop(right_root))

        return [
            sorted(component)
            for _, component in sorted(members.items(), key=lambda item: min(item[1]))
        ]

    @staticmethod
    def _matching_quality(
        components: list[list[int]], fragment_count: int
    ) -> dict[str, Any]:
        sizes = sorted((len(component) for component in components), reverse=True)
        largest = sizes[0] if sizes else 0
        return {
            "component_count": len(components),
            "component_sizes": sizes,
            "largest_component_size": largest,
            "largest_component_ratio": round(largest / max(1, fragment_count), 6),
        }

    async def _incremental_existing_candidates(
        self,
        memory_space_id: str,
        fragments: list[TopicFragmentDraft],
        existing: list[TopicMemory],
        directly_affected_uids: set[str],
    ) -> list[TopicMemory]:
        """Bound matching to vector neighbors plus directly affected Topics."""
        by_uid = {topic.topic_uid: topic for topic in existing}
        selected_uids = set(directly_affected_uids)
        target_vector = self._average_vectors(
            [item.embedding for item in fragments if item.embedding]
        )
        if self.vector_index is not None and target_vector:
            hits = await self.vector_index.search(
                memory_space_id=memory_space_id,
                artifact_type="topic",
                query_vector=target_vector,
                limit=max(
                    2,
                    min(
                        64,
                        int(self.config.get("incremental_topic_candidate_k", 8)),
                    ),
                ),
                provider=self.embedding_provider,
                input_format_versions=SUPPORTED_TOPIC_EMBEDDING_FORMATS,
            )
            selected_uids.update(hit.artifact_uid for hit in hits)
        elif not directly_affected_uids:
            # Compatibility fallback for tests or custom embeddings without an index.
            return existing
        missing_uids = sorted(selected_uids - set(by_uid))
        if missing_uids:
            for topic in await self.store.get_topics_by_uids(
                memory_space_id,
                missing_uids,
            ):
                by_uid[topic.topic_uid] = topic
        return [by_uid[uid] for uid in sorted(selected_uids) if uid in by_uid]

    async def _incremental_relation_topics(
        self,
        memory_space_id: str,
        changed_topics: list[TopicMemory],
        changed_topic_uids: set[str],
    ) -> list[TopicMemory]:
        """Load only changed Topics and their bounded vector neighborhoods."""
        by_uid = {topic.topic_uid: topic for topic in changed_topics}
        selected_uids = set(changed_topic_uids)
        candidate_limit = max(
            8,
            int(self.config.get("related_topic_candidate_limit", 24)),
            int(self.config.get("related_topic_top_n", 3)) * 4,
        )
        if self.vector_index is not None:
            for topic in changed_topics:
                vector = [float(value) for value in topic.metadata.get("embedding", [])]
                if not vector:
                    continue
                hits = await self.vector_index.search(
                    memory_space_id=memory_space_id,
                    artifact_type="topic",
                    query_vector=vector,
                    limit=min(128, candidate_limit),
                    provider=self.embedding_provider,
                    input_format_versions=SUPPORTED_TOPIC_EMBEDDING_FORMATS,
                )
                selected_uids.update(hit.artifact_uid for hit in hits)
        missing_uids = sorted(selected_uids - set(by_uid))
        if missing_uids:
            for topic in await self.store.get_topics_by_uids(
                memory_space_id,
                missing_uids,
            ):
                by_uid[topic.topic_uid] = topic
        return [by_uid[uid] for uid in sorted(by_uid)]

    async def _match_existing_topic_decision(
        self,
        synthesis: dict[str, Any],
        fragments: list[TopicFragmentDraft],
        existing: list[TopicMemory],
        used: set[str],
        *,
        require_source_overlap: bool = False,
        incremental: bool = False,
    ) -> tuple[TopicMemory | None, list[tuple[float, TopicMemory]], bool]:
        source_uids = {uid for item in fragments for uid in item.timeline_uids}
        incoming_fingerprints = {
            str(value)
            for fragment in fragments
            for fact in fragment.facts
            for value in fact.get("source_atom_fingerprints", [])
            if str(value)
        }
        incoming_actors = {
            str(ref.get("actor_id") or ref.get("actor_ref") or "")
            for fragment in fragments
            for key in ("participant_refs", "mentioned_actor_refs")
            for ref in fragment.metadata.get(key, [])
            if isinstance(ref, dict)
            and str(ref.get("actor_id") or ref.get("actor_ref") or "")
        }
        ranked: list[tuple[float, TopicMemory]] = []
        target_vector = self._average_vectors([item.embedding for item in fragments])
        incoming_terms = {
            self._norm(value)
            for value in [
                synthesis.get("title"),
                *(synthesis.get("keywords") or []),
            ]
            if self._norm(value)
        }
        provenance_loader = getattr(self.store, "get_topic_provenance", None)
        for topic in existing:
            if topic.topic_uid in used:
                continue
            metadata = topic.metadata
            previous_sources = set(metadata.get("source_timeline_uids", []))
            overlap = len(source_uids & previous_sources) / max(
                1, len(source_uids | previous_sources)
            )
            if require_source_overlap and overlap <= 0.0:
                continue
            stored_vector = metadata.get("embedding", [])
            semantic = (
                self._cosine(target_vector, stored_vector) if stored_vector else 0.0
            )
            existing_terms = {
                self._norm(value)
                for value in [topic.title, *(metadata.get("keywords") or [])]
                if self._norm(value)
            }
            lexical = (
                len(incoming_terms & existing_terms) / max(1, len(incoming_terms))
                if incoming_terms
                else 0.0
            )
            provenance = (
                await provenance_loader(topic.topic_uid)
                if callable(provenance_loader)
                else {}
            )
            existing_fingerprints = {
                str(row.get("source_atom_fingerprint") or "")
                for row in provenance.get("atom_sources", [])
                if str(row.get("source_atom_fingerprint") or "")
            }
            continuity = (
                len(incoming_fingerprints & existing_fingerprints)
                / len(incoming_fingerprints)
                if incoming_fingerprints
                else 0.0
            )
            existing_actors = {
                str(row.get("actor_id") or "")
                for row in provenance.get("actor_links", [])
                if str(row.get("actor_id") or "")
            }
            actor_affinity = (
                len(incoming_actors & existing_actors)
                / max(1, len(incoming_actors | existing_actors))
                if incoming_actors and existing_actors
                else 0.0
            )
            actor_time = (
                0.5 * actor_affinity
                + 0.5 * self._topic_fragment_time_affinity(topic, fragments)
            )
            if incoming_fingerprints and existing_fingerprints:
                score = (
                    0.40 * continuity
                    + 0.40 * semantic
                    + 0.10 * lexical
                    + 0.05 * actor_time
                    + 0.05 * overlap
                )
            else:
                # Legacy snapshots may not have fact provenance. Fall back to
                # semantics without allowing a broad shared Timeline to dominate.
                score = (
                    0.75 * semantic
                    + 0.15 * lexical
                    + 0.05 * actor_time
                    + 0.05 * overlap
                )
            ranked.append((score, topic))
        ranked.sort(key=lambda item: (-item[0], item[1].topic_uid))
        threshold = float(
            self.config.get(
                "incremental_topic_match_threshold"
                if incremental
                else "existing_topic_match_threshold",
                0.55,
            )
        )
        if not ranked or ranked[0][0] < threshold:
            return None, ranked, False
        margin = float(self.config.get("incremental_topic_match_margin", 0.04))
        close_candidates = bool(
            incremental
            and len(ranked) > 1
            and ranked[1][0] >= threshold
            and ranked[0][0] - ranked[1][0] < margin
        )
        if close_candidates:
            # Dense embedding spaces commonly put several broad sibling Topics just
            # above the continuation threshold. Merging one arbitrarily is unsafe,
            # while blocking every such delta behind manual review leaves its source
            # Timeline permanently unindexed. Only genuinely strong competing
            # candidates require a person; marginal ties become a new Topic and can
            # still be connected by the related-Topic graph.
            review_threshold = max(
                threshold,
                float(self.config.get("incremental_topic_review_threshold", 0.72)),
            )
            return None, ranked, ranked[1][0] >= review_threshold
        return ranked[0][1], ranked, False

    @classmethod
    def _component_fragment_uids(cls, fragments: list[TopicFragmentDraft]) -> list[str]:
        return sorted(
            {
                str(fragment.logical_fragment_uid or fragment.fragment_uid)
                for fragment in fragments
            }
        )

    @classmethod
    def _component_uid(cls, fragments: list[TopicFragmentDraft]) -> str:
        identity = "|".join(cls._component_fragment_uids(fragments))
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"livingmemory:topic-component:{identity}",
            )
        )

    @staticmethod
    def _topic_fragment_time_affinity(
        topic: TopicMemory, fragments: list[TopicFragmentDraft]
    ) -> float:
        starts = [item.started_at for item in fragments if item.started_at is not None]
        ends = [item.ended_at for item in fragments if item.ended_at is not None]
        if not starts or not ends or topic.started_at is None or topic.ended_at is None:
            return 0.0
        fragment_start = min(float(value) for value in starts)
        fragment_end = max(float(value) for value in ends)
        overlap_start = max(float(topic.started_at), fragment_start)
        overlap_end = min(float(topic.ended_at), fragment_end)
        if overlap_end < overlap_start:
            return 0.0
        union_start = min(float(topic.started_at), fragment_start)
        union_end = max(float(topic.ended_at), fragment_end)
        return (overlap_end - overlap_start + 1.0) / max(
            1.0, union_end - union_start + 1.0
        )

    async def _match_existing_topic(
        self,
        synthesis: dict[str, Any],
        fragments: list[TopicFragmentDraft],
        existing: list[TopicMemory],
        used: set[str],
        *,
        require_source_overlap: bool = False,
    ) -> TopicMemory | None:
        matched, _, _ = await self._match_existing_topic_decision(
            synthesis,
            fragments,
            existing,
            used,
            require_source_overlap=require_source_overlap,
            incremental=False,
        )
        return matched

    async def _existing_topic_fragment(
        self,
        run_uid: str,
        topic: TopicMemory,
        *,
        exclude_timeline_uids: set[str] | None = None,
    ) -> TopicFragmentDraft | None:
        """Project an existing Topic into a source-grounded incremental input."""
        provenance = await self.store.get_topic_provenance(topic.topic_uid)
        links = provenance.get("links", [])
        atoms = provenance.get("atoms", [])
        sources = provenance.get("atom_sources", [])
        existing_actor_links = [
            dict(value)
            for value in provenance.get("actor_links", [])
            if isinstance(value, dict)
        ]
        actor_links_by_atom: dict[str, list[dict[str, Any]]] = {}
        for value in provenance.get("atom_actor_links", []):
            if isinstance(value, dict):
                actor_links_by_atom.setdefault(
                    str(value.get("topic_atom_uid") or ""), []
                ).append(dict(value))
        excluded = set(exclude_timeline_uids or set())
        if excluded:
            existing_actor_links = [
                value
                for value in existing_actor_links
                if not (
                    (
                        source_uids := {
                            str(uid)
                            for uid in (value.get("metadata") or {}).get(
                                "timeline_uids", []
                            )
                            if str(uid)
                        }
                    )
                    and source_uids <= excluded
                )
            ]
            actor_links_by_atom = {
                atom_uid: [
                    value
                    for value in values
                    if str(value.get("timeline_uid") or "") not in excluded
                ]
                for atom_uid, values in actor_links_by_atom.items()
            }
        timeline_uids = sorted(
            {
                str(row.get("timeline_uid") or "")
                for row in links
                if row.get("timeline_uid")
                and str(row.get("timeline_uid")) not in excluded
            }
        )
        if not timeline_uids:
            return None
        sources_by_atom: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            sources_by_atom.setdefault(
                str(source.get("topic_atom_uid") or ""), []
            ).append(source)
        facts: list[dict[str, Any]] = []
        for atom in atoms:
            atom_uid = str(atom.get("atom_uid") or "")
            atom_sources = [
                row
                for row in sources_by_atom.get(atom_uid, [])
                if str(row.get("timeline_uid") or "") not in excluded
            ]
            source_timelines = sorted(
                {
                    str(row.get("timeline_uid") or "")
                    for row in atom_sources
                    if row.get("timeline_uid")
                }
            )
            if not source_timelines:
                continue
            facts.append(
                {
                    "fact_uid": f"existing:{atom_uid}",
                    "type": str(atom.get("atom_type") or "factual"),
                    "content": str(atom.get("content") or ""),
                    "importance": self._score(atom.get("importance"), topic.importance),
                    "confidence": self._score(atom.get("confidence"), topic.confidence),
                    "source_timeline_uids": source_timelines,
                    "source_atom_fingerprints": sorted(
                        {
                            str(row.get("source_atom_fingerprint") or "")
                            for row in atom_sources
                            if row.get("source_atom_fingerprint")
                        }
                    ),
                    "source_kinds_by_fingerprint": {
                        str(row.get("source_atom_fingerprint")): str(
                            row.get("source_kind") or "fact_fingerprint"
                        )
                        for row in atom_sources
                        if row.get("source_atom_fingerprint")
                    },
                    "source_timeline_uids_by_fingerprint": {
                        fingerprint: sorted(
                            {
                                str(row.get("timeline_uid") or "")
                                for row in atom_sources
                                if str(row.get("source_atom_fingerprint") or "")
                                == fingerprint
                                and row.get("timeline_uid")
                            }
                        )
                        for fingerprint in {
                            str(row.get("source_atom_fingerprint") or "")
                            for row in atom_sources
                            if row.get("source_atom_fingerprint")
                        }
                    },
                    "actor_refs": [
                        value
                        for value in actor_links_by_atom.get(atom_uid, [])
                        if self._valid_actor_relation_for_type(value)
                    ],
                }
            )
        if not facts:
            return None
        cluster_map = {
            str(row["timeline_uid"]): str(row.get("time_cluster_key") or "")
            for row in links
            if row.get("timeline_uid") and str(row.get("timeline_uid")) not in excluded
        }
        return TopicFragmentDraft(
            fragment_uid=f"existing:{topic.topic_uid}:r{topic.revision}",
            run_uid=run_uid,
            candidate_group_uid="existing-topic",
            memory_space_id=topic.memory_space_id,
            label=topic.title,
            summary=topic.summary,
            timeline_uids=timeline_uids,
            source_revisions={
                str(row["timeline_uid"]): int(row.get("source_timeline_revision") or 1)
                for row in links
                if row.get("timeline_uid")
                and str(row.get("timeline_uid")) not in excluded
            },
            facts=facts,
            time_cluster_keys=sorted(
                {value for value in cluster_map.values() if value}
            ),
            importance=topic.semantic_importance,
            confidence=topic.confidence,
            embedding=[float(value) for value in topic.metadata.get("embedding", [])],
            started_at=topic.started_at,
            ended_at=topic.ended_at,
            status="existing",
            metadata={
                "timeline_cluster_map": cluster_map,
                "existing_topic": True,
                "existing_topic_uid": topic.topic_uid,
                "narrative_schema_version": topic.metadata.get(
                    "narrative_schema_version"
                ),
                "conversation_roles": topic.metadata.get("conversation_roles", {}),
                "participant_refs": [
                    value
                    for value in existing_actor_links
                    if value.get("relation_type")
                    in {"speaker", "narrator", "responder"}
                    and self._valid_actor_relation_for_type(value)
                ],
                "mentioned_actor_refs": [
                    value
                    for value in existing_actor_links
                    if value.get("relation_type")
                    not in {"speaker", "narrator", "responder"}
                ],
            },
        )

    async def _retained_affected_topic_plan(
        self,
        *,
        run_uid: str,
        topic: TopicMemory,
        excluded_timeline_uids: set[str],
        candidate_map: dict[str, TimelineTopicCandidate],
    ) -> dict[str, Any] | None:
        """Rebuild an edited Topic from sources that still remain valid."""
        retained = await self._existing_topic_fragment(
            run_uid,
            topic,
            exclude_timeline_uids=excluded_timeline_uids,
        )
        if retained is None:
            return None
        synthesis = await self._synthesize_component_checkpointed(
            run_uid,
            [retained],
        )
        (
            rebuilt_topic,
            atoms,
            links,
            sources,
            actor_links,
            atom_actor_links,
        ) = self._materialize_snapshot(
            run_uid,
            topic.memory_space_id,
            synthesis,
            [retained],
            candidate_map,
            topic,
        )
        return {
            "topic": rebuilt_topic,
            "atoms": atoms,
            "links": links,
            "sources": sources,
            "actor_links": actor_links,
            "atom_actor_links": atom_actor_links,
            "matched": topic,
            "fragments": [retained],
            "synthesis": synthesis,
        }

    async def _resolve_maintenance_reviews_safely(
        self,
        memory_space_id: str,
        *,
        component_uids: list[str],
    ) -> None:
        """Keep optional queue bookkeeping from invalidating a published build."""
        try:
            await self.store.resolve_maintenance_reviews(
                memory_space_id,
                component_uids=component_uids,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[TopicMemory] Topic 已发布，但维护判定队列未能自动消项 "
                "(memory_space_id=%s)",
                memory_space_id,
                exc_info=True,
            )


__all__ = ["TopicComponentMatcherMixin"]
