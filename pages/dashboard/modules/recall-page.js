/**
 * Recall Page - 召回测试页面
 * 负责测试记忆召回功能
 */

import { esc, statusPill, normalizeImportance } from "./utils.js";

export class RecallPage {
  constructor(state, apiClient, readonlyViewer, confirmDialog, showToast) {
    this.state = state;
    this.api = apiClient;
    this.viewer = readonlyViewer;
    this.confirmDialog = confirmDialog;
    this.notify = showToast;
    this.sessionsLoaded = false;
    this.sessionsLoading = null;
    this.currentExport = null;
  }

  async fetchSessions(force = false) {
    if (this.sessionsLoaded && !force) return;
    if (this.sessionsLoading && !force) return this.sessionsLoading;
    this.sessionsLoading = this.api.get("sessions", { flat: "true", limit: 500 })
      .then(data => {
        const list = document.getElementById("recall-session-options");
        if (list) {
          list.innerHTML = (data.items || []).map(item => {
            const label = `${item.chat_type || ""} · ${item.target_id || ""} · ${item.platform_id || item.platform || ""}`;
            return `<option value="${esc(item.session_id)}" label="${esc(label)}"></option>`;
          }).join("");
        }
        this.sessionsLoaded = true;
      })
      .catch(error => console.warn("[LM] Failed to load recall sessions:", error))
      .finally(() => { this.sessionsLoading = null; });
    return this.sessionsLoading;
  }

