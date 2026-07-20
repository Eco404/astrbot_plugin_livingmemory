# Changelog

所有重要的更改都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
并且遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增
- **Rerank 并发控制**：新增 `topic_memory.rerank_concurrency`（默认 1、范围 1–32），片段匹配阶段可并行调用 Cloudflare 或 AstrBot Rerank Provider；进度栏显示完成数、活跃请求数与并发上限，任一请求失败时仍按既有策略整轮回退 Embedding。
- **断点任务取消与进度清理**：Topic 构建出现可恢复断点时，WebUI 同时提供“取消任务”；确认后删除该运行保存的扫描、片段、匹配、合成和检查点数据，并保留已正式落库的 Topic，避免破坏构建前数据。
- **Topic 相关子话题图（数据库 v9.4）**：保持叶子 Topic 内容独立，通过互为语义近邻的 `related_subtopic` 关系连接同一事件下的行程、天气、住宿、现场活动与后续恢复等子话题；详情浮窗可直接查看并跳转相关 Topic。
- **自动 Topic 记忆层（数据库 v9.3，实验性）**：LLM 将宽时间窗口拆为可溯源片段，再通过 Embedding 与可选 Rerank 跨时间归并；Topic 拥有独立原子、稳定 UID/revision，以及 `Topic → Timeline → 事实指纹` 来源链。
- **全量与增量维护**：首次增量任务自动转为全量扫描；后续新增 Timeline 可增量合并到已有 Topic，编辑或删除 Timeline 会将依赖 Topic 标记为 stale 并触发受影响记忆空间重建。
- **Topic WebUI**：新增只读 Topic 列表、原子与 Timeline 来源查看、全量/增量手动构建及实时进度显示。
- **可选 Rerank Provider**：新增 `provider_settings.rerank_provider_id`，未配置时保持纯 Embedding 匹配。
- **Cloudflare Workers AI Rerank**：AstrBot 尚未支持 Cloudflare Rerank Provider 时，插件可直接调用 `@cf/baai/bge-reranker-base`；原样使用接口返回的 `[0, 1]` 相关度，支持 429/5xx 重试、超时控制和失败后回退 Embedding。
- **模型测试 WebUI**：以独立卡片展示插件运行时实际使用的 LLM、Embedding、Rerank，标明显式配置、AstrBot 默认回退或 Cloudflare 内置来源，并可分别发起连接测试。
- **WebUI 权威人物资料与身份提示词**：新增“人物资料”页面，以平台稳定账号 ID 为主键管理姓名、性别、代词和备注；人物条目以默认折叠的紧凑列表展示，点击后展开编辑。资料独立保存到插件数据目录并在保存后立即更新 Timeline 与 Topic 生成链路；插件配置中不增加人物资料字段。提示词禁止从昵称、语气、兴趣或人格设定推断参与者身份，未知时改为重复昵称。本阶段不进行生成后的确定性身份校验。

### 兼容性
- Topic 功能默认关闭，v9→v9.3 迁移只创建结构，不调用模型、不生成 Topic，也不改变现有 Timeline/图谱/召回逻辑。

