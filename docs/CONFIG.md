# LivingMemory 配置参考

本文档详细介绍了 LivingMemory 插件的所有配置参数。

## 📋 配置概览

插件配置采用层次化结构，主要包含以下几个部分：
- 时区设置
- Provider 设置  
- 会话管理器
- 回忆引擎
- 反思引擎
- 遗忘代理
- 结果融合
- 稀疏检索器
- 过滤设置

## ⚙️ 详细配置参数

### 时区设置 (timezone_settings)

| 参数 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `timezone` | string | `"Asia/Shanghai"` | IANA 时区数据库名称，影响时间显示格式 |

**示例：**
```yaml
timezone_settings:
  timezone: "America/New_York"  # 纽约时区
```

**可用时区：**
- `Asia/Shanghai` - 中国标准时间
- `America/New_York` - 美国东部时间
- `Europe/London` - 格林威治时间
- `Asia/Tokyo` - 日本标准时间

### Provider 设置 (provider_settings)

| 参数 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `embedding_provider_id` | string | `""` | 指定用于生成向量的 Embedding Provider ID |
| `llm_provider_id` | string | `""` | 指定用于总结和评估的 LLM Provider ID |

**示例：**
```yaml
provider_settings:
  embedding_provider_id: "openai_embedding"
  llm_provider_id: "claude_3_5"
```

**注意：**
- 留空将自动使用 AstrBot 的默认 Provider
- 确保指定的 Provider 已在 AstrBot 中正确配置

### 会话管理器 (session_manager)

| 参数 | 类型 | 默认值 | 范围 | 描述 |
|------|------|--------|------|------|
| `max_sessions` | int | `1000` | 1-10000 | 同时维护的最大会话数量 |
| `session_ttl` | int | `3600` | 60-86400 | 会话生存时间（秒） |

**示例：**
```yaml
session_manager:
  max_sessions: 500      # 最大500个会话
  session_ttl: 7200      # 2小时过期
```

**优化建议：**
- 高并发场景：增大 `max_sessions`
- 内存紧张：减小 `session_ttl`
- 长对话场景：增大 `session_ttl`

### 回忆引擎 (recall_engine)

| 参数 | 类型 | 默认值 | 范围 | 描述 |
|------|------|--------|------|------|
| `top_k` | int | `5` | 1-50 | 单次检索返回的记忆数量 |
| `recall_strategy` | string | `"weighted"` | similarity/weighted | 召回策略 |
| `retrieval_mode` | string | `"hybrid"` | hybrid/dense/sparse | 检索模式 |
| `similarity_weight` | float | `0.6` | 0.0-1.0 | 相似度权重 |
| `importance_weight` | float | `0.2` | 0.0-1.0 | 重要性权重 |
| `recency_weight` | float | `0.2` | 0.0-1.0 | 新近度权重 |

**召回策略：**
- `similarity`: 纯基于相似度的召回
- `weighted`: 综合考虑相似度、重要性和新近度

**检索模式：**
- `hybrid`: 混合检索（推荐）
- `dense`: 纯密集向量检索
- `sparse`: 纯稀疏关键词检索

**权重调优指南：**
```yaml
# 重视语义相关性
recall_engine:
  similarity_weight: 0.7
  importance_weight: 0.2
  recency_weight: 0.1

# 重视重要信息
recall_engine:
  similarity_weight: 0.4
  importance_weight: 0.5
  recency_weight: 0.1

# 重视最新信息
recall_engine:
  similarity_weight: 0.4
  importance_weight: 0.2
  recency_weight: 0.4
```

### 反思引擎 (reflection_engine)

| 参数 | 类型 | 默认值 | 范围 | 描述 |
|------|------|--------|------|------|
| `summary_trigger_rounds` | int | `5` | 1-100 | 触发反思的对话轮次 |
| `importance_threshold` | float | `0.5` | 0.0-1.0 | 记忆重要性阈值 |
| `event_extraction_prompt` | text | 默认提示词 | - | 事件提取提示词 |
| `evaluation_prompt` | text | 默认提示词 | - | 重要性评估提示词 |

**触发轮次调优：**
- `1-3轮`: 频繁反思，适合重要对话
- `5-10轮`: 平衡模式（推荐）
- `15-30轮`: 长对话模式，减少反思频率

**重要性阈值：**
- `0.1-0.3`: 宽松模式，保存更多记忆
- `0.5-0.7`: 标准模式（推荐）
- `0.8-1.0`: 严格模式，只保存重要记忆

### 遗忘代理 (forgetting_agent)

| 参数 | 类型 | 默认值 | 范围 | 描述 |
|------|------|--------|------|------|
| `enabled` | bool | `true` | - | 是否启用自动遗忘 |
| `check_interval_hours` | int | `24` | 1-168 | 检查间隔（小时） |
| `retention_days` | int | `90` | 1-3650 | 记忆保留天数 |
| `importance_decay_rate` | float | `0.005` | 0.0-1.0 | 重要性衰减率 |
| `importance_threshold` | float | `0.1` | 0.0-1.0 | 遗忘重要性阈值 |
| `forgetting_batch_size` | int | `1000` | 100-10000 | 批处理大小 |

**遗忘策略配置：**
```yaml
# 保守遗忘（保存更多记忆）
forgetting_agent:
  retention_days: 180
  importance_decay_rate: 0.001
  importance_threshold: 0.05

# 标准遗忘（推荐）
forgetting_agent:
  retention_days: 90
  importance_decay_rate: 0.005
  importance_threshold: 0.1

# 激进遗忘（节省存储空间）
forgetting_agent:
  retention_days: 30
  importance_decay_rate: 0.01
  importance_threshold: 0.2
```

### 结果融合 (fusion)

