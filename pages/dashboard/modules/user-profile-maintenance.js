import { esc } from "./utils.js";

const DIMENSIONS = ["familiarity", "trust", "warmth", "ease", "tension", "concern"];

export class UserProfileMaintenance {
  constructor(api, showToast, confirmDialog) {
    this.api = api;
    this.showToast = showToast;
    this.confirmDialog = confirmDialog;
    this.items = [];
    this.detail = null;
    this.selectedScopeUid = "";
    this.searchTimer = null;
    this.bindPreview = null;
    this.sharePreview = null;
    this.buildCandidates = [];
    this.taskPollTimer = null;
    this.taskPollInFlight = false;
    this.taskPollGeneration = 0;
    this.operationInFlight = false;
    this.operationLabel = "";
    this.taskProgressBaseline = null;
    this.isActive = false;
  }

  initEventListeners() {
    document.getElementById("user-profile-search")?.addEventListener("input", () => {
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(() => this.loadList(), 250);
    });
    document.getElementById("user-profile-status")?.addEventListener("change", () => this.loadList());
    document.getElementById("user-profile-refresh")?.addEventListener("click", () => this.refresh());
    document.getElementById("user-profile-build-open")?.addEventListener("click", () => this.openBuildPanel());
    document.getElementById("user-profile-build-close")?.addEventListener("click", () => this.closeBuildPanel());
    document.getElementById("user-profile-build-list")?.addEventListener("click", event => {
      const button = event.target.closest("[data-profile-build-candidate]");
      if (button) this.buildCandidate(button);
    });
    document.getElementById("user-profile-list")?.addEventListener("click", event => {
      const item = event.target.closest("[data-profile-scope]");
      if (item) this.select(item.dataset.profileScope);
    });
    document.getElementById("user-profile-detail")?.addEventListener("click", event => this.handleDetailClick(event));
    document.getElementById("user-profile-detail")?.addEventListener("input", event => {
      const slider = event.target.closest("[data-relationship-dimension]");
      if (slider) slider.nextElementSibling.textContent = slider.value;
    });
    window.addEventListener("languagechange", () => {
      this.renderList();
      this.renderDetail();
      this.renderBuildCandidates();
    });
  }

  async activate() {
    this.isActive = true;
    if (!this.items.length) await this.loadList();
    else if (this.selectedScopeUid && !this.detail) await this.loadDetail();
    else if (this.selectedScopeUid) this.resumeTaskPolling();
  }

  deactivate() {
    this.isActive = false;
    this.stopTaskPolling();
  }

  async refresh() {
    await this.loadList();
    if (this.selectedScopeUid) await this.loadDetail();
  }

  async loadList() {
    const list = document.getElementById("user-profile-list");
    if (list) list.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
    try {
      const data = await this.api.get("user-profiles", {
        search: document.getElementById("user-profile-search")?.value || "",
        status: document.getElementById("user-profile-status")?.value || "",
        limit: 200,
      });
      this.items = data.items || [];
      if (this.selectedScopeUid && !this.items.some(item => item.profile_scope_uid === this.selectedScopeUid)) {
        this.stopTaskPolling();
        this.selectedScopeUid = "";
        this.detail = null;
      }
      this.renderList();
      this.renderDetail();
    } catch (error) {
      if (list) list.innerHTML = `<div class="identity-state error">${esc(error.message)}</div>`;
    }
  }

  renderList() {
    const list = document.getElementById("user-profile-list");
    if (!list) return;
    if (!this.items.length) {
      list.innerHTML = `<div class="identity-state user-profile-empty-state"><span>${esc(window.t("profile.empty"))}</span></div>`;
      return;
    }
    list.innerHTML = this.items.map(item => {
      const status = item.auto_enable_blocked ? "deleted" : item.enabled ? "enabled" : "disabled";
      const reviewCount = Number(item.pending_fact_count || 0) + Number(item.conflict_fact_count || 0) + Number(item.stale_fact_count || 0);
      const runningTasks = Number(item.running_task_count || 0);
      return `<button class="user-profile-list-item${item.profile_scope_uid === this.selectedScopeUid ? " active" : ""}" type="button" data-profile-scope="${esc(item.profile_scope_uid)}">
        <span class="user-profile-list-main"><strong>${esc(item.display_name || item.actor_id || item.logical_user_uid)}</strong><small>${esc(item.platform || "")} · ${esc(item.stable_user_id || "")}</small></span>
        <span class="user-profile-list-meta"><span class="status-badge ${esc(status)}">${esc(window.t(`profile.status.${status}`))}</span><small>${esc(item.bot_account)} / ${esc(item.persona_id || "-")}</small></span>
        <span class="user-profile-list-counts"><span>${Number(item.active_fact_count || 0)} ${esc(window.t("profile.factsShort"))}</span>${reviewCount ? `<span class="warning">${esc(window.t("profile.reviewShort", reviewCount))}</span>` : ""}${runningTasks ? `<span class="running">${esc(window.t("profile.tasksShort", runningTasks))}</span>` : ""}${item.has_gap ? `<span class="warning">${esc(window.t("profile.gapShort"))}</span>` : ""}</span>
      </button>`;
    }).join("");
  }

  async select(scopeUid) {
    this.stopTaskPolling();
    this.selectedScopeUid = scopeUid;
    this.detail = null;
    this.renderList();
    this.renderDetail(true);
    await this.loadDetail();
  }

  async loadDetail() {
    if (!this.selectedScopeUid) return;
    try {
      this.detail = await this.api.get("user-profiles/detail", {
        profile_scope_uid: this.selectedScopeUid,
      });
      this.renderDetail();
      this.resumeTaskPolling();
    } catch (error) {
      const root = document.getElementById("user-profile-detail");
      if (root) root.innerHTML = `<div class="identity-state error">${esc(error.message)}</div>`;
    }
  }