### 修复
- **并发 Topic 合成进度与维护入口**：并发合成阶段按实际完成的组件数推进阶段进度和总体进度，不再在长时间合成期间固定停留于 75%；同阶段乱序回调会保持单调进度。WebUI 将固定的“增量维护（24h）”主按钮收拢为“维护”面板；面板检测当前 revision 尚未被活跃 Topic 索引的 Timeline，默认全选并在用户确认后仅补建选中项。
- **Topic 泛化质量与可解释分数**：片段提示词改为单一检索意图边界；相关子话题关系使用全库词频、加权正文重合与来源组合证据，并过滤日期、时刻等结构词。片段匹配新增完整候选排序与双向相对排名证据；两个已形成的组件合并时按规模缓慢提高平均一致性要求，减少绝对分数饱和导致的保守拆分，同时阻止边界桥形成超大 Topic；Topic/原子置信度按独立时间簇和来源数量进行收缩校准。
- **Topic 匹配参数解耦与审计**：候选扫描阈值不再隐式控制 Rerank 候选线和组件凝聚度；新增独立最低两两/平均相似度参数。构建断点记录单片段 Topic 是因候选不足、未通过双向 Rerank，还是被组件凝聚度拒绝，便于后续使用真实样本调优。
- **内置 Cloudflare Rerank 启动期误判未启用**：Rerank 初始化从 LLM Provider 等待流程中解耦，避免静默等待聊天模型时提前返回并跳过 Cloudflare 客户端；`provider_settings.rerank_provider_id` 留空时内置 Cloudflare 仍可独立启用，二者同时配置时内置 Cloudflare 优先。修复 `topic_memory.llm_concurrency=30` 超出隐藏校验上限后导致整份插件配置回退默认值、连带关闭 Cloudflare 的问题，并将允许范围明确为 1-64。模型测试页会显示安全的初始化错误，不再将配置异常笼统标为“未启用”。
- **Topic 全量构建被无效原子指纹整体中断**：LLM 返回未知或被改写的 `source_atom_fingerprints` 时，安全剔除无效引用；事实内容与来源原子完全一致时确定性恢复正确指纹，否则保留 Timeline 来源并降级为事实指纹溯源。未知 Timeline 和越界来源不会被采纳；无法局部修复时整批回退为输入 Timeline 的确定性片段。
- **Topic 构建进度与重复启动**：新增覆盖六个处理阶段的总体进度；候选扫描完成不再显示误导性的 `running · completed`，最终异常会明确更新为 `failed · failed`。活动任务可在页面刷新后恢复轮询，构建期间按钮自动禁用，后端也会阻止重复启动。
- **Topic 合成遗漏来源导致全量失败**：模型未让原子覆盖全部已知片段时，使用输入中的事实原子确定性补齐并记录修复审计；未知片段或事实引用会被剔除，并由可验证输入补回。宽候选组先按 `topic_memory.fragment_extraction_batch_size` 提取，超大语义组件再按 `topic_memory.synthesis_batch_size` 分层合成，保持单一 Topic 与原始溯源，避免一次请求携带过大的上下文。
- **长任务详细进度**：Topic 页面在 LLM 运行期间显示当前候选组或语义组件、组件/批次片段数、分层级别、调用序号、总耗时以及距上次进度时间，便于区分长请求与任务停滞。
- **Topic LLM 并发控制**：新增 `topic_memory.llm_concurrency`（默认 2）；候选组、组内片段提取批次及同一合成层级均可并发调用 Provider，并由共享信号量限制全局并发。候选组进度按实际完成数聚合，额外显示活跃组数量；配置为 1 可恢复串行模式。
- **Topic 构建断点续建**：失败、取消或插件重启中断的任务可在 WebUI 使用原 `run_uid` 继续；候选组片段、Embedding、片段匹配、每个 Topic 组件合成及已完成物化均持久化检查点。输入、Prompt、Provider 或模型变化时，对应检查点自动失效并安全重算。
- **可恢复的 LLM 结构错误**：合成顶层遗漏/多写 `fragment_uids`、原子引用未知片段或事实、缺少标题摘要、无效原子数组，以及片段提取返回无效 JSON/越界来源时不再直接中断。系统只保留输入中可验证的来源，并以原 Timeline 片段或事实原子确定性补齐；Provider 请求最终失败和数据库错误仍会终止任务并保留断点。
- **Topic 提示词与溯源协议**：片段提取和 Topic 合成改用 `T1.A1`、`F1` 等批次内短引用，LLM 不再抄写真实 UUID、事实 UID、原子指纹或可由事实推导的片段范围；后端确定性恢复完整来源链。合成输入会剔除嵌套溯源元数据，并在 JSON 或引用校验失败时携带具体错误自动校正一次，减少 Token、超时和长标识抄写错误。
- **Topic 列表紧凑展示**：列表仅显示标题，整行支持鼠标点击及键盘 Enter/空格打开大尺寸来源详情浮窗；浮窗支持点击外部、右上角关闭按钮或 Escape 关闭。Topic 主页面贴合可视区域，列表内容改为内部滚动，避免详情拉长整个页面。
- **Topic 重复全量构建确认**：当前记忆空间已有 Topic 时，再次点击“全量构建”会显示二次确认，可选择保留现有 Topic 并更新，或先清空该空间的 Topic、原子、来源、索引及构建断点后从零构建；首次构建仍可直接启动。Topic 概览同步改为紧凑横向布局，为列表释放更多可视高度。
- **Topic 跨话题过度归并与来源空链**：Rerank 候选改为按 Embedding 排序并要求双向确认，组件聚类增加最差跨成员相似度与平均凝聚度约束，避免单链接传递将大量异质片段合成全局生活摘要；片段与最终 Topic 强制保证每条 Timeline 至少支撑一个事实原子，原子指纹未覆盖的来源会确定性补充事实指纹。匹配算法版本纳入断点哈希，旧聚类检查点不会被误复用。

## [2.3.6] - 2026-06-28

