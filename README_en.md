<div align="center">

[中文](README.md) | [English](README_en.md) | [Русский](README_ru.md)

</div>

# LivingMemory — Intelligent Long-Term Memory Plugin with Dynamic Lifecycle

**Version**: v2.1.9 | **Author**: lxfight | **License**: AGPLv3

---

## Core Features

- **Hybrid Retrieval**: Combines BM25 sparse retrieval and Faiss vector retrieval using the RRF fusion algorithm
- **Dual-Route Four-Mode Retrieval**: Simultaneously maintains document-route and graph-route retrieval, each supporting both keyword and vector search, then unified fusion ranking
- **Intelligent Summarization**: Uses LLM to automatically summarize conversation history and generate structured memories
- **Dual-Channel Summarization**: `canonical_summary` (fact-oriented, for retrieval) and `persona_summary` (persona-style, for injection) are decoupled and stored separately
- **Session Isolation**: Supports memory isolation by persona and session
- **Agent Proactive Recall**: Exposes the `recall_long_term_memory` tool so the Agent can choose when and with what keywords to recall, returning results directly to the tool context
- **Auto-Forgetting**: Intelligent cleanup mechanism based on time and importance
- **Data Safety**: Auto-backup before migration, index rebuild with backup rollback, and transaction-protected deletion
- **WebUI Management**: Visual memory management interface

---

## Quick Start

### Installation

Place the plugin folder in AstrBot's `data/plugins` directory. AstrBot will automatically install the dependencies.

### Configuration

Configure through the AstrBot console's plugin configuration page:

**Required Configuration**:
- `embedding_provider_id`: Vector embedding model ID (leave empty to use the default)
- `llm_provider_id`: Large language model ID (leave empty to use the default)

**WebUI Configuration**:
```json
{
  "webui_settings": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8080,
    "access_password": "test-token"
  }
}
```

---

## Commands

| Command | Description |
| :--- | :--- |
| `/lmem status` | View memory library status |
| `/lmem search <query> [k]` | Search memories (default 5 results) |
| `/lmem forget <id>` | Delete a specific memory |
| `/lmem rebuild-index` | Rebuild indexes (fix index inconsistency) |
| `/lmem rebuild-graph` | Rebuild graph memory indexes (backfill graph data for old memories) |
| `/lmem webui` | View WebUI information |
| `/lmem reset` | Reset the current session memory context |
| `/lmem cleanup [preview\|exec]` | Clean up memory injection fragments in message history (default preview) |
| `/lmem help` | Display help |

---

## Architecture Overview

### Module Structure

```
astrbot_plugin_livingmemory/
├── main.py                          # Plugin registration and lifecycle management
├── core/
│   ├── base/                        # Base components (config, constants, exceptions)
│   ├── managers/                    # Core managers (MemoryEngine, ConversationManager)
│   ├── retrieval/                   # Retrieval layer (document route, graph route, RRF fusion)
│   ├── validators/                  # Validators (IndexValidator)
│   ├── plugin_initializer.py        # Plugin initializer
│   ├── event_handler.py             # Event handler
│   └── command_handler.py           # Command handler
├── storage/                         # Storage layer (DBMigration, ConversationStore)
├── webui/                           # Web management interface
├── tests/                           # Test suite
└── docs/                            # Documentation
```

### Core Components

1. **PluginInitializer**: Responsible for plugin initialization
   - Non-blocking initialization mechanism
   - Provider waiting and retry logic
   - Automatic database migration

2. **EventHandler**: Handles event hooks
   - Group chat message capture
   - Memory recall
   - Memory reflection

3. **Agent Memory Tool**: Provides proactive recall capability for tool loop / agent mode
   - Tool name: `recall_long_term_memory`
   - Reuses existing session and persona isolation configuration
   - Returns raw memory list without extra prompt injection
   - Suitable for scenarios like "Do you still remember?", "What did I say before?", or "Help me recall"

4. **CommandHandler**: Handles commands
   - Unified command response format
   - Comprehensive error handling

5. **ConfigManager**: Configuration management
   - Centralized configuration loading
   - Configuration validation
   - Nested key access

---

## Agent Proactive Memory Recall

In addition to automatic memory recall, the plugin registers an LLM tool at runtime: `recall_long_term_memory`.

Characteristics of this tool:

- The Agent can decide whether to recall long-term memories itself, rather than relying solely on the current round's message as the query
- The tool's recall scope automatically inherits the current configuration's session isolation and persona isolation settings
- Retrieval results are returned as tool outputs into the agent context and do not go through the memory prompt injection path again
- More suitable when the user asks to "recall", "remember", or "what was mentioned before", or when references are ambiguous and historical context needs to be checked

Recommended call strategy:

- Prefer short keywords instead of copying the full user input
- Prioritize recalling themes, entity names, preferences, agreements, or historical events — high-information words
- If the first recall is not ideal, switch to a more specific or more abstract keyword and recall again

The returned result is a raw memory list, including memory content, relevance score, importance, and session/persona metadata, making it easy for the agent to judge which results are truly relevant

---

## Developer Guide

### Testing

```bash
# Run all tests
pytest tests/

# Run a specific test
pytest tests/test_config_manager.py

# View coverage
pytest --cov=core tests/
```

### Documentation

- [API Documentation](docs/API.md): Detailed API reference
- [Architecture Documentation](docs/ARCHITECTURE.md): System architecture description
- [Developer Guide](docs/DEVELOPMENT.md): Development and contribution guidelines

---

## Data Migration (v1.4.0-1.4.2)

If upgrading from v1.4.0-1.4.2, old data may not migrate automatically. Manual recovery steps:

1. Find the backup file: `data/plugin_data/astrbot_plugin_livingmemory/backups/livingmemory_backup_<timestamp>.db`
2. Move that file to: `data/plugin_data/astrbot_plugin_livingmemory/`
3. Rename it to: `livingmemory.db`
4. Reload the plugin; the system will automatically load and process the data

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md)

---

## Support

- **GitHub**: [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
- **Issues**: [GitHub Issues](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/issues)
- **QQ Group**: [![Join QQ Group](https://img.shields.io/badge/QQ%20Group-953245617-blue?style=flat-square&logo=tencent-qq)](https://qm.qq.com/cgi-bin/qm/qr?k=WdyqoP-AOEXqGAN08lOFfVSguF2EmBeO&jump_from=webapi&authKey=tPyfv90TVYSGVhbAhsAZCcSBotJuTTLf03wnn7/lQZPUkWfoQ/J8e9nkAipkOzwh)
  (Password: lxfight)

---

## License

This project is licensed under AGPLv3.
