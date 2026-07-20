"""Read-only retrieval over automatically maintained Topic memories."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ..models.topic_memory import TopicMemory


@dataclass(slots=True)
class TopicRecallResult:
    topic: TopicMemory
    relevance_score: float
    final_score: float
    embedding_score: float
    keyword_score: float
    rerank_score: float | None = None
    atoms: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    context_coverage: float = 0.0
    branch_scores: dict[str, float] = field(default_factory=dict)

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
            "rerank_score": (
                round(self.rerank_score, 6)
                if self.rerank_score is not None
                else None
            ),
            "context_coverage": round(self.context_coverage, 6),
            "branch_scores": {
                key: round(value, 6) for key, value in self.branch_scores.items()
            },
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
    ):
        self.store = store
        self.embedding_provider = embedding_provider
        self.rerank_provider = rerank_provider
        self.config = config or {}

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
        scan_limit = max(
            100, min(5000, int(self.config.get("recall_scan_limit", 2000)))
        )
        if payloads is None:
            payloads = await self.store.list_topic_recall_payloads(
                memory_space_id,
                limit=scan_limit,
            )
        if not payloads or self.embedding_provider is None:
            return []

        if query_vector is None:
            query_vector = (await self._get_embeddings([query]))[0]
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
                    atoms=payload["atoms"],
                    sources=payload["sources"],
                )
            )

        candidates.sort(key=lambda item: item.final_score, reverse=True)
        candidates = candidates[: max(k, min(len(candidates), k * 4))]
        rerank_enabled = (
            bool(self.config.get("recall_use_rerank", True))
            if use_rerank is None
            else bool(use_rerank)
        )
        if rerank_enabled and self.rerank_provider is not None and candidates:
            try:
                documents = [item.content for item in candidates]
                rows = await self.rerank_provider.rerank(
                    query, documents, top_n=len(documents)
                )
                rerank_map = {
                    int(getattr(row, "index", -1)): float(
                        getattr(row, "relevance_score", 0.0)
                    )
                    for row in rows
                }
                for index, candidate in enumerate(candidates):
                    rerank_score = rerank_map.get(index)
                    if rerank_score is None or not math.isfinite(rerank_score):
                        continue
                    rerank_score = max(0.0, min(1.0, rerank_score))
                    candidate.rerank_score = rerank_score
                    candidate.relevance_score = (
                        candidate.relevance_score * 0.45 + rerank_score * 0.55
                    )
                    candidate.final_score = self._rank_score(
                        candidate.topic, candidate.relevance_score
                    )
            except Exception:
                logger.warning(
                    "[TopicRecall] Rerank 失败，本轮保留 Embedding/关键词结果",
                    exc_info=True,
                )
        candidates.sort(key=lambda item: item.final_score, reverse=True)
        return candidates[:k]

    async def _get_embeddings(self, texts: list[str]) -> list[list[float]]:
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
        if not left or len(left) != len(right):
            return 0.0
        dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
        right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
        if left_norm <= 0 or right_norm <= 0:
            return 0.0
        return dot / (left_norm * right_norm)

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


__all__ = ["TopicRecallResult", "TopicRetriever"]
