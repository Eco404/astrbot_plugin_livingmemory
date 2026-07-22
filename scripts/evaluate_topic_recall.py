"""Evaluate Timeline, Topic, and production-style recall against labeled queries.

The script reads model credentials only at runtime. Reports contain model names,
result IDs, and Topic titles, but never credentials or full private memory text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_WORKSPACE_ROOT = _PLUGIN_ROOT.parent
_ASTRBOT_ROOT = _WORKSPACE_ROOT / "AstrBot"
for _path in (_WORKSPACE_ROOT, _ASTRBOT_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from astrbot.core.provider.provider import EmbeddingProvider

from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.providers.cloudflare_rerank import (
    CloudflareRerankClient,
)
from astrbot_plugin_livingmemory.core.retrieval.recall_pipeline import RecallPipeline
from astrbot_plugin_livingmemory.core.timeline_settings import (
    effective_timeline_settings,
)
from astrbot_plugin_livingmemory.core.topic_settings import effective_topic_settings


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=4)
    return parser.parse_args()


def _credential_sections(path: Path) -> dict[str, dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    result: dict[str, dict[str, str]] = {}
    for name in ("cloudflare_rerank", "cloudflare_embedding"):
        marker = f"# {name}"
        if marker not in text:
            raise ValueError(f"credentials are missing {name}")
        section = text.split(marker, 1)[1].split("\n# ", 1)[0]
        result[name] = {
            key: value
            for key, value in re.findall(
                r'"([^\"]+)"\s*:\s*"([^\"]*)"', section
            )
        }
    return result


class CloudflareEmbeddingProvider(EmbeddingProvider):
    def __init__(self, config: dict[str, str]):
        super().__init__(
            {
                "id": "cloudflare_workers_ai_embedding_eval",
                "type": "evaluation",
                "model": config["model"],
            },
            {},
        )
        self.account_id = config["account_id"]
        self.api_token = config["api_token"]
        self.model = config["model"]
        self.client = httpx.AsyncClient(timeout=60.0)
        self.cache: dict[str, list[float]] = {}

    @property
    def endpoint(self) -> str:
        return (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/ai/run/{self.model}"
        )

    async def get_embedding(self, text: str) -> list[float]:
        return (await self.get_embeddings([text]))[0]

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        normalized = [str(text) for text in texts]
        missing = list(dict.fromkeys(text for text in normalized if text not in self.cache))
        if missing:
            response = await self.client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_token}"},
                json={"text": missing},
            )
            response.raise_for_status()
            payload = response.json()
            vectors = (payload.get("result") or {}).get("data") or []
            if len(vectors) != len(missing):
                raise RuntimeError(
                    "Cloudflare embedding result count does not match input count"
                )
            for text, vector in zip(missing, vectors, strict=True):
                self.cache[text] = [float(value) for value in vector]
        return [self.cache[text] for text in normalized]

    def get_dim(self) -> int:
        return 1024

    async def close(self) -> None:
        await self.client.aclose()


def _engine_config(topic_settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    timeline = effective_timeline_settings()
    recall = {
        key.removeprefix("recall_engine."): value
        for key, value in timeline.items()
        if key.startswith("recall_engine.")
    }
    engine = {
        "graph_memory_enabled": False,
        "fallback_enabled": bool(recall["fallback_to_vector"]),
        "rrf_k": int(timeline["fusion_strategy.rrf_k"]),
        "importance_weight": float(recall["importance_weight"]),
        "decay_rate": float(timeline["importance_decay.decay_rate"]),
        "search_cache_enabled": False,
        "topic_memory": {"enabled": True, "recall_enabled": True, **topic_settings},
    }
    return engine, recall


def _topic_settings_from_database(path: Path) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='topic_setting_overrides'"
        ).fetchone()
        if table:
            for key, raw in db.execute(
                "SELECT setting_key, setting_value FROM topic_setting_overrides"
            ):
                try:
                    overrides[str(key)] = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
    return effective_topic_settings(overrides)


def _timeline_topic_map(path: Path) -> dict[int, list[str]]:
    result: dict[int, list[str]] = defaultdict(list)
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        for document_id, title in db.execute(
            """
            SELECT DISTINCT r.document_id, t.title
            FROM memory_registry r
            JOIN topic_timeline_links l ON l.timeline_uid = r.memory_uid
            JOIN topic_memories t ON t.topic_uid = l.topic_uid
            WHERE r.memory_layer = 'timeline' AND r.status = 'active'
              AND l.status = 'active' AND t.status = 'active'
            ORDER BY r.document_id, t.title
            """
        ):
            result[int(document_id)].append(str(title))
    return dict(result)


def _score_case(case: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    titles = sorted(
        {
            title
            for row in rows
            for title in row.get("topic_titles", [])
            if title
        }
    )
    must = set(case.get("must_any") or [])
    forbidden = set(case.get("must_not") or [])
    must_rank = next(
        (
            index
            for index, row in enumerate(rows, 1)
            if must & set(row.get("topic_titles") or [])
        ),
        None,
    )
    return {
        "result_count": len(rows),
        "topic_titles": titles,
        "must_hit": bool(must & set(titles)) if must else None,
        "must_rank": must_rank,
        "forbidden_hits": sorted(forbidden & set(titles)),
        "empty": not rows,
        "results": rows,
    }


def _summary(cases: list[dict[str, Any]], results: dict[str, Any]) -> dict[str, Any]:
    evaluated = [case for case in cases if case.get("must_any")]
    must_hits = sum(
        1 for case in evaluated if results[case["id"]]["must_hit"] is True
    )
    top_one_hits = sum(
        1 for case in evaluated if results[case["id"]]["must_rank"] == 1
    )
    reciprocal_rank = sum(
        1.0 / int(results[case["id"]]["must_rank"])
        for case in evaluated
        if results[case["id"]]["must_rank"] is not None
    )
    forbidden_cases = sum(
        1 for case in cases if results[case["id"]]["forbidden_hits"]
    )
    empty_cases = sum(1 for case in cases if results[case["id"]]["empty"])
    return {
        "evaluated_cases": len(evaluated),
        "must_hit_cases": must_hits,
        "recall_at_k": round(must_hits / max(1, len(evaluated)), 4),
        "top_one_accuracy": round(top_one_hits / max(1, len(evaluated)), 4),
        "mean_reciprocal_rank": round(
            reciprocal_rank / max(1, len(evaluated)), 4
        ),
        "forbidden_hit_cases": forbidden_cases,
        "empty_cases": empty_cases,
        "average_result_count": round(
            sum(results[case["id"]]["result_count"] for case in cases)
            / max(1, len(cases)),
            3,
        ),
    }


async def _evaluate(args: argparse.Namespace) -> dict[str, Any]:
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = list(dataset["cases"])
    credentials = _credential_sections(args.credentials)
    topic_settings = _topic_settings_from_database(args.database)
    engine_config, recall_config = _engine_config(topic_settings)
    temporary = Path(tempfile.mkdtemp(prefix="livingmemory-recall-eval-"))
    db_copy = temporary / "livingmemory.db"
    index_copy = temporary / "livingmemory.index"
    shutil.copy2(args.database, db_copy)
    shutil.copy2(args.index, index_copy)
    embedding = CloudflareEmbeddingProvider(credentials["cloudflare_embedding"])
    rerank_config = credentials["cloudflare_rerank"]
    reranker = CloudflareRerankClient(
        account_id=rerank_config["account_id"],
        api_token=rerank_config["api_token"],
        model=rerank_config["model"],
        max_retries=0,
    )
    faiss = FaissVecDB(str(db_copy), str(index_copy), embedding)
    await faiss.initialize()
    engine = MemoryEngine(
        str(db_copy),
        faiss,
        rerank_provider=reranker,
        config=engine_config,
    )
    await engine.initialize()
    pipeline = RecallPipeline(engine, recall_config)
    timeline_topics = _timeline_topic_map(db_copy)
    branch_map = {
        case["id"]: pipeline.build_query_branches(
            case["query"],
            case.get("recent_messages"),
            expansion_enabled=bool(case.get("recent_messages")),
            assistant_mode="normal",
        )
        for case in cases
    }
    await embedding.get_embeddings(
        list(
            dict.fromkeys(
                branch.text
                for branches in branch_map.values()
                for branch in branches
            )
        )
    )
    semaphore = asyncio.Semaphore(max(1, int(args.concurrency)))
    all_results: dict[str, Any] = {}

    async def evaluate_case(case: dict[str, Any], rerank_enabled: bool) -> dict[str, Any]:
        async with semaphore:
            branches = branch_map[case["id"]]
            timeline_outcome = await pipeline.search(
                current_query=case["query"],
                final_k=int(dataset.get("result_k", 5)),
                session_id=dataset.get("session_id"),
                recent_messages=case.get("recent_messages"),
                expansion_enabled=bool(case.get("recent_messages")),
                assistant_mode="normal",
                track_access=False,
            )
            engine.topic_recall_pipeline.config["recall_use_rerank"] = rerank_enabled
            engine.topic_retriever.config["recall_use_rerank"] = rerank_enabled
            topic_outcome = await engine.topic_recall_pipeline.search(
                branches=branches,
                memory_space_id=dataset["memory_space_id"],
                final_k=min(
                    int(dataset.get("result_k", 5)),
                    int(topic_settings.get("recall_top_k", 3)),
                ),
            )
            topics = topic_outcome.results
            fragments = []
            timeline_results = list(timeline_outcome.results)
            if topics:
                supplement_k = min(
                    int(topic_settings.get("timeline_supplement_k", 2)),
                    max(0, int(dataset.get("result_k", 5)) - len(topics)),
                )
                fragment_outcome = await engine.topic_recall_pipeline.search_fragment_supplements(
                    branches=branches,
                    topic_results=topics,
                    limit=supplement_k,
                    query_vectors=topic_outcome.query_vectors,
                )
                fragments = list(fragment_outcome.results)
                suppress_timeline = bool(
                    fragment_outcome.available_count > 0
                    and fragment_outcome.duplicate_parent_count
                    == fragment_outcome.available_count
                )
                if fragments or suppress_timeline:
                    timeline_results = []
                else:
                    timeline_results = engine.topic_recall_pipeline.select_timeline_supplements(
                        timeline_results,
                        topics,
                        supplement_k,
                    )

            timeline_rows = [
                {
                    "layer": "timeline",
                    "id": int(item.doc_id),
                    "score": round(float(item.final_score), 6),
                    "topic_titles": timeline_topics.get(int(item.doc_id), []),
                }
                for item in timeline_outcome.results
            ]
            topic_rows = [
                {
                    "layer": "topic",
                    "id": item.topic_uid,
                    "score": round(float(item.final_score), 6),
                    "topic_titles": [item.topic.title],
                }
                for item in topics
            ]
            current_rows = list(topic_rows)
            current_rows.extend(
                {
                    "layer": "topic_fragment",
                    "id": item.fragment_uid,
                    "score": round(float(item.final_score), 6),
                    "topic_titles": [
                        next(
                            (
                                topic.topic.title
                                for topic in topics
                                if topic.topic_uid == item.topic_uid
                            ),
                            "",
                        )
                    ],
                    "body_suppressed": bool(item.body_suppressed),
                    "fact_count": len(item.fact_contents),
                }
                for item in fragments
            )
            current_rows.extend(
                {
                    "layer": "timeline_supplement",
                    "id": int(item.doc_id),
                    "score": round(float(item.final_score), 6),
                    "topic_titles": timeline_topics.get(int(item.doc_id), []),
                }
                for item in timeline_results
            )
            return {
                "timeline": _score_case(case, timeline_rows),
                "topic": _score_case(case, topic_rows),
                "current": _score_case(case, current_rows),
            }

    try:
        for rerank_enabled in (False, True):
            label = "rerank_on" if rerank_enabled else "rerank_off"
            rows = await asyncio.gather(
                *(evaluate_case(case, rerank_enabled) for case in cases)
            )
            case_results = {
                case["id"]: row for case, row in zip(cases, rows, strict=True)
            }
            all_results[label] = {
                "summary": {
                    mode: _summary(
                        cases,
                        {key: value[mode] for key, value in case_results.items()},
                    )
                    for mode in ("timeline", "topic", "current")
                },
                "cases": case_results,
            }
    finally:
        await engine.close()
        await embedding.close()
        shutil.rmtree(temporary, ignore_errors=True)

    regressions = []
    for case in cases:
        off = all_results["rerank_off"]["cases"][case["id"]]["topic"]
        on = all_results["rerank_on"]["cases"][case["id"]]["topic"]
        if off["must_hit"] is True and on["must_hit"] is False:
            regressions.append(case["id"])
    return {
        "schema_version": 1,
        "dataset": str(args.dataset),
        "database": str(args.database),
        "case_count": len(cases),
        "models": {
            "embedding": credentials["cloudflare_embedding"]["model"],
            "rerank": credentials["cloudflare_rerank"]["model"],
        },
        "rerank_regressions": regressions,
        "runs": all_results,
    }


async def _main() -> None:
    args = _args()
    report = await _evaluate(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "case_count": report["case_count"],
                "rerank_regressions": report["rerank_regressions"],
                "summaries": {
                    label: run["summary"] for label, run in report["runs"].items()
                },
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
