# 整体架构

LivingMemory 以 Timeline 为来源层、Topic 为派生层。它们不是两套互不相干的记忆，也不共享同一批事实原子；二者通过稳定 UID、revision、正式片段与来源关系形成可维护链路。

![LivingMemory 整体架构](./assets/images/architecture-overview-zh.svg){.diagram}

## 数据层级

| 层级 | 主要内容 | 是否直接编辑 | 主要用途 |
| --- | --- | --- | --- |
| 原始会话 | 消息、发送者、角色、时间 | 否 | 总结证据、按需补证、会话审计 |
| Timeline | 摘要、事实、主题、情绪、时间与人物绑定 | 是 | 保存按时间发生的经历 |
| 正式片段 | 单一检索意图、事实、人物、情绪与来源 | 否 | Topic 构建与精简补充 |
| Topic | 主题正文、独立原子、人物索引、相关话题 | 否 | 跨时间归并与主要召回 |

## 为什么不让 Timeline 与 Topic 共用原子

Timeline 原子描述某次总结窗口中的事实，Topic 原子则描述跨片段归并后的稳定事实。如果直接共用，Topic 合并、拆分或重新表述时会反向修改来源层，也无法独立记录 Topic 的置信度和来源覆盖。

当前设计保留两套原子，并使用以下关系追溯：

```text
Topic atom
  -> formal fragment
  -> Timeline UID + revision
  -> Timeline atom / source fact key
  -> source window or source snapshot
```

## 稳定身份与版本

- Timeline 使用稳定 `memory_uid`，物理文档 ID 可以改变。
- 每次正文或来源变化递增 revision。
- 正式片段拥有稳定逻辑 ID 与独立 revision。
- Topic 使用稳定 UID；更新时原子发布完整新快照。
- 构建检查点绑定输入、配置、Prompt 与模型签名，变化后只复用仍然有效的阶段。

## 人物与情绪

消息发送者与 Timeline `role_bindings` 提供稳定人物锚点。正式片段只能引用输入中允许的 actor ref，不按昵称跨片段猜测身份。Topic 人物和事实人物关系存入独立关系表，详情页可追溯到片段与 Timeline。

情绪不是只保留一个正负标签。正式片段与 Topic 保存情绪事件、强度、目标与来源；召回时可作为轻量补充，避免主题摘要把互动中的感情色彩完全抹平。

## 图谱的位置

图谱仍可从 Timeline 事实生成实体与关系，并参与兼容的 Timeline 双路检索。但在当前 Topic 优先架构中，图谱不是 Topic 构建或 Topic 召回的权威来源。主要链路应以 Timeline、正式片段、Topic 及其来源关系为准。