  renderDetail(loading = false) {
    const root = document.getElementById("user-profile-detail");
    if (!root) return;
    if (!this.selectedScopeUid) {
      root.innerHTML = `<div class="identity-state">${esc(window.t("profile.choose"))}</div>`;
      return;
    }
    if (loading || !this.detail) {
      root.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
      return;
    }
    const data = this.detail;
    const scope = data.scope || {};
    const accounts = data.accounts || [];
    const activeFacts = (data.facts || []).filter(item => item.status === "active");
    const pendingFacts = (data.facts || []).filter(item => item.status === "pending" || item.status === "conflict");
    const historicalFacts = (data.facts || []).filter(item => !["active", "pending", "conflict"].includes(item.status));
    const relationship = data.relationship;
    const preview = data.injection_preview;
    const gapCount = Number(data.gap?.pending_count || 0) + Number(data.gap?.resumable_count || 0);
    const hasGeneratedProfile = (data.facts || []).length > 0 || Boolean(relationship);
    root.innerHTML = `
      <div class="user-profile-detail-head">
        <div><h2>${esc(accounts[0]?.last_observed_name || accounts[0]?.actor_id || scope.logical_user_uid)}</h2><p>${esc(scope.bot_account)} / ${esc(scope.persona_id || "-")} · ${esc(scope.profile_scope_uid)}</p></div>
        <div class="user-profile-head-actions"><button class="btn btn-secondary btn-sm" type="button" data-profile-action="${scope.enabled ? "disable" : "enable"}">${esc(window.t(scope.enabled ? "profile.disable" : "profile.enable"))}</button></div>
      </div>
      ${hasGeneratedProfile ? "" : `<div class="user-profile-initial-state"><div><strong>${esc(window.t("profile.notGeneratedTitle"))}</strong><p>${esc(window.t("profile.notGeneratedDescription"))}</p></div><button class="btn btn-primary btn-sm" type="button" data-profile-action="rebuild">${esc(window.t("profile.buildFromHistory"))}</button></div>`}
      ${this.renderArchitectureStatus(data)}
      ${this.renderTaskMonitor(data.tasks || [], data.gap || {})}
      ${scope.has_gap || data.gap?.has_gap ? `<div class="user-profile-alert">${esc(window.t("profile.gap", gapCount))}</div>` : ""}
      ${this.renderMaintenanceActions(data)}
      <details class="user-profile-section user-profile-disclosure" open>
        <summary><span>${esc(window.t("profile.injectionPreview"))}</span><small>${esc(window.t("profile.injectionMeta", Number(preview?.total_chars || 0), Number(preview?.fact_count || 0), window.t(preview?.relationship_included ? "profile.included" : "profile.notIncluded")))}</small></summary>
        <pre class="user-profile-preview">${esc(preview?.content || window.t("profile.noInjection"))}</pre>
      </details>
      ${this.renderFactSection(window.t("profile.activeFacts"), activeFacts, true)}
      ${this.renderFactSection(window.t("profile.pendingConflicts"), pendingFacts, true)}
      ${this.renderConflicts(data.conflicts || [])}
      ${this.renderFactSection(window.t("profile.historyFacts"), historicalFacts, false)}
      ${this.renderIdentityReviews(data.identity_reviews || [], accounts)}
      ${this.renderRelationship(relationship, data.relationship_revisions || [], scope)}
      ${this.renderAccounts(accounts)}
      ${this.renderSharing(data.share_group, scope)}
      ${this.renderTasks(data.tasks || [])}
      ${this.renderDangerZone()}`;
  }

  async openBuildPanel() {
    document.getElementById("user-profile-build-panel")?.classList.remove("hidden");
    await this.loadBuildCandidates();
  }

  closeBuildPanel() {
    document.getElementById("user-profile-build-panel")?.classList.add("hidden");
  }

  async loadBuildCandidates() {
    const root = document.getElementById("user-profile-build-list");
    if (root) root.innerHTML = `<div class="identity-state compact">${esc(window.t("common.loading"))}</div>`;
    try {
      const data = await this.api.get("user-profiles/build-candidates", { limit: 200 });
      this.buildCandidates = data.items || [];
      this.renderBuildCandidates();
    } catch (error) {
      if (root) root.innerHTML = `<div class="identity-state compact error">${esc(error.message)}</div>`;
    }
  }

  renderBuildCandidates() {
    const root = document.getElementById("user-profile-build-list");
    if (!root || root.closest(".hidden")) return;
    if (!this.buildCandidates.length) {
      root.innerHTML = `<div class="identity-state compact">${esc(window.t("profile.noBuildCandidates"))}</div>`;
      return;
    }
    root.innerHTML = `<div class="user-profile-build-candidates">${this.buildCandidates.map((item, index) => `<div class="user-profile-build-candidate">
      <div><strong>${esc(item.display_name || item.actor_id)}</strong><small>${esc(item.platform || "")} · ${esc(item.stable_user_id || "")} · ${esc(item.bot_account || "-")} / ${esc(item.persona_id || "-")}</small><span>${esc(window.t("profile.buildCandidateMeta", Number(item.timeline_count || 0), window.t(`profile.buildBasis.${item.identity_basis}`)))}</span></div>
      <button class="btn btn-primary btn-sm" type="button" data-profile-build-candidate="${index}">${esc(window.t("profile.build"))}</button>
    </div>`).join("")}</div>`;
  }

  async buildCandidate(button) {
    if (this.operationInFlight) return;
    const item = this.buildCandidates[Number(button.dataset.profileBuildCandidate)];
    if (!item) return;
    const confirmed = await this.confirmDialog.show({
      title: window.t("profile.build"),
      message: window.t("profile.confirmBuild", item.display_name || item.actor_id, item.bot_account, item.persona_id || "-", Number(item.timeline_count || 0)),
      confirmLabel: window.t("profile.build"),
    });
    if (!confirmed) return;
    if (!this.beginOperation(window.t("profile.taskPreparingBuild"))) return;
    button.disabled = true;
    try {
      const result = await this.api.post("user-profiles/build", {
        actor_id: item.actor_id,
        bot_account: item.bot_account,
        persona_id: item.persona_id,
        candidate_fingerprint: item.candidate_fingerprint,
      });
      this.selectedScopeUid = result.profile_scope_uid || "";
      this.taskProgressBaseline = {
        scope: this.selectedScopeUid,
        total: Number(result.event_count || 0),
      };
      if (result.status === "no_history") this.taskProgressBaseline = null;
      this.detail = null;
      this.showToast(window.t(result.status === "no_history" ? "profile.noHistoryToBuild" : "profile.rebuildScheduled"));
      this.closeBuildPanel();
      await this.loadList();
      if (this.selectedScopeUid) await this.loadDetail();
    } catch (error) {
      button.disabled = false;
      this.showToast(error.message, true);
    } finally {
      this.endOperation();
    }
  }

  renderArchitectureStatus(data) {
    const scope = data.scope || {};
    const counts = (data.facts || []).reduce((result, fact) => {
      const status = String(fact.status || "unknown");
      result[status] = Number(result[status] || 0) + 1;
      return result;
    }, {});
    const terminal = new Set(["completed", "completed_partial", "failed", "cancelled"]);
    const running = (data.tasks || []).filter(task => !terminal.has(String(task.status || ""))).length;
    const relationship = data.relationship;
    const currentRelationshipRevision = (data.relationship_revisions || []).find(
      item => Number(item.revision || 0) === Number(relationship?.revision || 0),
    );
    const personaBased = currentRelationshipRevision?.diagnostics?.persona_basis === "current_config";
    const scopeStatus = scope.auto_enable_blocked ? "deleted" : scope.enabled ? "enabled" : "disabled";
    return `<div class="user-profile-state-grid" data-profile-architecture-status>
      <div class="user-profile-state-item"><span>${esc(window.t("profile.state.scope"))}</span><strong>${esc(window.t(`profile.status.${scopeStatus}`))}</strong><small>${esc(scope.bot_account || "-")} / ${esc(scope.persona_id || "-")}</small></div>
      <div class="user-profile-state-item"><span>${esc(window.t("profile.state.objective"))}</span><strong>r${Number(data.fact_revision || 0)} · ${Number(counts.active || 0)} ${esc(window.t("profile.factStatus.active"))}</strong><small>${esc(window.t("profile.state.reviewCounts", Number(counts.pending || 0), Number(counts.conflict || 0), Number(counts.stale || 0)))}</small></div>
      <div class="user-profile-state-item"><span>${esc(window.t("profile.state.relationship"))}</span><strong>${relationship ? `r${Number(relationship.revision || 0)}` : esc(window.t("common.noData"))}</strong><small>${esc(window.t(personaBased ? "profile.state.executionPersona" : "profile.state.noPersonaBasis"))}</small></div>
      <div class="user-profile-state-item"><span>${esc(window.t("profile.state.maintenance"))}</span><strong>${esc(window.t("profile.state.queueCount", running))}</strong><small>${esc(window.t("profile.state.gapCount", Number(data.gap?.pending_count || 0) + Number(data.gap?.resumable_count || 0)))}</small></div>
    </div>`;
  }

