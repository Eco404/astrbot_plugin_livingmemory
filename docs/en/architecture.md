# Architecture

LivingMemory uses Timeline as its editable source layer and Topic as its derived retrieval layer. They do not share fact atoms; stable IDs, revisions, formal fragments, and provenance links connect them.

![LivingMemory architecture](../assets/images/architecture-overview-en.svg){.diagram}

## Data layers

| Layer | Contents | Directly editable | Purpose |
| --- | --- | --- | --- |
| Raw conversation | Messages, senders, roles, timestamps | No | Evidence, rebuilding, session audit |
| Timeline | Summary, facts, topics, affect, actor bindings | Yes | Chronological experience |
| Formal fragment | One retrieval intent plus fact-level provenance | No | Topic construction and concise supplements |
| Topic | Derived summary, atoms, actors, relations, affect | No | Cross-time organization and primary recall |
| User profile | Objective facts about the current user plus persona relationship state | Governance operations only | Private-user context and attitude continuity |

## User-profile derived layer

User profiles derive from Timeline alongside Topic; they are not a freely editable source-memory layer:

```text
stable private actor + Timeline revision
  -> objective sources and conflict projection
  -> Bot/persona/user profile revision

new user-side interaction
  -> persona/user relationship revision

current private request
  -> resolve its exact scope
  -> inject within a hard character budget
```

Objective facts keep the original Timeline key-fact text. The maintenance model may decide admission, category, confidence, importance, conflicts, and lifecycle, but cannot rewrite source text. Timeline edits, reconstruction, archive, and deletion withdraw the old revision's contribution and atomically publish a new profile; a failed job leaves the old profile available.

Relationship state is the current persona's subjective view of a logical user: six continuous dimensions, a few tags, a first-person narrative, and a short-lived aftereffect. It may read non-sensitive objective facts as context, but requires a new user-side interaction to change. It cannot create objective facts or feed Timeline summarization. Timeline events and jobs do not persist persona prompt text: maintenance resolves the current persona when it executes and validates its digest again before publication. Relationship state and revisions retain only that signature for audit.

Projection-history reads have no fixed total-row truncation and fetch through the SQLite cursor in batches. A relationship rebuild then processes meaningful Timeline rows in configurable model batches while preserving order and publishes once. Periodic lifecycle maintenance advances fact states, expires completed jobs, and compacts derivable old projection revisions so database and model context growth stay bounded.

A stable account maps to a logical user. Objective facts are isolated by Bot account, persona, and logical user by default; an explicit share group can join selected scopes for the same logical user. Relationship state always stays persona-specific. Nicknames never merge identity, and cross-platform accounts require manual administrator binding.

## Identity and provenance

Timeline has a stable `memory_uid` and revision. Formal fragments have stable logical IDs and revisions. Topic publication replaces the complete derived snapshot atomically. A Topic fact traces through its fragment to a specific Timeline revision and source fact or source window.

## Actors and affect

Message senders and Timeline role bindings anchor identity. The model may only select actor references supplied by code; unresolved mentions remain local rather than being merged by nickname. Affect events keep intensity, target, and source so retrieval does not reduce every interaction to neutral facts.

## The graph route

The graph remains an optional Timeline-derived retrieval route. It is not the authoritative source for Topic construction or Topic-first recall; provenance follows Timeline, formal fragments, Topic, and their link tables.