### 修复
- **WebUI 重要性 1.0 保存成 10.0**: 详情编辑提交显示值标记，后端兼容 0-1/0-10 两种重要性输入语义 (#189)
- **图记忆长文本召回 SQLite 表达式树过深**: 图节点 token 查询去重并分批执行，避免长中文输入生成过多 OR 条件 (#176)
- **extra_user_content 注入上下文残留**: 仅清理 LivingMemory 自己临时注入的记忆片段，并归一化纯文本历史 content parts，避免污染长期上下文 (#185)

## [2.3.5] - 2026-06-09

### 重构
- **EventHandler 模块化**: 将事件处理器拆分为 `GroupCapture`、`MemoryRecall`、`MemoryReflection` 三个子模块，提高代码可维护性 (#172)
- **Page API 模块化**: 将 `page_api.py` 的处理逻辑提取到 `page_api_modules/` 目录下独立模块，职责分离更清晰 (#173)
- **WebUI Dashboard 页面模块化**: 将前端页面逻辑拆分到 `modules/` 目录（memory-page.js、recall-page.js、graph-page.js、system-page.js）

### 测试
- 测试覆盖率从 73% 提升至 76% (#171)

### 修复
- **群聊全量捕获误唤醒 AstrBot**: 修复 `PassiveGroupCaptureFilter` 未正确屏蔽群消息导致触发 LLM 响应的问题 (#170)
- **inspect-stack 崩溃**: 传递 `plugin_name` 给 `StarTools.get_data_dir()` 避免堆栈检查失败 (#169)
- **WebUI Page API 过滤器规范化**: 统一前端 API 请求的参数处理逻辑

## [2.3.4] - 2026-06-02

### 修复
- **#166 排查确认**: TextPart 序列化崩溃非 LivingMemory 导致，而是其他插件（如 llmperception）注入 TextPart 引起。`mark_as_temp()` 标记的 TextPart 在 `dump_messages_with_checkpoints()` 中被过滤不落地，不会进入上下文压缩器。保持原有 `extra_user_content_parts` + `TextPart.mark_as_temp()` 注入方式不变。
- **系统概览页重要性分布图始终为空**: `get_statistics()` 遍历了全部文档却未对重要性分桶，现在在批次处理循环中按 0-10 分 10 档统计
- **系统概览页原子计数始终为 0**: `AtomStore` 缺少 `count_atoms()` 方法导致 `AttributeError` 被静默吞掉，现已新增该方法
- **系统概览页原子类型分布图始终为空**: 新增 `AtomStore.count_by_type()` 方法（SQL GROUP BY atom_type），修复 per-type 统计缺失
- **系统概览页 atom_breakdown 已接入**: `page_api.get_stats` 现在正确调用 `atom_store.count_by_type()` 填充类型分布数据
- **WebUI 记忆列表创建时间列始终显示 "--"**: 当 `metadata.create_time` 缺失时前端会忽略 SQL 层的 `created_at` 列，现增加 fallback
- **WebUI 记忆编辑静默失败**: 状态/类型/重要性的编辑操作未检查 API 响应是否成功，失败时仍弹成功 toast，现已用 `unwrapApiData()` 包装错误检测
- **知识图谱页 Graph2D 未初始化时崩溃**: `renderPayload` 中 `window.Graph2D.selectNode/selectMemory` 缺少 `state.isGraphReady` 守卫
- **召回测试结果点击无效**: 当召回的記憶不在当前记忆列表分页中时，点击无任何反馈，现已添加 API 回退直接拉取記憶详情
- **知识图谱节点详情面板类型字段丢失**: 图谱記憶对象使用 `memory_type` 字段名，前端错误使用了 `memory.type`
- **备份管理器版本号不匹配**: `PLUGIN_VERSION` 为 2.3.1 但 `metadata.yaml` 为 2.3.3，导致每次启动错误触发版本变更备份
- **`datetime.utcnow()` 弃用警告**: 迁移 `db_migration.py` 中 3 处调用为 `datetime.now(timezone.utc)`
- **记忆详情 fallback SQL 查询缺少列**: `_get_memory_record` 回退查询未选取 `doc_id`、`created_at`、`updated_at`
- **配置项 `enable_full_group_capture` 缺失**: `_conf_schema.json` 中未暴露该字段，用户无法在 WebUI 配置

### 变更
- `page_api.update_memory` 统一使用 `self._ok()` 返回格式
- 更新测试文件以匹配注入行为变更

## [2.3.3] - 2026-06-02

### 修复
- **WebUI 删除功能无效**: 修复因 AstrBot Dashboard iframe sandbox 缺少 `allow-modals` 导致 `window.confirm()` 被浏览器静默阻止、删除操作无法执行的问题
  - 用自定义 DOM 确认对话框（渲染在 peek 面板内）替代浏览器原生 `window.confirm()`
  - 支持确定/取消/ESC/遮罩点击关闭，取消时自动恢复记忆详情视图

## [2.3.2] - 2026-06-02

### 新增
- **知识图谱力导向布局优化**: 重构图谱可视化布局算法，实现更自然的节点分布
  - 优化斥力参数（6000→1800），节点分布更平滑
  - 增加边距离（80→120）和弹簧强度，改善节点间距
  - 实现基于距离的斥力衰减曲线，替代硬性截断
  - 自适应弹簧强度，长边使用更弱的拉力
  - 质量缩放的中心引力，重要节点更居中
  - 增加迭代次数，布局收敛更稳定
- **Peek 面板迷你图谱力导向布局**: 预览面板中的小型图谱也采用力导向算法，与主视图保持视觉一致性

### 优化
- 移除固定的中心节点锁定，所有节点自由受力运动
- 简化布局类命名：`CenteredForceLayout` → `ForceDirectedLayout`
- 清理废弃的 BFS 环形布局参数（`FORCE_LINK_DEPTH_GAP`、`FORCE_CENTER_PULL`、`FORCE_BRANCH_SPREAD`）

## [2.3.1] - 2026-05-30

### 新增
- **记忆注入方式 `extra_user_content`**: 将记忆追加到用户消息末尾（`mark_as_temp` 不污染对话历史），不影响前缀缓存，推荐作为默认方式
- **system_prompt 注入方式废弃**: 配置为 `system_prompt` 时自动回退至 `extra_user_content`（`InjectionAdapter` 废弃模式降级），保留配置项但标注 ⚠️已废弃
- **Agent 主动记忆写入工具** (`memorize_long_term_memory`): Agent 可主动调用写入长期记忆，通过 `agent_tools.enable_memorize_tool` 配置开关控制（默认关闭）
- **Agent 工具配置组** (`agent_tools`): 新增 `enable_recall_tool` 和 `enable_memorize_tool` 两个独立开关
- **两步确认删除**: dashboard 删除选中记忆改为两步确认（点击→按钮变为「确认删除 X 条?」→再次点击执行），替代被 AstrBot 插件页面拦截的 `window.confirm`

### 修复
- 修复 `MemoryProcessor.__init__` 参数名 `llm_provider_id` → `llm_provider`，兼容传入 provider 实例和 ID 字符串两种调用方式
- 修复 `test_tokenize_removes_common_stopwords` 在 jieba 未安装时的不稳定行为

### 优化
- **7 项异步性能优化**:
  - 记忆注入清理正则提到模块级常量（避免每次调用 `re.compile`）
  - 去重缓存改为惰性过期 + 超限逐条淘汰（消除 `sorted()` 排序开销）
  - 版本备份延迟到异步初始化阶段（通过 `asyncio.to_thread` 避免 `__init__` 中同步 I/O 阻塞）
  - jieba 分词通过 `tokenize_async()` 卸载到线程池
  - `hybrid_retriever` MMR 和 weighting 卸载到线程池
  - `memory_engine` 批处理 `json.loads` 通过 `_normalize_batch_metadata` + 线程池批量规范化
  - `_remove_fake_tool_call_from_context` 两轮扫描合并为单轮
- `InjectionAdapter` 新增 `_DEPRECATED_MODES` 映射，废弃模式统一降级

### 文档
- 更新 CHANGELOG v2.3.1 条目
- 更新所有文档版本号为 v2.3.1（API、ARCHITECTURE、DEVELOPMENT 中/英/俄）

### 测试
- 新增 17 个测试: hybrid_retriever 元数据多样性 + 删除回滚 (6)、memory_engine 更新回滚 + 分批 + 批量删除 (5)、event_handler 上下文扩展 + 重试逻辑 (4)、text_processor add_custom_words (3)
- 测试总数: 298 → 332，覆盖率: 69% → 70%
- 注入方式相关测试: 新增 `extra_user_content` 和 `system_prompt` 自动回退测试
- 修复 24 个因参数签名不匹配导致的测试失败

## [2.3.0] - 2026-05-29

### 新增
- **记忆原子化系统**: 将 LLM 输出的 `key_facts` 提升为独立检索单元 (`MemoryAtom`)，每条原子拥有独立的存活时间 (TTL) 和衰减曲线
  - 五种原子类型: `EPISODIC`(事件型, 7天)、`PLANNED`(计划型, 到期骤降)、`FACTUAL`(事实型, 180天)、`RELATIONAL`(关系型, 90天)、`PREFERENCE`(偏好型, 60天)
  - 三种衰减函数: `EXPONENTIAL`(指数)、`LINEAR`(线性)、`STEP`(阶梯)
  - TTL 动态修正: `ttl = base_ttl × (0.5+importance) × (1.0+0.1×reinforcement_count)`
  - 规则基分类器，零新增 LLM 调用
- **图谱时间感知增强**: 边置信度跨记忆动态更新 (EMA)、跨记忆语义边合并 (`semantic_edge_key`)、检索评分增加时间衰减乘子
- **原子生命周期管理器**: 后台周期维护 (过期/遗忘/强化检测)，基于 Jaccard + CJK bigram 的跨记忆原子强化
- **版本更新自动备份**: 插件启动时检测版本变更，自动将所有数据文件备份到 `backups/v{旧版本}/`，记录 `backup_info.json` 便于数据恢复
- **备份列表 API**: `GET /page/backups` 端点，支持前端查看完整备份历史

### 修复
- 修复图路由权重归一化未生效时双路融合数值不稳定的问题
- 修复 `page_api` 内容更新异常时新旧记忆并存的数据泄漏
- 修复 `memory_engine` 中 fire-and-forget 后台任务未跟踪，`close()` 时可能静默取消
- 修复 `event_handler` 记忆存储后元数据更新失败导致同一段消息被重复总结
- 修复 `command_handler` 中硬编码中文 `"无"` 未走 i18n

### 优化
- 图谱边存储增加 `semantic_edge_key`，跨记忆合并相同语义关系，避免重复边膨胀
- 边置信度采用 EMA 动态更新 (`new = old×0.7 + new×0.3`)，weight 累积证据计数
- `graph_extractor` 支持原子级提取路径 (`_extract_from_atoms`)，原子置信度传播到图谱边
- `atom_store` FTS5 搜索自动回退 LIKE 查询，兼容低版本 SQLite 的 CJK 分词缺陷

### 文档
- 更新 README（中/英/俄）: 补充记忆原子化、版本备份、架构模块说明

### 测试
- 新增 53 个原子系统测试: TTL 计算 (9)、衰减函数 (6)、分类器 (9)、AtomStore (9)、AtomLifecycleManager (3)、AtomRetriever (4)、图谱原子提取 (4)、边合并 (2)、向后兼容 (3)、其他 (4)
- 新增 22 个备份管理器测试: 版本检测、通配符备份、OSError 容错、多版本排序、损坏 JSON 回退、metadata 版本一致性校验
- 新增 1 个实际 `metadata.yaml` 版本号与 `PLUGIN_VERSION` 常量的一致性断言

## [2.2.12] - 2026-05-12

### 新增
- 新增配置 UI 与后端命令响应的英/俄双语国际化支持，适配 AstrBot 原生插件页面与命令输出。

### 修复
- 修复 MemoryProcessor 持有过期 LLM provider 引用时出现 `Cannot send a request, as the client has been closed` 的问题。
- 修复 WebUI 生命周期与历史消息批量清理逻辑，避免重载和端口占用引发的异常。

## [2.2.11] - 2026-05-06

### 新增
- 新增 AstrBot 官方插件 Pages 管理界面支持：可在 AstrBot WebUI 的插件详情页直接进入 `dashboard` 页面，无需额外登录插件独立 WebUI。
- 新增官方插件 Page 原生后端接口适配层，支持记忆统计、记忆列表、批量删除、记忆编辑、召回测试、知识图谱概览与图谱检索。

### 兼容性
- 官方插件 Pages 入口依赖 AstrBot 插件 Page / Bridge 能力，要求 AstrBot 版本 `>= 4.24.2`。
- 保留旧版独立 WebUI 兼容入口；当宿主环境不支持官方插件 Pages 或仍需独立访问时，可继续使用 `/lmem webui` 提供的旧入口。

### 优化
- `/lmem webui` 命令输出改为优先引导用户进入 AstrBot 官方插件页，同时保留旧独立 WebUI 兼容说明。
- 官方插件页面前端改为复用 AstrBot 登录态，并适配 sandbox iframe 环境下的 Bridge 请求、主题读取与页面初始化流程。

### 修复
- 修复 AstrBot 4.23.2 中 `documents_fts` 同名表冲突导致总结记忆存储失败的问题。
- 将插件自有 FTS 表统一迁移为 `livingmemory_memories_fts` 与 `livingmemory_graph_entries_fts`，避免再次污染宿主数据库命名空间。
- 新增 v6 数据库迁移：复制旧 `memories_fts` / `graph_entries_fts` 数据到前缀表，删除插件废弃 `documents_fts(search_text)`，保留 AstrBot 同名表。
- 修复 `/lmem webui` 在旧版独立 WebUI 未启用时缺少兼容提示的问题，并恢复兼容入口访问地址文案。
- 修复 `top_k=0` 私聊场景下消息存储优先级错误，避免错误写入事件默认文本。
- 调整记忆注入格式，同时兼容英文提示模板与既有中文记忆条目标识。

### 测试
- 补充 issue #102 回归测试，覆盖宿主 `documents_fts` 存在时 BM25 写入、旧 FTS 表前缀迁移、废弃插件冲突表清理。
- 补充 `/lmem webui` 兼容提示、`top_k=0` 私聊存储、记忆注入格式兼容回归测试。

### 移除
- 移除废弃的 `sparse_retriever` 实现与配置入口，统一使用文档路 BM25/向量检索、图路检索和 RRF 融合链路。
- 移除废弃的 `reflection_engine.save_original_conversation` 配置项和向量查询预处理开关，减少无效配置面。

### 文档
- 更新 README 与架构文档中的检索层描述，避免继续引用旧的单路混合检索表述。

## [2.2.3] - 2026-02-21

### 修复
- 统一清理插件运行时日志与命令返回中的 emoji 字符，避免日志检索和终端显示噪音
- 优化初始化、命令执行、索引重建、历史清理等失败路径的用户提示：错误信息包含失败动作、错误详情与建议排查步骤
- 修正插件帮助信息与注册元数据中的仓库地址，统一为 `https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory`

### 测试
- 补充 `CommandHandler` 与 `PluginInitializer` 单元测试，覆盖未初始化组件提示、异常提示可操作性、索引重建失败提示、Provider 超时错误信息
- 补充 real-db 功能测试，覆盖命令输入校验、状态异常提示、WebUI 启用/禁用提示分支、cleanup 预演与执行路径
- 新增插件主生命周期集成测试，覆盖初始化状态消息、`_ensure_plugin_ready` 失败分支、命令处理器未就绪提示、WebUI 启停联动与 `terminate` 资源清理

## [2.2.2] - 2026-02-21

### 新增
- 新增 `/lmem summarize` 命令：允许管理员手动立即触发当前会话的记忆总结，无需等待自动触发阈值
- 新增向量检索 token 超限保护：查询文本超过 2000 字符时自动截断，写入内容超过 4000 字符时自动截断，避免 embedding API 报错

### 修复
- 修复群聊记忆中发送者昵称丢失的问题（#59）：助手消息写入时正确标记 `is_bot_message=True`，`format_for_llm` 同时检查 `metadata` 标记和 `role` 字段，确保 Bot 消息以 `[Bot: 昵称]` 格式呈现给 LLM

### 优化
- 总结时自动注入当前日期时间（#74）：在 system_prompt 和提示词模板中注入 `{current_date}`，LLM 可将对话中的相对时间（"今天"、"明天"、"下周"等）转换为具体日期后写入记忆，避免记忆内容随时间推移失去时间参考意义

## [2.2.1] - 2026-02-21

### 修复
- 修复 tool 循环产生的最终总结被错误存入记忆的问题：在 `handle_memory_reflection` 中检测 `tools_call_name` 和 `tools_call_extra_content`，有工具调用上下文时直接跳过，避免 tool loop 的内部总结污染记忆
- 修复 `/reset` 或 `/new` 后插件仍读取旧对话内容进行总结的问题：新增 `after_message_sent` 钩子监听 AstrBot 的 `_clean_ltm_session` 信号，触发时同步调用 `conversation_manager.clear_session()` 清空消息历史和总结计数器
- 修复私聊场景下用户消息写入后未执行消息数量上限控制的问题：`handle_memory_recall` 写入用户消息后补充调用 `_enforce_message_limit`；`handle_memory_reflection` 写入助手消息后同样执行上限控制

## [2.2.0] - 2026-02-21

### 新增
- 新增定期自动备份功能：每日衰减后自动备份记忆数据库，可配置保留天数（默认 7 天），超期备份自动清理
- 新增图片转述内容存入记忆：读取 AstrBot 已完成的图片转述（`extra_user_content_parts`），按消息组件原始顺序正确映射，无转述时降级为 `[图片]` 占位

### 优化
- 所有辅助方法改为 `async def`，消除同步文件 IO 阻塞：`stopwords_manager`、`decay_scheduler`、`text_processor` 均改用 `aiofiles`
- 消息内容提取（`_extract_message_content`）按组件原始顺序拼接，文字与图片相对位置正确保留，不再重复提取转述内容
- `DecayScheduler` 状态管理（`_load_state`、`_save_state`、`_get_last_decay_date`、`_set_last_decay_date`、`_calculate_missed_days`）全部改为异步，避免阻塞事件循环

## [2.1.9] - 2026-02-21

### 修复
- 修复 `memory_engine.delete_memory()` 重复删除 `documents` 表的问题：`hybrid_retriever.delete_memory()` 内部已按顺序删除 BM25 → 向量索引 → documents，上层再次删除会造成连接竞争
- 修复 `update_memory()` 内容更新时旧记忆删除失败静默返回 `True` 的问题：现在改为回滚（删除刚创建的新记忆）并返回 `False`，避免新旧记录并存
- 修复 `status` 命令缺少 `@permission_type(PermissionType.ADMIN)` 装饰器，任意用户均可查看系统状态
- 修复 `help` 命令中仓库链接错误（指向了旧地址）

### 优化
- 数据库迁移（`DBMigration.migrate()`）执行前自动调用 `create_backup()` 创建完整备份，备份失败仅警告不中断迁移，迁移结果中附带 `backup_path`

## [2.1.8] - 2026-02-20

### 修复
- 修复向量索引冗余槽位导致每次启动都触发全量重建的问题：FAISS `ntotal` 包含逻辑删除后的空槽，属正常行为，不再触发重建；仅 BM25 冗余或索引缺失时才重建
- 修复 `get_persona_id` 与 AstrBot 主流程优先级不一致的问题：新增最高优先级 `session_service_config`（由 `/persona` 等命令写入），并正确处理 `[%None]`（明确无人格）不再 fallback 到默认人格
- 修复 `handle_memory_recall` 中 `persona_id` 获取路径：移除直接读取 `req.conversation.persona_id` 的逻辑（`on_llm_request` 钩子在 `_ensure_persona_and_skills` 之前触发，该字段不含 session_service_config 覆盖），统一走完整三级优先级

### 优化
- Provider 未就绪时的日志提示明确区分 Embedding Provider 和 LLM Provider，并附带配置建议
- 周期性重试日志显示当前哪个 Provider 仍未就绪
- 最终超时失败日志列出具体未就绪的 Provider 名称

## [2.1.7] - 2026-02-19

### 新增
- 新增双通道记忆总结机制：`canonical_summary`（事实导向，用于检索）与 `persona_summary`（人格风格，用于注入表达）解耦存储
- 新增 `SummaryValidator`（`_validate_summary_quality`）：对总结结果进行字段完整性、长度、泛化词检测，质量不达标时标记 `summary_quality=low`
- 新增 MMR（最大边际相关性）去重：召回结果在加权排序后执行 Jaccard 相似度去重，避免语义重复记忆占据 Top-K
- 新增 `score_breakdown` 字段：每条召回结果附带各维度分数明细（`rrf_normalized`、`importance`、`recency_weight`、`days_old`、`final_score`），便于调试
- 新增 `source_window` 元数据：记忆写入时记录来源会话窗口（`session_id`、`start_index`、`end_index`、`message_count`），支持后续溯源
- 新增 `summary_schema_version` 字段：新写入记忆标记为 `v2`，旧记录通过数据库迁移补标 `v1`
- 数据库迁移升级至 v4：为所有旧格式记录批量补充 `summary_schema_version=v1` 和 `summary_quality=unknown` 标记

### 修复
- 修复群聊双重写入 Bug：`handle_all_group_messages` 现在跳过 Bot 自身消息，避免 assistant 响应被写入两次（`handle_memory_reflection` 为唯一写入方）
- 修复 `persona_id` 获取不一致问题：优先从 `req.conversation.persona_id` 读取，确保召回与 LLM 调用使用完全相同的人格 ID
- 修复评分公式"清零"问题：将全乘法 `rrf * importance * recency` 改为加权求和 `0.5*rrf + 0.25*importance + 0.25*recency`，高重要性旧记忆不再被时间衰减压制至接近零
- 修复 `last_access_time` 未参与衰减计算的问题：时间衰减基准改为 `max(create_time, last_access_time)`，高频访问记忆衰减自然放缓
- 修复数据库迁移中 `json_set` 语法错误：将无效的 `CASE` 表达式替换为 `COALESCE(NULLIF(TRIM(metadata), ''), '{}')`
- 修复 `_build_storage_format` 中 `summary_quality` 被硬编码为 `"normal"` 的问题，现由 `_validate_summary_quality` 动态决定

### 优化
- 记忆注入改为追加到 `system_prompt` 末尾，确保人格提示词在前、记忆内容在后，符合 LLM 理解优先级
- `content` 字段默认改为存储 `canonical_summary + key_facts`，提升 BM25 检索稳定性
- MMR 参数（`mmr_lambda`）、评分权重（`score_alpha/beta/gamma`）均可通过配置覆盖

### 测试
- 新增 `MemoryProcessor` 群聊路径测试（7 个）：`interaction_type`、`participants` 提取、双通道摘要、缺失字段默认值、私聊无 `participants`、长内容不崩溃、泛化词质量标记
- 新增 `EventHandler` 边界条件与 `source_window` 测试（8 个）：空 prompt 跳过召回、`user_message_before/after` 注入位置、`source_window` 字段写入验证、过期任务跳过、错误/空响应跳过、重试超限放弃
- 新增 `HybridRetriever` 边界条件与回滚测试（7 个）：空查询返回空列表、两路失败返回空列表、单路降级、空 metadata 不崩溃、k 限制结果数量
- 新增 `MemoryEngine` 过滤/衰减/清理边界测试（11 个）：session 隔离、`decay_rate=0`/`days=0` 边界、衰减实际生效、`cleanup` 负数/零天边界、内容更新先建后删、删除不存在 ID、空查询、统计字段
- 全量测试 118 个，全部通过（pytest + pytest-asyncio）

## [2.1.4] - 2026-02-19

### 优化
- 优化记忆注入方式
- 优化删除逻辑，确保内容安全
- 改进 Webui 的会话处理逻辑
- 添加每日自动清理功能
- 优化记忆管理和初始化逻辑


## [2.1.2] - 2026-01-20

### 修复
- 修复历史消息清理功能无法处理多模态消息格式的问题
  - 支持 OpenAI 多模态格式: `{"role": "user", "content": [{"type": "text", "text": "xxx"}]}`
  - 正确清理 contexts 中 list 类型 content 的记忆注入片段
  - 修复清理逻辑只处理 string 类型 content 导致的清理失败

### 优化
- 简化记忆清理日志输出,移除冗余的 DEBUG 级别日志
- 优化 `_remove_injected_memories_from_context` 方法,支持三种 contexts 格式
- 改进 cleanup 命令,操作 AstrBot 数据库而非插件自身数据库

## [2.1.1] - 2026-01-19

### 新增
- 添加 `/lmem cleanup` 命令，支持清理历史消息中的记忆注入片段
- 增强记忆处理器，支持人格提示和上下文管理
- 处理 Message 对象的 metadata 字段，支持 JSON 字符串解析

### 优化
- 更新人格提示和总结要求，增强记忆生成的个性化和准确性
- 增强命令处理和事件处理逻辑，添加输入验证和后台任务管理
- 更新消息数量上限控制逻辑，仅删除已总结的消息

## [2.0.11] - 2026-01-06

### 新增
- 添加 LLM 调用重试机制和 JSON 修复功能，增强数据处理的鲁棒性
- 添加记忆重要性衰减调度器，支持每日自动衰减处理
- 增强事件处理器和记忆处理器，支持失败总结重试机制和 JSON 格式输出修复

### 优化
- 按创建时间降序排序记忆列表，优化用户体验
- 增强事件处理器和会话管理器，优化群聊判断逻辑

## [2.0.8] - 2026-01-05

### 修复
- 修复命令无法正确响应问题

### 优化
- 更新私聊提示，增强消息格式说明和昵称使用规则
- 重构自动发布工作流，简化版本检查与发布逻辑，移除旧的 release.yml 文件

## [2.0.6] - 2026-01-04

### 新增
- 添加索引维度检查与修复逻辑，确保与当前 embedding provider 维度一致
- 增强数据一致性检查，添加实际消息数量获取和同步逻辑
- 增强响应内容检查，过滤空回复和错误响应，确保消息记录的有效性

### 修复
- 修复指令无法使用问题

### 优化
- 优化代码格式，增强可读性，调整多个文件中的代码缩进和换行
- 增强调试信息，优化消息格式化逻辑，更新群聊提示文档

## [2.0.2] - 2025-12-18

### 修复
- 修复会话 message_count 不一致问题，增强消息获取逻辑和调试信息

### 优化
- 更新默认监听端口至 8888

## [2.0.1] - 2025-12-18

### 优化
- 优化自动发布工作流中的版本检查和日志输出
- 重构和增强代码结构，添加新测试和性能基准
- 删除 lint 和 test 工作流配置文件

## [2.0.0] - 2025-12-17

### 🎉 重大重构

这是一个完全重构的版本，旨在提升代码质量、可维护性和可测试性。

#### 架构改进
- **模块化设计**: 将1663行的main.py拆分为多个职责单一的模块
  - `PluginInitializer`: 负责插件初始化逻辑（380行）
  - `EventHandler`: 负责事件处理（450行）
  - `CommandHandler`: 负责命令处理（220行）
  - `ConfigManager`: 集中配置管理（95行）
  - main.py简化至280行，只保留插件注册和生命周期管理

#### 新增模块
- **异常处理系统** (`core/exceptions.py`)
  - 定义了8个自定义异常类
  - 统一的错误码体系
  - 清晰的异常继承关系

- **配置管理器** (`core/config_manager.py`)
  - 集中配置加载和验证
  - 支持点号分隔的嵌套键访问
  - 提供便捷的配置节访问属性

- **插件初始化器** (`core/plugin_initializer.py`)
  - 非阻塞初始化机制
  - Provider等待和重试逻辑
  - 清晰的初始化状态管理
  - 自动数据库迁移和索引重建

- **事件处理器** (`core/event_handler.py`)
  - 统一处理所有事件钩子
  - 群聊消息捕获
  - 记忆召回和反思
  - 消息去重机制

- **命令处理器** (`core/command_handler.py`)
  - 统一处理所有命令
  - 清晰的命令响应格式
  - 完善的错误处理

#### 测试基础设施
- 创建了完整的测试目录结构
- 添加了pytest配置文件
- 编写了ConfigManager和异常模块的单元测试
- 为后续测试覆盖奠定基础

#### 代码质量提升
- **代码量优化**: 核心代码从1663行优化至1483行（减少11%）
- **职责分离**: 每个模块职责单一，易于理解和维护
- **可测试性**: 模块解耦，支持依赖注入，易于测试
- **错误处理**: 统一的异常体系和错误处理流程
- **配置管理**: 集中化的配置加载和验证

#### 文档完善
- 新增 `REFACTOR_FEATURE_ANALYSIS.md`: 详细的功能分析文档
- 新增 `REFACTOR_PLAN.md`: 完整的重构计划文档
- 所有新模块都有完整的文档字符串

### 保持不变
- ✅ 所有现有功能完全保留
- ✅ 数据库结构完全兼容
- ✅ 配置文件格式完全兼容
- ✅ 所有公开API接口保持不变
- ✅ 向后兼容旧版本数据

### 技术债务清理
- 移除了重复的代码
- 统一了日志记录格式
- 规范了错误处理流程
- 优化了初始化逻辑

---

## [1.5.18] - 2025-11-06

### 工作流优化
- 创建了全新的 GitHub Actions 工作流系统
- 自动化版本发布流程
- 智能 Issue 管理

---

注意：请在每次发版前更新此文件，将 [Unreleased] 部分的内容移动到新版本号下。
