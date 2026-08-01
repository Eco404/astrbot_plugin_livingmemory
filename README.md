<div align="center">

<p><a href="README_zh.md">中文</a> &nbsp;/&nbsp; <strong>English</strong> &nbsp;/&nbsp; <a href="README_ru.md">Русский</a></p>

<h1>LivingMemory</h1>

<p><strong>Source-grounded long-term memory for AstrBot: preserve experience, reorganize themes, and recall with context.</strong></p>

<p><sub>CAPTURE &nbsp;&nbsp; ORGANIZE &nbsp;&nbsp; RECALL &nbsp;&nbsp; MAINTAIN</sub></p>

<p>
  <a href="https://github.com/Eco404/astrbot_plugin_livingmemory/releases"><img src="https://img.shields.io/github/v/release/Eco404/astrbot_plugin_livingmemory?style=flat-square&color=187b78" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-e8f2f1?style=flat-square&labelColor=264642" alt="Python 3.10 or later">
  <img src="https://img.shields.io/badge/AstrBot-%3E%3D%204.24.2-f3eee4?style=flat-square&labelColor=544c3d" alt="AstrBot 4.24.2 or later">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-f2e8e5?style=flat-square&labelColor=5b403a" alt="AGPL-3.0 license"></a>
</p>

<img src="docs/assets/images/architecture-overview-en.svg" width="100%" alt="LivingMemory layered memory architecture">

</div>

## Memory in layers

<table>
<tr>
<td width="33%"><strong>TIMELINE PRESERVES</strong><br><br>Continuous conversations become editable memories with facts, affect, time ranges, actor bindings, and source snapshots.</td>
<td width="33%"><strong>TOPIC ORGANIZES</strong><br><br>Formal fragments connect related experiences across time without losing their Timeline revision or provenance.</td>
<td width="33%"><strong>RECALL STAYS FOCUSED</strong><br><br>Topic-first retrieval adds only useful facts and fragments, then falls back to Timeline when needed.</td>
</tr>
</table>

## One maintainable memory system

| Build | Recall | Operate |
| :--- | :--- | :--- |
| **Two-layer architecture**<br>Timeline is the editable source; Topic is derived and read-only. | **Current-query control**<br>The current message qualifies candidates; recent context only adds bounded support. | **Atomic publication**<br>Failed or cancelled Topic builds leave the active generation intact. |
| **Traceable structure**<br>Facts, actors, emotion, formal fragments, and revisions retain source links. | **Optional Rerank**<br>A configured provider or built-in Cloudflare client can refine qualified candidates. | **Unified maintenance**<br>Review, rebuild, session audit, database health, recall traces, and model tests share one workspace. |
| **Bounded incremental updates**<br>New Timeline revisions match a limited neighborhood of existing Topics. | **Two Topic search modes**<br>Search titles, summaries, and facts by keyword, or use Embedding similarity to find related themes. | **Data lifecycle**<br>Backups, staged edits, import/export, cleanup, archive, and in-place reconstruction are explicit operations. |
| **Topic continuation**<br>After the base round count, pending themes may extend the summary boundary until the configured hard limit. | **Agent-native tools**<br>`recall_long_term_memory` and `memorize_long_term_memory` support active memory use. | **Duplicate-submit guards**<br>Long-running actions lock immediately and restore persisted task state after refresh. |

```mermaid
flowchart LR
    A[Conversation] --> B[Timeline]
    B --> C[Formal fragments]
    C --> D[Topic]
    D --> E[Topic-first recall]
    C --> E
    B --> F[Maintenance and reconstruction]
    F --> C
```

## Start in three moves

1. Install LivingMemory from the AstrBot Plugin Market. You can also use the WebUI `+` button to install from this repository URL or a downloaded ZIP archive.
2. Reload AstrBot and open the plugin configuration. Select an LLM and Embedding provider, or leave their IDs empty to use AstrBot defaults.
3. Verify Timeline creation, test the configured models, then enable Topic memory and run the first full build.

| Provider | Purpose |
| :--- | :--- |
| LLM | Timeline summarization, formal-fragment extraction, and Topic construction. |
| Embedding | Timeline, formal-fragment, and Topic vectors. |
| Rerank | Optional candidate refinement; the plugin also supports a built-in Cloudflare Workers AI client. |

Open the workspace at `Plugins -> LivingMemory -> Pages -> dashboard`. Plugin Pages requires **AstrBot 4.24.2 or later**.

## Go deeper

| Learn | Configure | Recall | Maintain |
| :--- | :--- | :--- | :--- |
| [Quick start](https://eco404.github.io/astrbot_plugin_livingmemory/en/guide/getting-started) | [Configuration](https://eco404.github.io/astrbot_plugin_livingmemory/en/configuration) | [Recall pipeline](https://eco404.github.io/astrbot_plugin_livingmemory/en/recall) | [Maintenance center](https://eco404.github.io/astrbot_plugin_livingmemory/en/maintenance) |

The complete architecture, Timeline and Topic contracts, commands, migration behavior, and data-safety boundaries are documented on the [VitePress site](https://eco404.github.io/astrbot_plugin_livingmemory/en/).

Database upgrades follow the public `v8 -> v9 -> v10` path and then the current `v10.x` schema. Back up plugin data before testing development builds or destructive maintenance operations.

## Project

[Documentation](https://eco404.github.io/astrbot_plugin_livingmemory/en/) · [Releases](https://github.com/Eco404/astrbot_plugin_livingmemory/releases) · [Changelog](CHANGELOG.md) · [Issues](https://github.com/Eco404/astrbot_plugin_livingmemory/issues)

LivingMemory is released under the [AGPL-3.0 license](LICENSE).
