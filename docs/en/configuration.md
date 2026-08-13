# Configuration

AstrBot plugin configuration contains stable startup switches and credentials. Runtime tuning lives in the LivingMemory WebUI **Settings** page.

## Plugin configuration

| Group | Contents |
| --- | --- |
| Providers | LLM, Embedding, optional AstrBot Rerank IDs |
| Topic memory | Construction, Topic-first recall, automatic maintenance |
| Cloudflare Rerank | Built-in client credentials and endpoint |
| Session capture | Full group-message capture |
| Agent tools | Active recall and active memory write |
| Graph memory | Compatible Timeline graph route and atom switches |
| Migration and backup | Automatic migration and backup policy |

An enabled and complete built-in Cloudflare Rerank configuration takes priority over `rerank_provider_id`.

## WebUI categories

Settings are grouped by recall, generation, session, injection, lifecycle, Topic construction, models, performance, isolation, and maintenance. Overrides are sparse: restoring a field to default removes the override so future code defaults can take effect.

Recall changes usually apply immediately. Build settings affect newly created jobs; running and resumed jobs use their captured configuration and model signatures.

## User profile settings

**Settings -> User profiles** contains every runtime parameter for this feature:

| Group | Controls |
| --- | --- |
| Basic | Objective profiles, private-user auto-enrollment, relationships, and injection |
| Models and tasks | Dedicated or fallback LLM provider, concurrency, Timeline/candidate/prompt bounds, request deadline, existing-fact context limit, retries, and backoff |
| Fact admission | Truth confidence, long-term profile value, pending retention, and legacy-summary review bounds |
| Inference and sensitive data | Independent Timeline count, evidence span, confidence, cross-batch evidence pool, candidate and evidence-expansion bounds, evidence timezone, and sensitive-inference switch |
| Conflicts | New-evidence count and span plus the automatic-resolution margin |
| Freshness | Fixed and review periods for preferences, habits, states, plans, and communication style |
| Injection | `layered` / `compact_snapshot`, total budget, relationship reserve, and per-fact limit |
| Relationship | Narrative length, aftereffect period, five sensitivity levels, and four behavior modes |
| Recovery | Startup recovery, lifecycle scans, completed-job retention, projection compaction, and relationship rebuild batches |

Profiles process new private Timeline changes only; upgrading does not backfill history automatically. Sensitive behavioral inference is off by default. Passwords, tokens, API keys, private keys, and verification codes are rejected regardless of that switch.

Objective facts and relationship state use separate model calls. Each business call permits one AstrBot Provider transport attempt and has a 180-second total deadline by default. A failed call is retried by its durable maintenance job up to three times; after exhaustion it remains visible for an administrator retry. When a successful response violates the JSON, source-reference, or publication contract, two whitelist-guided correction attempts run by default without relaxing validation. Fact maintenance is jointly bounded by 8 Timelines, 16 candidate facts, and a 16,000-character prompt target. It supplies at most 200 relevant existing facts and trims existing facts, then historical behavior evidence, before ever dropping a current candidate. Cross-time behavior first discovers candidates, then sends only candidates meeting deterministic Timeline-count and span thresholds to a strict decision call. Semantic expansion and local-time neighborhoods have independent bounds; evidence timestamps are rendered in `Asia/Shanghai` by default without rewriting source data. Subjective relationship text never becomes an objective fact.

Relationship maintenance does not store persona prompt snapshots. It resolves the current persona when work executes and verifies its digest again before publication; a mid-call change invalidates and retries the result. A prompt change under the same persona ID is reconciled into subjective narrative and tags when later user-side evidence arrives, but cannot independently change long-term relationship dimensions.

Budgets are hard limits. Defaults allow 800 injected characters, give the relationship a 350-character priority budget, and cap each fact at 200. Unused fact or relationship budget flows to the other side. Cross-field validation prevents a relationship reserve above the total, fixed periods longer than review periods, or sensitive-inference thresholds below ordinary inference thresholds.

A profile lifecycle scan runs every 24 hours by default. It advances expired active facts to stale, archives due pending and long-stale facts, and removes completed jobs, superseded projection revisions, and unreferenced inactive sources beyond their retention periods. An explicit historical rebuild completes all bounded objective-fact batches first, performs behavior discovery and strict publication once at the final batch, then reads projection history through that rebuild boundary, maintains meaningful Timeline rows in batches of 32 by default, and publishes one relationship revision.
