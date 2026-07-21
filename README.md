<div align="center">

[中文](README_zh.md) | [English](README.md) | [Русский](README_ru.md)

</div>

# LivingMemory - Intelligent Long-Term Memory Plugin with Dynamic Lifecycle

<p align="center">
  <a href="https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/releases"><img src="https://img.shields.io/github/v/release/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory?color=76bad9" alt="Release"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/en/"><img src="https://img.shields.io/badge/docs-English%20%7C%20中文-3d7f8f" alt="Documentation"></a>
  <a href="https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/stargazers"><img src="https://img.shields.io/github/stars/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory?style=social" alt="Stars"></a>
  <a href="https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-red" alt="License AGPLv3"></a>
</p>

<p align="center">
  <a href="https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/en/">English Documentation</a>
  ·
  <a href="https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/">中文文档</a>
</p>

---

## Core Features

- **Hybrid Retrieval**: Combines BM25 sparse retrieval and Faiss vector retrieval with RRF fusion.
- **Dual-Route Four-Mode Retrieval**: Maintains both document and graph routes, each supporting keyword and vector retrieval before unified ranking.
- **Intelligent Summarization**: Uses an LLM to summarize conversation history into structured memories.
- **Dual-Channel Summarization**: Stores `canonical_summary` and `persona_summary` separately for retrieval and prompt injection.
- **Session Isolation**: Supports persona-level and session-level memory isolation.
- **Agent Memory Tools**: Exposes `recall_long_term_memory` and `memorize_long_term_memory` so agents can actively recall or write long-term memories when needed.
- **Auto-Forgetting**: Cleans up stale memories based on time and importance.
- **Memory Atomization**: Each key fact becomes an independent retrieval unit with its own TTL, decay curve, and lifecycle management.
- **Time-Aware Graph**: Edge confidence updates dynamically via EMA as new evidence accumulates; cross-memory semantic edge merging; temporal decay in retrieval scoring.
- **Data Safety**: Automatic backup on plugin version update, pre-migration backup, rollback on index rebuild failure, and transactional deletion.
- **WebUI Management**: Supports the AstrBot official plugin Pages dashboard with trilingual (zh/en/ru) support and dark mode.
- **Topic Memory**: Builds read-only, source-grounded topic memories above the existing Timeline layer, with full/incremental maintenance, optional reranking, and Topic-first production recall with lightweight Timeline supplements.

---

## Quick Start

### Installation

Place the plugin folder under AstrBot's `data/plugins` directory. AstrBot installs dependencies automatically.

### Configuration

Configure the plugin from the AstrBot plugin configuration page.

**Required settings**:
- `embedding_provider_id`: Embedding model ID. Leave empty to use the AstrBot default.
- `llm_provider_id`: LLM model ID. Leave empty to use the AstrBot default.
- `rerank_provider_id`: Optional Rerank model ID used to validate cross-time Topic matches.

If AstrBot does not expose a suitable Rerank Provider, enable `cloudflare_rerank.enabled` and configure the Cloudflare `account_id` and `api_token` (or the `CLOUDFLARE_AUTH_TOKEN` environment variable). The default model is `@cf/baai/bge-reranker-base`. Cloudflare relevance scores are consumed directly in their documented `[0, 1]` range; temporary failures fall back to Embedding matching.

**Authoritative identity profiles**:
- Manage known participant facts from the **Identity Profiles** page in the plugin WebUI rather than AstrBot plugin settings. Profiles are stored in `authoritative_identities.json` under the plugin data directory and injected into Timeline summarization and both Topic extraction/synthesis prompts.
- The platform selector combines live AstrBot instances, recorded conversations, and existing profiles. Adapter aliases such as `aiocqhttp` and `qq_official` are canonicalized to `qq`. Blank platforms remain a backwards-compatible cross-platform wildcard but can collide with the same account ID on another service.
- `user_id` is required and should be the stable platform account ID. `platform` scopes that ID; `display_name` and `aliases` are recognition/display aids. Optional fields include `gender`, `pronouns`, and `notes`.
- For 示例甲, enter platform `qq`, stable account ID `10000001`, display name `示例甲`, gender `男性`, and pronouns `他, 他的`.
- Saves take effect immediately without restarting. Saving is blocked while a Topic build is active so one build cannot mix two profile versions.
- When neither a matching profile nor an explicit source pronoun exists, prompts require the model to repeat the display name. This stage intentionally does not add deterministic post-generation identity validation.