  renderMaintenanceActions(data) {
    const gap = data.gap || {};
    const resumable = Number(gap.resumable_count || 0);
    return `<section class="user-profile-operation-section">
      <h3>${esc(window.t("profile.maintenanceActions"))}</h3>
      <div class="maintenance-topic-action-list">
        ${resumable ? `<div class="maintenance-topic-action-row">
          <div><strong>${esc(window.t("profile.continueGap"))}</strong><p>${esc(window.t("profile.continueGapDescription", resumable))}</p></div>
          <button class="btn btn-primary" type="button" data-profile-continue-gap>${esc(window.t("profile.continueGap"))}</button>
        </div>` : ""}
        <div class="maintenance-topic-action-row">
          <div><strong>${esc(window.t("profile.rebuild"))}</strong><p>${esc(window.t("profile.rebuildDescription", Number(gap.pending_count || 0)))}</p><label class="user-profile-rebuild-option"><input id="profile-rebuild-clear-overrides" type="checkbox"> <span>${esc(window.t("profile.clearOverrides"))}</span></label></div>
          <button class="btn btn-primary" type="button" data-profile-action="rebuild">${esc(window.t("profile.rebuild"))}</button>
        </div>
      </div>
    </section>`;
  }

  renderDangerZone() {
    return `<details class="user-profile-section user-profile-disclosure user-profile-danger-zone">
      <summary><span>${esc(window.t("profile.dangerZone"))}</span><small>${esc(window.t("profile.dangerZoneDescription"))}</small></summary>
      <div class="maintenance-topic-action-list">
        <div class="maintenance-topic-action-row maintenance-topic-action-danger"><div><strong>${esc(window.t("profile.reset"))}</strong><p>${esc(window.t("profile.resetDescription"))}</p></div><button class="btn btn-danger" type="button" data-profile-action="reset">${esc(window.t("profile.reset"))}</button></div>
        <div class="maintenance-topic-action-row maintenance-topic-action-danger"><div><strong>${esc(window.t("profile.deleteDisable"))}</strong><p>${esc(window.t("profile.deleteDisableDescription"))}</p></div><button class="btn btn-danger" type="button" data-profile-action="delete-disable">${esc(window.t("profile.deleteDisable"))}</button></div>
      </div>
    </details>`;
  }

  isTaskTerminal(task) {
    return ["completed", "completed_partial", "failed", "cancelled"].includes(String(task?.status || ""));
  }

  taskProgress(task) {
    if (task?.progress_percent !== undefined && task?.progress_percent !== null) {
      const supplied = Number(task.progress_percent);
      if (Number.isFinite(supplied)) return Math.max(0, Math.min(100, supplied));
    }
    const status = String(task?.status || "pending");
    return ({ pending: 5, running_facts: 20, facts_completed: 40, facts_failed: 40, running_behavior: 55, running_relationship: 75, completed: 100, completed_partial: 100, failed: 100, cancelled: 100 })[status] ?? 0;
  }

  taskStageLabel(task) {
    if (task?.status === "facts_completed" && task?.result_summary?.relationship_error) {
      return window.t("profile.taskStatus.relationship_failed");
    }
    const key = `profile.taskStatus.${String(task?.status || "pending")}`;
    return window.t(key);
  }

  taskBatchMeta(task) {
    const timelines = Number(task?.batch_timeline_count || task?.total_count || task?.items?.length || 0);
    const candidates = Number(task?.batch_candidate_count || 0);
    const chars = Number(task?.batch_prompt_estimate_chars || 0);
    if (!timelines && !candidates && !chars) return "";
    return window.t("profile.taskBatchMeta", timelines, candidates, chars.toLocaleString());
  }

  taskElapsed(task) {
    const stored = Number(task?.request_elapsed_seconds || task?.relationship_elapsed_seconds || task?.facts_elapsed_seconds || 0);
    const running = ["running_facts", "running_behavior", "running_relationship"].includes(String(task?.status || ""));
    const live = running && Number(task?.updated_at || 0) > 0 ? Math.max(0, Date.now() / 1000 - Number(task.updated_at)) : 0;
    const seconds = Math.round(Math.max(stored, live));
    if (!seconds) return "";
    const duration = seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
    return window.t("profile.taskElapsed", duration);
  }

  renderTaskMonitor(tasks, gap) {
    const active = tasks.find(task => !this.isTaskTerminal(task));
    const blocked = tasks.find(task => String(task?.status || "") === "failed");
    const pendingCount = Number(gap?.pending_count || 0);
    const trackingQueue = this.taskProgressBaseline?.scope === this.selectedScopeUid;
    const visible = Boolean(this.operationLabel || active || blocked || trackingQueue);
    if (!visible) return `<div id="user-profile-task-monitor" class="user-profile-task-monitor hidden" aria-live="polite"></div>`;
    const displayed = active || blocked;
    let progress = displayed ? this.taskProgress(displayed) : 5;
    let label = displayed ? this.taskStageLabel(displayed) : window.t("profile.taskStatus.pending");
    let meta = displayed
      ? window.t("profile.taskProgress", Math.round(progress), Number(displayed.completed_stage_count || 0), Number(displayed.total_stage_count || 3), Number(displayed.total_count || displayed.items?.length || 0))
      : window.t("profile.taskWaiting", pendingCount);
    const details = displayed ? [this.taskBatchMeta(displayed), this.taskElapsed(displayed)].filter(Boolean).join(" · ") : "";
    let indeterminate = false;
    if (trackingQueue && this.taskProgressBaseline.total > 0 && !blocked) {
      const completed = Math.max(0, this.taskProgressBaseline.total - pendingCount);
      const activeContribution = active
        ? Number(active.total_count || active.items?.length || 0) * this.taskProgress(active) / 100
        : 0;
      const processed = Math.min(this.taskProgressBaseline.total, completed + activeContribution);
      const remaining = Math.max(0, Math.ceil(this.taskProgressBaseline.total - processed));
      progress = Math.max(0, Math.min(100, processed / this.taskProgressBaseline.total * 100));
      meta = window.t("profile.taskOverallProgress", Math.round(progress), Math.floor(processed), this.taskProgressBaseline.total, remaining);
    }
    if (this.operationLabel) {
      label = this.operationLabel;
      meta = window.t("profile.taskPreparing");
      indeterminate = true;
    }
    const error = displayed?.error ? `<div class="user-profile-task-error">${esc(displayed.error)}</div>` : "";
    return `<div id="user-profile-task-monitor" class="user-profile-task-monitor${indeterminate ? " is-indeterminate" : ""}" aria-live="polite">
      <div class="user-profile-task-monitor-head"><div><strong>${esc(label)}</strong><small>${esc(meta)}</small>${details ? `<small>${esc(details)}</small>` : ""}</div><span>${indeterminate ? "" : `${Math.round(progress)}%`}</span></div>
      <div class="topic-progress-track"><span style="width:${indeterminate ? 35 : progress}%"></span></div>${error}
    </div>`;
  }

