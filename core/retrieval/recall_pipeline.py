"""Shared production/test pipeline for structured Timeline recall."""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..topic_similarity import jaccard_similarity, retrieval_text_features
from .hybrid_retriever import HybridResult
from .temporal_constraint import TemporalConstraint, timeline_time_anchor

ASSISTANT_CONTEXT_MODES = {"exclude", "low_weight", "normal"}


@dataclass(slots=True)
class RecallQueryBranch:
    """One independently searched part of a conversational query."""

    name: str
    text: str
    weight: float
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "weight": round(self.weight, 4),
            "text": self.text,
        }


@dataclass(slots=True)
class RecallCandidate:
    """Candidate accumulated from one or more query branches."""

    result: HybridResult
    branch_scores: dict[str, dict[str, float]] = field(default_factory=dict)
    fused_score: float = 0.0
    relevance_score: float = 0.0
    best_source_score: float = 0.0
    selected: bool = False
    filter_reason: str | None = None
    event_started_at: float | None = None
    event_ended_at: float | None = None
    time_basis: str = "unavailable"
    time_fallback: bool = True
    matched_source_uids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.result.doc_id,
            "fused_score": round(self.fused_score, 6),
            "relevance_score": round(self.relevance_score, 6),
            "selected": self.selected,
            "filter_reason": self.filter_reason,
            "event_started_at": self.event_started_at,
            "event_ended_at": self.event_ended_at,
            "time_basis": self.time_basis,
            "time_fallback": self.time_fallback,
            "matched_source_uids": self.matched_source_uids,
            "branch_scores": self.branch_scores,
        }


@dataclass(slots=True)
class RecallPipelineResult:
    """Selected memories plus diagnostics for WebUI and logs."""

    results: list[HybridResult]
    branches: list[RecallQueryBranch]
    candidates: list[RecallCandidate]
    candidate_limit: int
    final_limit: int
    applied_threshold: float
    overlap_suppressed: int = 0
    temporal_constraint: TemporalConstraint | None = None
    temporal_suppressed: int = 0

    def diagnostics(self) -> dict[str, Any]:
        return {
            "query_branches": [item.to_dict() for item in self.branches],
            "candidate_limit": self.candidate_limit,
            "final_limit": self.final_limit,
            "applied_threshold": round(self.applied_threshold, 6),
            "candidate_count": len(self.candidates),
            "selected_count": len(self.results),
            "overlap_suppressed": self.overlap_suppressed,
            "temporal_constraint": (
                self.temporal_constraint.to_dict()
                if self.temporal_constraint is not None
                else None
            ),
            "temporal_suppressed": self.temporal_suppressed,
            "candidates": [item.to_dict() for item in self.candidates],
        }


