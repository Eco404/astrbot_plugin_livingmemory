"""Derived in-memory vector indexes for Topic and formal-fragment artifacts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any

import faiss
import numpy as np

from .embedding_signature import signature_mismatch_reason


class TopicVectorIndexCompatibilityError(RuntimeError):
    """Stored vectors cannot be queried with the active embedding provider."""

    def __init__(self, reason: str, incompatible_count: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.incompatible_count = max(0, int(incompatible_count))


@dataclass(frozen=True, slots=True)
class TopicVectorHit:
    artifact_uid: str
    score: float


@dataclass(slots=True)
class _VectorNamespace:
    index: Any
    id_to_uid: dict[int, str]
    dimension: int


class TopicVectorIndex:
    """Lazy derived index. SQLite remains authoritative; this cache is disposable."""

    PAGE_SIZE = 512

    def __init__(self, store: Any):
        self.store = store
        self._namespaces: dict[tuple[str, str, str, str], _VectorNamespace] = {}
        self._locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}

    def invalidate(self, memory_space_id: str | None = None, artifact_type: str | None = None) -> None:
        for key in list(self._namespaces):
            if memory_space_id is not None and key[0] != memory_space_id:
                continue
            if artifact_type is not None and key[1] != artifact_type:
                continue
            self._namespaces.pop(key, None)

    async def search(
        self,
        *,
        memory_space_id: str,
        artifact_type: str,
        query_vector: list[float],
        limit: int,
        provider: Any,
        input_format_versions: Iterable[str],
        artifact_status: str | None = "active",
    ) -> list[TopicVectorHit]:
        if not query_vector or limit <= 0:
            return []
        expected_formats = {str(value) for value in input_format_versions}
        provider_key = self._provider_key(provider, expected_formats)
        status_key = "*" if artifact_status is None else str(artifact_status)
        key = (str(memory_space_id), str(artifact_type), status_key, provider_key)
        namespace = self._namespaces.get(key)
        if namespace is None:
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                namespace = self._namespaces.get(key)
                if namespace is None:
                    namespace = await self._load_namespace(
                        memory_space_id=str(memory_space_id),
                        artifact_type=str(artifact_type),
                        provider=provider,
                        input_format_versions=expected_formats,
                        artifact_status=artifact_status,
                    )
                    self._namespaces[key] = namespace
        if namespace.dimension <= 0:
            return []
        if len(query_vector) != namespace.dimension:
            raise TopicVectorIndexCompatibilityError(
                "stored vector dimension differs from the active embedding model",
                1,
            )
        matrix = np.asarray([query_vector], dtype="float32")
        faiss.normalize_L2(matrix)
        scores, ids = namespace.index.search(matrix, min(limit, len(namespace.id_to_uid)))
        hits: list[TopicVectorHit] = []
        for score, internal_id in zip(scores[0], ids[0], strict=True):
            uid = namespace.id_to_uid.get(int(internal_id))
            if uid is not None:
                hits.append(TopicVectorHit(uid, float(score)))
        return hits

    async def _load_namespace(
        self,
        *,
        memory_space_id: str,
        artifact_type: str,
        provider: Any,
        input_format_versions: set[str],
        artifact_status: str | None,
    ) -> _VectorNamespace:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page_kwargs: dict[str, Any] = {
                "artifact_type": artifact_type,
                "limit": self.PAGE_SIZE,
                "offset": offset,
            }
            if artifact_type == "topic" and artifact_status != "active":
                page_kwargs["status"] = artifact_status
            page = await self.store.list_vector_artifacts(
                memory_space_id,
                **page_kwargs,
            )
            rows.extend(page)
            if len(page) < self.PAGE_SIZE:
                break
            offset += len(page)
        incompatible: dict[str, int] = {}
        vectors: list[list[float]] = []
        uids: list[str] = []
        dimension = 0
        for row in rows:
            vector = [float(value) for value in row.get("embedding", [])]
            if not vector:
                incompatible["missing_embedding"] = incompatible.get(
                    "missing_embedding", 0
                ) + 1
                continue
            reason = signature_mismatch_reason(
                row.get("embedding_signature"),
                provider,
                expected_formats=input_format_versions,
                dimension=len(vector),
            )
            if reason:
                incompatible[reason] = incompatible.get(reason, 0) + 1
                continue
            if dimension and len(vector) != dimension:
                incompatible["dimension_changed"] = incompatible.get(
                    "dimension_changed", 0
                ) + 1
                continue
            dimension = dimension or len(vector)
            vectors.append(vector)
            uids.append(str(row["artifact_uid"]))
        if incompatible:
            reason, count = max(incompatible.items(), key=lambda item: item[1])
            raise TopicVectorIndexCompatibilityError(
                reason,
                count,
            )
        if not vectors:
            return _VectorNamespace(faiss.IndexIDMap2(faiss.IndexFlatIP(1)), {}, 0)
        matrix = np.asarray(vectors, dtype="float32")
        faiss.normalize_L2(matrix)
        ids = np.arange(len(uids), dtype="int64")
        index = faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
        index.add_with_ids(matrix, ids)
        return _VectorNamespace(index, dict(enumerate(uids)), dimension)

    @staticmethod
    def _provider_key(provider: Any, input_format_versions: set[str]) -> str:
        provider_id, model_id = TopicVectorIndex._provider_identity(provider)
        formats = ",".join(sorted(input_format_versions))
        return f"{provider_id}\x1f{model_id}\x1f{formats}"

    @staticmethod
    def _provider_identity(provider: Any) -> tuple[str, str]:
        config = getattr(provider, "provider_config", {}) or {}
        provider_id = str(config.get("id") or getattr(provider, "id", "") or "").strip()
        model_id = str(
            config.get("model")
            or config.get("model_name")
            or getattr(provider, "model", "")
            or ""
        ).strip()
        return provider_id, model_id


__all__ = [
    "TopicVectorHit",
    "TopicVectorIndex",
    "TopicVectorIndexCompatibilityError",
]
