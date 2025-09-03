# 混合检索融合策略详解

LivingMemory 插件支持多种先进的结果融合策略，用于优化混合检索效果。

## 🎯 支持的融合策略

### 1. RRF (Reciprocal Rank Fusion) - 经典策略
```
分数 = Σ(1 / (k + rank_i))
```
- **适用场景**: 通用场景，平衡性好
- **参数**: `rrf_k` (默认: 60)
- **特点**: 简单有效，对排序位置敏感

### 2. Hybrid RRF - 动态RRF
- **适用场景**: 需要根据查询特征自适应调整
- **参数**: `rrf_k`, `diversity_bonus`
- **特点**: 根据查询长度和类型动态调整RRF参数
- **优势**: 短查询偏向稀疏检索，长查询偏向密集检索

### 3. Weighted - 加权融合
```
分数 = α × dense_score + β × sparse_score
```
- **适用场景**: 明确知道两种检索器的相对重要性
- **参数**: `dense_weight`, `sparse_weight`
- **特点**: 简单直观，可解释性强

### 4. Convex - 凸组合融合
```
分数 = λ × norm(dense) + (1-λ) × norm(sparse)
```
- **适用场景**: 需要数学严格的融合方法
- **参数**: `convex_lambda` (0.0-1.0)
- **特点**: 分数归一化到 [0,1]，数学性质好

### 5. Interleave - 交替融合
- **适用场景**: 需要保证结果多样性
- **参数**: `interleave_ratio` - 密集结果所占比例
- **特点**: 按比例交替选择不同检索器的结果

### 6. Rank Fusion - 基于排序的融合
```
分数 = Σ(weight_i / rank_i) + bias(if in both lists)
```
- **适用场景**: 重视文档在排序列表中的位置
- **参数**: `dense_weight`, `sparse_weight`, `rank_bias_factor`
- **特点**: 在两个列表中都出现的文档获得额外加分

### 7. Score Fusion - Borda Count融合
```
分数 = Σ(list_size - rank_i) × weight_i
```
- **适用场景**: 基于排序投票的民主融合
- **参数**: `dense_weight`, `sparse_weight`
- **特点**: 类似选举中的Borda计数法

### 8. Cascade - 级联融合
- **适用场景**: 大规模检索，需要效率优化
- **流程**: 稀疏检索初筛 → 密集检索精排
- **特点**: 先用快速的稀疏检索筛选候选，再用精确的密集检索排序

### 9. Adaptive - 自适应融合
- **适用场景**: 查询类型多样的场景
- **策略**: 根据查询特征选择最优融合方法
  - 关键词查询 → 偏向稀疏检索
  - 语义查询 → 偏向密集检索
  - 混合查询 → 使用RRF

## 📊 性能特征对比

| 策略 | 计算复杂度 | 参数调优难度 | 适应性 | 可解释性 |
|------|-----------|-------------|--------|----------|
| RRF | 低 | 低 | 中 | 中 |
| Hybrid RRF | 中 | 中 | 高 | 中 |
| Weighted | 低 | 低 | 低 | 高 |
| Convex | 低 | 中 | 中 | 高 |
| Interleave | 低 | 低 | 低 | 高 |
| Rank Fusion | 中 | 中 | 中 | 中 |
| Score Fusion | 高 | 中 | 中 | 中 |
| Cascade | 低 | 低 | 低 | 高 |
| Adaptive | 中 | 高 | 高 | 低 |

## 🛠️ 使用指南

### 配置示例

```yaml
fusion:
  strategy: "hybrid_rrf"
  rrf_k: 60
  dense_weight: 0.7
  sparse_weight: 0.3
  diversity_bonus: 0.1
  convex_lambda: 0.5
  interleave_ratio: 0.6
  rank_bias_factor: 0.15
```

### 命令行管理

```bash
# 查看当前配置
/lmem fusion show

# 切换到混合RRF
/lmem fusion hybrid_rrf

# 调整凸组合参数
/lmem fusion convex convex_lambda=0.6

# 调整权重
/lmem fusion weighted dense_weight=0.8

# 测试融合效果
/lmem test_fusion "用户的兴趣爱好" 5
```

## 🎯 策略选择建议

### 场景驱动的选择

1. **通用聊天机器人**: `hybrid_rrf` 或 `rrf`
   - 查询类型多样，需要自适应能力

2. **专业知识问答**: `weighted` 或 `convex`
   - 可以明确调优权重，提高精确度

3. **多样性优先**: `interleave`
   - 确保结果不会过于相似

4. **大规模数据库**: `cascade`
   - 效率优先，两阶段处理

5. **实验和调优**: `score_fusion` 或 `rank_fusion`
   - 更复杂的融合逻辑，适合深度优化

### 参数调优指南

1. **RRF参数 k**:
   - 较小值 (30-50): 更重视排序靠前的结果
   - 较大值 (80-120): 更平衡地考虑所有结果

2. **权重比例**:
   - 密集权重 > 稀疏权重: 语义查询为主
   - 密集权重 < 稀疏权重: 关键词查询为主

3. **多样性参数**:
   - 较大值: 鼓励结果多样性
   - 较小值: 优先考虑相关性

## 🧪 实验和评估

使用 `/lmem test_fusion` 命令可以：
- 测试不同策略的效果
- 查看融合过程的详细信息
- 对比不同参数设置的结果

建议在实际数据上进行A/B测试，选择最适合你使用场景的融合策略。