# 技术架构

LivingMemory 的运行时由事件钩子、记忆处理、检索融合、存储和 WebUI API 五个部分组成。它尽量把“自动记忆”和“主动工具”放在同一套数据模型上，避免两套记忆系统互相打架。

<img class="diagram" src="/images/architecture-flow.svg" alt="LivingMemory runtime architecture">

## 总体流程

1. AstrBot 收到消息后，`EventHandler` 捕获会话上下文。
2. 在 LLM 请求前，召回链路根据当前消息和最近上下文查询长期记忆。
3. 检索结果按配置注入到请求中，或作为 Agent 工具结果返回。
4. LLM 回复后，反思链路判断是否需要总结并写入新记忆。
5. 后台任务执行衰减、过期清理、备份和索引校验。

## 主要模块

| 模块 | 职责 |
| --- | --- |
| `main.py` | 注册插件、初始化核心组件、注册 Agent 工具和 Pages API |
| `core/plugin_initializer.py` | 非阻塞初始化、Provider 等待、数据库迁移、索引加载 |
| `core/event_handler.py` | 群聊捕获、记忆召回、记忆反思 |
| `core/managers/memory_engine.py` | 统一记忆写入、搜索、删除和索引维护 |
| `core/managers/graph_memory_manager.py` | 图谱节点、边、条目和图检索协调 |
| `core/managers/atom_lifecycle_manager.py` | 原子过期、遗忘、强化和生命周期维护 |
| `core/retrieval/` | BM25、向量、图谱、原子检索与 RRF 融合 |
| `storage/` | SQLite 存储、图谱存储、原子存储、数据库迁移 |
| `pages/dashboard/` | AstrBot Pages 管理界面 |

## 双路四模式检索

普通长期记忆和图谱记忆分别走两条路线：

| 路线 | 关键词模式 | 向量模式 |
| --- | --- | --- |
| 文档路 | `BM25Retriever` | `VectorRetriever` |
| 图谱路 | `GraphKeywordRetriever` | `GraphVectorRetriever` |

随后 `RRFFusion` 会融合多个排序列表，再叠加重要性、时间衰减、会话隔离和人格隔离等过滤条件。

## 记忆数据模型

| 类型 | 说明 |
| --- | --- |
| 会话消息 | 原始对话上下文，用于触发总结和补充查询 |
| 记忆条目 | LLM 总结后的长期记忆，包含摘要、重要性、会话和人格元数据 |
| 图谱节点与边 | 从记忆中抽取的实体和关系，支持跨记忆合并 |
| 记忆原子 | 独立事实单元，拥有类型、TTL、衰减和访问强化状态 |

### Timeline 逻辑身份与来源范围

每条新记忆都会获得稳定的 `memory_uid`、递增的 `revision` 和确定性的
`memory_space_id`。物理 `documents.id` 可以在“新 ID 重建”时变化，但
`memory_registry` 始终把同一个 `memory_uid` 指向当前文档，因此后续派生数据
不需要依赖易变化的物理 ID。

`memory_source_spans` 独立保存记忆对应的会话、消息 ID、消息索引和时间范围。
历史记忆无法回填消息 ID 时会标记为部分或不可追溯，不会伪造来源。当前这些
记忆仍属于 `timeline` 层；注册表和来源范围不会改变现有总结与召回行为。

### Topic 派生记忆存储（v9.1，阶段二基础）

Topic 记忆是从 Timeline 记忆自动整理出的派生层，当前只建立存储和溯源结构，
尚未启用自动构建与召回。`topic_memories` 保存 Topic 快照和独立的重要性状态，
`topic_memory_atoms` 保存 Topic 自己的事实原子；它们不会复用或修改
`memory_atoms` 中的 Timeline 原子。

`topic_timeline_links` 通过稳定 UID 建立双向多对多关联，同时保存 Timeline 修订、
时间簇、语义相似度、时间亲和度和贡献权重。多个时间相近的 Timeline 可以属于同一
时间簇，因此维护算法不会把单次长对话产生的多个分片直接当成多次独立佐证。

`topic_atom_sources` 进一步把 Topic 原子映射到 Timeline 原子 ID 或内容指纹。
Timeline 被编辑后，关联 Topic 会先标记为过期，后续维护任务只需重建受影响的片段。
`topic_maintenance_runs` 保存全量、增量和修复任务的游标与实时进度，支持中断恢复。
Topic 更新采用修订号乐观锁和单事务快照替换，暂不提供 WebUI 手工编辑入口。

### Topic 候选发现（v9.2，阶段三预览）

`TopicMaintenanceManager` 可以按 `memory_space_id` 对 Timeline 进行只读扫描。
扫描器提取规范化主题、事实指纹、独立原子指纹和词法特征，先按照来源时间间隔形成
时间簇，再综合主题、事实、原子、词法重合度和时间簇关系生成候选组。确定性结果只写入
`topic_candidate_groups`，状态固定为 `preview`，不会写入正式 Topic 或参与召回。
同一时间簇会作为一个宽松审核窗口，即使内部存在多个话题也会一起交给后续 LLM 分割；
它不表示这些 Timeline 已被判定为同一个 Topic。

`topic_maintenance_items` 按 Timeline 稳定 UID 和来源修订保存每个扫描结果；任务每批
提交游标和进度，取消或重启后可以继续。若暂停期间 Timeline 修订发生变化，旧扫描项
不会被复用，而会重新读取。候选组 ID 对同一次运行保持确定性，重复读取已完成任务不会
产生重复结果。该层只负责缩小后续 LLM 审核范围，不把规则聚类视为最终语义判断。

数据库架构在 Topic 开发期间按小版本推进：稳定身份层为 v9，Topic 存储基础为
v9.1，确定性候选扫描为 v9.2；仅在 Topic 构建、维护和召回形成完整闭环后升级到
v10。小版本以 `v9.2` 形式作为文本写入，即使旧数据库的版本列声明为 INTEGER，
也不会被 SQLite 转成浮点数，因此未来 v9.10 仍能正确排在 v9.2 之后。

## 数据安全设计

插件在高风险操作前尽量留下恢复点：

| 场景 | 保护措施 |
| --- | --- |
| 插件版本变化 | 启动时自动创建版本标记备份 |
| 数据库迁移 | 迁移前备份 |
| 索引重建 | 分批重建，失败后回滚 |
| 删除记忆 | 使用事务保护相关记录 |
| 管理页面操作 | 通过 Pages API 复用运行时组件，避免绕过 MemoryEngine |
