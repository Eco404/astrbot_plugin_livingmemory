"""Read-only retrieval over automatically maintained Topic memories."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from astrbot.api import logger

from ..embedding_signature import (
    SUPPORTED_TOPIC_EMBEDDING_FORMATS,
    TOPIC_FRAGMENT_EMBEDDING_FORMAT,
    signature_mismatch_reason,
)
from ..models.topic_memory import TopicFragmentDraft, TopicMemory
from ..topic_vector_index import (
    TopicVectorIndex,
    TopicVectorIndexCompatibilityError,
)
from ..topic_similarity import cosine_similarity


class TopicEmbeddingCompatibilityError(RuntimeError):
    """Stored Topic vectors do not match the active Embedding Provider."""

    def __init__(self, artifact: str, reason: str, count: int = 1):
        self.artifact = artifact
        self.reason = reason
        self.count = max(1, int(count))
        labels = {
            "missing_signature": "旧向量没有模型签名",
            "provider_changed": "Embedding Provider 已变更",
            "model_changed": "Embedding 模型已变更",
            "dimension_changed": "向量维度已变更",
            "invalid_dimension": "向量维度无效",
            "input_format_changed": "向量输入格式已升级",
            "missing_embedding": "向量数据缺失",
        }
        detail = labels.get(reason, reason)
        super().__init__(
            f"Topic {artifact}向量不可用：{detail}（{self.count} 项）。"
            "请在 Topic 记忆的维护面板执行“重新向量化并重算关系”。"
        )


@dataclass(slots=True)
class TopicRecallResult:
    topic: TopicMemory
    relevance_score: float
    final_score: float
    embedding_score: float
    keyword_score: float
    rerank_score: float | None = None
    base_relevance_score: float | None = None
    atoms: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    actors: list[dict[str, Any]] = field(default_factory=list)
    matched_actor_ids: list[str] = field(default_factory=list)
    actor_match_boost: float = 0.0
    context_coverage: float = 0.0
    branch_scores: dict[str, float] = field(default_factory=dict)
    current_relevance: float | None = None
    context_support: float = 0.0
    ranking_score: float | None = None
    rerank_rank: int | None = None
    rerank_percentile: float | None = None
    rerank_confidence: float = 0.0
    rerank_rank_boost: float = 0.0
    selected: bool = False
    filter_reason: str | None = None

    @property
    def topic_uid(self) -> str:
        return self.topic.topic_uid

    @property
    def content(self) -> str:
        return f"{self.topic.title}\n{self.topic.summary}".strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_uid": self.topic_uid,
            "title": self.topic.title,
            "relevance_score": round(self.relevance_score, 6),
            "final_score": round(self.final_score, 6),
            "embedding_score": round(self.embedding_score, 6),
            "keyword_score": round(self.keyword_score, 6),
            "base_relevance_score": round(
                self.base_relevance_score
                if self.base_relevance_score is not None
                else self.relevance_score,
                6,
            ),
            "current_relevance": round(
                self.current_relevance
                if self.current_relevance is not None
                else self.relevance_score,
                6,
            ),
            "context_support": round(self.context_support, 6),
            "ranking_score": round(
                self.ranking_score
                if self.ranking_score is not None
                else self.final_score,
                6,
            ),
            "rerank_score": (
                round(self.rerank_score, 6)
                if self.rerank_score is not None
                else None
            ),
            "rerank_rank": self.rerank_rank,
            "rerank_percentile": (
                round(self.rerank_percentile, 6)
                if self.rerank_percentile is not None
                else None
            ),
            "rerank_confidence": round(self.rerank_confidence, 6),
            "rerank_rank_boost": round(self.rerank_rank_boost, 6),
            "context_coverage": round(self.context_coverage, 6),
            "branch_scores": {
                key: round(value, 6) for key, value in self.branch_scores.items()
            },
            "matched_actor_ids": self.matched_actor_ids,
            "actor_match_boost": round(self.actor_match_boost, 6),
            "selected": self.selected,
            "filter_reason": self.filter_reason,
        }


@dataclass(slots=True)
class TopicFragmentRecallResult:
    fragment: TopicFragmentDraft
    topic_uid: str
    relevance_score: float
    final_score: float
    embedding_score: float
    keyword_score: float
    parent_topic_relevance: float
    rerank_score: float | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    context_coverage: float = 0.0
    branch_scores: dict[str, float] = field(default_factory=dict)
    current_relevance: float | None = None
    context_support: float = 0.0
    ranking_score: float | None = None
    rerank_rank: int | None = None
    rerank_percentile: float | None = None
    rerank_confidence: float = 0.0
    rerank_rank_boost: float = 0.0
    body_suppressed: bool = False
    selected: bool = False
    filter_reason: str | None = None

    @property
    def fragment_uid(self) -> str:
        return self.fragment.fragment_uid

    @property
    def fact_contents(self) -> list[str]:
        facts: list[str] = []
        seen: set[str] = set()
        for fact in self.fragment.facts:
            content = str(fact.get("content") or "").strip()
            if not content or content in seen:
                continue
            seen.add(content)
            facts.append(content)
        return facts

    @property
    def body_content(self) -> str:
        return "\n".join(
            value
            for value in (
                f"Topic 片段: {self.fragment.label}",
                self.fragment.summary,
            )
            if value
        ).strip()

    @property
    def content(self) -> str:
        facts = self.fact_contents
        lines = [] if self.body_suppressed else [self.body_content]
        if facts:
            label = "Topic 片段补充事实" if self.body_suppressed else "关键事实"
            lines.append(f"{label}: " + "；".join(facts))
        return "\n".join(value for value in lines if value).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fragment_uid": self.fragment_uid,
            "topic_uid": self.topic_uid,
            "label": self.fragment.label,
            "relevance_score": round(self.relevance_score, 6),
            "final_score": round(self.final_score, 6),
            "current_relevance": round(
                self.current_relevance
                if self.current_relevance is not None
                else self.relevance_score,
                6,
            ),
            "context_support": round(self.context_support, 6),
            "ranking_score": round(
                self.ranking_score
                if self.ranking_score is not None
                else self.final_score,
                6,
            ),
            "embedding_score": round(self.embedding_score, 6),
            "keyword_score": round(self.keyword_score, 6),
            "parent_topic_relevance": round(self.parent_topic_relevance, 6),
            "rerank_score": (
                round(self.rerank_score, 6)
                if self.rerank_score is not None
                else None
            ),
            "rerank_rank": self.rerank_rank,
            "rerank_percentile": (
                round(self.rerank_percentile, 6)
                if self.rerank_percentile is not None
                else None
            ),
            "rerank_confidence": round(self.rerank_confidence, 6),
            "rerank_rank_boost": round(self.rerank_rank_boost, 6),
            "body_suppressed": self.body_suppressed,
            "fact_count": len(self.fact_contents),
            "context_coverage": round(self.context_coverage, 6),
            "branch_scores": {
                key: round(value, 6) for key, value in self.branch_scores.items()
            },
            "selected": self.selected,
            "filter_reason": self.filter_reason,
        }


class TopicRetriever:
    """Search active Topics using their stored vectors and optional reranking."""

    def __init__(
        self,
        store,
        *,
        embedding_provider: Any,
        rerank_provider: Any = None,
        config: dict[str, Any] | None = None,
        provider_resolver: Callable[[], dict[str, Any]] | None = None,
        vector_index: TopicVectorIndex | None = None,
    ):
        self.store = store
        self.embedding_provider = embedding_provider
        self.rerank_provider = rerank_provider
        self.config = config or {}
        self.provider_resolver = provider_resolver
        self.vector_index = vector_index

    def refresh_providers(self) -> None:
        if self.provider_resolver is None:
            return
        resolved = self.provider_resolver()
        if "embedding_provider" in resolved:
            self.embedding_provider = resolved["embedding_provider"]
        if "rerank_provider" in resolved:
            self.rerank_provider = resolved["rerank_provider"]

    async def search(
        self,
        query: str,
        *,
        memory_space_id: str,
        k: int,
        use_rerank: bool | None = None,
        payloads: list[dict[str, Any]] | None = None,
        query_vector: list[float] | None = None,
    ) -> list[TopicRecallResult]:
        query = str(query or "").strip()
        if not query or not memory_space_id or k <= 0:
            return []
        self.refresh_providers()
        if self.embedding_provider is None:
            return []
        if query_vector is None:
            query_vector = (await self._get_embeddings([query]))[0]
        if payloads is None:
            if self.vector_index is not None:
                candidate_limit = max(
                    k,
                    min(
                        512,
                        k * max(4, int(self.config.get("recall_candidate_multiplier", 8))),
                    ),
                )
                try:
                    hits = await self.vector_index.search(
                        memory_space_id=memory_space_id,
                        artifact_type="topic",
                        query_vector=query_vector,
                        limit=candidate_limit,
                        provider=self.embedding_provider,
                        input_format_versions=SUPPORTED_TOPIC_EMBEDDING_FORMATS,
                    )
                except TopicVectorIndexCompatibilityError as exc:
                    raise TopicEmbeddingCompatibilityError(
                        "记忆", exc.reason, exc.incompatible_count
                    ) from exc
                payloads = await self.store.list_topic_recall_payloads(
                    memory_space_id,
                    topic_uids=[hit.artifact_uid for hit in hits],
                )
            else:
                scan_limit = max(
                    100, min(5000, int(self.config.get("recall_scan_limit", 2000)))
                )
                payloads = await self.store.list_topic_recall_payloads(
                    memory_space_id,
                    limit=scan_limit,
                )
        if not payloads:
            return []
        self.validate_topic_payload_embeddings(payloads, query_vector)
        query_features = self._text_features(query)
        candidates: list[TopicRecallResult] = []
        for payload in payloads:
            topic = payload["topic"]
            metadata = topic.metadata if isinstance(topic.metadata, dict) else {}
            embedding = metadata.get("embedding") or []
            if not embedding:
                continue
            embedding_score = max(0.0, self._cosine(query_vector, embedding))
            topic_text = "\n".join(
                [
                    topic.title,
                    topic.summary,
                    " ".join(str(item) for item in metadata.get("keywords", [])),
                    " ".join(str(item.get("content") or "") for item in payload["atoms"]),
                ]
            )
            topic_features = self._text_features(topic_text)
            keyword_score = len(query_features & topic_features) / max(
                1, len(query_features)
            )
            relevance = min(1.0, embedding_score * 0.82 + keyword_score * 0.18)
            candidates.append(
                TopicRecallResult(
                    topic=topic,
                    relevance_score=relevance,
                    final_score=self._rank_score(topic, relevance),
                    embedding_score=embedding_score,
                    keyword_score=keyword_score,
                    base_relevance_score=relevance,
                    current_relevance=relevance,
                    ranking_score=self._rank_score(topic, relevance),
                    atoms=payload["atoms"],
                    sources=payload["sources"],
                    actors=payload.get("actors", []),
                )
            )

        candidates.sort(key=lambda item: item.final_score, reverse=True)
        candidates = candidates[: max(k, min(len(candidates), k * 4))]
        rerank_enabled = (
            bool(self.config.get("recall_use_rerank", True))
            if use_rerank is None
            else bool(use_rerank)
        )
        rerank_weight = self._rerank_weight()
        if (
            rerank_enabled
            and rerank_weight > 0.0
            and self.rerank_provider is not None
            and candidates
        ):
            try:
                rerank_confidence = await self.assign_rerank_ranks(query, candidates)
                for candidate in candidates:
                    if candidate.rerank_percentile is None:
                        continue
                    candidate.rerank_rank_boost = self._rerank_rank_boost(
                        candidate.rerank_percentile,
                        rerank_confidence,
                    )
                    candidate.final_score = min(
                        1.0,
                        candidate.final_score + candidate.rerank_rank_boost,
                    )
                    candidate.ranking_score = candidate.final_score
            except Exception:
                logger.warning(
                    "[TopicRecall] Rerank 失败，本轮保留 Embedding/关键词结果",
                    exc_info=True,
                )
        candidates.sort(key=lambda item: item.final_score, reverse=True)
        return candidates[:k]

    def validate_topic_payload_embeddings(
        self,
        payloads: list[dict[str, Any]],
        query_vector: list[float],
    ) -> None:
        reasons: dict[str, int] = {}
        for payload in payloads:
            topic = payload["topic"]
            embedding = (
                topic.metadata.get("embedding", [])
                if isinstance(topic.metadata, dict)
                else []
            )
            reason = (
                "missing_embedding"
                if not embedding
                else signature_mismatch_reason(
                    topic.embedding_signature,
                    self.embedding_provider,
                    expected_formats=SUPPORTED_TOPIC_EMBEDDING_FORMATS,
                    dimension=len(query_vector),
                )
            )
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        if reasons:
            reason, count = max(reasons.items(), key=lambda item: item[1])
            raise TopicEmbeddingCompatibilityError("记忆", reason, count)

    def validate_fragment_embeddings(
        self,
        fragments: list[TopicFragmentDraft],
        query_vector: list[float],
    ) -> None:
        reasons: dict[str, int] = {}
        for fragment in fragments:
            reason = (
                "missing_embedding"
                if not fragment.embedding
                else signature_mismatch_reason(
                    fragment.embedding_signature,
                    self.embedding_provider,
                    expected_formats={TOPIC_FRAGMENT_EMBEDDING_FORMAT},
                    dimension=len(query_vector),
                )
            )
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        if reasons:
            reason, count = max(reasons.items(), key=lambda item: item[1])
            raise TopicEmbeddingCompatibilityError("片段", reason, count)

    async def assign_rerank_ranks(self, query: str, candidates: list[Any]) -> float:
        """Attach provider scores and relative ranks without changing relevance."""
        if not candidates or self.rerank_provider is None:
            return 0.0
        documents = [item.content for item in candidates]
        rows = await self.rerank_provider.rerank(
            query,
            documents,
            top_n=len(documents),
        )
        ranked_rows: list[tuple[int, float]] = []
        seen: set[int] = set()
        for row in rows:
            try:
                index = int(getattr(row, "index", -1))
                score = float(getattr(row, "relevance_score", 0.0))
            except (TypeError, ValueError):
                continue
            if index in seen or not 0 <= index < len(candidates):
                continue
            if not math.isfinite(score):
                continue
            seen.add(index)
            ranked_rows.append((index, score))
        ranked_rows.sort(key=lambda item: item[1], reverse=True)
        total = len(ranked_rows)
        if not total:
            return 0.0
        confidence = self._rerank_result_confidence(
            [score for _, score in ranked_rows]
        )
        previous_score: float | None = None
        previous_rank = 1
        for position, (index, score) in enumerate(ranked_rows, 1):
            rank = (
                previous_rank
                if previous_score is not None
                and math.isclose(score, previous_score, rel_tol=1e-9, abs_tol=1e-12)
                else position
            )
            candidate = candidates[index]
            candidate.rerank_score = score
            candidate.rerank_rank = rank
            candidate.rerank_percentile = (
                0.0 if total == 1 else 1.0 - (rank - 1) / (total - 1)
            )
            candidate.rerank_confidence = confidence
            previous_score = score
            previous_rank = rank
        return confidence

    def _rerank_weight(self) -> float:
        return max(
            0.0,
            min(1.0, float(self.config.get("recall_rerank_weight", 0.35))),
        )

    def _rerank_rank_boost(
        self,
        percentile: float,
        confidence: float = 1.0,
    ) -> float:
        return (
            min(0.15, self._rerank_weight() * 0.15)
            * max(0.0, min(1.0, float(percentile)))
            * max(0.0, min(1.0, float(confidence)))
        )

    def _rerank_result_confidence(self, scores: list[float]) -> float:
        """Estimate whether one rerank response has enough signal to affect order."""
        if len(scores) <= 1:
            return 0.0
        ordered = sorted((float(score) for score in scores), reverse=True)
        midpoint = len(ordered) // 2
        median = (
            ordered[midpoint]
            if len(ordered) % 2
            else (ordered[midpoint - 1] + ordered[midpoint]) / 2.0
        )
        top = ordered[0]
        if 0.0 <= ordered[-1] and top <= 1.0:
            contrast = max(0.0, min(1.0, (top - median) / 0.15))
            return math.sqrt(max(0.0, top) * contrast)
        scale = max(1.0, abs(top), abs(ordered[-1]))
        return max(0.0, min(1.0, (top - median) / scale))

    async def _get_embeddings(self, texts: list[str]) -> list[list[float]]:
        self.refresh_providers()
        provider = self.embedding_provider
        get_embeddings = getattr(provider, "get_embeddings", None)
        if callable(get_embeddings):
            return await get_embeddings(texts)
        get_batch = getattr(provider, "get_embeddings_batch", None)
        if callable(get_batch):
            try:
                return await get_batch(texts, batch_size=len(texts), tasks_limit=1)
            except TypeError:
                return await get_batch(texts)
        return [await provider.get_embedding(text) for text in texts]

    def _rank_score(self, topic: TopicMemory, relevance: float) -> float:
        importance = self._effective_importance(topic)
        return min(
            1.0,
            relevance * 0.82
            + importance * 0.10
            + max(0.0, min(1.0, float(topic.confidence))) * 0.08,
        )

    def _effective_importance(self, topic: TopicMemory) -> float:
        rate = max(0.0, float(self.config.get("recall_decay_rate", 0.01)))
        anchor = max(
            float(topic.decay_anchor_at or topic.updated_at or topic.created_at),
            float(topic.last_accessed_at or 0.0),
        )
        age_days = max(0.0, (time.time() - anchor) / 86400.0)
        access_resistance = 1.0 + math.log1p(max(0, topic.access_count)) * 0.25
        decayed = float(topic.base_importance) * math.exp(
            -rate * age_days / access_resistance
        )
        return max(0.0, min(1.0, decayed))

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        return cosine_similarity(left, right)

    @staticmethod
    def _text_features(text: str) -> set[str]:
        normalized = str(text or "").casefold()
        features = set(re.findall(r"[a-z0-9_]{2,}", normalized))
        for chunk in re.findall(r"[\u4e00-\u9fff]+", normalized):
            if len(chunk) == 1:
                features.add(chunk)
            else:
                features.update(
                    chunk[index : index + 2] for index in range(len(chunk) - 1)
                )
        return features or {"<empty>"}


__all__ = [
    "TopicEmbeddingCompatibilityError",
    "TopicFragmentRecallResult",
    "TopicRecallResult",
    "TopicRetriever",
]
