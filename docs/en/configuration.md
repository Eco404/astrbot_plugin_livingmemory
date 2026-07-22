# Configuration

LivingMemory defaults work for most users. The settings you usually need to touch are model providers, recall size, memory isolation, graph retrieval, backup, and cleanup.

## Recommended profiles

| Scenario | Recommendation |
| --- | --- |
| First-time setup | Configure only `provider_settings.llm_provider_id` and `provider_settings.embedding_provider_id`; keep the rest at defaults |
| Private long-term assistant | Keep persona and session filtering enabled; keep `summary_trigger_rounds` around 8-12 |
| Group companion | Enable `session_manager.enable_full_group_capture` and consider a larger `context_window_size` |
| Low-resource server | Reduce `index_rebuild_settings.embedding_batch_size`, keep `tasks_limit = 1`, and increase request delays |
| Higher recall quality | Keep graph memory and atomization enabled; set `recall_engine.top_k` to 5-8 |
| Cost-sensitive setup | Lower `top_k`, disable recent-context expansion, and increase summary trigger rounds |

## Model providers

| Key | Default | Description |
| --- | --- | --- |
| `provider_settings.embedding_provider_id` | empty | Generates memory vectors. Empty means AstrBot's default embedding provider |
| `provider_settings.llm_provider_id` | empty | Summarizes conversations and evaluates memory importance. Empty means AstrBot's default LLM |

Try to keep the embedding model stable. If you change it and old memories recall poorly, run `/lmem rebuild-index`.

## Session management

| Key | Default | Description |
| --- | --- | --- |
| `session_manager.enable_full_group_capture` | `true` | Captures group messages that do not directly mention the bot |
| `session_manager.context_window_size` | `50` | Historical message window used for summarization and context analysis |
| `session_manager.max_messages_per_session` | `1000` | Maximum stored messages for one session |
| `session_manager.cleanup_batch_size` | `50` | Number of old summarized messages cleaned per batch |

For very busy group chats, lower `context_window_size` or disable full group capture.

## Recall and injection

| Key | Default | Description |
| --- | --- | --- |
| `recall_engine.top_k` | `5` | Number of memories automatically recalled each turn |
| `recall_engine.max_k` | `10` | Maximum results returned by active agent recall |
| `recall_engine.importance_weight` | `1.0` | Importance weight in final ranking |
| `recall_engine.fallback_to_vector` | `true` | Falls back to vector search if hybrid retrieval fails |
| `recall_engine.injection_method` | `extra_user_content` | Where or how recalled memories are injected |
| `recall_engine.inject_with_recent_context` | `false` | Searches the last two turns as auxiliary branches while keeping the current message primary |
| `recall_engine.assistant_context_mode` | `exclude` | Excludes bot replies or searches them at low/normal weight |
| `recall_engine.candidate_multiplier` | `3` | Candidate pool multiplier before filtering |
| `recall_engine.min_relevance_score` | `0.38` | Minimum automatic-recall relevance; fewer than `top_k` results are allowed |
| `recall_engine.relative_score_floor` | `0.65` | Minimum relevance relative to the best candidate in the turn |
| `recall_engine.mmr_lambda` | `0.72` | Balance between relevance and result diversity |
| `recall_engine.context_overlap_suppression` | `true` | Suppresses Timelines whose source messages remain in the raw context |
| `recall_engine.search_cache_enabled` | `true` | Enables short-term retrieval caching |

`extra_user_content` is the safest default. Gemini providers automatically fall back from `fake_tool_call` to `extra_user_content`. DeepSeek V4 thinking mode can now use normal `fake_tool_call` on recent AstrBot versions; the legacy `fake_tool_call_deepseek_v4` option is kept only as a compatibility alias and automatically falls back to `fake_tool_call`.

With recent-context expansion enabled, recent user messages are searched as a separate lower-weight branch instead of being concatenated repeatedly into the current query. Keep `assistant_context_mode=exclude` to prevent bot paraphrases from amplifying keywords; use `low_weight` or `normal` only when the previous bot response is useful retrieval context.

## Topic-first recall

| Key | Default | Description |
| --- | --- | --- |
| `topic_memory.recall_enabled` | `true` | Prefer active Topics when Topic memory is enabled |
| `topic_memory.recall_top_k` | `3` | Maximum Topics injected per recall |
| `topic_memory.recall_candidate_multiplier` | `4` | Topic candidate multiplier before filtering |
| `topic_memory.recall_scan_limit` | `2000` | Topic vector scan limit per memory space |
| `topic_memory.recall_min_relevance` | `0.32` | Minimum current-query relevance for a Topic |
| `topic_memory.recall_relative_floor` | `0.70` | Minimum relevance relative to the best Topic |
| `topic_memory.recall_selection_relative_floor` | `0.90` | Dynamic final-selection floor relative to the best current-query relevance; results may stop below the configured limit |
| `topic_memory.recall_mmr_lambda` | `0.78` | Balance between Topic relevance and diversity |
| `topic_memory.recall_use_rerank` | `true` | Rerank qualified Topic candidates once with the current query |
| `topic_memory.recall_rerank_weight` | `0.35` | Maximum influence of relative Rerank order, reduced by per-call confidence; raw absolute scores are not blended |
| `topic_memory.recall_context_support_cap` | `0.08` | Maximum ranking bonus from recent context; context cannot qualify a Topic alone |
| `topic_memory.recall_context_overlap_threshold` | `0.80` | Suppress a Topic when this fraction of its sources is already visible |
| `topic_memory.timeline_supplement_k` | `2` | Maximum Timeline supplements after a Topic match |