| 参数 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `strategy` | string | `"rrf"` | 融合策略 |
| `rrf_k` | int | `60` | RRF 参数 k |
| `dense_weight` | float | `0.7` | 密集检索权重 |
| `sparse_weight` | float | `0.3` | 稀疏检索权重 |
| `convex_lambda` | float | `0.5` | 凸组合参数 |
| `interleave_ratio` | float | `0.5` | 交替融合比例 |
| `rank_bias_factor` | float | `0.1` | 排序偏置因子 |
| `diversity_bonus` | float | `0.1` | 多样性奖励 |

**融合策略详解：**
- `rrf`: 经典 Reciprocal Rank Fusion
- `hybrid_rrf`: 自适应 RRF
- `weighted`: 加权融合
- `convex`: 凸组合融合
- `interleave`: 交替融合
- `rank_fusion`: 基于排序的融合
- `score_fusion`: Borda Count 融合
- `cascade`: 级联融合
- `adaptive`: 自适应融合

详细说明请参考 [FUSION_STRATEGIES.md](../FUSION_STRATEGIES.md)

### 稀疏检索器 (sparse_retriever)

| 参数 | 类型 | 默认值 | 范围 | 描述 |
|------|------|--------|------|------|
| `enabled` | bool | `true` | - | 是否启用稀疏检索 |
| `bm25_k1` | float | `1.2` | 0.1-10.0 | BM25 k1 参数 |
| `bm25_b` | float | `0.75` | 0.0-1.0 | BM25 b 参数 |
| `use_jieba` | bool | `true` | - | 是否使用中文分词 |

**BM25 参数调优：**
- `k1`: 控制词频饱和度
  - 较小值（0.5-1.0）：词频影响较小
  - 较大值（1.5-2.0）：词频影响较大
- `b`: 控制文档长度归一化
  - 0.0：不考虑文档长度
  - 1.0：完全归一化文档长度

**中文优化配置：**
```yaml
sparse_retriever:
  enabled: true
  bm25_k1: 1.2      # 适合中文的词频参数
  bm25_b: 0.75      # 中等长度归一化
  use_jieba: true   # 启用中文分词
```

### 过滤设置 (filtering_settings)

| 参数 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `use_persona_filtering` | bool | `true` | 是否启用人格记忆过滤 |
| `use_session_filtering` | bool | `true` | 是否启用会话记忆隔离 |

**过滤模式组合：**
```yaml
# 完全隔离模式
filtering_settings:
  use_persona_filtering: true
  use_session_filtering: true

# 人格共享模式
filtering_settings:
  use_persona_filtering: true
  use_session_filtering: false

# 会话共享模式
filtering_settings:
  use_persona_filtering: false
  use_session_filtering: true

# 全局共享模式
filtering_settings:
  use_persona_filtering: false
  use_session_filtering: false
```

## 🎯 场景化配置示例

### 个人助手配置
```yaml
# 适合个人日常使用
session_manager:
  max_sessions: 100
  session_ttl: 7200

recall_engine:
  top_k: 3
  similarity_weight: 0.5
  importance_weight: 0.3
  recency_weight: 0.2

reflection_engine:
  summary_trigger_rounds: 10
  importance_threshold: 0.4

filtering_settings:
  use_persona_filtering: true
  use_session_filtering: false
```

### 客服机器人配置
```yaml
# 适合客服场景
session_manager:
  max_sessions: 1000
  session_ttl: 1800

recall_engine:
  top_k: 5
  similarity_weight: 0.7
  importance_weight: 0.2
  recency_weight: 0.1

reflection_engine:
  summary_trigger_rounds: 5
  importance_threshold: 0.6

filtering_settings:
  use_persona_filtering: false
  use_session_filtering: true
```

### 教育辅导配置
```yaml
# 适合教育辅导场景
session_manager:
  max_sessions: 500
  session_ttl: 3600

recall_engine:
  top_k: 8
  similarity_weight: 0.4
  importance_weight: 0.4
  recency_weight: 0.2

reflection_engine:
  summary_trigger_rounds: 8
  importance_threshold: 0.3

forgetting_agent:
  retention_days: 180
  importance_decay_rate: 0.002
```

## 🔧 配置验证

### 使用命令验证
```bash
# 验证当前配置
/lmem config validate

# 查看配置摘要
/lmem config show
```

### 配置文件验证
插件会在启动时自动验证配置：
- ✅ 参数类型检查
- ✅ 数值范围验证
- ✅ 必需字段验证
- ✅ 权重总和警告

## 💡 性能优化建议

### 内存优化
- 减少 `max_sessions` 和 `session_ttl`
- 降低 `top_k` 值
- 启用积极的遗忘策略

### 准确性优化
- 增加 `top_k` 值
- 调整权重配比
- 使用混合检索模式
- 优化融合策略参数

### 响应速度优化
- 使用 `cascade` 融合策略
- 减少 `top_k` 值
- 选择更快的检索模式

## ⚠️ 注意事项

1. **权重总和**：确保回忆引擎的三个权重总和接近 1.0
2. **Provider 可用性**：确保指定的 Provider 已正确配置
3. **存储空间**：长期使用需要考虑遗忘策略以控制存储增长
4. **中文支持**：启用 jieba 分词以获得更好的中文检索效果
5. **配置热更新**：部分配置修改需要重启插件才能生效

## 🔍 配置调试

### 查看生效配置
```bash
/lmem config show
```

### 测试检索效果
```bash
# 测试不同检索模式
/lmem search_mode hybrid
/lmem search "测试查询" 5

# 测试融合策略
/lmem fusion show
/lmem test_fusion "测试查询" 5
```

### 性能监控
```bash
# 查看记忆库状态
/lmem status

# 检查会话数量
/lmem config show | grep 会话
```