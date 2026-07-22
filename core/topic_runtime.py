"""Immutable per-task runtime state for Topic construction."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .embedding_signature import provider_dimension, provider_identity


@dataclass(frozen=True, slots=True)
class TopicProviderSignature:
    provider_id: str
    model_id: str
    dimension: int = 0

    @classmethod
    def from_provider(
        cls,
        provider: Any,
        *,
        include_dimension: bool = False,
    ) -> "TopicProviderSignature":
        provider_id, model_id = provider_identity(provider)
        return cls(
            provider_id=provider_id,
            model_id=model_id,
            dimension=provider_dimension(provider) if include_dimension else 0,
        )


@dataclass(frozen=True, slots=True)
class TopicBuildRunContext:
    memory_space_id: str
    run_uid: str
    config: Mapping[str, Any]
    llm_provider: Any
    embedding_provider: Any
    rerank_provider: Any
    llm_signature: TopicProviderSignature
    embedding_signature: TopicProviderSignature
    rerank_signature: TopicProviderSignature
    llm_concurrency: int
    rerank_concurrency: int
    llm_semaphore: asyncio.Semaphore
    rerank_semaphore: asyncio.Semaphore

    @classmethod
    def create(
        cls,
        *,
        memory_space_id: str,
        run_uid: str,
        config: dict[str, Any],
        llm_provider: Any,
        embedding_provider: Any,
        rerank_provider: Any,
    ) -> "TopicBuildRunContext":
        snapshot = MappingProxyType(copy.deepcopy(config))
        llm_concurrency = max(1, min(64, int(snapshot.get("llm_concurrency", 1))))
        rerank_concurrency = max(
            1, min(32, int(snapshot.get("rerank_concurrency", 1)))
        )
        return cls(
            memory_space_id=memory_space_id,
            run_uid=run_uid,
            config=snapshot,
            llm_provider=llm_provider,
            embedding_provider=embedding_provider,
            rerank_provider=rerank_provider,
            llm_signature=TopicProviderSignature.from_provider(llm_provider),
            embedding_signature=TopicProviderSignature.from_provider(
                embedding_provider,
                include_dimension=True,
            ),
            rerank_signature=TopicProviderSignature.from_provider(rerank_provider),
            llm_concurrency=llm_concurrency,
            rerank_concurrency=rerank_concurrency,
            llm_semaphore=asyncio.Semaphore(llm_concurrency),
            rerank_semaphore=asyncio.Semaphore(rerank_concurrency),
        )


__all__ = ["TopicBuildRunContext", "TopicProviderSignature"]
