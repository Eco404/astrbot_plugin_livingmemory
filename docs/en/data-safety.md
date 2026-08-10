# Data safety and migration

## Authoritative data

`livingmemory.db` stores Timeline, Topic, atoms, actor relations, build jobs, and diagnostics. `conversations.db` stores raw messages and summary cursors. FAISS indexes are disposable runtime products; SQLite remains authoritative.

## Version path

Version 3.7.x uses the v10 schema family. The public migration path is:

```text
v8 -> v9 -> v10 -> current v10.x
```

Schema migration does not call the LLM or synthesize Topic content. Semantic products are created later by an explicit build or maintenance operation.

## Cleanup semantics

Cleaning summarized chat keeps Timeline and Topic. Removing all raw conversation loses message-level evidence where no snapshot exists. Deleting Timeline removes its evidence and repairs or archives derived Topics. Clearing Topic never deletes Timeline.

## Backups and health

Plugin version changes, database migrations, and scheduled maintenance can create backups. The health page runs SQLite integrity, foreign-key, and provenance checks without modifying data until users select a repair.

Database compaction uses a dedicated connection and briefly locks SQLite. Deleting rows alone does not return file space to the operating system.