  updateTaskRegions() {
    if (!this.detail) return;
    const monitor = document.getElementById("user-profile-task-monitor");
    if (monitor) monitor.outerHTML = this.renderTaskMonitor(this.detail.tasks || [], this.detail.gap || {});
    const history = document.getElementById("user-profile-task-history");
    if (history) history.innerHTML = this.renderTaskRows(this.detail.tasks || []);
    const architecture = document.querySelector?.("[data-profile-architecture-status]");
    if (architecture) architecture.outerHTML = this.renderArchitectureStatus(this.detail);
  }

  beginOperation(label) {
    if (this.operationInFlight) return false;
    this.operationInFlight = true;
    this.operationLabel = label;
    this.updateTaskRegions();
    return true;
  }

  endOperation() {
    this.operationInFlight = false;
    this.operationLabel = "";
    this.updateTaskRegions();
  }

  stopTaskPolling() {
    clearTimeout(this.taskPollTimer);
    this.taskPollTimer = null;
    this.taskPollGeneration += 1;
  }

  resumeTaskPolling() {
    if (!this.isActive || !this.selectedScopeUid || !this.detail) return;
    const tasks = this.detail.tasks || [];
    const activeTask = tasks.find(task => !this.isTaskTerminal(task));
    const blockedTask = tasks.find(task => String(task?.status || "") === "failed");
    const pendingCount = Number(this.detail.gap?.pending_count || 0);
    if (activeTask && pendingCount > 0 && this.taskProgressBaseline?.scope !== this.selectedScopeUid) {
      this.taskProgressBaseline = { scope: this.selectedScopeUid, total: pendingCount };
      this.updateTaskRegions();
    }
    const tracksStartedQueue = !blockedTask && this.detail.scope?.enabled !== false
      && this.taskProgressBaseline?.scope === this.selectedScopeUid
      && pendingCount > 0;
    const hasWork = Boolean(activeTask) || tracksStartedQueue;
    if (!hasWork || this.taskPollTimer || this.taskPollInFlight) return;
    const generation = this.taskPollGeneration;
    this.taskPollTimer = setTimeout(() => this.pollTasks(generation), 400);
  }

  async pollTasks(generation = this.taskPollGeneration) {
    if (generation !== this.taskPollGeneration || this.taskPollInFlight || !this.selectedScopeUid) return;
    this.taskPollTimer = null;
    this.taskPollInFlight = true;
    const scopeUid = this.selectedScopeUid;
    try {
      const data = await this.api.get("user-profiles/tasks", { profile_scope_uid: scopeUid });
      if (generation !== this.taskPollGeneration || scopeUid !== this.selectedScopeUid || !this.detail) return;
      this.detail.tasks = data.items || [];
      if (data.gap) this.detail.gap = data.gap;
      this.updateTaskRegions();
      const hasActiveTask = this.detail.tasks.some(task => !this.isTaskTerminal(task));
      const activeTask = this.detail.tasks.find(task => !this.isTaskTerminal(task));
      const blockedTask = this.detail.tasks.find(task => String(task?.status || "") === "failed");
      const pendingCount = Number(this.detail.gap?.pending_count || 0);
      const wasTrackingQueue = this.taskProgressBaseline?.scope === scopeUid;
      const tracksStartedQueue = !blockedTask && this.detail.scope?.enabled !== false
        && wasTrackingQueue
        && pendingCount > 0;
      if (hasActiveTask || tracksStartedQueue) {
        let delay = 1200;
        const retryAt = Number(activeTask?.next_retry_at || 0) * 1000;
        if (retryAt > Date.now()) delay = Math.max(delay, Math.min(30000, retryAt - Date.now() + 250));
        this.taskPollTimer = setTimeout(() => this.pollTasks(generation), delay);
        return;
      }
      this.taskProgressBaseline = null;
      this.stopTaskPolling();
      if (blockedTask) {
        this.updateTaskRegions();
        return;
      }
      if (wasTrackingQueue && pendingCount > 0) {
        this.updateTaskRegions();
        return;
      }
      await this.loadList();
      if (this.selectedScopeUid === scopeUid) await this.loadDetail();
      this.showToast(window.t("profile.taskCompleted"));
    } catch (error) {
      if (generation === this.taskPollGeneration && scopeUid === this.selectedScopeUid) {
        this.taskPollTimer = setTimeout(() => this.pollTasks(generation), 3000);
      }
    } finally {
      this.taskPollInFlight = false;
      if (generation !== this.taskPollGeneration) this.resumeTaskPolling();
    }
  }

  renderFactSection(title, facts, actionable) {
    const rows = facts.length ? facts.map(fact => {
      const source = fact.sources?.[0];
      const canGovern = actionable || fact.status === "excluded";
      const controls = canGovern ? `<div class="user-profile-fact-actions">
        ${fact.status === "pending" ? `<button class="btn btn-secondary btn-sm" data-fact-action="confirm" data-fact-uid="${esc(fact.profile_fact_uid)}">${esc(window.t("profile.confirmFact"))}</button>` : ""}
        ${fact.status === "excluded" ? `<button class="btn btn-secondary btn-sm" data-fact-action="resume" data-fact-uid="${esc(fact.profile_fact_uid)}">${esc(window.t("profile.resumeFact"))}</button>` : `<button class="btn btn-secondary btn-sm" data-fact-action="pause" data-fact-uid="${esc(fact.profile_fact_uid)}">${esc(window.t("profile.pauseFact"))}</button>`}
        ${fact.status !== "excluded" ? `<button class="btn btn-secondary btn-sm" data-fact-action="${fact.pinned ? "unpin" : "pin"}" data-fact-uid="${esc(fact.profile_fact_uid)}">${esc(window.t(fact.pinned ? "profile.unpinFact" : "profile.pinFact"))}</button>` : ""}
        ${fact.status !== "excluded" ? `<button class="btn btn-danger btn-sm" data-fact-action="exclude" data-fact-uid="${esc(fact.profile_fact_uid)}">${esc(window.t("profile.excludeFact"))}</button>` : ""}
      </div>` : "";
      return `<div class="user-profile-fact">
        <div class="user-profile-fact-main"><span class="status-badge">${esc(window.t(`profile.category.${fact.category}`))}</span><strong>${esc(fact.display_text || fact.raw_fact || "")}</strong><small>${esc(window.t(`profile.factStatus.${fact.status}`))} · ${Math.round(Number(fact.confidence || 0) * 100)}% · ${Math.round(Number(fact.importance || 0) * 100)}%${fact.inference_kind === "behavioral_inference" ? ` · ${esc(window.t("profile.derivedPattern"))} · ${Number(fact.sources?.length || 0)} ${esc(window.t("profile.sourcesShort"))}` : ""}</small></div>
        ${source ? `<a class="user-profile-source" href="#" data-open-timeline="${esc(source.timeline_uid)}">${esc(source.timeline_uid)} r${Number(source.timeline_revision || 1)}</a>` : ""}
        ${controls}
      </div>`;
    }).join("") : `<div class="identity-state compact">${esc(window.t("common.noData"))}</div>`;
    return `<details class="user-profile-section user-profile-disclosure"${actionable && facts.length ? " open" : ""}><summary><span>${esc(title)}</span><small>${facts.length}</small></summary><div class="user-profile-facts">${rows}</div></details>`;
  }

