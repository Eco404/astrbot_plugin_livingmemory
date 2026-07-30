# Quick start

This page covers the minimum setup needed to verify the complete memory path before tuning thresholds.

## Requirements

| Component | Requirement |
| --- | --- |
| AstrBot | `4.24.2` or newer |
| Python | `3.10+` |
| LLM Provider | Timeline summarization and Topic construction |
| Embedding Provider | Timeline, formal-fragment, and Topic vectors |
| Rerank Provider | Optional candidate refinement |

## Install

Installing through the AstrBot WebUI is recommended; you do not need to copy the plugin directory manually.

### Install from the Plugin Market

1. Start AstrBot and open its admin panel. The default address is `http://localhost:6185`; replace `localhost` with the server IP address or domain when AstrBot runs on another host.
2. Open **Plugins** from the sidebar and select the **Plugin Market** tab.
3. Search for `LivingMemory` or `astrbot_plugin_livingmemory`, open the plugin details, and install it.
4. Wait for dependency installation and plugin loading to finish, then verify that LivingMemory appears in the installed-plugin list.

### Install from a URL or archive

Use manual installation when the market does not yet contain the required version or when you need a specific branch:

1. Open **Plugins** in the AstrBot WebUI and click the `+` button in the lower-right corner.
2. Install from a repository URL, or upload a ZIP archive downloaded from the repository.
3. Check the plugin status after installation. If loading fails, fix the reported problem and use **Try one-click reload fix** instead of restarting the whole AstrBot process.

::: tip Download or dependency failures
AstrBot distributes plugins through GitHub. If network access is restricted, configure an HTTP proxy in AstrBot's other settings or upload a downloaded ZIP archive. If a Python dependency is missing, inspect the platform log first and then use the WebUI Pip-package installer when needed.
:::

See the [official AstrBot WebUI documentation](https://docs.astrbot.app/en/use/webui.html#plugins) for the current plugin-management workflow.

## Configure

In plugin configuration, select the LLM and Embedding providers or leave them empty to use AstrBot defaults. Enable Topic construction, Topic-first recall, and automatic maintenance when ready.

## First run

1. Accumulate enough conversation or run `/lmem summarize`.
2. Confirm a structured entry on **Timeline memory**.
3. Validate LLM, Embedding, and optional Rerank in **Maintenance → Model test**.
4. Run one full build from **Topic memory**.
5. Compare Timeline-only, Topic-only, and current behavior in **Recall test**.

<div class="status-strip">
  <div><strong>Timeline exists</strong><span>Summary, facts, time, and session ownership are correct.</span></div>
  <div><strong>Topic is traceable</strong><span>Formal fragments open their source Timeline.</span></div>
  <div><strong>Recall is explainable</strong><span>Diagnostics show thresholds, filters, and actual injection.</span></div>
</div>
