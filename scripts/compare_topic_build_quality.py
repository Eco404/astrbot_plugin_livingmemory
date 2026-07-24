"""Compare an incremental Topic build with a full-build reference database.

The script grafts Timeline rows that only exist in the full reference into a copy
of the base database, runs the real incremental pipeline, and compares the two
derived Topic views semantically. Credentials are read at runtime and are never
written to the report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_WORKSPACE_ROOT = _PLUGIN_ROOT.parent
_ASTRBOT_ROOT = _WORKSPACE_ROOT / "AstrBot"
for _path in (_WORKSPACE_ROOT, _ASTRBOT_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

from astrbot_plugin_livingmemory.core.managers.topic_build_manager import (
    TopicBuildManager,
)
from astrbot_plugin_livingmemory.core.managers.topic_maintenance_manager import (
    TopicMaintenanceManager,
)
from astrbot_plugin_livingmemory.core.models.identity_profile import (
    SupplementalIdentityStore,
)
from astrbot_plugin_livingmemory.core.models.topic_memory import TopicMaintenanceMode
from astrbot_plugin_livingmemory.core.providers.cloudflare_rerank import (
    CloudflareRerankClient,
)
from astrbot_plugin_livingmemory.core.topic_settings import effective_topic_settings
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore
from astrbot_plugin_livingmemory.storage.topic_memory_store import TopicMemoryStore

from evaluate_topic_recall import CloudflareEmbeddingProvider


class _SingleAttemptLLMProvider:
    """Delegate to AstrBot while bounding its provider-level retry loop."""

    def __init__(self, provider: ProviderOpenAIOfficial) -> None:
        self.provider = provider
        self.provider_config = provider.provider_config

    async def text_chat(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["request_max_retries"] = 1
        return await self.provider.text_chat(*args, **kwargs)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-database", required=True, type=Path)
    parser.add_argument("--full-database", required=True, type=Path)
    parser.add_argument("--full-conversations", required=True, type=Path)
    parser.add_argument("--identities", required=True, type=Path)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume the newest pending run in an existing work directory.",
    )
    return parser.parse_args()


def _credential_sections(path: Path) -> dict[str, dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    result: dict[str, dict[str, str]] = {}
    for name in ("cloudflare_rerank", "cloudflare_embedding", "ecoapi_gpt"):
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


def _settings(path: Path) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    with sqlite3.connect(path) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='topic_setting_overrides'"
        ).fetchone()
        if exists:
            for key, raw in db.execute(
                "SELECT setting_key, setting_value FROM topic_setting_overrides"
            ):
                try:
                    overrides[str(key)] = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
    return effective_topic_settings(overrides)


def _active_timelines(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        return {
            str(uid): int(document_id)
            for uid, document_id in db.execute(
                "SELECT memory_uid, document_id FROM memory_registry "
                "WHERE memory_layer='timeline' AND status='active'"
            )
        }


def _upsert_source_table(
    source: sqlite3.Connection, target: sqlite3.Connection, table: str
) -> int:
    columns = [str(row[1]) for row in source.execute(f"PRAGMA table_info({table})")]
    primary_keys = [
        str(row[1])
        for row in source.execute(f"PRAGMA table_info({table})")
        if int(row[5]) > 0
    ]
    if not primary_keys:
        raise ValueError(f"source table has no primary key: {table}")
    rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{column}=excluded.{column}"
        for column in columns
        if column not in primary_keys
    )
    target.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(primary_keys)}) DO UPDATE SET {updates}",
        rows,
    )
    return len(rows)


def _sync_timeline_sources(base: Path, full: Path) -> list[str]:
    """Make source-layer input identical while retaining base derived Topics."""
    base_timelines = _active_timelines(base)
    full_timelines = _active_timelines(full)
    new_uids = sorted(set(full_timelines) - set(base_timelines))
    if not new_uids:
        raise ValueError("full reference contains no Timeline absent from base")
    source = sqlite3.connect(full)
    target = sqlite3.connect(base)
    try:
        target.execute("PRAGMA foreign_keys=ON")
        for table in (
            "documents",
            "memory_registry",
            "memory_source_spans",
            "memory_atoms",
        ):
            _upsert_source_table(source, target, table)
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()
    return new_uids


def _topic_rows(path: Path, timeline_uids: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in timeline_uids)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"""
            SELECT DISTINCT t.*
            FROM topic_memories t
            JOIN topic_timeline_links l ON l.topic_uid=t.topic_uid
            WHERE t.status='active' AND l.status='active'
              AND l.timeline_uid IN ({placeholders})
            ORDER BY t.title, t.topic_uid
            """,
            timeline_uids,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            atoms = [
                str(value)
                for (value,) in db.execute(
                    "SELECT content FROM topic_memory_atoms WHERE topic_uid=? "
                    "AND status='active' ORDER BY content",
                    (item["topic_uid"],),
                )
            ]
            item["atoms"] = atoms
            item["text"] = "\n".join(
                [str(item["title"]), str(item["summary"]), *atoms]
            )
            item["timeline_uids"] = [
                str(value)
                for (value,) in db.execute(
                    "SELECT timeline_uid FROM topic_timeline_links "
                    "WHERE topic_uid=? AND status='active' ORDER BY timeline_uid",
                    (item["topic_uid"],),
                )
            ]
            actor_rows = db.execute(
                "SELECT actor_id, relation_type, resolution_status "
                "FROM topic_actor_links WHERE topic_uid=? ORDER BY actor_id, relation_type",
                (item["topic_uid"],),
            ).fetchall()
            item["actor_links"] = [tuple(actor) for actor in actor_rows]
            result.append(item)
        return result


def _source_facts(path: Path, timeline_uids: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in timeline_uids)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute(
                f"""
                SELECT a.id, a.content, a.atom_type, r.memory_uid AS timeline_uid
                FROM memory_atoms a
                JOIN memory_registry r ON r.document_id=a.parent_memory_id
                WHERE r.memory_uid IN ({placeholders}) AND a.status='active'
                ORDER BY r.memory_uid, a.id
                """,
                timeline_uids,
            )
        ]


def _active_topic_state(path: Path) -> dict[str, tuple[int, str, str]]:
    with sqlite3.connect(path) as db:
        return {
            str(uid): (int(revision), str(title), str(summary))
            for uid, revision, title, summary in db.execute(
                "SELECT topic_uid, revision, title, summary FROM topic_memories "
                "WHERE status='active'"
            )
        }


def _latest_run(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM topic_maintenance_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else {}


def _draft_groups(path: Path, run_uid: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        jobs = db.execute(
            "SELECT group_uid, input_hash, status FROM topic_build_group_jobs "
            "WHERE run_uid=?",
            (run_uid,),
        ).fetchall()
        for job in jobs:
            fragments = db.execute(
                "SELECT label, summary, facts FROM topic_fragment_drafts "
                "WHERE run_uid=? AND candidate_group_uid=? ORDER BY fragment_uid",
                (run_uid, job["group_uid"]),
            ).fetchall()
            texts = []
            labels = []
            for fragment in fragments:
                try:
                    facts = json.loads(fragment["facts"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    facts = []
                fact_text = "\n".join(
                    str(item.get("content") or "")
                    for item in facts
                    if isinstance(item, dict)
                )
                labels.append(str(fragment["label"]))
                texts.append(
                    "\n".join(
                        [str(fragment["label"]), str(fragment["summary"]), fact_text]
                    )
                )
            result[str(job["input_hash"])] = {
                "status": str(job["status"]),
                "labels": labels,
                "texts": texts,
            }
    return result


async def _compare_completed_fragment_groups(
    embedding: CloudflareEmbeddingProvider,
    incremental_db: Path,
    incremental_run_uid: str,
    full_db: Path,
) -> list[dict[str, Any]]:
    full_run_uid = str(_latest_run(full_db).get("run_uid") or "")
    incremental_groups = _draft_groups(incremental_db, incremental_run_uid)
    full_groups = _draft_groups(full_db, full_run_uid)
    rows = []
    for input_hash, incremental in sorted(incremental_groups.items()):
        full = full_groups.get(input_hash)
        if incremental["status"] != "completed" or full is None:
            continue
        texts = list(incremental["texts"]) + list(full["texts"])
        vectors = await embedding.get_embeddings(texts) if texts else []
        inc_vectors = vectors[: len(incremental["texts"])]
        full_vectors = vectors[len(incremental["texts"]) :]
        best_scores = [
            max((_cosine(vector, candidate) for candidate in inc_vectors), default=0.0)
            for vector in full_vectors
        ]
        rows.append(
            {
                "input_hash": input_hash,
                "incremental_fragment_count": len(incremental["texts"]),
                "full_fragment_count": len(full["texts"]),
                "incremental_labels": incremental["labels"],
                "full_labels": full["labels"],
                "minimum_full_to_incremental_similarity": round(
                    min(best_scores, default=0.0), 6
                ),
                "average_full_to_incremental_similarity": round(
                    sum(best_scores) / max(1, len(best_scores)), 6
                ),
            }
        )
    return rows


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


async def _semantic_comparison(
    embedding: CloudflareEmbeddingProvider,
    reranker: CloudflareRerankClient,
    facts: list[dict[str, Any]],
    incremental: list[dict[str, Any]],
    full: list[dict[str, Any]],
) -> dict[str, Any]:
    texts = [item["text"] for item in incremental + full]
    vectors = await embedding.get_embeddings(texts) if texts else []
    inc_vectors = vectors[: len(incremental)]
    full_vectors = vectors[len(incremental) :]
    topic_matches = []
    for full_index, topic in enumerate(full):
        scored = sorted(
            (
                (_cosine(full_vectors[full_index], vector), index)
                for index, vector in enumerate(inc_vectors)
            ),
            reverse=True,
        )
        score, index = scored[0] if scored else (0.0, -1)
        topic_matches.append(
            {
                "full_title": topic["title"],
                "incremental_title": (
                    incremental[index]["title"] if index >= 0 else None
                ),
                "embedding_similarity": round(score, 6),
            }
        )

    fact_rows = []
    topic_texts = [item["text"] for item in incremental]
    fact_vectors = await embedding.get_embeddings(
        [str(item["content"]) for item in facts]
    )
    for fact, fact_vector in zip(facts, fact_vectors, strict=True):
        scores = [_cosine(fact_vector, vector) for vector in inc_vectors]
        best_index = max(range(len(scores)), key=scores.__getitem__) if scores else -1
        rerank_score = 0.0
        if topic_texts:
            reranked = await reranker.rerank(str(fact["content"]), topic_texts)
            rerank_score = max(
                (float(item.relevance_score) for item in reranked), default=0.0
            )
        fact_rows.append(
            {
                "source_atom_id": fact["id"],
                "source_fact": fact["content"],
                "best_topic": (
                    incremental[best_index]["title"] if best_index >= 0 else None
                ),
                "embedding_similarity": round(
                    scores[best_index] if best_index >= 0 else 0.0, 6
                ),
                "rerank_score": round(rerank_score, 6),
            }
        )
    return {"topic_matches": topic_matches, "fact_coverage": fact_rows}


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    args.work_dir.mkdir(parents=True, exist_ok=True)
    incremental_db = args.work_dir / "incremental.db"
    conversation_db = args.work_dir / "conversations.db"
    identities_path = args.work_dir / "authoritative_identities.json"
    new_uids = sorted(
        set(_active_timelines(args.full_database))
        - set(_active_timelines(args.base_database))
    )
    if args.resume:
        if not incremental_db.exists():
            raise ValueError("resume requested but incremental work database is absent")
    else:
        if incremental_db.exists():
            raise ValueError("work directory already contains an incremental database")
        shutil.copy2(args.base_database, incremental_db)
        shutil.copy2(args.full_conversations, conversation_db)
        shutil.copy2(args.identities, identities_path)
        grafted = _sync_timeline_sources(incremental_db, args.full_database)
        if grafted != new_uids:
            raise ValueError("grafted Timeline set does not match reference difference")
    before_topics = _active_topic_state(args.base_database)
    credentials = _credential_sections(args.credentials)
    embedding = CloudflareEmbeddingProvider(credentials["cloudflare_embedding"])
    rerank_values = credentials["cloudflare_rerank"]
    reranker = CloudflareRerankClient(
        account_id=rerank_values["account_id"],
        api_token=rerank_values["api_token"],
        model=rerank_values["model"],
        max_retries=0,
    )
    llm_values = credentials["ecoapi_gpt"]
    llm = ProviderOpenAIOfficial(
        {
            "id": "topic-build-quality-eval",
            "type": "openai_chat_completion",
            "key": [llm_values["api_token"]],
            "api_base": llm_values["base_url"],
            "model": llm_values["model"],
            "timeout": 180,
        },
        {},
    )
    # Keep the quality run bounded. Production retry policy does not affect output
    # quality, and nested SDK/provider/build retries can otherwise multiply one
    # deterministic gateway timeout into dozens of identical requests.
    llm.client.max_retries = 0
    store = TopicMemoryStore(str(incremental_db))
    await store.initialize()
    conversations = ConversationStore(str(conversation_db))
    await conversations.initialize()
    settings = _settings(incremental_db)
    settings["llm_max_retries"] = 1
    manager = TopicBuildManager(
        str(incremental_db),
        store,
        TopicMaintenanceManager(str(incremental_db), store),
        llm_provider=_SingleAttemptLLMProvider(llm),
        embedding_provider=embedding,
        rerank_provider=reranker,
        config=settings,
        identity_profile_store=SupplementalIdentityStore(identities_path),
        conversation_store=conversations,
    )
    spaces = {
        value
        for value, in sqlite3.connect(incremental_db).execute(
            "SELECT DISTINCT memory_space_id FROM memory_registry "
            "WHERE memory_uid IN (%s)" % ",".join("?" for _ in new_uids),
            new_uids,
        )
    }
    if len(spaces) != 1:
        raise ValueError(f"expected one memory space for new Timelines, got {spaces}")
    memory_space_id = spaces.pop()
    last_progress: tuple[str, int, int] | None = None

    def progress(event: dict[str, Any]) -> None:
        nonlocal last_progress
        current = (
            str(event.get("stage")),
            int(event.get("current") or 0),
            int(event.get("total") or 0),
        )
        if current != last_progress:
            print(json.dumps({"progress": current}, ensure_ascii=False), flush=True)
            last_progress = current

    started_at = time.time()
    build_error: dict[str, str] | None = None
    partial_groups: list[dict[str, Any]] = []
    try:
        try:
            if args.resume:
                with sqlite3.connect(incremental_db) as db:
                    pending = db.execute(
                        "SELECT run_uid FROM topic_maintenance_runs "
                        "WHERE status IN ('pending', 'failed') "
                        "ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                if pending is None:
                    raise ValueError("resume requested but no pending Topic run exists")
                build_result = await manager.resume_run(
                    str(pending[0]), progress_callback=progress
                )
            else:
                build_result = await manager.build_space(
                    str(memory_space_id),
                    mode=TopicMaintenanceMode.INCREMENTAL,
                    timeline_uids=new_uids,
                    progress_callback=progress,
                )
        except Exception as exc:
            build_result = None
            build_error = {
                "type": type(exc).__name__,
                "message": str(exc)[:1000],
            }
            failed_run = _latest_run(incremental_db)
            partial_groups = await _compare_completed_fragment_groups(
                embedding,
                incremental_db,
                str(failed_run.get("run_uid") or ""),
                args.full_database,
            )
            semantic = {"topic_matches": [], "fact_coverage": []}
            incremental_topics = _topic_rows(incremental_db, new_uids)
            full_topics = _topic_rows(args.full_database, new_uids)
        else:
            incremental_topics = _topic_rows(incremental_db, new_uids)
            full_topics = _topic_rows(args.full_database, new_uids)
            facts = _source_facts(args.full_database, new_uids)
            semantic = await _semantic_comparison(
                embedding, reranker, facts, incremental_topics, full_topics
            )
    finally:
        await manager.close()
        await conversations.close()
        await llm.terminate()
        await embedding.close()

    after_topics = _active_topic_state(incremental_db)
    if build_error is not None:
        failed_run = _latest_run(incremental_db)
        full_hashes = set(
            _draft_groups(
                args.full_database,
                str(_latest_run(args.full_database).get("run_uid") or ""),
            )
        )
        incremental_hashes = set(
            _draft_groups(
                incremental_db, str(failed_run.get("run_uid") or "")
            )
        )
        return {
            "schema_version": 1,
            "elapsed_seconds": round(time.time() - started_at, 3),
            "models": {
                "llm": llm_values["model"],
                "embedding": credentials["cloudflare_embedding"]["model"],
                "rerank": rerank_values["model"],
            },
            "memory_space_id": memory_space_id,
            "new_timeline_uids": new_uids,
            "build_result": None,
            "build_error": build_error,
            "failed_run": {
                key: failed_run.get(key)
                for key in (
                    "run_uid",
                    "status",
                    "stage",
                    "current_group_index",
                    "total_groups",
                    "error",
                )
            },
            "source_input_hashes_present_in_full_reference": sorted(
                incremental_hashes & full_hashes
            ),
            "completed_fragment_group_comparisons": partial_groups,
            "atomic_publish_preserved_old_topics": after_topics == before_topics,
            "active_topic_count_before": len(before_topics),
            "active_topic_count_after": len(after_topics),
            "full_reference_topic_count_for_new_timelines": len(full_topics),
            "checks": {
                "build_completed": False,
                "atomic_publish_preserved_old_topics": after_topics == before_topics,
                "shared_pipeline_inputs_match_reference": bool(
                    incremental_hashes & full_hashes
                ),
            },
            "passed": False,
            "incremental_database": str(incremental_db),
        }
    linked_incremental_uids = {item["topic_uid"] for item in incremental_topics}
    changed_existing = {
        uid
        for uid, state in after_topics.items()
        if uid in before_topics and state != before_topics[uid]
    }
    unrelated_changes = sorted(changed_existing - linked_incremental_uids)
    topic_matches = semantic["topic_matches"]
    fact_coverage = semantic["fact_coverage"]
    min_topic_similarity = min(
        (float(item["embedding_similarity"]) for item in topic_matches), default=0.0
    )
    min_fact_embedding = min(
        (float(item["embedding_similarity"]) for item in fact_coverage), default=0.0
    )
    min_fact_rerank = min(
        (float(item["rerank_score"]) for item in fact_coverage), default=0.0
    )
    unresolved = sum(
        1
        for topic in incremental_topics
        for actor in topic["actor_links"]
        if str(actor[2]) == "unresolved"
    )
    checks = {
        "all_reference_topics_have_semantic_match": min_topic_similarity >= 0.72,
        "all_source_facts_are_covered": all(
            float(item["embedding_similarity"]) >= 0.58
            or float(item["rerank_score"]) >= 0.35
            for item in fact_coverage
        ),
        "no_unrelated_existing_topic_rewrites": not unrelated_changes,
        "participant_links_resolved": unresolved == 0,
        "topic_count_is_comparable": (
            abs(len(incremental_topics) - len(full_topics)) <= max(1, len(full_topics) // 2)
        ),
    }
    return {
        "schema_version": 1,
        "elapsed_seconds": round(time.time() - started_at, 3),
        "models": {
            "llm": llm_values["model"],
            "embedding": credentials["cloudflare_embedding"]["model"],
            "rerank": rerank_values["model"],
        },
        "memory_space_id": memory_space_id,
        "new_timeline_uids": new_uids,
        "build_result": build_result,
        "incremental_topic_count": len(incremental_topics),
        "full_reference_topic_count": len(full_topics),
        "incremental_titles": [item["title"] for item in incremental_topics],
        "full_reference_titles": [item["title"] for item in full_topics],
        "minimum_topic_similarity": round(min_topic_similarity, 6),
        "minimum_fact_embedding_similarity": round(min_fact_embedding, 6),
        "minimum_fact_rerank_score": round(min_fact_rerank, 6),
        "unresolved_actor_link_count": unresolved,
        "unrelated_changed_topic_uids": unrelated_changes,
        **semantic,
        "checks": checks,
        "passed": all(checks.values()),
        "incremental_database": str(incremental_db),
    }


async def _main() -> None:
    args = _args()
    report = await _run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