Topic retrieval reuses vectors saved during Topic construction and does not invoke an LLM. The combined injection never exceeds `recall_engine.top_k`. If no active/relevant Topic exists, recall falls back to Timeline-only behavior. Partial source overlap only reduces the Topic score; the Topic is suppressed only after the configured coverage threshold is reached.

## Memory isolation

| Key | Default | Description |
| --- | --- | --- |
| `filtering_settings.use_persona_filtering` | `true` | Only recall memories for the current persona |
| `filtering_settings.use_session_filtering` | `true` | Only recall memories for the current session |

Disable session filtering only if you intentionally want different chats to share one long-term memory pool. Keep persona filtering enabled when the bot has distinct personas.

## Reflection and lifecycle

| Key | Default | Description |
| --- | --- | --- |
| `reflection_engine.summary_trigger_rounds` | `10` | Number of conversation rounds before summarization |
| `importance_decay.decay_rate` | `0.01` | Daily importance decay |
| `importance_decay.access_decay_window_days` | `30.0` | Time window for access reinforcement |
| `importance_decay.access_decay_max_count` | `10` | Maximum access reinforcement count |

Lower `summary_trigger_rounds` if you want the bot to remember faster. Raise it if you want fewer LLM calls.

## Agent tools

| Key | Default | Description |
| --- | --- | --- |
| `agent_tools.enable_recall_tool` | `true` | Registers `recall_long_term_memory` for active recall |
| `agent_tools.enable_memorize_tool` | `false` | Registers `memorize_long_term_memory` for active writes |

The write tool is powerful and depends on model discipline. Start with active recall, then enable active writes after observing stable behavior.

## Graph memory and atomization

| Key | Default | Description |
| --- | --- | --- |
| `graph_memory.enabled` | `true` | Enables graph-route retrieval |
| `graph_memory.document_route_weight` | `0.65` | Document-route weight |
| `graph_memory.graph_route_weight` | `0.35` | Graph-route weight |
| `graph_memory.cross_route_bonus` | `0.08` | Bonus when both routes hit the same memory |
| `graph_memory.expansion_hops` | `1` | Graph neighbor expansion hops |
| `graph_memory.dynamic_route_weighting` | `true` | Adjusts route weights based on query intent |
| `graph_memory.atom_enabled` | `true` | Enables memory atomization |

For relationship-heavy use, increase graph-route weight or set `expansion_hops` to `2`. If the database is large, second-hop expansion adds query cost, so use the WebUI recall debugger to inspect results first.

## Backup, migration, and cleanup

| Key | Default | Description |
| --- | --- | --- |
| `migration_settings.auto_migrate` | `true` | Migrates old databases at startup |
| `migration_settings.create_backup` | `true` | Creates a backup before migration |
| `backup_settings.enabled` | `true` | Daily database backup |
| `backup_settings.keep_days` | `7` | Backup retention days |
| `forgetting_agent.auto_cleanup_enabled` | `true` | Daily cleanup for old low-importance memories |
| `forgetting_agent.cleanup_days_threshold` | `30` | Age threshold for cleanup candidates |
| `forgetting_agent.cleanup_importance_threshold` | `0.3` | Importance threshold for cleanup candidates |

For production use, keep backups and migration backups enabled. To make cleanup more conservative, raise the day threshold or lower the importance threshold.

## Index rebuild tuning

| Key | Default | Description |
| --- | --- | --- |
| `index_rebuild_settings.batch_size` | `50` | Memories read per batch |
| `index_rebuild_settings.embedding_batch_size` | `8` | Texts per embedding request |
| `index_rebuild_settings.tasks_limit` | `1` | Embedding concurrency limit |
| `index_rebuild_settings.max_retries` | `5` | Retry count for a failed batch |
| `index_rebuild_settings.request_delay` | `5.0` | Delay between embedding requests |
| `index_rebuild_settings.max_failure_ratio` | `0.02` | Allowed failure ratio |

If you hit API rate limits, increase `request_delay` first, then lower `embedding_batch_size`. Avoid raising concurrency blindly; index rebuilds are more about finishing reliably than finishing aggressively.
