# Timeline memory

Timeline records what happened during a continuous conversation window. It is the editable source for Topic reconstruction, actor provenance, and source lifecycle state.

## Generation boundaries

Timeline can be produced by base-round summarization with topic continuation, idle summarization after a minimum number of rounds, or explicit manual and Agent writes. On restart, overdue idle windows are checked again instead of being silently skipped.

## Topic continuation

Topic continuation only refines the base-round boundary for an active conversation. It does not block idle, manual, or failed-job retry summaries. Once the base window is reached, the pending window may contain multiple provisional topics. A new dialogue unit may continue when it matches any existing topic. If it matches none, the preceding units are summarized and the new unit becomes the next window's seed. A configurable hard limit always summarizes the window even when a topic keeps continuing.

The current-message embedding already produced by Topic recall is cached in the conversation database and reused when compatible. Only missing vectors are requested. Cached features are removed after their source messages are summarized or deleted. If Embedding is unavailable, the system safely falls back to the round boundary instead of losing the memory.

## Structured fields

New records may include summaries, timestamped facts, topics, affect, importance, quality state, conversation and persona identity, role bindings, source ranges or source snapshots, stable memory identity, and revision. `canonical_summary` preserves the model summary and appends key facts as the rich retrieval and Timeline-injection body; `persona_summary` retains the original model summary without that fact expansion.

## Quality and reconstruction

Meaningless windows can produce a valid `no_memory` decision. Repairable model errors use source-grounded deterministic correction. Remaining quality risk is stored with a subtle marker so users can later select in-place reconstruction from original messages or snapshots.

Batch reconstruction finishes all selected Timeline items before synchronizing affected Topics, avoiding order-dependent derived updates.

## Dynamic importance

Timeline separates window-level importance from decay and access effects. Because one Timeline can contain several subjects with different value, its overall importance is not added directly to every derived Topic.

A Topic derives its base importance from its own formal fragments and facts. Timeline lifecycle state is only a bounded modifier: it cannot raise a Topic above its semantic base and may only attenuate it slightly when sources become cold. Topic access and decay remain independent, and the current score is recomputed from state rather than accumulated as deltas.
