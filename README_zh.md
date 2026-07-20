<div align="center">

[中文](README_zh.md) | [English](README.md) | [Русский](README_ru.md)

</div>

# LivingMemory - 动态生命周期记忆插件

<p align="center">
  <a href="https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/releases"><img src="https://img.shields.io/github/v/release/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory?color=76bad9" alt="Release"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/"><img src="https://img.shields.io/badge/docs-中文%20%7C%20English-3d7f8f" alt="Documentation"></a>
  <a href="https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/stargazers"><img src="https://img.shields.io/github/stars/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory?style=social" alt="Stars"></a>
  <a href="https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-red" alt="License AGPLv3"></a>
</p>

<p align="center">
  <a href="https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/">中文文档</a>
  ·
  <a href="https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/en/">English Documentation</a>
</p>

---

## 核心特性

- **混合检索**: 结合 BM25 稀疏检索和 Faiss 向量检索，使用 RRF 融合算法
- **双路四模式检索**: 同时维护文档路与图路，两边都支持关键词检索与向量检索，再统一融合排序
- **智能总结**: 使用 LLM 自动总结对话历史，生成结构化记忆
- **双通道总结**: `canonical_summary`（事实导向，用于检索）与 `persona_summary`（人格风格，用于注入）解耦存储
- **会话隔离**: 支持按人格和会话隔离记忆
- **Agent 主动回忆**: 暴露 `recall_long_term_memory` 工具，Agent 可自行选择回忆时机与关键词，将结果直接带回工具上下文
- **自动遗忘**: 基于时间和重要性的智能清理机制
- **数据安全**: 迁移前自动备份、索引重建带备份回滚、删除操作带事务保护
- **定时自动备份**: 每日自动备份记忆数据库，支持保留策略和过期清理
- **伪造工具调用注入**: 新的记忆注入策略，模拟 LLM 工具调用，兼容 Agent / Tool Loop 模式，使记忆上下文与真实召回不可区分
- **图片转述记忆**: 自动将 AstrBot 图片转述结果存入长期记忆，支持视觉对话的召回
- **记忆原子化系统**: 将每个关键事实提升为独立检索单元，拥有独立的存活时间 (TTL)、衰减曲线和生命周期管理
- **时间感知图谱**: 边置信度随证据累积动态更新，跨记忆语义边合并，检索评分引入时间衰减
- **3D 知识图谱 WebUI**: 交互式 3D 力导向图可视化记忆实体与关系，支持缩放、旋转和节点查看
- **安全分批索引重建**: 以小批量原子方式重建大型索引，防止内存溢出和损坏；失败时自动回滚
- **版本备份**: 插件版本更新时自动备份所有数据文件到版本标记目录，便于数据恢复
- **WebUI 管理**: 可视化记忆管理界面，支持三语（中/英/俄）和深色模式
- **实验性 Topic 记忆**: 在原 Timeline 层之上构建可溯源、只读的主题记忆，支持全量/增量维护和可选 Rerank；当前阶段不参与正式召回。

---

## 快速开始

### 安装

将插件文件夹放置于 AstrBot 的 `data/plugins` 目录下，AstrBot 将自动安装依赖。

### 配置

通过 AstrBot 控制台的插件配置页面进行配置：

**必需配置**:
- `embedding_provider_id`: 向量嵌入模型 ID（留空使用默认）
- `llm_provider_id`: 大语言模型 ID（留空使用默认）
- `rerank_provider_id`: 可选的 Rerank 模型 ID，用于复核跨时间 Topic 片段是否属于同一话题

如果 AstrBot 暂无可用 Rerank Provider，可开启 `cloudflare_rerank.enabled`，填写 Cloudflare `account_id` 与 `api_token`（或设置环境变量 `CLOUDFLARE_AUTH_TOKEN`）。默认模型为 `@cf/baai/bge-reranker-base`；Cloudflare 返回的相关度按 `[0, 1]` 原样使用，临时调用失败时回退到 Embedding 匹配。

**权威人物资料**:
- 在插件 WebUI 的“人物资料”页面管理已知参与者的稳定身份事实，不占用 AstrBot 插件配置。这些资料保存在插件数据目录的 `authoritative_identities.json`，会注入 Timeline 总结和 Topic 片段提取/合成提示词。
- `user_id` 必填并应使用平台稳定账号 ID；`platform` 用于区分平台，`display_name` 和 `aliases` 只用于识别/展示。可选字段还有 `gender`、`pronouns` 和 `notes`。
- 例如示例甲可填写平台 `qq`、稳定账号 ID `10000001`、显示名 `示例甲`、性别 `男性`、代词 `他, 他的`。
- 保存后对新生成的 Timeline 和 Topic 立即生效，无需重启插件。Topic 构建运行期间暂时禁止保存，避免同一构建任务混用两版资料。
- 未匹配资料且来源没有明确代词时，提示词要求模型重复昵称，不根据昵称、人格或表达习惯推断性别。本阶段仅做资料注入与提示词约束，不做生成结果的确定性身份校验。

