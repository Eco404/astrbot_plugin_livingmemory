# Maintenance center

The maintenance center uses one interaction contract: inspect, preview, confirm, execute, show progress, and preserve rollback boundaries.

![Maintenance safety flow](../assets/images/maintenance-flow-en.svg){.diagram}

## Topic maintenance

It provides revectorization, related-Topic recalculation, incremental completion, ambiguity review, split and merge, archived Topic management, and complete derived-data cleanup for a selected memory space. Split/merge previews preserve the selected primary Topic and fragment groups across redraws.

## Timeline maintenance

Users can detect reconstructable or low-quality Timeline entries, apply staged edits in a batch, and manage inactive Timeline. Reconstruction preserves the Timeline ID; all selected Timeline items finish before Topic synchronization begins.

## Session and database maintenance

Session audit distinguishes message cleanup, removal of raw conversations, alias merging, and complete memory-chain deletion. Cross-database work is persisted as recoverable maintenance tasks.

Database health checks are read-only until users select repairs. Completed build artifacts can be cleaned automatically during daily maintenance; `VACUUM` remains an explicit operation because it temporarily locks SQLite.

## Traces and tests

Production recall tracing is off by default. When enabled, records show original triggers, selected memories, exact injection, and diagnostics. Recall and model tests retain independent history and JSON export.

## Atomic publication

Long Topic jobs stage all derived output and switch the formal snapshot only after validation. Existing Topics remain available during construction, and failed or cancelled jobs never expose partial atoms or actor links.

Confirmation controls lock immediately after submission to prevent duplicate operations. Persisted tasks restore their progress after a page refresh.
