# Data safety and migration

## Authoritative data

`livingmemory.db` stores Timeline, Topic, user profiles, persona relationships, atoms, actor relations, build jobs, and diagnostics. `conversations.db` stores raw messages and summary cursors. FAISS indexes are disposable runtime products; SQLite remains authoritative.

## Version path

Version 3.8.x uses schema v10.4. The public migration path is:

```text
v8 -> v9 -> v10 -> current v10.x
```

Schema migration does not call the LLM or synthesize Topic content. Semantic products are created later by an explicit build or maintenance operation.

The v10.3 to v10.4 migration only creates user-profile, provenance, conflict, relationship-revision, account-binding, sharing, and maintenance-job tables. It does not scan or backfill old Timeline, and it does not modify Timeline, Topic, conversations, or supplemental profiles. New private Timeline changes are processed after upgrade; old history requires an explicit administrator rebuild.

## User-profile privacy boundaries

- Only private users with a stable platform account ID are enrolled. Group chats, nicknames, and temporary sessions are neither enrolled nor injected.
- Cross-platform accounts are never merged automatically. Manual binding retains original-account provenance so unbinding can reproject it.
- Sensitive behavioral inference is off by default and uses higher evidence thresholds. Passwords, tokens, API keys, private keys, and verification codes are always rejected.
- Conflicting facts pause injection and cannot be silently overwritten. Manual decisions and later automatic review retain their sources and override records.
- Objective profiles and relationships do not feed Timeline summarization, Topic construction, or the knowledge graph.
- Passive injection and the active tool can only read the current stable private user. The tool also requires an explicit `include_user_profile=true` request.

## Cleanup semantics

Cleaning summarized chat keeps Timeline and Topic. Removing all raw conversation loses message-level evidence where no snapshot exists. Deleting Timeline removes its evidence and repairs or archives derived Topics. Clearing Topic never deletes Timeline.

Resetting a user profile clears derived facts while keeping the scope enabled and starts from new changes. Deleting and disabling a profile also removes its relationship state and prevents automatic re-enrollment until an administrator restores it.

## Backups and health

Plugin version changes, database migrations, and scheduled maintenance can create backups. The health page runs SQLite integrity, foreign-key, and provenance checks without modifying data until users select a repair.

Database compaction uses a dedicated connection and briefly locks SQLite. Deleting rows alone does not return file space to the operating system.