  /**
   * 初始化召回页面事件监听
   */
  initEventListeners() {
    const queryInput = document.getElementById("recall-query");
    const searchBtn = document.getElementById("recall-search-btn");
    const kSlider = document.getElementById("recall-k");
    const kValue = document.getElementById("recall-k-value");

    // k 值滑块
    if (kSlider && kValue) {
      kSlider.addEventListener("input", () => {
        kValue.textContent = kSlider.value;
      });
    }

    // 搜索按钮
    if (searchBtn) {
      searchBtn.addEventListener("click", () => this.runRecall());
    }

    // 回车搜索（Ctrl+Enter 或 Cmd+Enter）
    if (queryInput) {
      queryInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
          this.runRecall();
        }
      });
    }
    document.getElementById("recall-export-current")?.addEventListener("click", () => this.exportJson(this.currentExport));
    document.getElementById("recall-history-refresh")?.addEventListener("click", () => this.loadHistory());
    document.getElementById("recall-history-clear")?.addEventListener("click", () => this.clearHistory());
    document.getElementById("recall-history-list")?.addEventListener("click", event => this.handleHistoryClick(event));
  }

  /**
   * 执行召回测试
   */
  async runRecall() {
    const query = document.getElementById("recall-query").value.trim();
    const k = parseInt(document.getElementById("recall-k").value) || 5;
    const sessionId = document.getElementById("recall-session").value.trim();
    const mode = document.getElementById("recall-mode")?.value || "current";

    if (!query) {
      this.showToast(window.t("recall.enterQuery"), true);
      return;
    }

    const searchBtn = document.getElementById("recall-search-btn");
    if (searchBtn) searchBtn.disabled = true;

    const startTime = Date.now();

    try {
      const params = { query, k, mode };
      if (sessionId) params.session_id = sessionId;

      const data = await this.api.post("recall/test", params);
      const elapsed = Date.now() - startTime;

      this.state._recallCache = { data, elapsed };
      this.currentExport = data;
      document.getElementById("recall-export-current")?.classList.remove("hidden");
      this.renderResults(data, elapsed);
      await this.loadHistory();
    } catch (e) {
      this.showToast(e.message || window.t("recall.fail"), true);
      document.getElementById("recall-results").innerHTML = "";
      document.getElementById("recall-stats").classList.add("hidden");
      document.getElementById("recall-diagnostics")?.classList.add("hidden");
      this.state._recallCache = null;
      this.currentExport = null;
      document.getElementById("recall-export-current")?.classList.add("hidden");
    } finally {
      if (searchBtn) searchBtn.disabled = false;
    }
  }

  /**
   * 渲染召回结果
   * @param {Object} data - 召回结果数据
   * @param {number} elapsed - 耗时（毫秒）
   */
  renderResults(data, elapsed) {
    // 后端返回 results，不是 memories
    const memories = data.results || data.memories || [];
    const count = memories.length;

    // 更新统计信息
    const statsEl = document.getElementById("recall-stats");
    const countText = document.getElementById("recall-count-text");
    const timeText = document.getElementById("recall-time-text");

    if (statsEl) statsEl.classList.remove("hidden");
    if (countText) {
      countText.textContent = count === 0
        ? window.t("recall.noMatch")
        : window.t("recall.resultsCount", count);
    }
    if (timeText) {
      const time = data.elapsed_time_ms || elapsed;
      timeText.textContent = window.t("recall.timeElapsed", (time / 1000).toFixed(2));
    }

    this.renderDiagnostics(data.diagnostics || {});

    const resultsEl = document.getElementById("recall-results");
    if (!resultsEl) return;

    if (count === 0) {
      resultsEl.innerHTML = '<div class="table-empty">' + window.t("recall.noMatch") + '</div>';
      return;
    }

    let html = '<div class="recall-results-list">';

    memories.forEach((mem, idx) => {
      // 后端返回 memory_id 和 content
      const memoryId = mem.memory_id || mem.id;
      const content = mem.content || mem.text || mem.summary || "";
      // 后端返回 similarity_score，不是 score
      const score = mem.similarity_score != null ? Number(mem.similarity_score).toFixed(3) :
                    (mem.score != null ? Number(mem.score).toFixed(3) : "--");
      const importance = normalizeImportance(mem.metadata?.importance || 0.5).toFixed(1);
      const type = mem.metadata?.memory_layer || mem.metadata?.memory_type || "GENERAL";
      const status = mem.metadata?.status || "active";
      const breakdown = mem.score_breakdown || {};

      const scoreNum = Number(score);
      const scoreCls = scoreNum >= 0.75 ? "high" : scoreNum >= 0.45 ? "medium" : "low";

      html += '<div class="result-card recall-result-item" data-memory-id="' + memoryId + '">';
      html += '<div class="result-card-header recall-result-header">';
      html += '<span class="result-rank recall-result-rank">#' + (idx + 1) + '</span>';
      html += '<span class="cell-mono recall-result-id">ID: ' + memoryId + '</span>';
      html += '<span class="result-score-badge ' + scoreCls + ' recall-result-score">Score: ' + score + '</span>';
      html += statusPill(status);
      html += '<span class="type-tag">' + esc(type) + '</span>';
      html += '</div>';
      html += '<div class="result-content recall-result-content">' + esc(content) + '</div>';
      html += '<div class="recall-result-meta text-secondary">';
      html += '<span>' + window.t("detail.importance") + ': ' + importance + '/10</span>';
      if (mem.metadata?.session_id) {
        html += '<span>Session: ' + esc(String(mem.metadata.session_id)) + '</span>';
      }
      if (breakdown.recall_relevance_score != null) {
        html += '<span>' + window.t("recall.relevance") + ': ' + Number(breakdown.recall_relevance_score).toFixed(3) + '</span>';
      }
      if (breakdown.recall_branch_count != null) {
        html += '<span>' + window.t("recall.branchCount") + ': ' + Number(breakdown.recall_branch_count).toFixed(0) + '</span>';
      }
      html += '</div>';
      html += '</div>';
    });

    html += '</div>';
    resultsEl.innerHTML = html;

    // 绑定点击事件
    resultsEl.querySelectorAll(".recall-result-item").forEach(item => {
      item.addEventListener("click", async () => {
        const memoryId = item.dataset.memoryId;
        const memory = memories.find(m => String(m.memory_id || m.id) === memoryId);
        if (memory) {
          await this.openReadonlyResult(memory, item);
        }
      });
    });
  }

  async openReadonlyResult(memory, trigger) {
    const layer = memory.metadata?.memory_layer || "timeline";
    let content = memory.content || memory.text || memory.summary || "";
    const identifiers = [String(memory.memory_id || memory.id || "")];
    if ((layer === "timeline" || layer === "timeline_supplement") && /^\d+$/.test(identifiers[0])) {
      try {
        const detail = await this.api.get("memories/detail", { memory_id: identifiers[0] });
        content = detail.content || detail.text || detail.summary || content;
        if (detail.session_id || detail.metadata?.session_id) identifiers.push(String(detail.session_id || detail.metadata.session_id));
      } catch (error) {
        console.warn("[LM] Failed to load read-only Timeline detail:", error);
      }
    }
    this.viewer?.showReadonlyRecord({
      title: layer === "topic" ? "Topic 记忆" : layer === "topic_fragment" ? "Topic 正式片段" : "Timeline 记忆",
      content,
      contentLabel: "只读内容",
      identifiers,
      metadata: [
        { label: "层", value: layer },
        { label: "得分", value: String(memory.similarity_score ?? "--") },
      ],
    }, trigger);
  }

  async loadHistory() {
    const target = document.getElementById("recall-history-list");
    if (!target) return;
    try {
      const data = await this.api.get("recall/traces", { type: "test", limit: 50 });
      const items = data.items || [];
      target.innerHTML = items.length ? items.map(item => `
        <div class="recall-history-row" data-trace-uid="${esc(item.trace_uid)}">
          <button type="button" class="recall-history-open" data-history-open="${esc(item.trace_uid)}">
            <span><strong>${esc(item.query_text || "--")}</strong><small>${esc(item.mode)} · ${Number(item.result_count || 0)} 条 · ${new Date(Number(item.created_at || 0) * 1000).toLocaleString()}</small></span>
            <span class="status-badge status-${esc(item.status)}">${esc(item.status)}</span>
          </button>
          <button class="btn btn-secondary btn-sm" type="button" data-history-export="${esc(item.trace_uid)}">导出</button>
          <button class="btn btn-danger btn-sm" type="button" data-history-delete="${esc(item.trace_uid)}">删除</button>
        </div>`).join("") : `<span class="text-tertiary">暂无测试历史</span>`;
    } catch (error) {
      target.innerHTML = `<span class="text-tertiary">${esc(error.message || "加载失败")}</span>`;
    }
  }

  async handleHistoryClick(event) {
    const open = event.target.closest("[data-history-open]");
    const exportButton = event.target.closest("[data-history-export]");
    const deleteButton = event.target.closest("[data-history-delete]");
    const uid = open?.dataset.historyOpen || exportButton?.dataset.historyExport || deleteButton?.dataset.historyDelete;
    if (!uid) return;
    if (deleteButton) {
      if (deleteButton.disabled) return;
      deleteButton.disabled = true;
      try {
        await this.api.post("recall/traces/delete", { trace_uid: uid, type: "test" });
        await this.loadHistory();
        this.showToast(window.t("recall.historyDeleted"));
      } catch (error) {
        this.showToast(error.message || window.t("recall.historyDeleteFailed"), true);
        deleteButton.disabled = false;
      }
      return;
    }
    try {
      const record = await this.api.get("recall/traces/detail", { trace_uid: uid });
      const payload = record.result || {};
      if (exportButton) {
        await this.exportJson(record);
        return;
      }
      this.currentExport = record;
      document.getElementById("recall-export-current")?.classList.remove("hidden");
      this.renderResults(payload, Number(record.elapsed_ms || 0));
    } catch (error) {
      this.showToast(error.message || window.t("recall.historyLoadFailed"), true);
    }
  }

  async clearHistory() {
    const confirmed = await this.confirmDialog.show({
      title: window.t("recall.clearHistoryTitle"),
      message: window.t("recall.clearHistoryMessage"),
      confirmLabel: window.t("common.clear"),
    });
    if (!confirmed) return;
    const button = document.getElementById("recall-history-clear");
    button.disabled = true;
    try {
      await this.api.post("recall/traces/clear", { type: "test" });
      await this.loadHistory();
      this.showToast(window.t("recall.historyCleared"));
    } catch (error) {
      this.showToast(error.message || window.t("recall.historyClearFailed"), true);
    } finally {
      button.disabled = false;
    }
  }

  async exportJson(value) {
    if (!value) return;
    const text = JSON.stringify(value, null, 2);
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }
    this.showToast("JSON 已复制到剪贴板");
  }

  renderDiagnostics(diagnostics) {
    const element = document.getElementById("recall-diagnostics");
    if (!element) return;
    const branches = diagnostics.query_branches || [];
    const candidates = diagnostics.candidates || [];
    if (!branches.length && !candidates.length) {
      element.classList.add("hidden");
      element.innerHTML = "";
      return;
    }

    const filtered = candidates.filter(item => !item.selected);
    let html = '<div class="recall-diagnostics-header">';
    html += '<strong>' + window.t("recall.diagnostics") + '</strong>';
    html += '<span>' + window.t("recall.candidateSummary", diagnostics.candidate_count || 0, diagnostics.selected_count || 0) + '</span>';
    html += '<span>' + window.t("recall.threshold") + ': ' + Number(diagnostics.applied_threshold || 0).toFixed(3) + '</span>';
    html += '<span>' + window.t("recall.overlapSuppressed") + ': ' + Number(diagnostics.overlap_suppressed || 0) + '</span>';
    html += '</div>';
    html += '<div class="recall-query-branches">';
    branches.forEach(branch => {
      html += '<div class="recall-query-branch">';
      html += '<span class="type-tag">' + esc(branch.name) + '</span>';
      html += '<span class="text-secondary">' + esc(branch.role) + ' · weight ' + Number(branch.weight).toFixed(2) + '</span>';
      html += '<span class="recall-query-text">' + esc(branch.text || "") + '</span>';
      html += '</div>';
    });
    html += '</div>';
    const topic = diagnostics.topic;
    if (topic) {
      html += '<div class="recall-topic-diagnostics">';
      html += '<strong>' + window.t("recall.topicDiagnostics") + '</strong>';
      html += '<span>' + window.t("recall.candidateSummary", topic.candidate_count || 0, topic.selected_count || 0) + '</span>';
      html += '<span>' + window.t("recall.threshold") + ': ' + Number(topic.applied_threshold || 0).toFixed(3) + '</span>';
      if (Number(topic.selection_threshold || 0) > Number(topic.applied_threshold || 0)) {
        html += '<span>' + window.t("topic.selectionThreshold") + ': ' + Number(topic.selection_threshold).toFixed(3) + '</span>';
      }
      html += '<span>' + window.t("recall.topicContextSuppressed") + ': ' + Number(topic.context_suppressed || 0) + '</span>';
      if (diagnostics.topic_space_id) {
        html += '<span class="cell-mono">' + esc(diagnostics.topic_space_id) + '</span>';
      }
      html += '</div>';
      const topicCandidates = topic.candidates || [];
      if (topicCandidates.length) {
        html += '<details class="recall-filtered"><summary>' + window.t("recall.topicCandidates", topicCandidates.length) + '</summary>';
        html += '<div class="recall-filtered-list">';
        topicCandidates.forEach(item => {
          html += '<div><span class="cell-mono">' + esc(item.title || item.topic_uid) + '</span>';
          html += '<span>' + (item.selected ? window.t("recall.selected") : window.t("recall.filtered")) + '</span>';
          html += '<span>current ' + Number(item.current_relevance || 0).toFixed(3) + '</span>';
          html += '<span>context +' + Number(item.context_support || 0).toFixed(3) + '</span>';
          html += '<span>rank ' + Number(item.ranking_score || item.final_score || 0).toFixed(3) + '</span>';
          html += '<span>emb ' + Number(item.embedding_score || 0).toFixed(3) + '</span>';
          html += '<span>key ' + Number(item.keyword_score || 0).toFixed(3) + '</span>';
          if (item.rerank_score != null) {
            html += '<span>rerank raw ' + Number(item.rerank_score).toFixed(3) + '</span>';
            html += '<span>rerank #' + esc(String(item.rerank_rank || "--")) + ' · strength ' + Number(item.rerank_confidence || 0).toFixed(3) + ' · +' + Number(item.rerank_rank_boost || 0).toFixed(3) + '</span>';
          }
          if (Number(item.actor_match_boost || 0) > 0) {
            html += '<span>' + window.t("recall.actorBoost") + ' +' + Number(item.actor_match_boost).toFixed(3) + '</span>';
            html += '<span class="cell-mono">' + esc((item.matched_actor_ids || []).join(", ")) + '</span>';
          }
          html += '<span>coverage ' + Number(item.context_coverage || 0).toFixed(2) + '</span>';
          if (item.filter_reason) html += '<span>' + esc(item.filter_reason) + '</span>';
          html += '</div>';
        });
        html += '</div></details>';
      }
    }
    const topicFragments = diagnostics.topic_fragments;
    if (topicFragments) {
      html += '<div class="recall-topic-diagnostics">';
      html += '<strong>' + window.t("recall.fragmentDiagnostics") + '</strong>';
      html += '<span>' + window.t("recall.candidateSummary", topicFragments.candidate_count || 0, topicFragments.selected_count || 0) + '</span>';
      html += '<span>' + window.t("recall.availableFragments") + ': ' + Number(topicFragments.available_count || 0) + '</span>';
      html += '<span>' + window.t("recall.threshold") + ': ' + Number(topicFragments.applied_threshold || 0).toFixed(3) + '</span>';
      if (Number(topicFragments.selection_threshold || 0) > Number(topicFragments.applied_threshold || 0)) {
        html += '<span>' + window.t("topic.selectionThreshold") + ': ' + Number(topicFragments.selection_threshold).toFixed(3) + '</span>';
      }
      if (Number(topicFragments.duplicate_parent_count || 0) > 0) {
        html += '<span>' + window.t("topic.duplicateFragments") + ': ' + Number(topicFragments.duplicate_parent_count) + '</span>';
      }
      html += '</div>';
      const fragmentCandidates = topicFragments.candidates || [];
      if (fragmentCandidates.length) {
        html += '<details class="recall-filtered"><summary>' + window.t("recall.fragmentCandidates", fragmentCandidates.length) + '</summary>';
        html += '<div class="recall-filtered-list">';
        fragmentCandidates.forEach(item => {
          html += '<div><span class="cell-mono">' + esc(item.label || item.fragment_uid) + '</span>';
          html += '<span>' + (item.selected ? window.t("recall.selected") : window.t("recall.filtered")) + '</span>';
          html += '<span>current ' + Number(item.current_relevance || 0).toFixed(3) + '</span>';
          html += '<span>context +' + Number(item.context_support || 0).toFixed(3) + '</span>';
          html += '<span>rank ' + Number(item.ranking_score || item.final_score || 0).toFixed(3) + '</span>';
          html += '<span>parent ' + Number(item.parent_topic_relevance || 0).toFixed(3) + '</span>';
          html += '<span>emb ' + Number(item.embedding_score || 0).toFixed(3) + '</span>';
          html += '<span>key ' + Number(item.keyword_score || 0).toFixed(3) + '</span>';
          if (item.body_suppressed) {
            html += '<span>' + window.t("topic.fragmentBodySuppressed") + ' · ' + window.t("topic.fragmentFacts", Number(item.fact_count || 0)) + '</span>';
          }
          if (item.rerank_score != null) {
            html += '<span>rerank raw ' + Number(item.rerank_score).toFixed(3) + '</span>';
            html += '<span>rerank #' + esc(String(item.rerank_rank || "--")) + ' · strength ' + Number(item.rerank_confidence || 0).toFixed(3) + ' · +' + Number(item.rerank_rank_boost || 0).toFixed(3) + '</span>';
          }
          if (item.filter_reason) html += '<span>' + esc(item.filter_reason) + '</span>';
          html += '</div>';
        });
        html += '</div></details>';
      }
    }
    if (filtered.length) {
      html += '<details class="recall-filtered"><summary>' + window.t("recall.filteredCandidates", filtered.length) + '</summary>';
      html += '<div class="recall-filtered-list">';
      filtered.forEach(item => {
        html += '<div><span class="cell-mono">ID ' + esc(String(item.memory_id)) + '</span>';
        html += '<span>rel ' + Number(item.relevance_score || 0).toFixed(3) + '</span>';
        html += '<span>score ' + Number(item.fused_score || 0).toFixed(3) + '</span>';
        html += '<span>' + esc(item.filter_reason || "--") + '</span></div>';
      });
      html += '</div></details>';
    }
    element.innerHTML = html;
    element.classList.remove("hidden");
  }

  /**
   * 显示 Toast 提示
   * @param {string} message - 提示消息
   * @param {boolean} isError - 是否为错误
   */
  showToast(message, isError = false) {
    if (this.notify) this.notify(message, isError);
    else if (window.lmShowToast) window.lmShowToast(message, isError);
  }
}
