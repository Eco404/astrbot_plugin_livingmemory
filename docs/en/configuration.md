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
