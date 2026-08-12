# Data safety and migration

## Authoritative data

`livingmemory.db` stores Timeline, Topic, user profiles, persona relationships, atoms, actor relations, build jobs, and diagnostics. `conversations.db` stores raw messages and summary cursors. FAISS indexes are disposable runtime products; SQLite remains authoritative.

## Version path

Version 3.8.x uses schema v10.4. The public migration path is:

```text
v8 -> v9 -> v10 -> v10.4
```

Schema migration does not call the LLM or synthesize Topic content. Semantic products are created later by an explicit build or maintenance operation.

The single v10.3 to v10.4 migration creates user-profile, provenance, conflict, relationship-revision, account-binding, sharing, maintenance-job, and legacy-Timeline identity-review tables. Migration does not scan or backfill old Timeline and does not modify Timeline, Topic, conversations, or supplemental profiles. New private Timeline changes are processed after upgrade; old history requires an explicit administrator scan or rebuild. Unpublished development schemas are not part of the release migration path and test databases are rebuilt before release.

An explicit history scan prefers native `role_bindings`. When an old Timeline lacks them, the resolver only combines exact evidence within the same Bot/persona: private target, stable actors from the same session, explicit human message senders, and canonical platform. A unique result is bound automatically; unresolved or conflicting evidence enters the review queue. Administrators can bind, ignore, or restore an item, while original Timeline metadata remains unchanged.

## User-profile privacy boundaries

- Only private users with a stable platform account ID are enrolled. Group chats, nicknames, and temporary sessions are neither enrolled nor injected.
- Cross-platform accounts are never merged automatically. Manual binding retains original-account provenance so unbinding can reproject it.
- Sensitive behavioral inference is off by default and uses higher evidence thresholds. Passwords, tokens, API keys, private keys, and verification codes are always rejected.
- Conflicting facts pause injection and cannot be silently overwritten. Manual decisions and later automatic review retain their sources and override records.
- A legacy Timeline summary without message-level attribution cannot independently become an active objective fact; retained candidates stay pending. Relationship maintenance may use it as weak history but cannot infer a major event or an extreme change from it.
- Objective profiles and relationships do not feed Timeline summarization, Topic construction, or the knowledge graph.
- Timeline events and maintenance jobs never persist persona prompt text. Relationship maintenance resolves the current persona at execution time; the database keeps only a digest signature on relationship state and revisions for audit. A persona change during an LLM call invalidates and retries that result.
- Passive injection and the active tool can only read the current stable private user. The tool also requires an explicit `include_user_profile=true` request.

## Cleanup semantics

Cleaning summarized chat keeps Timeline and Topic. Removing all raw conversation loses message-level evidence where no snapshot exists. Deleting Timeline removes its evidence and repairs or archives derived Topics. Clearing Topic never deletes Timeline.

Resetting a user profile clears derived facts while keeping the scope enabled and starts from new changes. Deleting and disabling a profile also removes its relationship state and prevents automatic re-enrollment until an administrator restores it.

## Backups and health

Plugin version changes, database migrations, and scheduled maintenance can create backups. The health page runs SQLite integrity, foreign-key, and provenance checks without modifying data until users select a repair.

Database compaction uses a dedicated connection and briefly locks SQLite. Deleting rows alone does not return file space to the operating system.

Periodic profile lifecycle scans advance expired fact states, remove completed jobs beyond their retention period, and compact old projection revisions that are deterministically superseded. Current projections, sources still referenced by facts, and relationship revision audit data are retained.
