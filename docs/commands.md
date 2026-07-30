# 命令速查

| 命令 | 说明 |
| --- | --- |
| `/lmem status` | 查看当前记忆库状态 |
| `/lmem search <query> [k]` | 搜索长期记忆 |
| `/lmem forget <id>` | 删除指定 Timeline 记忆 |
| `/lmem summarize` | 立即总结当前会话尚未总结的消息 |
| `/lmem rebuild-index` | 重建 Timeline 向量索引 |
| `/lmem rebuild-graph` | 重建兼容的图记忆派生数据 |
| `/lmem reset` | 重置当前会话记忆上下文 |
| `/lmem cleanup [preview\|exec]` | 预览或清理历史消息中的旧注入片段 |
| `/lmem webui` | 查看 WebUI 入口信息 |
| `/lmem help` | 显示命令帮助 |

Topic 的全量构建、增量补建、审查、重算关系与清理主要在 WebUI 中进行，因为这些操作需要记忆空间选择、预览、进度和二次确认。
