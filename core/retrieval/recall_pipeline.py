"""Shared production/test pipeline for structured Timeline recall."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .hybrid_retriever import HybridResult


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.result.doc_id,
            "fused_score": round(self.fused_score, 6),
            "relevance_score": round(self.relevance_score, 6),
            "selected": self.selected,
            "filter_reason": self.filter_reason,
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

    def diagnostics(self) -> dict[str, Any]:
        return {
            "query_branches": [item.to_dict() for item in self.branches],
            "candidate_limit": self.candidate_limit,
            "final_limit": self.final_limit,
            "applied_threshold": round(self.applied_threshold, 6),
            "candidate_count": len(self.candidates),
            "selected_count": len(self.results),
            "overlap_suppressed": self.overlap_suppressed,
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
        )[-4:]
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
    ) -> RecallPipelineResult:
        final_limit = max(0, int(final_k))
        branches = self.build_query_branches(
            current_query,
            recent_messages,
            expansion_enabled=expansion_enabled,
            assistant_mode=assistant_mode,
        )
        if final_limit <= 0 or not branches:
            return RecallPipelineResult([], branches, [], 0, final_limit, 0.0)

        multiplier = max(1, int(self._config("candidate_multiplier", 3)))
        candidate_limit = min(50, max(final_limit, final_limit * multiplier))
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

        candidates = sorted(
            candidate_map.values(), key=lambda item: item.fused_score, reverse=True
        )
        suppress_overlap = bool(
            self._config("context_overlap_suppression", True)
        )
        non_overlapping: list[RecallCandidate] = []
        overlap_suppressed = 0
        for candidate in candidates:
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
        applied_threshold = max(min_relevance, best_relevance * relative_floor)

        eligible: list[RecallCandidate] = []
        for candidate in non_overlapping:
            if candidate.relevance_score < min_relevance:
                candidate.filter_reason = "below_min_relevance"
                continue
            if candidate.relevance_score < applied_threshold:
                candidate.filter_reason = "below_relative_floor"
                continue
            eligible.append(candidate)

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
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = cls._clean_text(message.get("content"))
            if content:
                normalized.append({"role": role, "content": content})
        if (
            normalized
            and normalized[-1]["role"] == "user"
            and cls._comparable_text(normalized[-1]["content"])
            == cls._comparable_text(current_query)
        ):
            normalized.pop()
        return normalized

    @staticmethod
    def _unique_role_parts(
        messages: list[dict[str, str]], role: str
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
        normalized = str(text or "").casefold()
        latin = set(re.findall(r"[a-z0-9_]{2,}", normalized))
        chinese_chunks = re.findall(r"[\u4e00-\u9fff]+", normalized)
        chinese: set[str] = set()
        for chunk in chinese_chunks:
            if len(chunk) == 1:
                chinese.add(chunk)
            else:
                chinese.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
        return latin | chinese or {"<empty>"}

    @staticmethod
    def _jaccard(left: set[str], right: set[str]) -> float:
        return len(left & right) / max(1, len(left | right))

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