  renderConflicts(conflicts) {
    const open = conflicts.filter(item => item.status === "open");
    if (!open.length) return "";
    const facts = new Map((this.detail.facts || []).map(item => [item.profile_fact_uid, item]));
    return `<details class="user-profile-section user-profile-disclosure user-profile-conflict-section" open><summary><span>${esc(window.t("profile.conflicts"))}</span><small>${open.length}</small></summary>${open.map(conflict => `<div class="user-profile-conflict">
      <strong>${esc(conflict.conflict_key || conflict.conflict_uid)}</strong>
      <p>${esc(conflict.resolution_reason || "")}</p>
      <div class="user-profile-conflict-options">${(conflict.fact_uids || []).map(uid => `<button class="btn btn-secondary btn-sm" data-conflict-action="select" data-conflict-uid="${esc(conflict.conflict_uid)}" data-fact-uid="${esc(uid)}">${esc(facts.get(uid)?.display_text || facts.get(uid)?.raw_fact || uid)}</button>`).join("")}</div>
      <div class="user-profile-fact-actions"><button class="btn btn-secondary btn-sm" data-conflict-action="pause" data-conflict-uid="${esc(conflict.conflict_uid)}">${esc(window.t("profile.keepPaused"))}</button><button class="btn btn-danger btn-sm" data-conflict-action="exclude" data-conflict-uid="${esc(conflict.conflict_uid)}">${esc(window.t("profile.excludeConflict"))}</button></div>
    </div>`).join("")}</details>`;
  }

  renderRelationship(relationship, revisions, scope) {
    const dimensions = DIMENSIONS.map(key => `<label class="relationship-dimension"><span>${esc(window.t(`profile.dimension.${key}`))}</span><input type="range" min="0" max="100" value="${Math.round(Number(relationship?.[key] || 0) * 100)}" data-relationship-dimension="${key}"><output>${Math.round(Number(relationship?.[key] || 0) * 100)}</output></label>`).join("");
    const revisionRows = revisions.slice(0, 12).map(item => {
      const personaBasis = item.diagnostics?.persona_basis === "current_config" ? ` · ${window.t("profile.state.executionPersona")}` : "";
      return `<div class="relationship-revision"><span>r${Number(item.revision)} · ${esc(item.operation)}${esc(personaBasis)} · ${this.formatTime(item.created_at)}</span>${item.full_snapshot ? `<button class="btn btn-secondary btn-sm" data-relationship-rollback="${Number(item.revision)}">${esc(window.t("profile.rollback"))}</button>` : ""}</div>`;
    }).join("");
    return `<details class="user-profile-section user-profile-disclosure user-profile-relationship" open><summary><span>${esc(window.t("profile.relationship"))}</span><small>r${Number(relationship?.revision || 0)}</small></summary><div class="user-profile-section-head user-profile-section-actions"><span></span><button class="btn btn-secondary btn-sm" data-relationship-action="freeze">${esc(window.t(scope.relationship_frozen ? "profile.unfreeze" : "profile.freeze"))}</button></div>
      <div class="relationship-dimensions">${dimensions}</div>
      <div class="relationship-fields"><label class="relationship-field"><span>${esc(window.t("profile.stanceTags"))}</span><input class="input" id="relationship-tags" value="${esc((relationship?.stance_tags || []).join(", "))}"></label>
      <label class="relationship-field"><span>${esc(window.t("profile.subjectiveSummary"))}</span><textarea class="input" id="relationship-summary" rows="4">${esc(relationship?.subjective_summary || "")}</textarea></label></div>
      <div class="relationship-overrides"><label class="relationship-field"><span>${esc(window.t("profile.sensitivity"))}</span><select class="select input" id="relationship-sensitivity"><option value="">${esc(window.t("profile.useGlobal"))}</option>${["very_slow", "slow", "balanced", "fast", "very_fast"].map(value => `<option value="${value}"${scope.relationship_sensitivity_override === value ? " selected" : ""}>${esc(window.t(`profile.sensitivityOption.${value}`))}</option>`).join("")}</select></label><label class="relationship-field"><span>${esc(window.t("profile.behaviorMode"))}</span><select class="select input" id="relationship-behavior"><option value="">${esc(window.t("profile.useGlobal"))}</option>${["restrained", "natural", "high_autonomy", "unrestricted"].map(value => `<option value="${value}"${scope.relationship_behavior_override === value ? " selected" : ""}>${esc(window.t(`profile.behaviorOption.${value}`))}</option>`).join("")}</select></label></div>
      <div class="user-profile-fact-actions relationship-actions"><button class="btn btn-primary btn-sm" data-relationship-action="save">${esc(window.t("common.save"))}</button><button class="btn btn-danger btn-sm" data-relationship-action="reset">${esc(window.t("profile.resetRelationship"))}</button><button class="btn btn-secondary btn-sm" data-relationship-action="rebuild">${esc(window.t("profile.rebuildRelationship"))}</button></div>
      <div class="relationship-revisions">${revisionRows || `<div class="identity-state compact">${esc(window.t("common.noData"))}</div>`}</div>
    </details>`;
  }

  renderIdentityReviews(items, accounts) {
    const pendingCount = items.filter(item => item.status === "pending_review").length;
    const rows = items.map((item, index) => {
      const pending = item.status === "pending_review";
      const reason = item.evidence?.decision_reason || item.identity_basis || "";
      const options = accounts.map(account => `<option value="${esc(account.actor_id)}">${esc(account.actor_id)}</option>`).join("");
      return `<div class="user-profile-identity-review" data-identity-review-row>
        <div><a href="#" data-open-timeline="${esc(item.timeline_uid)}"><strong>${esc(item.timeline_uid)}</strong></a><small>r${Number(item.timeline_revision || 1)} · ${esc(reason)} · ${this.formatTime(item.updated_at)}</small></div>
        <span class="status-badge ${pending ? "warning" : ""}">${esc(window.t(`profile.identityStatus.${item.status}`))}</span>
        <div class="user-profile-fact-actions">
          ${pending ? `<select class="select input" data-identity-actor="${index}">${options}</select><button class="btn btn-primary btn-sm" data-identity-review-action="bind" data-timeline-uid="${esc(item.timeline_uid)}" data-timeline-revision="${Number(item.timeline_revision || 1)}" data-memory-space-id="${esc(item.memory_space_id)}" data-evidence-fingerprint="${esc(item.evidence_fingerprint)}">${esc(window.t("profile.bindTimeline"))}</button><button class="btn btn-secondary btn-sm" data-identity-review-action="ignore" data-timeline-uid="${esc(item.timeline_uid)}" data-timeline-revision="${Number(item.timeline_revision || 1)}" data-memory-space-id="${esc(item.memory_space_id)}" data-evidence-fingerprint="${esc(item.evidence_fingerprint)}">${esc(window.t("profile.ignoreTimeline"))}</button>` : `<button class="btn btn-secondary btn-sm" data-identity-review-action="restore" data-timeline-uid="${esc(item.timeline_uid)}" data-timeline-revision="${Number(item.timeline_revision || 1)}" data-memory-space-id="${esc(item.memory_space_id)}" data-evidence-fingerprint="${esc(item.evidence_fingerprint)}">${esc(window.t("profile.restoreTimelineReview"))}</button>`}
        </div>
      </div>`;
    }).join("");
    return `<details class="user-profile-section user-profile-disclosure"${pendingCount ? " open" : ""}><summary><span>${esc(window.t("profile.identityReviews"))}</span><small>${esc(window.t("profile.identityReviewSummary", pendingCount, items.length))}</small></summary><div class="user-profile-section-head user-profile-section-actions"><span></span><button class="btn btn-secondary btn-sm" data-identity-review-scan>${esc(window.t("profile.scanIdentityReviews"))}</button></div>${rows || `<div class="identity-state compact">${esc(window.t("profile.noIdentityReviews"))}</div>`}</details>`;
  }