class RecallPipeline:
    """Build weighted query branches, retrieve, filter and diversify results."""

    _USER_CONTEXT_WEIGHT = 0.45
    _ASSISTANT_CONTEXT_WEIGHT = 0.40
    _CROSS_BRANCH_BONUS = 0.03

    def __init__(self, memory_engine, config_manager=None):
        self.memory_engine = memory_engine
        self.config_manager = config_manager

    def _config(self, key: str, default: Any) -> Any:
        manager = self.config_manager
        if manager is None:
            return default
        if isinstance(manager, dict):
            recall = manager.get("recall_engine", manager)
            if isinstance(recall, dict):
                return recall.get(key, default)
        getter = getattr(manager, "get", None)
        if callable(getter):
            return getter(f"recall_engine.{key}", default)
        return default

    @classmethod
    def normalize_assistant_mode(cls, value: Any) -> str:
        normalized = str(value or "exclude").strip().lower()
        aliases = {
            "off": "exclude",
            "none": "exclude",
            "disabled": "exclude",
            "low": "low_weight",
            "weighted": "low_weight",
            "full": "normal",
            "include": "normal",
        }
        normalized = aliases.get(normalized, normalized)
        return normalized if normalized in ASSISTANT_CONTEXT_MODES else "exclude"

    def build_query_branches(
        self,
        current_query: str,
        recent_messages: list[dict[str, Any]] | None = None,
        *,
        expansion_enabled: bool | None = None,
        assistant_mode: str | None = None,
    ) -> list[RecallQueryBranch]:
        current = self._clean_text(current_query)
        if not current:
            return []

        branches = [
            RecallQueryBranch(
                name="current",
                text=current,
                weight=1.0,
                role="user",
            )
        ]
        enabled = (
            bool(self._config("inject_with_recent_context", False))
            if expansion_enabled is None
            else bool(expansion_enabled)
        )
        if not enabled or not recent_messages:
            return branches

        mode = self.normalize_assistant_mode(
            assistant_mode
            if assistant_mode is not None
            else self._config("assistant_context_mode", "exclude")
        )
        normalized_messages = self._recent_history_without_current(
            recent_messages, current
        )
        max_age_seconds = max(
            0,
            int(self._config("recent_context_max_age_seconds", 7200)),
        )
        if max_age_seconds > 0:
            cutoff = time.time() - max_age_seconds
            normalized_messages = [
                message
                for message in normalized_messages
                if (timestamp := self._message_timestamp_seconds(
                    message.get("timestamp")
                ))
                is not None
                and timestamp >= cutoff
            ]
        normalized_messages = normalized_messages[-4:]
        user_parts = self._unique_role_parts(normalized_messages, "user")
        assistant_parts = self._unique_role_parts(normalized_messages, "assistant")
        user_weight = self._clamp(
            float(self._config("recent_user_weight", self._USER_CONTEXT_WEIGHT))
        )
        assistant_weight = self._clamp(
            float(
                self._config(
                    "recent_assistant_weight",
                    self._ASSISTANT_CONTEXT_WEIGHT,
                )
            )
        )

        if user_parts:
            branches.append(
                RecallQueryBranch(
                    name="recent_user",
                    text=" | ".join(user_parts),
                    weight=user_weight,
                    role="user",
                )
            )
        if assistant_parts and mode != "exclude":
            weight = (
                assistant_weight * 0.5
                if mode == "low_weight"
                else assistant_weight
            )
            branches.append(
                RecallQueryBranch(
                    name="recent_assistant",
                    text=" | ".join(assistant_parts),
                    weight=weight,
                    role="assistant",
                )
            )
        return branches

    async def search(
        self,
        *,
        current_query: str,
        final_k: int,
        session_id: str | None = None,
        persona_id: str | None = None,
        recent_messages: list[dict[str, Any]] | None = None,
        expansion_enabled: bool | None = None,
        assistant_mode: str | None = None,
        context_session_id: str | None = None,
        visible_message_start_index: int | None = None,
        visible_message_end_index: int | None = None,
        track_access: bool = True,
        temporal: TemporalConstraint | None = None,
    ) -> RecallPipelineResult:
        final_limit = max(0, int(final_k))
        branches = self.build_query_branches(
            current_query,
            recent_messages,
            expansion_enabled=expansion_enabled,
            assistant_mode=assistant_mode,
        )
        if final_limit <= 0 or not branches:
            return RecallPipelineResult(
                [],
                branches,
                [],
                0,
                final_limit,
                0.0,
                temporal_constraint=temporal,
            )

        multiplier = max(1, int(self._config("candidate_multiplier", 3)))
        candidate_limit = min(50, max(final_limit, final_limit * multiplier))
        if temporal is not None:
            identity_store = getattr(self.memory_engine, "memory_identity_store", None)
            list_document_ids = getattr(identity_store, "list_timeline_document_ids", None)
            if callable(list_document_ids):
                session_scope = [session_id] if session_id else []
                resolver = getattr(self.memory_engine, "resolve_session_scope", None)
                if session_id and callable(resolver):
                    resolved = resolver(session_id)
                    if asyncio.iscoroutine(resolved):
                        resolved = await resolved
                    session_scope = list(resolved or session_scope)
                scoped_document_ids = await list_document_ids(
                    session_ids=session_scope,
                    persona_id=persona_id,
                    limit=2000,
                )
                candidate_limit = max(candidate_limit, len(scoped_document_ids))
        branch_results = await asyncio.gather(
            *(
                self.memory_engine.search_memories(
                    query=branch.text,
                    k=candidate_limit,
                    session_id=session_id,
                    persona_id=persona_id,
                    track_access=False,
                )
                for branch in branches
            )
        )

        candidate_map: dict[int, RecallCandidate] = {}
        branch_map = {item.name: item for item in branches}
        for branch, results in zip(branches, branch_results, strict=True):
            for result in results:
                candidate = candidate_map.get(result.doc_id)
                if candidate is None:
                    candidate = RecallCandidate(result=self._clone_result(result))
                    candidate_map[result.doc_id] = candidate
                relevance = self._result_relevance(result)
                candidate.branch_scores[branch.name] = {
                    "weight": round(branch.weight, 6),
                    "result_score": round(float(result.final_score), 6),
                    "relevance": round(relevance, 6),
                }
                weighted_source_score = branch.weight * float(result.final_score)
                if weighted_source_score > candidate.best_source_score:
                    candidate.result = self._clone_result(result)
                    candidate.best_source_score = weighted_source_score

        for candidate in candidate_map.values():
            rank_product = 1.0
            relevance_product = 1.0
            for branch_name, scores in candidate.branch_scores.items():
                weight = branch_map[branch_name].weight
                rank_product *= 1.0 - self._clamp(weight * scores["result_score"])
                relevance_product *= 1.0 - self._clamp(
                    weight * scores["relevance"]
                )
            support_bonus = min(
                0.06,
                max(0, len(candidate.branch_scores) - 1)
                * self._CROSS_BRANCH_BONUS,
            )
            candidate.fused_score = self._clamp(1.0 - rank_product + support_bonus)
            candidate.relevance_score = self._clamp(1.0 - relevance_product)
            candidate.result.final_score = candidate.fused_score
            raw_breakdown = getattr(candidate.result, "score_breakdown", None)
            breakdown = dict(raw_breakdown) if isinstance(raw_breakdown, dict) else {}
            breakdown.update(
                {
                    "recall_fused_score": round(candidate.fused_score, 4),
                    "recall_relevance_score": round(
                        candidate.relevance_score, 4
                    ),
                    "recall_branch_count": float(len(candidate.branch_scores)),
                }
            )
            candidate.result.score_breakdown = breakdown

        if temporal is not None and candidate_map:
            await self._attach_time_anchors(candidate_map)

        candidates = sorted(
            candidate_map.values(), key=lambda item: item.fused_score, reverse=True
        )
        temporal_suppressed = 0
        temporally_visible: list[RecallCandidate] = []
        for candidate in candidates:
            if temporal is None:
                temporally_visible.append(candidate)
                continue
            has_time = candidate.event_started_at is not None or candidate.event_ended_at is not None
            if not has_time:
                candidate.filter_reason = "temporal_anchor_unavailable"
                temporal_suppressed += 1
                continue
            if temporal.has_range and not temporal.overlaps(
                candidate.event_started_at,
                candidate.event_ended_at,
            ):
                candidate.filter_reason = "outside_temporal_range"
                temporal_suppressed += 1
                continue
            temporally_visible.append(candidate)

        suppress_overlap = bool(
            self._config("context_overlap_suppression", True)
        )
        non_overlapping: list[RecallCandidate] = []
        overlap_suppressed = 0
        for candidate in temporally_visible:
            if suppress_overlap and self._overlaps_visible_context(
                candidate.result,
                session_id=context_session_id or session_id,
                visible_start=visible_message_start_index,
                visible_end=visible_message_end_index,
            ):
                candidate.filter_reason = "current_context_overlap"
                overlap_suppressed += 1
                continue
            non_overlapping.append(candidate)

        min_relevance = self._clamp(
            float(self._config("min_relevance_score", 0.38))
        )
        relative_floor = self._clamp(
            float(self._config("relative_score_floor", 0.65))
        )
        best_relevance = max(
            (item.relevance_score for item in non_overlapping), default=0.0
        )
        applied_threshold = (
            min_relevance
            if temporal is not None and temporal.order in {"earliest", "latest"}
            else max(min_relevance, best_relevance * relative_floor)
        )

        eligible: list[RecallCandidate] = []
        for candidate in non_overlapping:
            if candidate.relevance_score < min_relevance:
                candidate.filter_reason = "below_min_relevance"
                continue
            if candidate.relevance_score < applied_threshold:
                candidate.filter_reason = "below_relative_floor"
                continue
            eligible.append(candidate)

        if temporal is not None and temporal.order in {"earliest", "latest"}:
            reverse = temporal.order == "latest"
            if reverse:
                eligible.sort(
                    key=lambda item: (
                        -temporal.sort_value(
                            item.event_started_at, item.event_ended_at
                        ),
                        -item.relevance_score,
                        item.result.doc_id,
                    )
                )
            else:
                eligible.sort(
                    key=lambda item: (
                        temporal.sort_value(
                            item.event_started_at, item.event_ended_at
                        ),
                        -item.relevance_score,
                        item.result.doc_id,
                    )
                )
            selected = eligible[:final_limit]
        else:
            mmr_lambda = self._clamp(float(self._config("mmr_lambda", 0.72)))
            selected = self._select_mmr(eligible, final_limit, mmr_lambda)
        selected_ids = {item.result.doc_id for item in selected}
        for candidate in eligible:
            if candidate.result.doc_id in selected_ids:
                candidate.selected = True
                candidate.filter_reason = None
            else:
                candidate.filter_reason = "diversity_or_result_limit"

        results = [item.result for item in selected]
        if track_access and results:
            recorder = getattr(self.memory_engine, "record_memory_access", None)
            if callable(recorder):
                recorder([item.doc_id for item in results])

        return RecallPipelineResult(
            results=results,
            branches=branches,
            candidates=candidates,
            candidate_limit=candidate_limit,
            final_limit=final_limit,
            applied_threshold=applied_threshold,
            overlap_suppressed=overlap_suppressed,
            temporal_constraint=temporal,
            temporal_suppressed=temporal_suppressed,
        )

    async def _attach_time_anchors(
        self, candidate_map: dict[int, RecallCandidate]
    ) -> None:
        identity_store = getattr(self.memory_engine, "memory_identity_store", None)
        resolver = getattr(identity_store, "get_time_anchors_by_document_ids", None)
        anchors = (
            await resolver(list(candidate_map)) if callable(resolver) else {}
        )
        for document_id, candidate in candidate_map.items():
            anchor = anchors.get(document_id)
            if anchor:
                candidate.event_started_at = anchor.get("started_at")
                candidate.event_ended_at = anchor.get("ended_at")
                candidate.time_basis = str(anchor.get("time_basis") or "unavailable")
                candidate.time_fallback = bool(anchor.get("time_fallback", True))
                memory_uid = str(anchor.get("memory_uid") or "")
                candidate.matched_source_uids = [memory_uid] if memory_uid else []
            else:
                (
                    candidate.event_started_at,
                    candidate.event_ended_at,
                    candidate.time_basis,
                    candidate.time_fallback,
                ) = timeline_time_anchor(candidate.result.metadata)
            candidate.result.metadata.update(
                {
                    "event_started_at": candidate.event_started_at,
                    "event_ended_at": candidate.event_ended_at,
                    "time_basis": candidate.time_basis,
                    "time_fallback": candidate.time_fallback,
                    "matched_source_uids": list(candidate.matched_source_uids),
                }
            )

    @classmethod
    def _select_mmr(
        cls,
        candidates: list[RecallCandidate],
        limit: int,
        mmr_lambda: float,
    ) -> list[RecallCandidate]:
        if limit <= 0 or not candidates:
            return []
        remaining = list(candidates)
        selected: list[RecallCandidate] = []
        token_cache = {
            item.result.doc_id: cls._text_features(item.result.content)
            for item in remaining
        }
        while remaining and len(selected) < limit:
            if not selected:
                selected.append(remaining.pop(0))
                continue
            best_index = 0
            best_value = -math.inf
            for index, candidate in enumerate(remaining):
                tokens = token_cache[candidate.result.doc_id]
                max_similarity = max(
                    cls._jaccard(
                        tokens,
                        token_cache[selected_item.result.doc_id],
                    )
                    for selected_item in selected
                )
                value = (
                    mmr_lambda * candidate.fused_score
                    - (1.0 - mmr_lambda) * max_similarity
                )
                if value > best_value:
                    best_index = index
                    best_value = value
            selected.append(remaining.pop(best_index))
        return selected

    @staticmethod
    def _result_relevance(result: HybridResult) -> float:
        raw_breakdown = getattr(result, "score_breakdown", None)
        breakdown = raw_breakdown if isinstance(raw_breakdown, dict) else {}
        vector_values = [
            getattr(result, "vector_score", None),
            breakdown.get("document_vector_score"),
            breakdown.get("graph_vector_score"),
        ]
        keyword_values = [
            getattr(result, "bm25_score", None),
            breakdown.get("document_keyword_score"),
            breakdown.get("graph_keyword_score"),
        ]

        def finite_scores(values: list[Any]) -> list[float]:
            output: list[float] = []
            for value in values:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(parsed):
                    output.append(max(0.0, min(1.0, parsed)))
            return output

        vectors = finite_scores(vector_values)
        keywords = finite_scores(keyword_values)
        signals = vectors + [value * 0.55 for value in keywords]
        if signals:
            return max(signals)
        return max(0.0, min(1.0, float(result.final_score)))

    @staticmethod
    def _clone_result(result: Any) -> HybridResult:
        """Copy only the stable retrieval contract, avoiding provider/mock internals."""

        def number(value: Any, default: float = 0.0) -> float:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return default
            return parsed if math.isfinite(parsed) else default

        metadata = getattr(result, "metadata", {})
        raw_breakdown = getattr(result, "score_breakdown", None)
        return HybridResult(
            doc_id=getattr(result, "doc_id", 0),
            final_score=number(getattr(result, "final_score", 0.0)),
            rrf_score=number(getattr(result, "rrf_score", 0.0)),
            bm25_score=(
                number(getattr(result, "bm25_score"))
                if isinstance(getattr(result, "bm25_score", None), (int, float))
                else None
            ),
            vector_score=(
                number(getattr(result, "vector_score"))
                if isinstance(getattr(result, "vector_score", None), (int, float))
                else None
            ),
            content=str(getattr(result, "content", "") or ""),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            score_breakdown=(
                dict(raw_breakdown) if isinstance(raw_breakdown, dict) else {}
            ),
        )

    @staticmethod
    def _overlaps_visible_context(
        result: HybridResult,
        *,
        session_id: str | None,
        visible_start: int | None,
        visible_end: int | None,
    ) -> bool:
        if visible_start is None or visible_end is None or visible_end <= visible_start:
            return False
        if not session_id:
            return False
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        if metadata.get("session_id") != session_id:
            return False
        source = metadata.get("source_window")
        if not isinstance(source, dict):
            return False
        try:
            source_start = int(source.get("start_index"))
            source_end = int(source.get("end_index"))
        except (TypeError, ValueError):
            return False
        return source_start < visible_end and source_end > visible_start

    @classmethod
    def _recent_history_without_current(
        cls,
        messages: list[dict[str, Any]],
        current_query: str,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = cls._clean_text(message.get("content"))
            if content:
                normalized.append(
                    {
                        "role": role,
                        "content": content,
                        "timestamp": message.get("timestamp"),
                    }
                )
        if (
            normalized
            and normalized[-1]["role"] == "user"
            and cls._comparable_text(normalized[-1]["content"])
            == cls._comparable_text(current_query)
        ):
            normalized.pop()
        return normalized

    @staticmethod
    def _message_timestamp_seconds(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            timestamp = float(value)
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                timestamp = float(stripped)
            except ValueError:
                try:
                    timestamp = datetime.fromisoformat(
                        stripped.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    return None
        else:
            return None
        if timestamp > 100_000_000_000:
            timestamp /= 1000.0
        return timestamp if timestamp > 0 and math.isfinite(timestamp) else None

    @staticmethod
    def _unique_role_parts(
        messages: list[dict[str, Any]], role: str
    ) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for message in messages:
            if message.get("role") != role:
                continue
            content = message.get("content", "")
            key = RecallPipeline._comparable_text(content)
            if key and key not in seen:
                seen.add(key)
                output.append(content)
        return output

    @staticmethod
    def _clean_text(value: Any) -> str:
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            value = " ".join(parts)
        text = str(value or "")
        return " ".join(text.split()).strip()

    @staticmethod
    def _comparable_text(value: Any) -> str:
        text = RecallPipeline._clean_text(value).casefold()
        text = re.sub(r"<system_reminder>.*?</system_reminder>", "", text)
        return re.sub(r"\W+", "", text, flags=re.UNICODE)

    @staticmethod
    def _text_features(text: str) -> set[str]:
        return retrieval_text_features(text)

    @staticmethod
    def _jaccard(left: set[str], right: set[str]) -> float:
        return jaccard_similarity(left, right)

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))


__all__ = [
    "ASSISTANT_CONTEXT_MODES",
    "RecallCandidate",
    "RecallPipeline",
    "RecallPipelineResult",
    "RecallQueryBranch",
]
