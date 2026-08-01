"""Semantic boundary detection for pending Timeline conversation windows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..embedding_signature import provider_dimension, provider_identity
from ..models.conversation_models import Message
from ..topic_similarity import cosine_similarity

PENDING_MESSAGE_EMBEDDING_FORMAT = "pending-message-query-v1"
CONTINUATION_SIMILARITY_THRESHOLD = 0.68
TOPIC_CENTER_SIMILARITY_THRESHOLD = 0.72


@dataclass(slots=True)
class DialogueUnit:
    messages: list[Message]
    start_offset: int
    end_offset: int
    anchor_message: Message
    text: str


@dataclass(slots=True)
class ContinuationDecision:
    action: str
    unit_count: int
    summary_end_offset: int
    reason: str
    provisional_topic_count: int = 0
    max_similarity: float | None = None


def build_dialogue_units(messages: list[Message]) -> list[DialogueUnit]:
    """Build user-led dialogue units while preserving proactive Bot messages."""
    groups: list[tuple[int, list[Message]]] = []
    current: list[Message] = []
    current_start = 0
    for offset, message in enumerate(messages):
        if message.role == "user":
            if current:
                groups.append((current_start, current))
            current_start = offset
            current = [message]
        else:
            if not current:
                current_start = offset
            current.append(message)
    if current:
        groups.append((current_start, current))

    units: list[DialogueUnit] = []
    for start, group in groups:
        anchor = next((item for item in group if item.role == "user"), group[0])
        text = Message.content_to_text(anchor.content).strip()
        if not text:
            text = "\n".join(
                Message.content_to_text(item.content).strip()
                for item in group
                if Message.content_to_text(item.content).strip()
            )
        units.append(
            DialogueUnit(
                messages=group,
                start_offset=start,
                end_offset=start + len(group),
                anchor_message=anchor,
                text=text,
            )
        )
    return units


class TimelineTopicContinuationEvaluator:
    """Reuse pending query embeddings to delay fixed-round summaries safely."""

    def __init__(self, store: Any, provider_resolver: Callable[[], Any]) -> None:
        self.store = store
        self.provider_resolver = provider_resolver

    async def evaluate(
        self,
        messages: list[Message],
        *,
        base_rounds: int,
        force_rounds: int,
    ) -> ContinuationDecision:
        units = build_dialogue_units(messages)
        count = len(units)
        if count < base_rounds:
            return ContinuationDecision("insufficient", count, 0, "below_base_rounds")
        if count >= force_rounds:
            return ContinuationDecision(
                "summarize", count, len(messages), "force_summary_limit"
            )

        provider = self.provider_resolver()
        if provider is None:
            return ContinuationDecision(
                "summarize", count, len(messages), "embedding_unavailable_fallback"
            )
        try:
            vectors = await self._vectors(units, provider)
        except Exception:
            return ContinuationDecision(
                "summarize", count, len(messages), "embedding_failed_fallback"
            )
        if len(vectors) != count or any(not vector for vector in vectors):
            return ContinuationDecision(
                "summarize", count, len(messages), "embedding_invalid_fallback"
            )

        base_vectors = vectors[:base_rounds]
        topic_count = self._provisional_topic_count(base_vectors)
        if count == base_rounds:
            return ContinuationDecision(
                "continue", count, 0, "base_window_ready", topic_count
            )

        latest = units[-1]
        # Every post-base unit was admitted only after matching a seed topic, so
        # it may serve as a newer expression of that topic without creating a
        # new cluster center.
        similarities = [
            cosine_similarity(vectors[-1], vector) for vector in vectors[:-1]
        ]
        best = max(similarities, default=0.0)
        if best >= CONTINUATION_SIMILARITY_THRESHOLD:
            return ContinuationDecision(
                "continue", count, 0, "matched_existing_topic", topic_count, best
            )
        return ContinuationDecision(
            "summarize",
            count,
            latest.start_offset,
            "new_topic_boundary",
            topic_count,
            best,
        )

    async def _vectors(
        self, units: list[DialogueUnit], provider: Any
    ) -> list[list[float]]:
        message_ids = [int(unit.anchor_message.id or 0) for unit in units]
        stored = await self.store.get_pending_message_features(message_ids)
        provider_id, model_id = provider_identity(provider)
        expected_dimension = provider_dimension(provider)
        vectors: list[list[float] | None] = [None] * len(units)
        missing_indexes: list[int] = []
        for index, unit in enumerate(units):
            item = stored.get(int(unit.anchor_message.id or 0))
            vector = list(item.get("embedding") or []) if item else []
            compatible = bool(
                item
                and item.get("text_hash")
                == self.store.pending_feature_text_hash(unit.text)
                and item.get("input_format_version")
                == PENDING_MESSAGE_EMBEDDING_FORMAT
                and item.get("provider_id") == provider_id
                and item.get("model_id") == model_id
                and int(item.get("dimension") or 0) == len(vector)
                and (expected_dimension <= 0 or len(vector) == expected_dimension)
            )
            if compatible:
                vectors[index] = vector
            else:
                missing_indexes.append(index)

        if missing_indexes:
            generated = await self._embed(
                provider, [units[index].text for index in missing_indexes]
            )
            if len(generated) != len(missing_indexes):
                raise ValueError("Embedding provider returned an unexpected batch size")
            for index, vector in zip(missing_indexes, generated, strict=True):
                normalized = [float(value) for value in vector]
                vectors[index] = normalized
                message_id = int(units[index].anchor_message.id or 0)
                if message_id > 0:
                    await self.store.upsert_pending_message_feature(
                        message_id=message_id,
                        session_id=units[index].anchor_message.session_id,
                        text=units[index].text,
                        embedding=normalized,
                        provider_id=provider_id,
                        model_id=model_id,
                        input_format_version=PENDING_MESSAGE_EMBEDDING_FORMAT,
                    )
        return [list(vector or []) for vector in vectors]

    @staticmethod
    async def _embed(provider: Any, texts: list[str]) -> list[list[float]]:
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

    @staticmethod
    def _provisional_topic_count(vectors: list[list[float]]) -> int:
        centers: list[list[float]] = []
        for vector in vectors:
            if not centers or max(
                cosine_similarity(vector, center) for center in centers
            ) < TOPIC_CENTER_SIMILARITY_THRESHOLD:
                centers.append(vector)
        return len(centers)

__all__ = [
    "PENDING_MESSAGE_EMBEDDING_FORMAT",
    "ContinuationDecision",
    "DialogueUnit",
    "TimelineTopicContinuationEvaluator",
    "build_dialogue_units",
]
