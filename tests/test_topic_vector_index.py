from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot_plugin_livingmemory.core.embedding_signature import (
    TOPIC_CENTROID_EMBEDDING_FORMAT,
    make_embedding_signature,
)
from astrbot_plugin_livingmemory.core.managers.topic_relation_builder import (
    vector_neighbor_rankings,
)
from astrbot_plugin_livingmemory.core.models.topic_memory import TopicMemory
from astrbot_plugin_livingmemory.core.topic_vector_index import (
    TopicVectorIndex,
    TopicVectorIndexCompatibilityError,
)


class _Provider:
    provider_config = {"id": "embedding-1", "model": "model-1"}


class _VectorStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def list_vector_artifacts(
        self,
        memory_space_id,
        *,
        artifact_type,
        limit,
        offset,
        status="active",
    ):
        self.calls.append((memory_space_id, artifact_type, limit, offset, status))
        return self.rows[offset : offset + limit]


@pytest.mark.asyncio
async def test_vector_index_pages_and_reuses_a_derived_namespace():
    provider = _Provider()
    signature = make_embedding_signature(
        provider,
        dimension=2,
        input_format_version=TOPIC_CENTROID_EMBEDDING_FORMAT,
        generated_at=1.0,
    )
    store = _VectorStore(
        [
            {
                "artifact_uid": "left",
                "embedding": [1.0, 0.0],
                "embedding_signature": signature,
            },
            {
                "artifact_uid": "right",
                "embedding": [0.0, 1.0],
                "embedding_signature": signature,
            },
            {
                "artifact_uid": "near-left",
                "embedding": [0.9, 0.1],
                "embedding_signature": signature,
            },
        ]
    )
    index = TopicVectorIndex(store)
    index.PAGE_SIZE = 2

    first = await index.search(
        memory_space_id="space-1",
        artifact_type="topic",
        query_vector=[1.0, 0.0],
        limit=2,
        provider=provider,
        input_format_versions={TOPIC_CENTROID_EMBEDDING_FORMAT},
    )
    second = await index.search(
        memory_space_id="space-1",
        artifact_type="topic",
        query_vector=[0.0, 1.0],
        limit=1,
        provider=provider,
        input_format_versions={TOPIC_CENTROID_EMBEDDING_FORMAT},
    )

    assert [item.artifact_uid for item in first] == ["left", "near-left"]
    assert [item.artifact_uid for item in second] == ["right"]
    assert [call[3] for call in store.calls] == [0, 2]


@pytest.mark.asyncio
async def test_vector_index_uses_separate_topic_status_namespaces():
    provider = _Provider()
    signature = make_embedding_signature(
        provider,
        dimension=2,
        input_format_version=TOPIC_CENTROID_EMBEDDING_FORMAT,
        generated_at=1.0,
    )

    class _StatusStore(_VectorStore):
        async def list_vector_artifacts(
            self,
            memory_space_id,
            *,
            artifact_type,
            limit,
            offset,
            status="active",
        ):
            self.calls.append((memory_space_id, artifact_type, limit, offset, status))
            uid = "archived" if status == "archived" else "active"
            return [] if offset else [{
                "artifact_uid": uid,
                "embedding": [1.0, 0.0],
                "embedding_signature": signature,
            }]

    store = _StatusStore([])
    index = TopicVectorIndex(store)
    active = await index.search(
        memory_space_id="space-1",
        artifact_type="topic",
        query_vector=[1.0, 0.0],
        limit=1,
        provider=provider,
        input_format_versions={TOPIC_CENTROID_EMBEDDING_FORMAT},
    )
    archived = await index.search(
        memory_space_id="space-1",
        artifact_type="topic",
        query_vector=[1.0, 0.0],
        limit=1,
        provider=provider,
        input_format_versions={TOPIC_CENTROID_EMBEDDING_FORMAT},
        artifact_status="archived",
    )

    assert [item.artifact_uid for item in active] == ["active"]
    assert [item.artifact_uid for item in archived] == ["archived"]
    assert [call[4] for call in store.calls] == ["active", "archived"]


@pytest.mark.asyncio
async def test_vector_index_rejects_unsigned_artifacts():
    store = _VectorStore(
        [
            {
                "artifact_uid": "legacy",
                "embedding": [1.0, 0.0],
                "embedding_signature": {},
            }
        ]
    )
    index = TopicVectorIndex(store)

    with pytest.raises(
        TopicVectorIndexCompatibilityError,
        match="missing_signature",
    ):
        await index.search(
            memory_space_id="space-1",
            artifact_type="topic",
            query_vector=[1.0, 0.0],
            limit=1,
            provider=_Provider(),
            input_format_versions={TOPIC_CENTROID_EMBEDDING_FORMAT},
        )


def test_relation_candidates_are_limited_to_vector_neighbors():
    topics = [
        TopicMemory(
            topic_uid="left",
            memory_space_id="space-1",
            title="Left",
            summary="Left summary",
            metadata={"embedding": [1.0, 0.0]},
        ),
        TopicMemory(
            topic_uid="near-left",
            memory_space_id="space-1",
            title="Near left",
            summary="Near left summary",
            metadata={"embedding": [0.9, 0.1]},
        ),
        TopicMemory(
            topic_uid="right",
            memory_space_id="space-1",
            title="Right",
            summary="Right summary",
            metadata={"embedding": [0.0, 1.0]},
        ),
    ]

    rankings = vector_neighbor_rankings(
        topics,
        candidate_limit=1,
        similarity_threshold=0.5,
    )

    assert rankings["left"][0][1] == "near-left"
    assert rankings["near-left"][0][1] == "left"
    assert rankings["right"] == []
