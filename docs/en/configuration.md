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
| Models and tasks | Dedicated or fallback LLM provider, concurrency, batching, retries, and backoff |
| Fact admission | Acceptance confidence and pending retention |
| Inference and sensitive data | Independent Timeline count, evidence span, confidence, and sensitive-inference switch |
| Conflicts | New-evidence count and span plus the automatic-resolution margin |
| Freshness | Fixed and review periods for preferences, habits, states, plans, and communication style |
| Injection | `layered` / `compact_snapshot`, total budget, relationship reserve, and per-fact limit |
| Relationship | Narrative length, aftereffect period, five sensitivity levels, and four behavior modes |
| Recovery | Startup recovery limit and completed-job retention |

Profiles process new private Timeline changes only; upgrading does not backfill history automatically. Sensitive behavioral inference is off by default. Passwords, tokens, API keys, private keys, and verification codes are rejected regardless of that switch.

Objective facts and relationship state use separate model calls. Provider retries do not consume the per-batch business-call allowance, and one fact namespace is always maintained serially. Relationship behavior controls how state may influence a reply; subjective relationship text never becomes an objective fact.

Budgets are hard limits. Defaults allow 800 injected characters, reserve 200 for the relationship, and cap each fact at 200. Cross-field validation prevents a relationship reserve above the total, fixed periods longer than review periods, or sensitive-inference thresholds below ordinary inference thresholds.
