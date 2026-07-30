# 快速开始

本页只覆盖让 LivingMemory 正常运行所需的最少步骤。构建参数、阈值和维护策略可以在确认基础链路正常后再调整。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | `4.24.2` 或更高版本 |
| Python | `3.10+` |
| LLM Provider | 用于 Timeline 总结和 Topic 构建 |
| Embedding Provider | 用于 Timeline、正式片段和 Topic 向量 |
| Rerank Provider | 可选；用于候选精排与跨时间匹配 |

## 安装

推荐通过 AstrBot WebUI 安装，不需要手动复制插件目录。

### 从插件市场安装

1. 启动 AstrBot，打开管理面板。默认地址为 `http://localhost:6185`；部署在其他主机时，将 `localhost` 替换为对应的 IP 或域名。
2. 在左侧菜单进入 **插件**，切换到 **插件市场**。
3. 搜索 `LivingMemory` 或 `astrbot_plugin_livingmemory`，打开插件详情并点击安装。
4. 等待依赖安装和插件加载完成，然后确认 LivingMemory 出现在已安装插件列表中。

### 通过 URL 或压缩包安装

如果插件市场尚未收录当前版本，或者需要安装指定分支：

1. 进入 AstrBot WebUI 的 **插件** 页面，点击右下角的 `+`。
2. 选择通过仓库 URL 安装，或上传从仓库下载的 ZIP 压缩包。
3. 安装完成后检查插件状态。如果插件加载失败，可在错误修复后使用 **尝试一键重载修复**，无需重启整个 AstrBot。

::: tip 下载或依赖安装失败
插件使用 GitHub 分发。网络受限时，可在 AstrBot 的其他设置中配置 HTTP 代理，或者改用 ZIP 压缩包上传。如果报错缺少 Python 依赖，先检查平台日志，再使用 WebUI 提供的 Pip 库安装功能补充依赖。
:::

更新的管理面板操作说明可参考 [AstrBot 官方 WebUI 文档](https://docs.astrbot.app/use/webui.html#插件)。

进入 AstrBot 的插件配置页面，优先确认以下项目：

1. `provider_settings.llm_provider_id`：留空时回退到 AstrBot 默认 LLM。
2. `provider_settings.embedding_provider_id`：留空时回退到 AstrBot 默认 Embedding。
3. `topic_memory.enabled`：准备使用 Topic 时开启。
4. `topic_memory.recall_enabled`：让实际对话优先召回 Topic。
5. `topic_memory.auto_maintenance`：新增 Timeline 后自动执行局部维护。

::: tip Rerank 可以后配
没有 Rerank 时，插件仍可依赖 Embedding 与关键词运行。AstrBot 暂无合适 Provider 时，也可以启用插件内置 Cloudflare Workers AI Rerank。
:::

## 第一次运行

1. 在真实会话中积累达到总结条件的消息，或使用 `/lmem summarize` 手动触发。
2. 打开插件 WebUI 的 **Timeline 记忆**，确认出现结构化记忆。
3. 前往 **维护 → 模型测试**，验证 LLM、Embedding 和可选 Rerank。
4. 开启 Topic 后，进入 **Topic 记忆** 执行一次全量构建。
5. 在 **维护 → 召回测试** 中分别测试“仅 Timeline”“仅 Topic”和“当前插件行为”。

## 成功标准

<div class="status-strip">
  <div><strong>Timeline 可见</strong><span>摘要、事实、时间和会话归属正确。</span></div>
  <div><strong>Topic 可追溯</strong><span>Topic 能打开正式片段和来源 Timeline。</span></div>
  <div><strong>召回可解释</strong><span>诊断中能看到候选、阈值、过滤原因和实际注入。</span></div>
</div>

## 下一步

- 阅读 [整体架构](/architecture)，理解为什么 Topic 不能直接编辑。
- 阅读 [召回与注入](/recall)，调整当前查询与跨轮上下文的权重。
- 阅读 [维护中心](/maintenance)，了解重构、审查和数据库操作的安全边界。
