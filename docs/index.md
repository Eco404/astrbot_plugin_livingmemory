---
layout: home
title: LivingMemory
titleTemplate: 可溯源的长期记忆
hero:
  name: LivingMemory
  text: 让长期记忆既能记住，也能整理
  tagline: 将连续对话沉淀为 Timeline，再并行整理为可溯源的 Topic 与当前用户画像。召回保持精简，对象理解保持连续，来源仍可检查。
  image:
    src: /logo.png
    alt: LivingMemory
  actions:
    - theme: brand
      text: 开始使用
      link: /guide/getting-started
    - theme: alt
      text: 理解记忆架构
      link: /architecture
features:
  - title: Timeline 保存经历
    details: 按会话和人格总结连续对话，保留事实、情绪、时间范围、人物绑定与来源快照。
  - title: Topic 整理主题
    details: 将 Timeline 切成正式片段并跨时间归并，可按关键字或 Embedding 相关性搜索主题记忆。
  - title: Topic 优先召回
    details: 以当前消息决定候选资格，可选 Rerank 精排，再用事实和片段补足细节与情绪。
  - title: 当前用户画像
    details: 为稳定私聊对象加载精确作用域，只注入有效客观事实与当前 persona 的关系状态。
---

<section class="home-band">
  <span class="home-kicker">Memory architecture</span>
  <h2>一份来源，两条可回溯的派生路径</h2>
  <p>LivingMemory 不把聊天记录、长期经历、主题知识和用户画像混成一个列表。Timeline 负责保存发生过什么；Topic 组织跨时间记忆，用户画像理解当前私聊对象，两条派生路径都保留来源且不反向改写 Timeline。</p>

![LivingMemory 记忆架构](./assets/images/architecture-overview-zh.svg){.diagram}

  <div class="home-memory-grid">
    <div>
      <h3>Timeline 是来源层</h3>
      <p>固定轮次、空闲窗口或手动命令触发总结。原始消息可清理，但来源快照、时间范围和稳定记忆身份仍可支持审计与重构。</p>
    </div>
    <div>
      <h3>Topic 是派生层</h3>
      <p>Topic 不允许直接编辑。Timeline 改变后，关联正式片段、事实、人物关系与 Topic 会通过局部维护重新同步；日常浏览可使用关键字或语义相关性搜索。</p>
    </div>
    <div>
      <h3>用户画像是并行派生层</h3>
      <p>稳定私聊 actor 的确定性事实与 persona 关系独立维护。只有 active 事实可注入，关系维护执行时读取当前人格，数据库只保留 digest。</p>
    </div>
    <div>
      <h3>运行时按请求汇合</h3>
      <p>Topic/Timeline 负责相关长期记忆，画像按 Bot、persona 与逻辑用户精确加载；普通召回无结果时，画像仍可独立注入。</p>
    </div>
  </div>
</section>

<section class="home-band">
  <span class="home-kicker">Recall</span>
  <h2>相关性优先，同时保留语境</h2>
  <p>当前用户消息始终决定 Topic 是否合格；最近上下文只提供有限辅助。正式片段与事实补充具体情节和情绪，必要时回退 Timeline；当前用户画像则走独立精确作用域，并明确服从当前对话。</p>

![Topic 优先召回](./assets/images/recall-flow-zh.svg){.diagram}
</section>

<section class="home-band">
  <span class="home-kicker">Operations</span>
  <h2>长期运行需要维护，而不是放任数据库增长</h2>
  <p>统一维护中心将高风险操作从日常页面中分离。检查、预览、确认、进度和回滚构成一致的操作约束。</p>
  <div class="home-ops-grid">
    <div>
      <h3>构建与修复</h3>
      <p>全量构建原子发布；增量只处理新增或待同步的 Timeline；低质量 Timeline 可同 ID 重构。</p>
    </div>
    <div>
      <h3>审计与诊断</h3>
      <p>查看 Topic 待审查、最近真实召回、召回测试、模型连接、会话审计和数据库健康状态。</p>
    </div>
    <div>
      <h3>清理与归档</h3>
      <p>清理已完成任务中间数据、归档派生 Topic、管理不活跃 Timeline，并推进画像事实生命周期与可重建投影压缩。</p>
    </div>
    <div>
      <h3>迁移与恢复</h3>
      <p>数据库 v8 依次迁移到 v9、v10 和正式 v10.4；用户画像结构合并为一次 v10.3 到 v10.4 升级。</p>
    </div>
  </div>
</section>