**实验性 Topic 记忆**:
- 开启 `topic_memory.enabled` 后，在 Topic 记忆页面执行一次全量构建；`topic_memory.auto_maintenance` 控制后续自动维护。
- Topic 是自动派生的只读数据。应编辑来源 Timeline，关联 Topic 会被标记为待重建并自动更新。
- 宽候选组按 `topic_memory.fragment_extraction_batch_size`（默认 12）分批提取；大型 Topic 组件再按 `topic_memory.synthesis_batch_size`（默认 12）分层合成，最终仍归并为同一个 Topic。可在 Topic 页面查看当前组件、批次、调用序号和耗时。
- `topic_memory.llm_concurrency` 控制候选组、组内批次和同层 Topic 合成的共享 LLM 并发上限，默认 2；设为 1 使用串行模式。提高并发前应确认 Provider 与上游 API 的速率限制。
- `topic_memory.rerank_concurrency` 独立控制片段匹配阶段的 Rerank 请求并发上限，默认 1、范围 1–32；设为 1 使用串行模式。Cloudflare 返回 429 时应降低该值。
- Topic 片段匹配会同时使用 Embedding、Rerank 原始相关度和双向相对排名。完整候选排序可避免不同 Rerank 模型分数过度集中时绝对阈值失效；两个已形成的组件越大，合并所需的平均一致性会缓慢提高，单片段接入不受影响，从而阻止少数边界桥把不同检索意图连成超大 Topic。
- 相关子话题图会忽略纯日期、时刻和拆分后的年月日结构词；`Expo2026` 等含字母的命名标识仍会保留，避免共同发生日期被误当成话题关系证据。
- 构建失败、取消或因插件重启中断后，Topic 页面会显示“从断点继续”。候选片段、向量、匹配、组件合成和已写入 Topic 都会按原 `run_uid` 复用；配置、Prompt、Provider、模型或输入发生变化时，只重算受影响阶段。
- LLM 输出中的可验证结构错误会使用输入来源确定性修复并写入 `validation_repairs`；无法验证的模型引用会被丢弃，不会作为 Topic 来源保存。
- 片段提取以“一个未来检索意图”为边界；相关子话题关系要求全库区分度与正文语义共同支持，不会仅因一个泛化关键词或共享 Timeline 建边。Topic 与原子置信度按独立时间簇校准，避免同一段连续对话被误当成多次独立证据。
- 数据库 v9.4 迁移只增加 Topic 相关子话题关系结构，不自动调用模型、不生成或改写 Topic，也不改变当前召回逻辑。

管理界面通过 AstrBot 官方插件页面（插件 → LivingMemory → Pages → dashboard）访问，无需额外配置。

---

## 命令

| 命令 | 说明 |
| :--- | :--- |
| `/lmem status` | 查看记忆库状态 |
| `/lmem search <query> [k]` | 搜索记忆（默认 5 条） |
| `/lmem forget <id>` | 删除指定记忆 |
| `/lmem rebuild-index` | 重建索引（修复索引不一致） |
| `/lmem rebuild-graph` | 重建图记忆索引（为旧记忆回填图数据） |
| `/lmem webui` | 查看 WebUI 信息 |
| `/lmem summarize` | 立即触发当前会话的记忆总结 |
| `/lmem reset` | 重置当前会话记忆上下文 |
| `/lmem cleanup [preview\|exec]` | 清理历史消息中的记忆注入片段（默认 preview 预演） |
| `/lmem help` | 显示帮助 |

---

## 架构说明

### 模块结构

```
astrbot_plugin_livingmemory/
├── main.py                          # 插件注册和生命周期管理
├── core/
│   ├── base/                        # 基础组件（配置、常量、异常）
│   ├── managers/                    # 核心管理器（MemoryEngine、ConversationManager、
│   │                                #   GraphMemoryManager、AtomLifecycleManager、BackupManager）
│   ├── models/                      # 数据模型（GraphNode/Edge/Entry、MemoryAtom）
│   ├── processors/                  # 处理器（MemoryProcessor、GraphExtractor、AtomClassifier）
│   ├── retrieval/                   # 检索层（文档路、图路、原子路、RRF 融合、双路融合）
│   ├── validators/                  # 验证器（IndexValidator）
│   ├── i18n_backend.py              # 后端国际化
│   ├── plugin_initializer.py        # 插件初始化器
│   ├── event_handler.py             # 事件处理器
│   └── command_handler.py           # 命令处理器
├── storage/                         # 存储层（GraphStore、AtomStore、ConversationStore、DBMigration）
├── pages/dashboard/                 # 插件页面（表格管理 + 3D 图谱可视化）
├── tests/                           # 测试套件
└── docs/                            # 文档
```

