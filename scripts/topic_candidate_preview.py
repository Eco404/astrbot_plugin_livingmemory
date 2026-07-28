"""Run deterministic Topic-candidate discovery without creating final Topics."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(_PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PARENT))

from astrbot_plugin_livingmemory.core.managers.topic_maintenance_manager import (
    TopicMaintenanceManager,
)
from astrbot_plugin_livingmemory.storage.db_migration import DBMigration


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Path to livingmemory.db")
    parser.add_argument("--memory-space", help="Scan only this memory_space_id")
    parser.add_argument("--resume", help="Resume an existing maintenance run UID")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--time-gap-hours", type=float, default=6.0)
    parser.add_argument("--similarity-threshold", type=float, default=0.52)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument(
        "--show-labels",
        action="store_true",
        help="Include candidate labels, which may contain private memory text",
    )
    return parser.parse_args()


def _inspect_database(path: Path, requested_space: str | None) -> list[str]:
    if not path.is_file():
        raise SystemExit(f"Database does not exist: {path}")
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute(
            "SELECT version FROM db_version ORDER BY id DESC LIMIT 1"
        ).fetchone()
        version = DBMigration.normalize_version(row[0]) if row else "0"
        if not DBMigration.version_at_least(version, "9.2"):
            raise SystemExit(
                f"Database version is {version}; start the plugin once to migrate to v9.2"
            )
        if requested_space:
            exists = db.execute(
                "SELECT 1 FROM memory_registry WHERE memory_space_id = ? LIMIT 1",
                (requested_space,),
            ).fetchone()
            if not exists:
                raise SystemExit(f"Unknown memory space: {requested_space}")
            return [requested_space]
        return [
            str(item[0])
            for item in db.execute(
                """
                SELECT DISTINCT memory_space_id FROM memory_registry
                WHERE memory_layer = 'timeline' AND status = 'active'
                ORDER BY memory_space_id
                """
            ).fetchall()
        ]


def _summarize(result: dict[str, Any], *, show_labels: bool) -> dict[str, Any]:
    groups = result["groups"]
    payload: dict[str, Any] = {
        "run_uid": result["run_uid"],
        "memory_space_id": result["memory_space_id"],
        "status": result["status"],
        "processed_items": result["processed_items"],
        "total_items": result["total_items"],
        "candidate_groups": result["candidate_groups"],
        "group_sizes": sorted((len(group.timeline_uids) for group in groups), reverse=True),
        "time_cluster_counts": sorted(
            (len(group.time_cluster_keys) for group in groups), reverse=True
        ),
        "cohesion_range": [
            min((group.cohesion for group in groups), default=0.0),
            max((group.cohesion for group in groups), default=0.0),
        ],
    }
    if show_labels:
        payload["labels"] = [group.label for group in groups]
    return payload


async def _main(args: argparse.Namespace) -> None:
    database = Path(args.database).resolve()
    manager = TopicMaintenanceManager(str(database))
    if args.resume:
        result = await manager.resume_scan(
            args.resume,
            max_batches=args.max_batches,
        )
        output = [_summarize(result, show_labels=args.show_labels)]
    else:
        spaces = _inspect_database(database, args.memory_space)
        output = []
        for space_id in spaces:
            result = await manager.start_scan(
                space_id,
                batch_size=args.batch_size,
                time_gap_seconds=args.time_gap_hours * 3600.0,
                similarity_threshold=args.similarity_threshold,
                max_batches=args.max_batches,
            )
            output.append(_summarize(result, show_labels=args.show_labels))
    print(json.dumps({"runs": output}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
