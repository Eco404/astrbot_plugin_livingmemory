# Maintenance center

The maintenance center uses one interaction contract: inspect, preview, confirm, execute, show progress, and preserve rollback boundaries.

![Maintenance safety flow](../assets/images/maintenance-flow-en.svg){.diagram}

## Topic maintenance

It provides revectorization, related-Topic recalculation, incremental completion, ambiguity review, split and merge, archived Topic management, and complete derived-data cleanup for a selected memory space. Split/merge previews preserve the selected primary Topic and fragment groups across redraws.

## Timeline maintenance

Users can detect reconstructable or low-quality Timeline entries, apply staged edits in a batch, and manage inactive Timeline. Reconstruction preserves the Timeline ID; all selected Timeline items finish before Topic synchronization begins.

## User profile maintenance

The user-profile tab groups scopes by stable private account, Bot account, and persona. Four compact status cells summarize the scope and Bot/persona, objective revision and fact-state counts, relationship revision and execution-time persona basis, and active jobs plus unprojected gaps. Its effective-injection preview uses the same relevance, freshness, and character-budget rules as the private-chat path and shows actual characters, fact count, and whether the relationship was included.

| Area | Operations |
| --- | --- |
| Objective facts | Inspect raw Timeline facts and revisions; confirm pending facts, pause/resume injection, pin, or exclude; only active facts can inject |
| Conflicts | Select a winner, keep injection paused, or exclude all; later evidence may still trigger automatic review |
| Relationship | Edit six dimensions, tags, narrative, sensitivity, and behavior; freeze, reset, rebuild, or roll back a revision |
| Accounts | Manually bind accounts after a preview; unbinding reprojects from original-account provenance |
| Sharing | Explicitly share objective facts across selected scopes for one logical user; relationships stay isolated |
| Recovery | Inspect and retry jobs, handle detected gaps, or rebuild from history |

Raw objective-fact text cannot be edited on this page. Correct the authoritative Timeline or use confirmation, pause, exclusion, and conflict overrides. `pending`, `conflict`, and `stale` are explicitly grouped as non-injecting states. Relationship dimensions and subjective narrative are directly editable, with prior values retained in revision history. Model-maintained revisions identify the current persona used at execution but never store persona prompt text or snapshots.

**Reset profile** clears derived output but keeps the scope enabled and processes new changes from that point. **Delete and disable** also removes relationship state and blocks automatic re-enrollment. Historical rebuild is separate: it previews exactly attributable Timelines, ambiguous identities, missing projections, facts, and overrides, then validates both profile and history fingerprints. Ambiguous legacy identities are never guessed. Attributable history completes objective facts in bounded batches, runs cross-Timeline behavior discovery and strict publication once in the final batch, then rebuilds the relationship with the current same-ID persona. Behavior discovery uses short references, readable local observation time, and bounded semantic/time expansion; application-side Timeline, span, confidence, profile-value, and sensitive-data checks still decide publication. A failed stage never skips its checkpoint or publishes later relationship state. The task view reports Timeline count, candidate count, prompt estimate, objective/behavior/relationship stage elapsed time, and automatic retry state.

Periodic profile lifecycle work runs alongside administrator actions. It advances expired `active` facts to `stale`, archives due `pending` and long-stale facts, and removes completed jobs and deterministically rebuildable projection revisions after their retention windows. Current projections and sources still referenced by facts remain. Active-job and gap counters distinguish work in progress from events that have not yet been projected into a published profile.

Maintenance jobs expose state-specific controls: running or failed jobs can be cancelled, failed jobs can also be retried, and completed job records can be deleted individually or cleared together. Batches from one historical build share a build identifier. Cancelling one batch cancels every unfinished batch and event in that build without rolling back fact revisions that were already published atomically. After cancellation or exhausted automatic retries, **Continue from gap** resumes only unfinished events and rebuilds the persona relationship once at the end.

## Session and database maintenance

Session audit distinguishes message cleanup, removal of raw conversations, alias merging, and complete memory-chain deletion. Cross-database work is persisted as recoverable maintenance tasks.

Database health checks are read-only until users select repairs. Completed build artifacts can be cleaned automatically during daily maintenance; `VACUUM` remains an explicit operation because it temporarily locks SQLite.

## Traces and tests

Production recall tracing is off by default. When enabled, records show original triggers, selected memories, exact injection, and diagnostics. Recall and model tests retain independent history and JSON export.

## Atomic publication

Long Topic jobs stage all derived output and switch the formal snapshot only after validation. Existing Topics remain available during construction, and failed or cancelled jobs never expose partial atoms or actor links.

Confirmation controls lock immediately after submission to prevent duplicate operations. Persisted tasks restore their progress after a page refresh.