  renderAccounts(accounts) {
    const rows = accounts.map(account => `<div class="user-profile-account"><div><strong>${esc(account.actor_id)}</strong><small>${esc((account.observed_names || []).join(" · "))}</small></div>${accounts.length > 1 ? `<button class="btn btn-secondary btn-sm" data-account-unbind="${esc(account.actor_id)}">${esc(window.t("profile.unbind"))}</button>` : ""}</div>`).join("");
    return `<details class="user-profile-section user-profile-disclosure"><summary><span>${esc(window.t("profile.accounts"))}</span><small>${accounts.length}</small></summary>${rows}<div class="user-profile-inline-form"><input class="input" id="profile-bind-target" placeholder="${esc(window.t("profile.bindTargetPlaceholder"))}"><input class="input" id="profile-bind-actors" placeholder="${esc(window.t("profile.bindActorsPlaceholder"))}"><button class="btn btn-secondary btn-sm" data-account-action="bind">${esc(window.t("profile.bind"))}</button></div></details>`;
  }

  renderSharing(group, scope) {
    const members = group?.members || [];
    return `<details class="user-profile-section user-profile-disclosure"><summary><span>${esc(window.t("profile.sharing"))}</span><small>${group ? esc(group.name) : esc(window.t("profile.notShared"))}</small></summary>${group ? `<p>${esc(group.name)} · ${members.map(item => `${item.bot_account}/${item.persona_id}`).join(", ")}</p>` : `<p class="text-secondary">${esc(window.t("profile.notShared"))}</p>`}<div class="user-profile-inline-form"><input class="input" id="profile-share-name" value="${esc(group?.name || "")}" placeholder="${esc(window.t("profile.shareName"))}"><input class="input" id="profile-share-scopes" value="${esc((members.length ? members.map(item => item.profile_scope_uid) : [scope.profile_scope_uid]).join(", "))}" placeholder="${esc(window.t("profile.shareScopesPlaceholder"))}"><button class="btn btn-secondary btn-sm" data-share-action="save">${esc(window.t("profile.saveSharing"))}</button></div></details>`;
  }

  renderTasks(tasks) {
    const active = tasks.filter(task => !this.isTaskTerminal(task)).length;
    const completed = tasks.filter(task => ["completed", "completed_partial"].includes(String(task.status || ""))).length;
    return `<details class="user-profile-section user-profile-disclosure"${active ? " open" : ""}><summary><span>${esc(window.t("profile.tasks"))}</span><small>${esc(window.t("profile.taskSummary", active, tasks.length))}</small></summary>${completed ? `<div class="user-profile-section-head user-profile-section-actions"><span></span><button class="btn btn-secondary btn-sm" data-task-clear-completed>${esc(window.t("profile.clearCompleted"))}</button></div>` : ""}<div id="user-profile-task-history">${this.renderTaskRows(tasks)}</div></details>`;
  }

  renderTaskRows(tasks) {
    if (!tasks.length) return `<div class="identity-state compact">${esc(window.t("common.noData"))}</div>`;
    return tasks.map(task => {
      const progress = this.taskProgress(task);
      const retryable = ["failed", "facts_failed"].includes(String(task.status || ""))
        || (task.status === "facts_completed" && Boolean(task.error));
      const cancellable = !["completed", "completed_partial", "cancelled"].includes(String(task.status || ""));
      const deletable = ["completed", "completed_partial"].includes(String(task.status || ""));
      const total = Number(task.total_count || task.items?.length || 0);
      const batch = this.taskBatchMeta(task);
      const elapsed = this.taskElapsed(task);
      const retryMeta = task.automatic_retry_pending ? window.t("profile.taskRetryMeta", Number(task.retries || 0), Number(task.retry_limit || 0)) : "";
      const diagnostics = [batch, elapsed, retryMeta].filter(Boolean).join(" · ");
      const actions = `${retryable ? `<button class="btn btn-secondary btn-sm" data-task-retry="${esc(task.task_uid)}">${esc(window.t("profile.retry"))}</button>` : ""}${cancellable ? `<button class="btn btn-secondary btn-sm" data-task-cancel="${esc(task.task_uid)}">${esc(window.t("common.cancel"))}</button>` : ""}${deletable ? `<button class="btn btn-ghost btn-sm" data-task-delete="${esc(task.task_uid)}">${esc(window.t("common.delete"))}</button>` : ""}`;
      return `<div class="user-profile-task"><div class="user-profile-task-main"><span><strong>${esc(window.t("profile.maintenanceTask"))} ${esc(String(task.task_uid || "").slice(0, 8))}</strong><span class="status-badge status-${esc(String(task.status || "pending"))}">${esc(this.taskStageLabel(task))}</span></span><small>${esc(window.t("profile.taskRowMeta", Math.round(progress), total, Number(task.retries || 0), this.formatTime(task.updated_at)))}</small>${diagnostics ? `<small>${esc(diagnostics)}</small>` : ""}<span class="maintenance-task-progress" aria-label="${Math.round(progress)}%"><span style="width:${progress}%"></span></span>${task.error ? `<small class="user-profile-task-error">${esc(task.error)}</small>` : ""}</div>${actions ? `<div class="user-profile-task-actions">${actions}</div>` : ""}</div>`;
    }).join("");
  }

  async handleDetailClick(event) {
    const source = event.target.closest("[data-open-timeline]");
    if (source) {
      event.preventDefault();
      window.dispatchEvent(new CustomEvent("livingmemory:open-timeline-source", { detail: { timelineUid: source.dataset.openTimeline } }));
      return;
    }
    const action = event.target.closest("[data-profile-action]");
    if (action) return this.profileAction(action.dataset.profileAction, action);
    const fact = event.target.closest("[data-fact-action]");
    if (fact) return this.factAction(fact.dataset.factUid, fact.dataset.factAction, fact);
    const conflict = event.target.closest("[data-conflict-action]");
    if (conflict) return this.conflictAction(conflict.dataset.conflictUid, conflict.dataset.conflictAction, conflict.dataset.factUid || null, conflict);
    const relationship = event.target.closest("[data-relationship-action]");
    if (relationship) return this.relationshipAction(relationship.dataset.relationshipAction, relationship);
    const rollback = event.target.closest("[data-relationship-rollback]");
    if (rollback) return this.relationshipRollback(Number(rollback.dataset.relationshipRollback), rollback);
    const unbind = event.target.closest("[data-account-unbind]");
    if (unbind) return this.unbindAccount(unbind.dataset.accountUnbind, unbind);
    const bind = event.target.closest('[data-account-action="bind"]');
    if (bind) return this.bindAccounts(bind);
    const share = event.target.closest('[data-share-action="save"]');
    if (share) return this.saveSharing(share);
    const scan = event.target.closest("[data-identity-review-scan]");
    if (scan) return this.scanIdentityReviews(scan);
    const identityReview = event.target.closest("[data-identity-review-action]");
    if (identityReview) return this.identityReviewAction(identityReview);
    const retry = event.target.closest("[data-task-retry]");
    if (retry) return this.retryTask(retry.dataset.taskRetry, retry);
    const cancel = event.target.closest("[data-task-cancel]");
    if (cancel) return this.cancelTask(cancel.dataset.taskCancel, cancel);
    const remove = event.target.closest("[data-task-delete]");
    if (remove) return this.deleteTask(remove.dataset.taskDelete, remove);
    const clearCompleted = event.target.closest("[data-task-clear-completed]");
    if (clearCompleted) return this.clearCompletedTasks(clearCompleted);
    const continueGap = event.target.closest("[data-profile-continue-gap]");
    if (continueGap) return this.continueGap(continueGap);
  }

