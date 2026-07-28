"""Vector-neighbor candidate generation for the related Topic graph."""

from __future__ import annotations

from typing import Any

import faiss
import numpy as np

from ..models.topic_memory import TopicMemory


def vector_neighbor_rankings(
    topics: list[TopicMemory],
    *,
    candidate_limit: int,
    similarity_threshold: float,
) -> dict[str, list[tuple[float, str]]]:
    """Return bounded nearest neighbors without a Python all-pairs loop."""
    result = {topic.topic_uid: [] for topic in topics}
    rows: list[tuple[str, list[float]]] = []
    dimension = 0
    for topic in topics:
        vector = [float(value) for value in topic.metadata.get("embedding", [])]
        if not vector:
            continue
        if dimension and len(vector) != dimension:
            continue
        dimension = dimension or len(vector)
        rows.append((topic.topic_uid, vector))
    rows.sort(key=lambda row: row[0])
    if len(rows) <= 1 or dimension <= 0:
        return result
    matrix = np.asarray([row[1] for row in rows], dtype="float32")
    faiss.normalize_L2(matrix)
    index: Any
    if len(rows) >= 500:
        index = faiss.IndexHNSWFlat(dimension, 32, faiss.METRIC_INNER_PRODUCT)
        index.hnsw.efConstruction = max(64, candidate_limit * 4)
        index.hnsw.efSearch = max(64, candidate_limit * 4)
    else:
        index = faiss.IndexFlatIP(dimension)
    index.add(matrix)
    scores, positions = index.search(
        matrix, min(len(rows), max(2, candidate_limit + 1))
    )
    for row_index, (score_row, position_row) in enumerate(
        zip(scores, positions, strict=True)
    ):
        uid = rows[row_index][0]
        candidates: list[tuple[float, str]] = []
        for score, position in zip(score_row, position_row, strict=True):
            if int(position) < 0 or int(position) == row_index:
                continue
            if float(score) < similarity_threshold:
                continue
            candidates.append((float(score), rows[int(position)][0]))
        result[uid] = sorted(candidates, key=lambda item: (-item[0], item[1]))
    return result


__all__ = ["vector_neighbor_rankings"]
