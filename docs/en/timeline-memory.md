# Timeline memory

Timeline records what happened during a continuous conversation window. It is the editable source for Topic reconstruction, actor provenance, and dynamic importance.

## Generation boundaries

Timeline can be produced by round-based summarization, idle summarization after a minimum number of rounds, or explicit manual and Agent writes. On restart, overdue idle windows are checked again instead of being silently skipped.

## Structured fields

New records may include summaries, timestamped facts, topics, affect, importance, quality state, conversation and persona identity, role bindings, source ranges or source snapshots, stable memory identity, and revision.

## Quality and reconstruction

Meaningless windows can produce a valid `no_memory` decision. Repairable model errors use source-grounded deterministic correction. Remaining quality risk is stored with a subtle marker so users can later select in-place reconstruction from original messages or snapshots.

Batch reconstruction finishes all selected Timeline items before synchronizing affected Topics, avoiding order-dependent derived updates.

## Dynamic importance

Timeline separates source importance from decay and access effects. Topic effective importance is projected from current Timeline state and evidence strength, rather than incrementally written back into a second decay system.