  async profileAction(action, button = null) {
    if (this.operationInFlight) return;
    const body = { profile_scope_uid: this.selectedScopeUid };
    if (["reset", "delete-disable"].includes(action)) {
      const confirmed = await this.confirmDialog.show({ title: window.t(`profile.${action === "reset" ? "reset" : "deleteDisable"}`), message: window.t(`profile.confirm.${action}`), confirmLabel: window.t("common.confirm") });
      if (!confirmed) return;
      body.fingerprint = this.detail.fingerprint;
    }
    if (action !== "rebuild" && !this.beginOperation(window.t("profile.taskApplyingOperation"))) return;
    if (button) button.disabled = true;
    try {
      if (action === "rebuild") return this.rebuildProfile();
      await this.api.post(`user-profiles/${action}`, body);
      this.showToast(window.t("profile.operationDone"));
      await this.refresh();
      if (action === "enable" && this.detail?.gap?.has_gap) await this.offerRebuild();
    } catch (error) {
      this.showToast(error.message, true);
    } finally {
      if (button) button.disabled = false;
      if (action !== "rebuild") this.endOperation();
    }
  }

  async offerRebuild() {
    const confirmed = await this.confirmDialog.show({ title: window.t("profile.gapTitle"), message: window.t("profile.gapRebuildPrompt"), confirmLabel: window.t("profile.rebuild"), danger: false });
    if (confirmed) await this.rebuildProfile();
  }

  async rebuildProfile() {
    if (this.operationInFlight) return;
    let started = false;
    try {
      const preview = await this.api.post("user-profiles/rebuild/preview", { profile_scope_uid: this.selectedScopeUid });
      const clear = document.getElementById("profile-rebuild-clear-overrides")?.checked || false;
      const confirmed = await this.confirmDialog.show({
        title: window.t("profile.rebuild"),
        message: window.t(
          "profile.rebuildImpact",
          preview.timeline_count,
          preview.missing_timeline_count,
          preview.ambiguous_identity_count,
          preview.fact_count,
          preview.override_count,
        ),
        confirmLabel: window.t("profile.rebuild"),
      });
      if (!confirmed) return;
      started = this.beginOperation(window.t("profile.taskPreparingRebuild"));
      if (!started) return;
      const result = await this.api.post("user-profiles/rebuild/start", { profile_scope_uid: this.selectedScopeUid, fingerprint: preview.fingerprint, history_fingerprint: preview.history_fingerprint, clear_overrides: clear });
      this.taskProgressBaseline = {
        scope: this.selectedScopeUid,
        total: Number(result.event_count || 0),
      };
      if (result.status === "no_history") this.taskProgressBaseline = null;
      this.showToast(window.t(result.status === "no_history" ? "profile.noHistoryToBuild" : "profile.rebuildScheduled"));
      await this.loadDetail();
      this.resumeTaskPolling();
    } catch (error) {
      this.showToast(error.message, true);
    } finally {
      if (started) this.endOperation();
    }
  }