### 核心组件

1. **PluginInitializer**: 负责插件初始化
   - 非阻塞初始化机制
   - Provider等待和重试
   - 自动数据库迁移

2. **EventHandler**: 处理事件钩子
   - 群聊消息捕获
   - 记忆召回
   - 记忆反思

3. **Agent 记忆工具**: 为 tool loop / agent 模式提供主动回忆能力
   - 工具名：`recall_long_term_memory`
   - 复用现有会话隔离和人格隔离配置
   - 返回原始记忆列表，不额外注入 prompt
   - 适合“你还记得吗”“我之前说过什么”“帮我回忆一下”这类场景

4. **CommandHandler**: 处理命令
   - 统一命令响应格式
   - 完善的错误处理

5. **FakeToolCallFormatter** (`core/utils/`): 将记忆格式化为伪造的 LLM 工具调用
   - 兼容 Agent / Tool Loop 执行模式
   - 每轮由 `EventHandler` 自动清理

6. **AtomClassifier** (`core/processors/`): 规则基原子分类器
   - 将关键事实分类为 EPISODIC/FACTUAL/RELATIONAL/PREFERENCE/PLANNED 五种类型
   - 零额外 LLM 调用

7. **AtomLifecycleManager** (`core/managers/`): 原子生命周期管理
   - 后台周期维护（过期 / 遗忘 / 强化检测）
   - 基于 Jaccard + CJK bigram 的跨记忆原子强化

8. **BackupManager** (`core/managers/`): 版本备份管理
   - 插件启动时检测版本变更，自动备份所有数据文件到版本标记目录
   - 支持备份历史查询与数据恢复

9. **ConfigManager**: 配置管理
   - 集中配置加载
   - 配置验证
   - 嵌套键访问

---

## Agent 主动记忆回忆

除了自动记忆召回外，插件还会在运行时注册一个 LLM 工具：`recall_long_term_memory`。

这个工具的特点：

- Agent 可以自己决定是否回忆长期记忆，而不是只能依赖当前轮消息作为查询词
- 工具回忆范围自动继承当前配置中的会话隔离与人格隔离设置
- 检索结果作为工具返回进入 agent 上下文，不会再次走记忆 prompt 注入链路
- 更适合用户要求“回忆”“想起”“之前提过什么”或当前指代不清、需要补查历史上下文的情况

建议的调用策略：

- 优先使用简短关键词，而不是直接复制整句用户输入
- 优先回忆主题、实体名、偏好、约定、历史事件等高信息量词语
- 如果第一次回忆结果不理想，可以换一个更具体或更抽象的关键词再次回忆

返回结果为原始记忆列表，包含记忆内容、相关分数、重要性及会话/人格元数据，便于 agent 自行判断哪些结果真正相关

---

## 开发者指南

### 测试

```bash
# 运行所有测试
pytest tests/

# 运行特定测试
pytest tests/test_config_manager.py

# 查看覆盖率
pytest --cov=core tests/
```


### 文档

- [VitePress 文档站](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/): 快速开始、功能说明、WebUI 使用、技术架构和文档部署说明
- [English Documentation](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/en/): English documentation site

---

## 数据迁移（v1.4.0-1.4.2）

如果您从 v1.4.0-1.4.2 版本升级，旧数据可能无法自动迁移。手动恢复步骤：

1. 找到备份文件：`data/plugin_data/astrbot_plugin_livingmemory/backups/livingmemory_backup_<时间戳>.db`
2. 将该文件移动到：`data/plugin_data/astrbot_plugin_livingmemory/`
3. 重命名为：`livingmemory.db`
4. 重载插件，系统会自动加载和处理数据

---

## 更新记录

详见 [CHANGELOG.md](CHANGELOG.md)

---

## 支持

- **GitHub**: [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
- **问题反馈**: [GitHub Issues](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/issues)
- **QQ 群**: [![加入QQ群](https://img.shields.io/badge/QQ群-953245617-blue?style=flat-square&logo=tencent-qq)](https://qm.qq.com/cgi-bin/qm/qr?k=WdyqoP-AOEXqGAN08lOFfVSguF2EmBeO&jump_from=webapi&authKey=tPyfv90TVYSGVhbAhsAZCcSBotJuTTLf03wnn7/lQZPUkWfoQ/J8e9nkAipkOzwh)
  （口令：lxfight）

---

## 许可证

本项目遵循 AGPLv3 许可证。
