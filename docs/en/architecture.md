# Architecture

LivingMemory is built from event hooks, memory processing, retrieval fusion, storage, and a Pages API. Automatic memory and active agent tools share the same core data model so they do not become two separate memory systems.

<img class="diagram" src="/images/architecture-flow.svg" alt="LivingMemory runtime architecture">

## Runtime flow

1. AstrBot receives a message and `EventHandler` captures session context.
2. Before the LLM request, the recall pipeline searches long-term memory using the current message and optional recent context.
3. Retrieved memories are injected into the request or returned as agent tool results.
4. After the LLM responds, the reflection pipeline decides whether to summarize and store new memory.
5. Background tasks handle decay, cleanup, backup, and index validation.

## Main modules

| Module | Responsibility |
| --- | --- |
| `main.py` | Registers the plugin, initializes runtime components, registers agent tools and Pages API |
| `core/plugin_initializer.py` | Non-blocking initialization, provider waiting, database migration, index loading |
| `core/event_handler.py` | Group capture, memory recall, memory reflection |
| `core/managers/memory_engine.py` | Unified write, search, delete, and index maintenance |
| `core/managers/graph_memory_manager.py` | Coordinates graph nodes, edges, entries, and graph retrieval |
| `core/managers/atom_lifecycle_manager.py` | Maintains atom expiration, forgetting, reinforcement, and lifecycle state |
| `core/retrieval/` | BM25, vector, graph, atom retrieval, and RRF fusion |
| `storage/` | SQLite storage, graph storage, atom storage, database migration |
| `pages/dashboard/` | AstrBot Pages management UI |

## Dual-route retrieval

Document memories and graph memories are searched through two routes:

| Route | Keyword mode | Vector mode |
| --- | --- | --- |
| Document route | `BM25Retriever` | `VectorRetriever` |
| Graph route | `GraphKeywordRetriever` | `GraphVectorRetriever` |

`RRFFusion` merges the ranked lists, then the runtime applies importance, time decay, session filtering, and persona filtering.

## Memory data model

| Type | Description |
| --- | --- |
| Session messages | Raw conversation context used for summarization triggers and expanded queries |
| Memory entries | LLM-generated long-term memories with summaries, importance, session, and persona metadata |
| Graph nodes and edges | Entities and relationships extracted from memories, with cross-memory merging |
| Memory atoms | Independent fact units with type, TTL, decay, and access reinforcement |

### Timeline identity and source spans

Every new memory receives a stable `memory_uid`, a monotonic `revision`, and a
deterministic `memory_space_id`. The physical `documents.id` may change during a
new-ID replacement, while `memory_registry` keeps the same logical UID mapped to
the current document.

`memory_source_spans` stores the session, message IDs, message indexes, and time
range separately. Legacy memories without stable message evidence are marked as
partially or not traceable instead of receiving fabricated provenance. These
records remain in the `timeline` layer and do not change existing generation or
retrieval behavior.

### Derived Topic-memory storage (phase-two foundation)

Topic memories are automatically derived from Timeline memories. This phase only
introduces storage and provenance; generation and retrieval remain disabled.
`topic_memories` stores generated snapshots and independent importance state,
while `topic_memory_atoms` stores Topic-owned atoms without reusing or mutating
Timeline atoms in `memory_atoms`.

`topic_timeline_links` forms a bidirectional many-to-many index using stable UIDs
and records source revisions, time clusters, semantic similarity, temporal
affinity, and contribution weight. Nearby Timeline fragments can share one time
cluster, so a long conversation is not treated as several independent votes.

`topic_atom_sources` maps each Topic atom to a Timeline atom ID or content
fingerprint. Editing a Timeline marks only dependent Topics stale for later
targeted rebuilding. `topic_maintenance_runs` stores resumable cursors and live
progress for full, incremental, and repair jobs. Topic snapshots use optimistic
revisions and transactional replacement and have no manual WebUI editing path.

## Data safety

| Scenario | Protection |
| --- | --- |
| Plugin version change | Startup creates a version-tagged backup |
| Database migration | Backup before migration |
| Index rebuild | Batched rebuild with rollback on failure |
| Memory deletion | Transactional deletion of related records |
| Dashboard operations | Pages API reuses MemoryEngine and GraphStore instead of bypassing backend safety logic |