  async factAction(uid, action, button = null) {
    if (!this.beginOperation(window.t("profile.taskApplyingOperation"))) return;
    if (button) button.disabled = true;
    try {
      await this.api.post("user-profiles/facts/action", { profile_scope_uid: this.selectedScopeUid, profile_fact_uid: uid, action, expected_revision: this.detail.fact_revision });
      await this.loadDetail();
    } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; this.endOperation(); }
  }

  async conflictAction(uid, resolution, factUid, button = null) {
    if (!this.beginOperation(window.t("profile.taskApplyingOperation"))) return;
    if (button) button.disabled = true;
    try {
      await this.api.post("user-profiles/conflicts/resolve", { profile_scope_uid: this.selectedScopeUid, conflict_uid: uid, resolution, selected_fact_uid: factUid, expected_revision: this.detail.fact_revision });
      await this.loadDetail();
    } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; this.endOperation(); }
  }

  async relationshipAction(action, button = null) {
    if (this.operationInFlight) return;
    let started = false;
    try {
      if (!["save", "freeze"].includes(action)) {
        const confirmed = await this.confirmDialog.show({ title: window.t(`profile.${action === "reset" ? "resetRelationship" : "rebuildRelationship"}`), message: window.t(`profile.confirm.relationship-${action}`), confirmLabel: window.t("common.confirm") });
        if (!confirmed) return;
      }
      started = this.beginOperation(window.t(action === "rebuild" ? "profile.taskRebuildingRelationship" : "profile.taskApplyingOperation"));
      if (!started) return;
      if (button) button.disabled = true;
      if (action === "save") {
        const changes = {};
        document.querySelectorAll("[data-relationship-dimension]").forEach(input => { changes[input.dataset.relationshipDimension] = Number(input.value); });
        changes.stance_tags = (document.getElementById("relationship-tags")?.value || "").split(",").map(value => value.trim()).filter(Boolean);
        changes.subjective_summary = document.getElementById("relationship-summary")?.value || "";
        await this.api.post("user-profiles/relationship/update", { profile_scope_uid: this.selectedScopeUid, expected_revision: this.detail.relationship?.revision || 0, changes, sensitivity_override: document.getElementById("relationship-sensitivity")?.value || null, behavior_override: document.getElementById("relationship-behavior")?.value || null });
      } else if (action === "freeze") {
        await this.api.post("user-profiles/relationship/freeze", { profile_scope_uid: this.selectedScopeUid, frozen: !this.detail.scope.relationship_frozen });
      } else {
        await this.api.post(`user-profiles/relationship/${action}`, { profile_scope_uid: this.selectedScopeUid, fingerprint: this.detail.fingerprint, use_all_history: true });
      }
      await this.loadDetail();
    } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; if (started) this.endOperation(); }
  }

  async relationshipRollback(revision, button = null) {
    if (this.operationInFlight) return;
    const confirmed = await this.confirmDialog.show({ title: window.t("profile.rollback"), message: window.t("profile.confirm.rollback", revision), confirmLabel: window.t("profile.rollback") });
    if (!confirmed) return;
    if (!this.beginOperation(window.t("profile.taskApplyingOperation"))) return;
    if (button) button.disabled = true;
    try { await this.api.post("user-profiles/relationship/rollback", { profile_scope_uid: this.selectedScopeUid, revision }); await this.loadDetail(); } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; this.endOperation(); }
  }

  async bindAccounts(button = null) {
    if (this.operationInFlight) return;
    const target_actor_id = document.getElementById("profile-bind-target")?.value.trim() || "";
    const actor_ids = (document.getElementById("profile-bind-actors")?.value || "").split(",").map(value => value.trim()).filter(Boolean);
    let started = false;
    try {
      const preview = await this.api.post("user-profiles/accounts/bind/preview", { target_actor_id, actor_ids });
      if (preview.blocked_reason) throw new Error(preview.blocked_reason);
      const confirmed = await this.confirmDialog.show({ title: window.t("profile.bind"), message: window.t("profile.bindImpact", preview.moved_account_count, preview.fact_count, preview.scope_collisions.length), confirmLabel: window.t("profile.bind") });
      if (!confirmed) return;
      started = this.beginOperation(window.t("profile.taskApplyingOperation"));
      if (!started) return;
      if (button) button.disabled = true;
      await this.api.post("user-profiles/accounts/bind", { target_actor_id, actor_ids, fingerprint: preview.fingerprint });
      await this.refresh();
      this.trackCurrentQueue();
    } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; if (started) this.endOperation(); }
  }

  async unbindAccount(actor_id, button = null) {
    if (this.operationInFlight) return;
    let started = false;
    try {
      const preview = await this.api.post("user-profiles/accounts/unbind/preview", { actor_id });
      if (preview.blocked_reason) throw new Error(preview.blocked_reason);
      const confirmed = await this.confirmDialog.show({ title: window.t("profile.unbind"), message: window.t("profile.unbindImpact", preview.source_count, preview.new_scope_count), confirmLabel: window.t("profile.unbind") });
      if (!confirmed) return;
      started = this.beginOperation(window.t("profile.taskApplyingOperation"));
      if (!started) return;
      if (button) button.disabled = true;
      await this.api.post("user-profiles/accounts/unbind", { actor_id, fingerprint: preview.fingerprint });
      await this.refresh();
      this.trackCurrentQueue();
    } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; if (started) this.endOperation(); }
  }

  async saveSharing(button = null) {
    if (this.operationInFlight) return;
    const name = document.getElementById("profile-share-name")?.value.trim() || "";
    const profile_scope_uids = (document.getElementById("profile-share-scopes")?.value || "").split(",").map(value => value.trim()).filter(Boolean);
    const share_group_uid = this.detail.share_group?.share_group_uid || null;
    let started = false;
    try {
      const preview = await this.api.post("user-profiles/share-groups/preview", { profile_scope_uids, share_group_uid });
      const confirmed = await this.confirmDialog.show({ title: window.t("profile.sharing"), message: window.t("profile.shareImpact", preview.fact_count, preview.potential_conflict_categories.length), confirmLabel: window.t("common.save") });
      if (!confirmed) return;
      started = this.beginOperation(window.t("profile.taskApplyingOperation"));
      if (!started) return;
      if (button) button.disabled = true;
      await this.api.post("user-profiles/share-groups/save", { name, profile_scope_uids, share_group_uid, fingerprint: preview.fingerprint });
      await this.refresh();
    } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; if (started) this.endOperation(); }
  }

  async retryTask(task_uid, button = null) {
    if (!this.beginOperation(window.t("profile.taskPreparingRetry"))) return;
    if (button) button.disabled = true;
    try { await this.api.post("user-profiles/tasks/retry", { task_uid }); await this.loadDetail(); this.resumeTaskPolling(); } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; this.endOperation(); }
  }

  async cancelTask(task_uid, button = null) {
    if (this.operationInFlight) return;
    const confirmed = await this.confirmDialog.show({ title: window.t("profile.cancelTask"), message: window.t("profile.confirm.cancelTask"), confirmLabel: window.t("common.cancel") });
    if (!confirmed || !this.beginOperation(window.t("profile.taskCancelling"))) return;
    if (button) button.disabled = true;
    try {
      await this.api.post("user-profiles/tasks/cancel", { task_uid });
      this.taskProgressBaseline = null;
      this.stopTaskPolling();
      await this.loadDetail();
      await this.loadList();
    } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; this.endOperation(); }
  }

  async deleteTask(task_uid, button = null) {
    if (this.operationInFlight) return;
    const confirmed = await this.confirmDialog.show({ title: window.t("profile.deleteTask"), message: window.t("profile.confirm.deleteTask"), confirmLabel: window.t("common.delete") });
    if (!confirmed || !this.beginOperation(window.t("profile.taskApplyingOperation"))) return;
    if (button) button.disabled = true;
    try { await this.api.post("user-profiles/tasks/delete", { task_uid }); await this.loadDetail(); }
    catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; this.endOperation(); }
  }

  async clearCompletedTasks(button = null) {
    if (this.operationInFlight) return;
    const confirmed = await this.confirmDialog.show({ title: window.t("profile.clearCompleted"), message: window.t("profile.confirm.clearCompleted"), confirmLabel: window.t("profile.clearCompleted") });
    if (!confirmed || !this.beginOperation(window.t("profile.taskApplyingOperation"))) return;
    if (button) button.disabled = true;
    try { await this.api.post("user-profiles/tasks/clear-completed", { profile_scope_uid: this.selectedScopeUid }); await this.loadDetail(); }
    catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; this.endOperation(); }
  }

  async continueGap(button = null) {
    if (this.operationInFlight) return;
    if (!this.beginOperation(window.t("profile.taskContinuingGap"))) return;
    if (button) button.disabled = true;
    try {
      const result = await this.api.post("user-profiles/rebuild/continue", { profile_scope_uid: this.selectedScopeUid });
      this.taskProgressBaseline = { scope: this.selectedScopeUid, total: Number(result.event_count || 0) };
      await this.loadDetail();
      this.resumeTaskPolling();
    } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; this.endOperation(); }
  }

  trackCurrentQueue() {
    const pending = Number(this.detail?.gap?.pending_count || 0);
    if (!this.selectedScopeUid || pending <= 0) return;
    this.taskProgressBaseline = { scope: this.selectedScopeUid, total: pending };
    this.updateTaskRegions();
    this.resumeTaskPolling();
  }

  async scanIdentityReviews(button = null) {
    if (!this.beginOperation(window.t("profile.taskScanningIdentity"))) return;
    if (button) button.disabled = true;
    try {
      const data = await this.api.post("user-profiles/identity-reviews/scan", { profile_scope_uid: this.selectedScopeUid });
      if (this.detail) this.detail.identity_reviews = data.items || [];
      this.renderDetail();
      this.showToast(window.t("profile.identityScanDone", data.diagnostics?.legacy_auto_resolved_count || 0, data.diagnostics?.pending_review_count || 0));
    } catch (error) { this.showToast(error.message, true); }
    finally { if (button) button.disabled = false; this.endOperation(); }
  }

  async identityReviewAction(button) {
    if (this.operationInFlight) return;
    const action = button.dataset.identityReviewAction;
    const confirmed = await this.confirmDialog.show({ title: window.t(`profile.identityAction.${action}`), message: window.t(`profile.confirm.identity-${action}`), confirmLabel: window.t("common.confirm") });
    if (!confirmed) return;
    const row = button.closest("[data-identity-review-row]");
    const actor_id = action === "bind" ? row?.querySelector("[data-identity-actor]")?.value || "" : null;
    if (!this.beginOperation(window.t("profile.taskApplyingOperation"))) return;
    button.disabled = true;
    try {
      await this.api.post("user-profiles/identity-reviews/action", {
        profile_scope_uid: this.selectedScopeUid,
        timeline_uid: button.dataset.timelineUid,
        timeline_revision: Number(button.dataset.timelineRevision || 1),
        memory_space_id: button.dataset.memorySpaceId,
        evidence_fingerprint: button.dataset.evidenceFingerprint,
        action,
        actor_id,
      });
      await this.loadDetail();
    } catch (error) { this.showToast(error.message, true); }
    finally { button.disabled = false; this.endOperation(); }
  }

  formatTime(value) {
    const numeric = Number(value || 0);
    if (!numeric) return "-";
    return new Date(numeric * 1000).toLocaleString();
  }
}
