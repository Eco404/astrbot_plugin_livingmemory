/**
 * Recall Page - 召回测试页面
 * 负责测试记忆召回功能
 */

import { esc, statusPill, normalizeImportance } from "./utils.js";

export class RecallPage {
  constructor(state, apiClient, peekPanel) {
    this.state = state;
    this.api = apiClient;
    this.peek = peekPanel;
    this.sessionsLoaded = false;
    this.sessionsLoading = null;
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
      this.renderResults(data, elapsed);
    } catch (e) {
      this.showToast(e.message || window.t("recall.fail"), true);
      document.getElementById("recall-results").innerHTML = "";
      document.getElementById("recall-stats").classList.add("hidden");
      document.getElementById("recall-diagnostics")?.classList.add("hidden");
      this.state._recallCache = null;
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
      item.addEventListener("click", () => {
        const memoryId = item.dataset.memoryId;
        const memory = memories.find(m => String(m.memory_id || m.id) === memoryId);
        if (memory) {
          this.peek.renderMemory({
            memory_id: memory.memory_id || memory.id,
            summary: memory.content || memory.text || memory.summary,
            content: memory.content || memory.text,
            memory_type: memory.metadata?.memory_type,
            importance: memory.metadata?.importance,
            status: memory.metadata?.status,
            created_at: memory.metadata?.create_time
              ? new Date(memory.metadata.create_time * 1000).toLocaleString()
              : "--",
            raw: memory
          });
        }
      });
    });
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
    if (window.lmShowToast) {
      window.lmShowToast(message, isError);
    }
  }
}