**Experimental Topic memory**:
- Enable `topic_memory.enabled`, then run one full build from the Topic Memory page. Automatic maintenance can be controlled by `topic_memory.auto_maintenance`.
- `topic_memory.recall_enabled` defaults to on. Active Topics are recalled first with a small Timeline supplement budget configured from **Topic Memory → Settings**. Missing or failed Topic retrieval falls back to Timeline-only recall.
- Topic recall reuses stored embeddings, optionally reranks candidates, and never invokes an LLM. Visible-source overlap, relevance thresholds, and diversity controls are managed from the Topic settings panel.
- Topic memories are derived and read-only. Edit their source Timeline memories instead; dependent Topics are marked stale and rebuilt.
- **Maintenance** checks whether each active Timeline revision has an active Topic index, lists missing entries with all selected by default, and incrementally processes only the confirmed selection instead of using a fixed 24-hour window.
- Wide candidate groups are extracted in batches of 12 by default and large Topic components are synthesized hierarchically in batches of 12. Both values are available from **Topic Memory → Settings**, while the page reports the active component, batch, LLM call, and elapsed time.
- **Topic Memory → Settings** owns recall, build-quality, Rerank, batching, concurrency, and resilience parameters. Sparse overrides can be reset individually or together so updated code defaults apply automatically. Recall settings apply immediately; build settings affect new tasks.
- LLM and Rerank concurrency both default to 1. Check Provider rate limits before increasing them and lower Rerank concurrency if Cloudflare returns HTTP 429.
- Topic-fragment matching combines Embedding similarity, native Rerank relevance, and reciprocal relative ranks. Requesting the complete candidate ordering keeps matching useful when a provider's absolute scores are tightly clustered. Merging two established components now raises the required average cohesion gradually with their combined size, while singleton attachment remains unchanged, preventing a few boundary bridges from creating oversized Topics.
- Related-topic edges are sparse, undirected links without a parent/child hierarchy. They ignore standalone calendar dates, clock values, and split date parts, while named identifiers containing letters, such as `Expo2026`, remain available as evidence.
- After changing relation thresholds or the maximum degree, use **Topic Memory → Maintenance → Recompute related topics** to replace only the relation graph from stored Topic vectors. It does not invoke LLM, Embedding, or Rerank providers and does not rewrite Topic content.
- Failed, cancelled, or restart-interrupted builds expose a **Resume from checkpoint** action. Candidate fragments, embeddings, matching, component synthesis, and completed materialization are reused under the same `run_uid`; changed input, prompts, Provider, model, or relevant configuration invalidates only the affected checkpoint.
- Recoverable LLM structure errors are deterministically repaired from supplied sources and recorded in `validation_repairs`. Unverifiable model references are discarded rather than persisted as Topic provenance.
- Fragment extraction targets one future retrieval intent per fragment. Related-topic edges combine semantic neighbors, corpus-aware keyword rarity, lexical overlap, and Timeline provenance under a per-Topic degree limit instead of requiring reciprocal Top-N as a hard gate. Topic and atom confidence is calibrated by independent time-cluster evidence so several nearby memories do not masquerade as repeated confirmation.
- Database v9.6 formalizes revision-scoped Topic fragments and backfills existing fragment provenance without invoking a model or rewriting Topics. Legacy fragments are served only after a later rebuild normalizes the Bot's first-person Timeline narration into mapped third-person roles; `topic_memory.enabled` and `topic_memory.recall_enabled` control recall.

**Memory injection compatibility**:
- `fake_tool_call` automatically falls back to `extra_user_content` for Gemini providers to avoid tool-message protocol incompatibility.
- DeepSeek V4 `thinking` mode can use normal `fake_tool_call` on recent AstrBot versions. The legacy `fake_tool_call_deepseek_v4` option is kept for compatibility and automatically falls back to `fake_tool_call`.

### AstrBot Version Requirement

- The **AstrBot official plugin Pages dashboard** requires **AstrBot >= 4.24.2**.

### Management Entry

1. Open the AstrBot official WebUI.
2. Go to `Plugins -> LivingMemory -> Pages -> dashboard`.

---

## Commands

| Command | Description |
| :--- | :--- |
| `/lmem status` | View memory status |
| `/lmem search <query> [k]` | Search memories (default 5 items) |
| `/lmem forget <id>` | Delete a specific memory |
| `/lmem rebuild-index` | Rebuild indexes |
| `/lmem rebuild-graph` | Rebuild graph memory indexes |
| `/lmem webui` | View WebUI information |
| `/lmem summarize` | Trigger immediate summarization for the current session |
| `/lmem reset` | Reset current session memory context |
| `/lmem cleanup [preview\|exec]` | Clean injected memory fragments from history |
| `/lmem help` | Show help |

---

## Architecture

### Module Structure

```
astrbot_plugin_livingmemory/
├── main.py                          # Plugin registration and lifecycle management
├── core/
│   ├── base/                        # Base components
│   ├── managers/                    # Core managers
│   ├── retrieval/                   # Retrieval layer
│   ├── validators/                  # Validators
│   ├── plugin_initializer.py        # Plugin initializer
│   ├── event_handler.py             # Event handler
│   └── command_handler.py           # Command handler
├── storage/                         # Storage layer
├── pages/dashboard/                 # AstrBot official plugin Pages assets
├── tests/                           # Test suite
└── docs/                            # Documentation
```

### Core Components

1. **PluginInitializer**
   - Non-blocking initialization
   - Provider wait and retry
   - Automatic database migration

2. **EventHandler**
   - Group message capture
   - Memory recall
   - Memory reflection

3. **Agent Memory Tools**
   - `recall_long_term_memory`: actively recalls long-term memories, reusing current session/persona filtering settings and returning raw memory results
   - `memorize_long_term_memory`: actively writes long-term memories, always using the current UMO and persona while reusing the automatic summarization storage format
   - Useful for scenarios such as “do you remember”, “what did I say before”, and “please remember ...”

4. **CommandHandler**
   - Unified command responses
   - Structured error handling

5. **PluginPageApi**
   - Registers plugin page APIs through `register_web_api`
   - Reuses the runtime memory engine and graph components
   - Provides memory management, recall debugging, and graph queries for `pages/dashboard`

6. **ConfigManager**
   - Centralized configuration loading
   - Configuration validation
   - Nested key access

---

## Agent Memory Tools

The plugin registers two LLM tools at runtime so agents can actively manage long-term memory:

- `recall_long_term_memory`: actively recalls existing memories. Use it when the user asks what the bot remembers, asks about previous context, or when ambiguous references require checking history. Prefer short keywords such as topics, entities, preferences, or agreements.
- `memorize_long_term_memory`: actively writes long-term memory. Use it when the user explicitly asks the bot to remember something, or when stable preferences, durable facts, agreements, identity details, or long-lived project context appear.

---

## Developer Guide

### Testing

```bash
# Run all tests
pytest tests/

# Run a specific test
pytest tests/test_config_manager.py

# Show coverage
pytest --cov=core tests/
```

### Documentation

- [VitePress Documentation Site](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/en/): Quick start, features, WebUI usage, architecture, and docs deployment.
- [中文文档](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/): Chinese documentation site.

---

## Data Migration (v1.4.0-v1.4.2)

If you upgrade from v1.4.0-v1.4.2, old data may not migrate automatically. Manual recovery steps:

1. Locate the backup file: `data/plugin_data/astrbot_plugin_livingmemory/backups/livingmemory_backup_<timestamp>.db`
2. Move it to `data/plugin_data/astrbot_plugin_livingmemory/`
3. Rename it to `livingmemory.db`
4. Reload the plugin. The system will load and process the data automatically.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## Support

- **GitHub**: [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
- **Issues**: [GitHub Issues](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/issues)
- **QQ Group**: [![Join QQ Group](https://img.shields.io/badge/QQ%20Group-953245617-blue?style=flat-square&logo=tencent-qq)](https://qm.qq.com/cgi-bin/qm/qr?k=WdyqoP-AOEXqGAN08lOFfVSguF2EmBeO&jump_from=webapi&authKey=tPyfv90TVYSGVhbAhsAZCcSBotJuTTLf03wnn7/lQZPUkWfoQ/J8e9nkAipkOzwh)
  (Password: lxfight)

---

## License

This project is licensed under AGPLv3.
