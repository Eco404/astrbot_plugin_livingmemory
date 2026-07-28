import { esc } from "./utils.js";

export class MaintenancePage {
  constructor(topicPage, recallPage, modelPage, showToast, confirmDialog) {
    this.topicPage = topicPage;
    this.recallPage = recallPage;
    this.modelPage = modelPage;
    this.showToast = showToast;
    this.confirmDialog = confirmDialog;
    this.tab = "topic";
    this.reviewUid = null;
    this.governance = null;
    this.sessionAudit = [];
    this.sessionPreview = null;
    this.timelineRebuildItems = [];
    this.timelineRebuildPoller = null;
    this.timelineStagedItems = [];
    this.databaseHealth = null;
    this.databaseRepairPoller = null;
    this.databaseRepairTaskUid = null;
    this.topicMaintenanceRequestId = 0;
  }

  initEventListeners() {
    document.querySelectorAll("[data-maintenance-tab]").forEach(button => {
      button.addEventListener("click", () => this.selectTab(button.dataset.maintenanceTab));
    });
    document.getElementById("maintenance-topic-space")?.addEventListener("change", () => this.changeTopicSpace());
    document.getElementById("maintenance-open-topic")?.addEventListener("click", event => this.openTopicMaintenance(event.currentTarget));
    document.getElementById("maintenance-open-archived-topics")?.addEventListener("click", () => this.openArchivedTopics());
    document.getElementById("maintenance-open-reviews")?.addEventListener("click", () => this.openReviews());
    document.getElementById("maintenance-review-refresh")?.addEventListener("click", () => this.loadReviews());
    document.getElementById("maintenance-review-list")?.addEventListener("click", event => {
      const item = event.target.closest("[data-review-uid]");
      if (item) this.loadReviewDetail(item.dataset.reviewUid);
    });
    document.getElementById("maintenance-review-detail")?.addEventListener("click", event => {
      const button = event.target.closest("[data-review-action]");
      if (button) this.resolveReview(button.dataset.reviewAction);
    });
    document.getElementById("maintenance-open-governance")?.addEventListener("click", () => this.openGovernance());
    document.getElementById("topic-review-close")?.addEventListener("click", () => this.closeReviews());
    document.getElementById("topic-review-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.closeReviews();
    });
    document.getElementById("session-audit-refresh")?.addEventListener("click", () => this.loadSessionAudit());
    document.getElementById("session-audit-filter")?.addEventListener("input", () => this.renderSessionAudit());
    document.getElementById("session-audit-action")?.addEventListener("click", () => this.openSessionMaintenance());
    document.getElementById("session-task-clear")?.addEventListener("click", () => this.clearSessionTasks());
    document.getElementById("session-task-list")?.addEventListener("click", event => {
      const button = event.target.closest("[data-session-task-delete]");
      if (button) this.deleteSessionTask(button.dataset.sessionTaskDelete, button);
    });
    document.getElementById("timeline-rebuild-preview")?.addEventListener("click", () => this.previewTimelineRebuild());
    document.getElementById("timeline-rebuild-select-all")?.addEventListener("click", () => this.toggleTimelineRebuildSelection());
    document.getElementById("timeline-inactive-open")?.addEventListener("click", () => this.openInactiveTimelines());
    document.getElementById("timeline-rebuild-start")?.addEventListener("click", () => this.startTimelineRebuild());
    document.getElementById("timeline-rebuild-list")?.addEventListener("change", () => this.updateTimelineRebuildSelection());
    document.getElementById("timeline-rebuild-task-refresh")?.addEventListener("click", () => this.loadTimelineRebuildTasks());
    document.getElementById("timeline-rebuild-task-clear")?.addEventListener("click", () => this.clearTimelineRebuildTasks());
    document.getElementById("timeline-staged-open")?.addEventListener("click", () => this.openTimelineStagedEdits());
    document.getElementById("timeline-rebuild-task-list")?.addEventListener("click", event => {
      const button = event.target.closest("[data-timeline-rebuild-action]");
      if (button) this.handleTimelineRebuildTaskAction(button.dataset.timelineRebuildAction, button.dataset.taskUid, button);
    });
    document.getElementById("recall-trace-enabled")?.addEventListener("change", event => this.setRecallTraceEnabled(event.target.checked));
    document.getElementById("recent-recall-refresh")?.addEventListener("click", () => this.loadRecentRecalls());
    document.getElementById("recent-recall-clear")?.addEventListener("click", () => this.clearRecentRecalls());
    document.getElementById("recent-recall-list")?.addEventListener("toggle", event => {
      const details = event.target.closest("details[data-recall-trace-uid]");
      if (details?.open) this.loadRecentRecallDetail(details);
    }, true);
    document.getElementById("recent-recall-list")?.addEventListener("click", event => {
      const button = event.target.closest("[data-recent-recall-action]");
      if (button) this.handleRecentRecallAction(button.dataset.recentRecallAction, button.dataset.traceUid, button);
    });
    document.getElementById("database-health-refresh")?.addEventListener("click", () => this.loadDatabaseHealth());
    document.getElementById("database-health-repair")?.addEventListener("click", () => this.repairDatabaseIssues());
    document.getElementById("database-health-issues")?.addEventListener("change", () => this.updateDatabaseSelection());
    document.getElementById("session-maintenance-operation")?.addEventListener("change", () => this.resetSessionPreview());
    document.getElementById("session-maintenance-close")?.addEventListener("click", () => this.closeSessionMaintenance());
    document.getElementById("session-maintenance-cancel")?.addEventListener("click", () => this.closeSessionMaintenance());
    document.getElementById("session-maintenance-submit")?.addEventListener("click", () => this.submitSessionMaintenance());
    document.getElementById("session-maintenance-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.closeSessionMaintenance();
    });
    document.getElementById("topic-governance-close")?.addEventListener("click", () => this.closeGovernance());
    document.getElementById("topic-governance-cancel")?.addEventListener("click", () => this.closeGovernance());
    document.getElementById("topic-governance-submit")?.addEventListener("click", () => this.submitGovernance());
    document.getElementById("topic-governance-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.closeGovernance();
    });
    document.getElementById("topic-governance-body")?.addEventListener("change", event => this.handleGovernanceChange(event));
    document.getElementById("topic-governance-body")?.addEventListener("click", event => {
      if (event.target.closest("[data-governance-add-group]")) this.addGovernanceGroup();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && document.getElementById("topic-review-overlay")?.classList.contains("visible")) {
        this.closeReviews();
      }
    });
    window.addEventListener("livingmemory:topic-maintenance-updated", event => {
      const selected = this.reviewSpace();
      if (this.tab === "topic" && (!event.detail?.memory_space_id || event.detail.memory_space_id === selected)) {
        this.loadTopicMaintenanceCounts();
      }
    });
    window.addEventListener("livingmemory:timeline-staged-updated", () => this.loadTimelineStagedCount());
  }

  async activate() {
    await this.topicPage.fetch();
    this.syncTopicSpaces();
    if (this.tab === "topic") this.loadTopicMaintenanceCounts();
    if (this.tab === "models") this.modelPage.fetch();
    if (this.tab === "recall") this.recallPage.fetchSessions();
    if (this.tab === "sessions") this.loadSessionAudit();
    if (this.tab === "recent-recall") this.loadRecentRecalls();
    if (this.tab === "timeline-rebuild") {
      this.loadTimelineRebuildTasks();
      this.loadTimelineStagedCount();
    }
    if (this.tab === "database") this.resumeDatabaseRepair();
  }

  selectTab(tab) {
    if (!tab) return;
    this.tab = tab;
    document.querySelectorAll("[data-maintenance-tab]").forEach(button => {
      const active = button.dataset.maintenanceTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".maintenance-panel").forEach(panel => panel.classList.toggle("active", panel.id === `maintenance-panel-${tab}`));
    if (tab === "models") this.modelPage.fetch();
    if (tab === "recall") {
      this.recallPage.fetchSessions();
      this.recallPage.loadHistory();
    }
    if (tab === "topic") this.loadTopicMaintenanceCounts();
    if (tab === "sessions") this.loadSessionAudit();
    if (tab === "recent-recall") this.loadRecentRecalls();
    if (tab === "timeline-rebuild") {
      this.loadTimelineRebuildTasks();
      this.loadTimelineStagedCount();
    }
    if (tab === "database" && !this.databaseHealth) this.resumeDatabaseRepair();
  }

  selectedDatabaseIssues() {
    return Array.from(document.querySelectorAll("[data-database-issue]:checked"))
      .map(input => input.value)
      .filter(Boolean);
  }

  updateDatabaseSelection() {
    const selected = this.selectedDatabaseIssues();
    const button = document.getElementById("database-health-repair");
    if (button) button.disabled = selected.length === 0 || Boolean(this.databaseRepairTaskUid);
    const label = document.getElementById("database-health-selection");
    if (label) label.textContent = selected.length
      ? window.t("maintenance.databaseSelected", selected.length)
      : "";
  }

  formatDatabaseBytes(value) {
    const bytes = Math.max(0, Number(value || 0));
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  renderDatabaseHealth() {
    const data = this.databaseHealth;
    const summary = document.getElementById("database-health-summary");
    const databases = document.getElementById("database-health-databases");
    const issues = document.getElementById("database-health-issues");
    if (!summary || !databases || !issues || !data) return;

    const healthy = data.summary?.status === "healthy";
    summary.innerHTML = `<div class="database-health-state ${healthy ? "is-healthy" : "has-issues"}">
      <span class="database-health-indicator" aria-hidden="true"></span>
      <span><strong>${esc(healthy ? window.t("maintenance.databaseHealthy") : window.t("maintenance.databaseNeedsAttention"))}</strong>
      <small>${esc(window.t("maintenance.databaseSummary", Number(data.summary?.issue_group_count || 0), Number(data.summary?.issue_count || 0), Number(data.summary?.repairable_count || 0)))}</small></span>
    </div>`;

    databases.innerHTML = (data.databases || []).map(database => {
      const ok = database.integrity === "ok" && !(database.foreign_key_violations || []).length;
      return `<div class="database-health-database">
        <span><strong>${esc(database.label || database.filename)}</strong><small>${esc(database.filename || "--")} · ${esc(this.formatDatabaseBytes(database.size_bytes))}${database.schema_version ? ` · v${esc(database.schema_version)}` : ""}</small></span>
        <span class="status-badge ${ok ? "status-completed" : "status-failed"}">${esc(ok ? window.t("maintenance.databaseIntegrityOk") : window.t("maintenance.databaseIntegrityIssue"))}</span>
        <small>${esc(window.t("maintenance.databaseForeignKeys", Number((database.foreign_key_violations || []).length)))}</small>
      </div>`;
    }).join("");

    const rows = data.issues || [];
    issues.innerHTML = rows.length ? rows.map(issue => {
      const action = issue.repair_action === "rebuild_graph_memory"
        ? window.t("maintenance.databaseRepairRebuild")
        : issue.repair_action === "delete_orphan_graph_entries"
          ? window.t("maintenance.databaseRepairDelete")
          : window.t("maintenance.databaseManualOnly");
      return `<label class="database-health-issue ${issue.repairable ? "is-repairable" : "is-manual"}">
        <input type="checkbox" data-database-issue value="${esc(issue.issue_uid)}" ${issue.repairable ? "" : "disabled"}>
        <span class="database-health-issue-copy">
          <span><strong>${esc(issue.title)}</strong><span class="status-badge status-failed">${Number(issue.count || 1)}</span></span>
          <small>${esc(issue.description || "")}</small>
          ${issue.excerpt ? `<p>${esc(issue.excerpt)}</p>` : ""}
          <code>${esc(issue.issue_uid)}</code>
        </span>
        <span class="database-health-action">${esc(action)}</span>
      </label>`;
    }).join("") : `<div class="identity-state database-health-empty">${esc(window.t("maintenance.databaseNoIssues"))}</div>`;
    this.updateDatabaseSelection();
  }

  async loadDatabaseHealth() {
    const button = document.getElementById("database-health-refresh");
    const summary = document.getElementById("database-health-summary");
    const issues = document.getElementById("database-health-issues");
    if (!button || !summary || !issues) return;
    button.disabled = true;
    this.renderDatabaseProgress({
      status: "running",
      stage: "checking",
      current: 0,
      total: 0,
      percent: 0,
      current_step: window.t("maintenance.databaseChecking"),
      created_at: Date.now() / 1000,
      indeterminate: true,
    });
    summary.innerHTML = `<div class="identity-state">${esc(window.t("maintenance.databaseChecking"))}</div>`;
    issues.innerHTML = "";
    try {
      this.databaseHealth = await this.topicPage.api.get("database/health");
      this.renderDatabaseHealth();
    } catch (error) {
      summary.innerHTML = `<div class="identity-state identity-state-error">${esc(error.message)}</div>`;
      this.showToast(error.message, true);
    } finally {
      button.disabled = false;
      if (!this.databaseRepairTaskUid) {
        document.getElementById("database-repair-progress")?.classList.add("hidden");
      }
    }
  }

  formatElapsed(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds || 0)));
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return minutes ? `${minutes}m ${rest}s` : `${rest}s`;
  }

  renderDatabaseProgress(job) {
    const panel = document.getElementById("database-repair-progress");
    if (!panel || !job) return;
    panel.classList.remove("hidden");
    const percent = Math.max(0, Math.min(100, Number(job.percent || 0)));
    const stage = String(job.stage || "pending");
    const stageLabels = {
      checking: window.t("maintenance.databaseStageChecking"),
      validating: window.t("maintenance.databaseStageValidating"),
      repairing: window.t("maintenance.databaseStageRepairing"),
      verifying: window.t("maintenance.databaseStageVerifying"),
      completed: window.t("maintenance.databaseStageCompleted"),
      failed: window.t("maintenance.databaseStageFailed"),
      cancelled: window.t("maintenance.databaseStageCancelled"),
    };
    const stageElement = document.getElementById("database-repair-progress-stage");
    const percentElement = document.getElementById("database-repair-progress-percent");
    const bar = document.getElementById("database-repair-progress-bar");
    const track = bar?.parentElement;
    const step = document.getElementById("database-repair-progress-step");
    const count = document.getElementById("database-repair-progress-count");
    const elapsed = document.getElementById("database-repair-progress-elapsed");
    const error = document.getElementById("database-repair-progress-error");
    if (stageElement) stageElement.textContent = stageLabels[stage] || window.t("maintenance.databaseRepairProgress");
    if (percentElement) percentElement.textContent = job.indeterminate ? "" : `${percent.toFixed(1)}%`;
    if (bar) bar.style.width = job.indeterminate ? "35%" : `${percent}%`;
    track?.classList.toggle("is-indeterminate", Boolean(job.indeterminate));
    if (step) step.textContent = job.current_step || "";
    if (count) count.textContent = job.total ? `${Number(job.current || 0)} / ${Number(job.total || 0)}` : "";
    if (elapsed) {
      const now = Date.now() / 1000;
      elapsed.textContent = window.t("maintenance.operationElapsed", this.formatElapsed(now - Number(job.created_at || now)));
    }
    if (error) error.textContent = job.error || "";
  }

  async resumeDatabaseRepair() {
    try {
      const job = await this.topicPage.api.get("database/repair/progress");
      if (["pending", "running"].includes(job.status)) {
        this.databaseRepairTaskUid = job.job_uid;
        this.renderDatabaseProgress(job);
        this.pollDatabaseRepair(job.job_uid);
        return;
      }
      if (job.health) {
        this.databaseHealth = job.health;
        this.renderDatabaseHealth();
        this.renderDatabaseProgress(job);
      } else {
        await this.loadDatabaseHealth();
      }
    } catch (_) {
      await this.loadDatabaseHealth();
    }
  }

  pollDatabaseRepair(jobUid) {
    clearTimeout(this.databaseRepairPoller);
    this.databaseRepairTaskUid = jobUid;
    const poll = async () => {
      try {
        const job = await this.topicPage.api.get("database/repair/progress", { job_uid: jobUid });
        this.renderDatabaseProgress(job);
        const terminal = ["completed", "completed_with_errors", "failed", "cancelled"].includes(job.status);
        if (!terminal) {
          this.databaseRepairPoller = setTimeout(poll, 800);
          return;
        }
        this.databaseRepairTaskUid = null;
        this.databaseRepairPoller = null;
        if (job.health) {
          this.databaseHealth = job.health;
          this.renderDatabaseHealth();
        }
        const failed = Number((job.failed || []).length);
        if (job.status === "failed") this.showToast(job.error || window.t("maintenance.databaseRepairFailed"), true);
        else this.showToast(failed
          ? window.t("maintenance.databaseRepairPartial", failed)
          : window.t("maintenance.databaseRepairComplete"), failed > 0);
        const button = document.getElementById("database-health-repair");
        if (button) button.disabled = this.selectedDatabaseIssues().length === 0;
      } catch (error) {
        this.databaseRepairTaskUid = null;
        this.databaseRepairPoller = null;
        this.showToast(error.message, true);
      }
    };
    this.databaseRepairPoller = setTimeout(poll, 250);
  }

  async repairDatabaseIssues() {
    const issueUids = this.selectedDatabaseIssues();
    if (!issueUids.length) return;
    const confirmed = await this.confirmDialog.show({
      title: window.t("maintenance.confirmDatabaseRepair"),
      message: window.t("maintenance.confirmDatabaseRepairMessage", issueUids.length),
      confirmLabel: window.t("maintenance.repairSelected"),
      danger: true,
    });
    if (!confirmed) return;

    const button = document.getElementById("database-health-repair");
    button.disabled = true;
    try {
      const job = await this.topicPage.api.post("database/repair", {
        issues: issueUids.map(issue_uid => ({ issue_uid })),
      });
      this.renderDatabaseProgress(job);
      this.pollDatabaseRepair(job.job_uid);
    } catch (error) {
      this.showToast(error.message, true);
      button.disabled = false;
    }
  }

  syncTopicSpaces() {
    const source = document.getElementById("topic-space");
    const target = document.getElementById("maintenance-topic-space");
    if (!source || !target) return;
    [target, document.getElementById("timeline-rebuild-space")].filter(Boolean).forEach(select => {
      const previous = select.value;
      select.innerHTML = source.innerHTML;
      select.value = previous && Array.from(select.options).some(option => option.value === previous)
        ? previous
        : source.value;
    });
    this.renderTopicMaintenanceState();
  }

  renderTopicMaintenanceState() {
    const selected = this.reviewSpace();
    document.getElementById("maintenance-topic-empty")?.classList.toggle("hidden", Boolean(selected));
    document.getElementById("maintenance-topic-actions")?.classList.toggle("hidden", !selected);
  }

  async changeTopicSpace() {
    const selected = this.reviewSpace();
    const topicSpace = document.getElementById("topic-space");
    this.renderTopicMaintenanceState();
    if (topicSpace) topicSpace.value = selected;
    if (!selected) {
      this.topicMaintenanceRequestId += 1;
      this.setTopicMaintenanceCount("maintenance-topic-unindexed-count", 0);
      this.setTopicMaintenanceCount("maintenance-topic-review-count", 0);
      return;
    }
    await this.topicPage.fetch();
    this.syncTopicSpaces();
    await this.loadTopicMaintenanceCounts();
  }

  setTopicMaintenanceCount(id, value) {
    const target = document.getElementById(id);
    if (target) target.textContent = String(value);
  }

  async loadTopicMaintenanceCounts() {
    const space = this.reviewSpace();
    this.renderTopicMaintenanceState();
    if (!space) return;
    const requestId = ++this.topicMaintenanceRequestId;
    this.setTopicMaintenanceCount("maintenance-topic-unindexed-count", "…");
    this.setTopicMaintenanceCount("maintenance-topic-review-count", "…");
    try {
      const [unindexed, reviews] = await Promise.all([
        this.topicPage.api.get("topics/maintenance/unindexed", { memory_space_id: space }),
        this.topicPage.api.get("topics/reviews", { memory_space_id: space }),
      ]);
      if (requestId !== this.topicMaintenanceRequestId || space !== this.reviewSpace()) return;
      this.setTopicMaintenanceCount("maintenance-topic-unindexed-count", Number(unindexed.total || 0));
      this.setTopicMaintenanceCount("maintenance-topic-review-count", Number(reviews.total ?? reviews.items?.length ?? 0));
    } catch (error) {
      if (requestId !== this.topicMaintenanceRequestId) return;
      this.setTopicMaintenanceCount("maintenance-topic-unindexed-count", "!");
      this.setTopicMaintenanceCount("maintenance-topic-review-count", "!");
      this.showToast(error.message, true);
    }
  }

  selectedTimelineRebuildIds() {
    return Array.from(document.querySelectorAll("[data-timeline-rebuild-select]:checked"))
      .map(input => Number(input.value))
      .filter(Number.isFinite);
  }

  updateTimelineRebuildSelection() {
    const selected = this.selectedTimelineRebuildIds();
    const availableInputs = Array.from(document.querySelectorAll("[data-timeline-rebuild-select]:not(:disabled)"));
    const button = document.getElementById("timeline-rebuild-start");
    if (button) button.disabled = selected.length === 0;
    const selectAll = document.getElementById("timeline-rebuild-select-all");
    if (selectAll) {
      const allSelected = availableInputs.length > 0 && availableInputs.every(input => input.checked);
      selectAll.textContent = window.t(allSelected ? "common.deselectAll" : "common.selectAll");
      selectAll.disabled = availableInputs.length === 0;
    }
    const summary = document.getElementById("timeline-rebuild-summary");
    if (summary && this.timelineRebuildItems.length) {
      const available = this.timelineRebuildItems.filter(item => item.reconstructable).length;
      const blocked = this.timelineRebuildItems.length - available;
      summary.textContent = window.t("maintenance.rebuildSummary", this.timelineRebuildItems.length, available, blocked, selected.length);
    }
  }

  toggleTimelineRebuildSelection() {
    const inputs = Array.from(document.querySelectorAll("[data-timeline-rebuild-select]:not(:disabled)"));
    const allSelected = inputs.length > 0 && inputs.every(input => input.checked);
    inputs.forEach(input => { input.checked = !allSelected; });
    this.updateTimelineRebuildSelection();
  }

  async previewTimelineRebuild() {
    const space = document.getElementById("timeline-rebuild-space")?.value || "";
    const qualityFilter = document.getElementById("timeline-rebuild-quality")?.value || "all";
    if (!space) return this.showToast(window.t("topic.chooseSpace"), true);
    const button = document.getElementById("timeline-rebuild-preview");
    const list = document.getElementById("timeline-rebuild-list");
    button.disabled = true;
    list.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
    try {
      const data = await this.topicPage.api.post("timeline/rebuild/preview", { memory_space_id: space, quality_filter: qualityFilter, limit: 2000 });
      this.timelineRebuildItems = data.items || [];
      document.getElementById("timeline-rebuild-options")?.classList.toggle("hidden", !this.timelineRebuildItems.length);
      list.innerHTML = this.timelineRebuildItems.length ? this.timelineRebuildItems.map(item => {
        const blocked = (item.blocked_reasons || []).join("；");
        const warnings = (item.identity_warnings || []).join("、");
        return `<label class="timeline-rebuild-row ${item.reconstructable ? "" : "is-blocked"}">
          <input type="checkbox" data-timeline-rebuild-select value="${Number(item.memory_id)}" ${item.reconstructable ? "" : "disabled"}>
          <span class="timeline-rebuild-main"><strong>ID ${Number(item.memory_id)}${item.summary_quality === "low" ? ` <span class="timeline-quality-flag" title="${esc(window.t("memory.lowQualityHint"))}" aria-label="${esc(window.t("memory.lowQuality"))}"></span>` : ""}</strong><span>${esc(item.excerpt || "--")}</span><small>${esc(item.session_id || "--")} · ${Number(item.message_count || 0)} / ${Number(item.expected_message_count || 0)} ${esc(window.t("maintenance.sourceMessages"))} · Topic ${Number(item.topic_count || 0)}</small></span>
          <span class="timeline-rebuild-state ${item.reconstructable ? "is-ready" : "is-blocked"}">${esc(item.reconstructable ? window.t("maintenance.rebuildable") : blocked)}${warnings ? `<small>${esc(window.t("maintenance.identityWarning"))}: ${esc(warnings)}</small>` : ""}</span>
        </label>`;
      }).join("") : `<div class="identity-state">${esc(window.t("maintenance.noTimelinesInSpace"))}</div>`;
      this.updateTimelineRebuildSelection();
    } catch (error) {
      list.innerHTML = `<div class="identity-state identity-state-error">${esc(error.message)}</div>`;
      this.showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async startTimelineRebuild() {
    const memoryIds = this.selectedTimelineRebuildIds();
    if (!memoryIds.length) return;
    const topicMode = document.getElementById("timeline-rebuild-topic-mode")?.value || "local";
    const confirmed = await this.confirmDialog.show({
      title: window.t("maintenance.confirmTimelineRebuild"),
      message: window.t("maintenance.confirmTimelineRebuildMessage", memoryIds.length),
      confirmLabel: window.t("maintenance.startRebuild"),
    });
    if (!confirmed) return;
    const button = document.getElementById("timeline-rebuild-start");
    button.disabled = true;
    try {
      const task = await this.topicPage.api.post("timeline/rebuild/start", { memory_ids: memoryIds, topic_mode: topicMode });
      this.showToast(window.t("maintenance.rebuildStarted"));
      await this.loadTimelineRebuildTasks();
      this.pollTimelineRebuildTask(task.task_uid);
    } catch (error) {
      this.showToast(error.message, true);
      button.disabled = false;
    }
  }

  async loadTimelineRebuildTasks() {
    const target = document.getElementById("timeline-rebuild-task-list");
    if (!target) return;
    try {
      const data = await this.topicPage.api.get("timeline/rebuild/tasks", { limit: 30 });
      const tasks = data.items || [];
      target.innerHTML = tasks.length ? tasks.map(task => {
        const total = Number(task.total_count || 0);
        const done = Number(task.completed_count || 0);
        const failed = Number(task.failed_count || 0);
        const progress = total ? Math.max(0, Math.min(100, done / total * 100)) : 0;
        const terminal = ["completed", "completed_with_review", "completed_with_errors", "failed", "cancelled"].includes(task.status);
        const resumable = ["failed", "completed_with_errors"].includes(task.status);
        const cancellable = ["queued", "running", "cancelling"].includes(task.status);
        return `<div class="session-task-row timeline-rebuild-task-row">
          <span class="session-task-main"><strong>${esc(task.topic_mode === "full" ? window.t("maintenance.topicSyncFull") : window.t("maintenance.topicSyncLocal"))}</strong><small>${done} / ${total} · ${esc(window.t("maintenance.failedCount", failed))}</small><span class="maintenance-task-progress" aria-label="${progress.toFixed(0)}%"><span style="width:${progress}%"></span></span></span>
          <span class="status-badge status-${esc(task.status)}">${esc(task.status)}</span>
          <small class="session-task-detail">${esc(task.current_step || "")}${task.error ? ` · ${esc(task.error)}` : ""}</small>
          <span class="session-task-actions">
            ${resumable ? `<button class="btn btn-secondary btn-sm" data-timeline-rebuild-action="resume" data-task-uid="${esc(task.task_uid)}">${esc(window.t("maintenance.resumeTask"))}</button>` : ""}
            ${cancellable ? `<button class="btn btn-danger btn-sm" data-timeline-rebuild-action="cancel" data-task-uid="${esc(task.task_uid)}">${esc(window.t("common.cancel"))}</button>` : ""}
            ${terminal ? `<button class="session-task-delete" data-timeline-rebuild-action="delete" data-task-uid="${esc(task.task_uid)}" title="${esc(window.t("common.delete"))}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2m-9 0 1 14h8l1-14M10 10v6m4-6v6"/></svg></button>` : ""}
          </span>
        </div>`;
      }).join("") : `<span class="text-tertiary">${esc(window.t("maintenance.noTasks"))}</span>`;
    } catch (error) {
      target.innerHTML = `<span class="text-danger">${esc(error.message)}</span>`;
    }
  }

  pollTimelineRebuildTask(taskUid) {
    if (this.timelineRebuildPoller) clearInterval(this.timelineRebuildPoller);
    this.timelineRebuildPoller = setInterval(async () => {
      try {
        const task = await this.topicPage.api.get("timeline/rebuild/task", { task_uid: taskUid });
        await this.loadTimelineRebuildTasks();
        if (["completed", "completed_with_review", "completed_with_errors", "failed", "cancelled"].includes(task.status)) {
          clearInterval(this.timelineRebuildPoller);
          this.timelineRebuildPoller = null;
          if (this.tab === "timeline-rebuild" && document.getElementById("timeline-rebuild-space")?.value) {
            await this.previewTimelineRebuild();
          } else {
            document.getElementById("timeline-rebuild-start").disabled = this.selectedTimelineRebuildIds().length === 0;
          }
        }
      } catch (_) {
        clearInterval(this.timelineRebuildPoller);
        this.timelineRebuildPoller = null;
      }
    }, 1500);
  }

  async handleTimelineRebuildTaskAction(action, taskUid, button) {
    if (!taskUid || button.disabled) return;
    button.disabled = true;
    try {
      if (action === "resume") {
        await this.topicPage.api.post("timeline/rebuild/resume", { task_uid: taskUid });
        this.pollTimelineRebuildTask(taskUid);
      } else if (action === "cancel") {
        await this.topicPage.api.post("timeline/rebuild/cancel", { task_uid: taskUid });
      } else if (action === "delete") {
        await this.topicPage.api.post("timeline/rebuild/tasks/delete", { task_uid: taskUid });
      }
      await this.loadTimelineRebuildTasks();
    } catch (error) {
      this.showToast(error.message, true);
      button.disabled = false;
    }
  }

  async clearTimelineRebuildTasks() {
    const confirmed = await this.confirmDialog.show({
      title: window.t("maintenance.clearRebuildTasks"),
      message: window.t("maintenance.clearRebuildTasksMessage"),
      confirmLabel: window.t("common.clear"),
    });
    if (!confirmed) return;
    const button = document.getElementById("timeline-rebuild-task-clear");
    if (button) button.disabled = true;
    try {
      await this.topicPage.api.post("timeline/rebuild/tasks/clear", {});
      await this.loadTimelineRebuildTasks();
    } catch (error) {
      this.showToast(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async loadTimelineStagedCount() {
    const badge = document.getElementById("timeline-staged-count");
    if (!badge) return;
    try {
      const data = await this.topicPage.api.get("timeline/staged-edits", { limit: 2000 });
      this.timelineStagedItems = data.items || [];
      badge.textContent = String(Number(data.total ?? this.timelineStagedItems.length));
    } catch (_) {
      badge.textContent = "!";
    }
  }

  async openTimelineStagedEdits() {
    let overlay = document.getElementById("timeline-staged-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "timeline-staged-overlay";
      overlay.className = "modal-overlay timeline-staged-overlay";
      document.body.appendChild(overlay);
    }
    overlay.classList.add("visible");
    overlay.innerHTML = `<div class="modal timeline-staged-modal" role="dialog" aria-modal="true">
      <div class="modal-header"><div><div class="modal-title">${esc(window.t("maintenance.stagedEditsTitle"))}</div><p class="text-secondary">${esc(window.t("maintenance.stagedEditsIntro"))}</p></div><button class="modal-close" data-staged-close aria-label="Close">×</button></div>
      <div class="modal-body"><div class="identity-state">${esc(window.t("common.loading"))}</div></div>
      <div class="modal-footer"><label class="timeline-staged-mode"><span>${esc(window.t("maintenance.topicSyncMode"))}</span><select class="select input input-sm" data-staged-topic-mode><option value="local">${esc(window.t("maintenance.topicSyncLocal"))}</option><option value="full">${esc(window.t("maintenance.topicSyncFull"))}</option></select></label><button class="btn btn-danger" data-staged-delete disabled>${esc(window.t("maintenance.deleteStaged"))}</button><button class="btn btn-primary" data-staged-apply disabled>${esc(window.t("maintenance.applyStaged"))}</button></div>
      </div>`;
    const close = () => overlay.classList.remove("visible");
    overlay.querySelector("[data-staged-close]")?.addEventListener("click", close);
    overlay.onclick = event => { if (event.target === overlay) close(); };
    try {
      const space = document.getElementById("timeline-rebuild-space")?.value || "";
      const data = await this.topicPage.api.get("timeline/staged-edits", { memory_space_id: space, limit: 2000 });
      this.timelineStagedItems = data.items || [];
      const body = overlay.querySelector(".modal-body");
      body.innerHTML = this.timelineStagedItems.length
        ? `<div class="timeline-staged-list">${this.timelineStagedItems.map(item => `<label class="timeline-staged-row"><input type="checkbox" data-staged-select value="${esc(item.edit_uid)}" checked><span><strong>Timeline #${Number(item.memory_id)}</strong><small>r${Number(item.source_revision)} · ${esc(item.status || "pending")}${item.reason ? ` · ${esc(item.reason)}` : ""}</small><span>${esc(item.preview || item.source_excerpt || "--")}</span>${item.last_error ? `<small class="text-danger">${esc(item.last_error)}</small>` : ""}</span></label>`).join("")}</div>`
        : `<div class="identity-state">${esc(window.t("maintenance.noStagedEdits"))}</div>`;
      const update = () => {
        const any = overlay.querySelectorAll("[data-staged-select]:checked").length > 0;
        overlay.querySelector("[data-staged-apply]").disabled = !any;
        overlay.querySelector("[data-staged-delete]").disabled = !any;
      };
      body.addEventListener("change", update);
      update();
      overlay.querySelector("[data-staged-apply]")?.addEventListener("click", () => this.applyTimelineStagedEdits(overlay));
      overlay.querySelector("[data-staged-delete]")?.addEventListener("click", () => this.deleteTimelineStagedEdits(overlay));
      await this.loadTimelineStagedCount();
    } catch (error) {
      overlay.querySelector(".modal-body").innerHTML = `<div class="identity-state identity-state-error">${esc(error.message)}</div>`;
    }
  }

  selectedTimelineStagedUids(overlay) {
    return Array.from(overlay.querySelectorAll("[data-staged-select]:checked")).map(input => input.value).filter(Boolean);
  }

  async applyTimelineStagedEdits(overlay) {
    const editUids = this.selectedTimelineStagedUids(overlay);
    if (!editUids.length) return;
    const button = overlay.querySelector("[data-staged-apply]");
    button.disabled = true;
    try {
      const task = await this.topicPage.api.post("timeline/staged-edits/apply", {
        edit_uids: editUids,
        topic_mode: overlay.querySelector("[data-staged-topic-mode]")?.value || "local"
      });
      overlay.classList.remove("visible");
      this.showToast(window.t("maintenance.rebuildStarted"));
      await this.loadTimelineRebuildTasks();
      await this.loadTimelineStagedCount();
      this.pollTimelineRebuildTask(task.task_uid);
    } catch (error) {
      this.showToast(error.message, true);
      button.disabled = false;
    }
  }

  async deleteTimelineStagedEdits(overlay) {
    const editUids = this.selectedTimelineStagedUids(overlay);
    if (!editUids.length) return;
    const confirmed = await this.confirmDialog.show({
      title: window.t("maintenance.deleteStaged"),
      message: window.t("maintenance.stagedEditsIntro"),
      confirmLabel: window.t("common.delete"),
      danger: true,
    });
    if (!confirmed) return;
    try {
      await this.topicPage.api.post("timeline/staged-edits/delete", { edit_uids: editUids });
      await this.loadTimelineStagedCount();
      await this.openTimelineStagedEdits();
    } catch (error) {
      this.showToast(error.message, true);
    }
  }

  createStateMaintenanceModal(id, titleKey, introKey) {
    let overlay = document.getElementById(id);
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = id;
      overlay.className = "modal-overlay timeline-staged-overlay";
      document.body.appendChild(overlay);
    }
    overlay.innerHTML = `<div class="modal timeline-staged-modal" role="dialog" aria-modal="true">
      <div class="modal-header"><div><div class="modal-title">${esc(window.t(titleKey))}</div><p class="text-secondary">${esc(window.t(introKey))}</p></div><button class="modal-close" data-state-close aria-label="${esc(window.t("common.close"))}">×</button></div>
      <div class="modal-body"><div class="identity-state">${esc(window.t("common.loading"))}</div></div>
      <div class="modal-footer"><button class="btn btn-secondary" data-state-select-all disabled>${esc(window.t("common.selectAll"))}</button><button class="btn btn-danger" data-state-submit disabled></button></div>
    </div>`;
    overlay.classList.add("visible");
    const close = () => overlay.classList.remove("visible");
    overlay.querySelector("[data-state-close]")?.addEventListener("click", close);
    overlay.onclick = event => { if (event.target === overlay) close(); };
    return overlay;
  }

  bindStateMaintenanceSelection(overlay, submitLabelKey) {
    const update = () => {
      const inputs = Array.from(overlay.querySelectorAll("[data-state-select]"));
      const selected = inputs.filter(input => input.checked);
      const allSelected = inputs.length > 0 && selected.length === inputs.length;
      const toggle = overlay.querySelector("[data-state-select-all]");
      const submit = overlay.querySelector("[data-state-submit]");
      if (toggle) {
        toggle.disabled = inputs.length === 0;
        toggle.textContent = window.t(allSelected ? "common.deselectAll" : "common.selectAll");
      }
      if (submit) {
        submit.disabled = selected.length === 0;
        submit.textContent = window.t(submitLabelKey);
      }
    };
    overlay.querySelector(".modal-body")?.addEventListener("change", update);
    overlay.querySelector("[data-state-select-all]")?.addEventListener("click", () => {
      const inputs = Array.from(overlay.querySelectorAll("[data-state-select]"));
      const allSelected = inputs.length > 0 && inputs.every(input => input.checked);
      inputs.forEach(input => { input.checked = !allSelected; });
      update();
    });
    update();
    return () => Array.from(overlay.querySelectorAll("[data-state-select]:checked")).map(input => input.value);
  }

  async openArchivedTopics() {
    const space = this.reviewSpace();
    if (!space) return this.showToast(window.t("topic.chooseSpace"), true);
    const overlay = this.createStateMaintenanceModal(
      "archived-topic-maintenance-overlay",
      "maintenance.archivedTopicsTitle",
      "maintenance.archivedTopicsIntro",
    );
    try {
      const data = await this.topicPage.api.get("topics", {
        memory_space_id: space,
        status: "archived",
        limit: 500,
      });
      const items = data.items || [];
      overlay.querySelector(".modal-body").innerHTML = items.length
        ? `<div class="timeline-staged-list">${items.map(item => `<label class="timeline-staged-row"><input type="checkbox" data-state-select value="${esc(item.topic_uid)}"><span><strong>${esc(item.title || item.topic_uid)}</strong><small>${esc(item.topic_uid)} · r${Number(item.revision || 0)}</small><span>${esc(item.summary || "--")}</span></span></label>`).join("")}</div>`
        : `<div class="identity-state">${esc(window.t("maintenance.archivedTopicsEmpty"))}</div>`;
      const selected = this.bindStateMaintenanceSelection(overlay, "maintenance.deleteArchivedTopics");
      overlay.querySelector("[data-state-submit]")?.addEventListener("click", async event => {
        const topicUids = selected();
        if (!topicUids.length) return;
        const confirmed = await this.confirmDialog.show({
          title: window.t("maintenance.deleteArchivedConfirmTitle"),
          message: window.t("maintenance.deleteArchivedConfirmMessage", topicUids.length),
          confirmLabel: window.t("common.delete"),
          danger: true,
        });
        if (!confirmed) return;
        event.currentTarget.disabled = true;
        try {
          const result = await this.topicPage.api.post("topics/archived/delete", {
            memory_space_id: space,
            topic_uids: topicUids,
          });
          this.showToast(window.t("maintenance.archivedTopicsDeleted", Number(result.deleted_count || 0)));
          overlay.classList.remove("visible");
          await this.topicPage.fetch();
          this.syncTopicSpaces();
          await this.loadTopicMaintenanceCounts();
        } catch (error) {
          this.showToast(error.message, true);
          event.currentTarget.disabled = false;
        }
      });
    } catch (error) {
      overlay.querySelector(".modal-body").innerHTML = `<div class="identity-state identity-state-error">${esc(error.message)}</div>`;
    }
  }

  async openInactiveTimelines() {
    const space = document.getElementById("timeline-rebuild-space")?.value || "";
    if (!space) return this.showToast(window.t("topic.chooseSpace"), true);
    const overlay = this.createStateMaintenanceModal(
      "inactive-timeline-maintenance-overlay",
      "maintenance.inactiveTimelinesTitle",
      "maintenance.inactiveTimelinesIntro",
    );
    try {
      const data = await this.topicPage.api.get("timeline/inactive", {
        memory_space_id: space,
        limit: 500,
      });
      const items = data.items || [];
      overlay.querySelector(".modal-body").innerHTML = items.length
        ? `<div class="timeline-staged-list">${items.map(item => `<label class="timeline-staged-row"><input type="checkbox" data-state-select value="${Number(item.memory_id)}"><span><strong>Timeline #${Number(item.memory_id)}</strong><small>${esc(item.status)} · r${Number(item.revision || 0)} · Topic ${Number(item.topic_count || 0)}</small><span>${esc(item.excerpt || "--")}</span></span></label>`).join("")}</div>`
        : `<div class="identity-state">${esc(window.t("maintenance.inactiveTimelinesEmpty"))}</div>`;
      const selected = this.bindStateMaintenanceSelection(overlay, "maintenance.restoreSelected");
      const submit = overlay.querySelector("[data-state-submit]");
      submit?.classList.remove("btn-danger");
      submit?.classList.add("btn-primary");
      submit?.addEventListener("click", async event => {
        const memoryIds = selected().map(Number).filter(Number.isFinite);
        if (!memoryIds.length) return;
        const confirmed = await this.confirmDialog.show({
          title: window.t("maintenance.restoreInactiveConfirmTitle"),
          message: window.t("maintenance.restoreInactiveConfirmMessage", memoryIds.length),
          confirmLabel: window.t("common.restore"),
        });
        if (!confirmed) return;
        event.currentTarget.disabled = true;
        try {
          const result = await this.topicPage.api.post("timeline/inactive/restore", {
            memory_space_id: space,
            memory_ids: memoryIds,
          });
          this.showToast(window.t("maintenance.inactiveTimelinesRestored", Number(result.restored_count || 0)));
          overlay.classList.remove("visible");
        } catch (error) {
          this.showToast(error.message, true);
          event.currentTarget.disabled = false;
        }
      });
    } catch (error) {
      overlay.querySelector(".modal-body").innerHTML = `<div class="identity-state identity-state-error">${esc(error.message)}</div>`;
    }
  }

  async openTopicMaintenance(trigger) {
    const selected = document.getElementById("maintenance-topic-space")?.value || "";
    if (!selected) {
      this.showToast(window.t("topic.chooseSpace"), true);
      return;
    }
    const topicSpace = document.getElementById("topic-space");
    topicSpace.value = selected;
    await this.topicPage.fetch();
    this.topicPage.openMaintenance(trigger);
  }

  selectedSessionIds() {
    return Array.from(document.querySelectorAll("[data-session-audit-select]:checked")).map(input => input.value);
  }

  async loadSessionAudit() {
    const list = document.getElementById("session-audit-list");
    if (!list) return;
    list.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
    try {
      const [audit, tasks] = await Promise.all([
        this.topicPage.api.get("sessions/audit", { limit: 2000 }),
        this.topicPage.api.get("sessions/maintenance/tasks", { limit: 20 }),
      ]);
      this.sessionAudit = audit.items || [];
      this.renderSessionAudit();
      this.renderSessionTasks(tasks.items || []);
    } catch (error) {
      list.innerHTML = `<div class="identity-state identity-state-error">${esc(error.message)}</div>`;
    }
  }

  renderSessionAudit() {
    const list = document.getElementById("session-audit-list");
    if (!list) return;
    const query = String(document.getElementById("session-audit-filter")?.value || "").trim().toLowerCase();
    const visible = this.sessionAudit.filter(item => !query || [item.session_id, item.platform, item.bot_account, item.target_id].join(" ").toLowerCase().includes(query));
    document.getElementById("session-audit-summary").textContent = window.t("maintenance.sessionsSummary", visible.length, this.sessionAudit.length);
    list.innerHTML = visible.length ? visible.map(item => {
      const state = item.active ? window.t("maintenance.activeSession") : item.raw_session_missing ? window.t("maintenance.rawMissing") : window.t("maintenance.inactiveSession");
      const blocked = (item.cleanup_block_reasons || []).join("；");
      const aliases = item.possible_aliases || [];
      const safety = item.safe_to_cleanup ? window.t("maintenance.safeCleanup") : window.t("maintenance.reviewRequired");
      return `<label class="session-audit-row">
        <input type="checkbox" data-session-audit-select value="${esc(item.session_id)}">
        <span class="session-audit-main"><strong>${esc(item.target_id || item.session_id)}</strong><code>${esc(item.session_id)}</code><small>${esc(item.platform)} · ${esc(window.t("maintenance.botAccount"))} ${esc(item.bot_account || "--")} · ${esc(item.chat_type)} · ${esc(state)}</small></span>
        <span class="session-audit-counts"><span>${esc(window.t("maintenance.rawMessages"))} <b>${Number(item.message_count || 0)}</b></span><span>${esc(window.t("maintenance.unsummarized"))} <b>${Number(item.unsummarized_message_count || 0)}</b></span><span>Timeline <b>${Number(item.timeline_count || 0)}</b></span><span>Topic <b>${Number(item.topic_count || 0)}</b></span><span>Fragment <b>${Number(item.fragment_count || 0)}</b></span><span>${esc(window.t("maintenance.evidenceRefs"))} <b>${Number(item.raw_evidence_reference_count || 0)}</b></span></span>
        <span class="session-audit-meta"><time>${this.formatTimestamp(item.last_active_at) || "--"}</time><small class="${item.safe_to_cleanup ? "session-audit-safe" : "session-audit-blocked"}">${esc(safety)}</small>${item.is_alias ? `<small>${esc(window.t("maintenance.aliasOf"))}: ${esc(item.canonical_session_id)}</small>` : ""}${aliases.length ? `<small>${esc(window.t("maintenance.possibleAliases"))}: ${aliases.map(esc).join(" · ")}</small>` : ""}${blocked ? `<small class="session-audit-blocked">${esc(blocked)}</small>` : ""}</span>
      </label>`;
    }).join("") : `<div class="identity-state">${esc(window.t("maintenance.sessionsEmpty"))}</div>`;
  }

  renderSessionTasks(tasks) {
    const target = document.getElementById("session-task-list");
    if (!target) return;
    target.innerHTML = tasks.length ? tasks.map(task => {
      const total = Math.max(1, Number((task.source_session_ids || []).length));
      const done = Math.min(total, Number((task.result?.completed_sessions || []).length));
      const progress = task.status === "completed" ? 100 : Math.max(0, Math.min(100, done / total * 100));
      return `<div class="session-task-row">
      <span class="session-task-main"><strong>${esc(window.t(`maintenance.operation.${task.operation}`))}</strong><small>${(task.source_session_ids || []).map(esc).join(" · ")}</small><span class="maintenance-task-progress" aria-label="${progress.toFixed(0)}%"><span style="width:${progress}%"></span></span></span>
      <span class="status-badge status-${esc(task.status)}">${esc(task.status)}</span>
      <small class="session-task-detail">${esc(task.current_step || "")}${task.error ? ` · ${esc(task.error)}` : ""}</small>
      <span class="session-task-actions">${["completed", "failed", "cancelled"].includes(task.status) ? `<button class="session-task-delete" type="button" data-session-task-delete="${esc(task.task_uid)}" title="${esc(window.t("common.delete"))}" aria-label="${esc(window.t("common.delete"))}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2m-9 0 1 14h8l1-14M10 10v6m4-6v6"/></svg></button>` : ""}</span>
    </div>`;
    }).join("") : `<span class="text-tertiary">${esc(window.t("maintenance.noTasks"))}</span>`;
  }

  async deleteSessionTask(taskUid, button) {
    if (!taskUid || button?.disabled) return;
    if (button) button.disabled = true;
    try {
      await this.topicPage.api.post("sessions/maintenance/tasks/delete", { task_uid: taskUid });
      await this.loadSessionAudit();
      this.showToast(window.t("maintenance.taskDeleted"));
    } catch (error) {
      this.showToast(error.message || window.t("maintenance.taskDeleteFailed"), true);
      if (button) button.disabled = false;
    }
  }

  async clearSessionTasks() {
    const confirmed = await this.confirmDialog.show({
      title: window.t("maintenance.clearTasksTitle"),
      message: window.t("maintenance.clearTasksMessage"),
      confirmLabel: window.t("common.clear"),
    });
    if (!confirmed) return;
    const button = document.getElementById("session-task-clear");
    button.disabled = true;
    try {
      await this.topicPage.api.post("sessions/maintenance/tasks/clear", {});
      await this.loadSessionAudit();
      this.showToast(window.t("maintenance.tasksCleared"));
    } catch (error) {
      this.showToast(error.message || window.t("maintenance.tasksClearFailed"), true);
    } finally {
      button.disabled = false;
    }
  }

  async loadRecentRecalls() {
    const target = document.getElementById("recent-recall-list");
    if (!target) return;
    try {
      const data = await this.topicPage.api.get("recall/traces", { type: "production", limit: 100 });
      const toggle = document.getElementById("recall-trace-enabled");
      if (toggle) toggle.checked = Boolean(data.production_enabled);
      const items = data.items || [];
      target.innerHTML = items.length ? items.map(item => `
        <div class="recent-recall-row">
          <details class="recall-trace-record" data-recall-trace-uid="${esc(item.trace_uid)}">
            <summary><span><strong>${esc(item.query_text || "--")}</strong><small>${esc(item.session_id || "--")} · ${Number(item.result_count || 0)} 条 · ${new Date(Number(item.created_at || 0) * 1000).toLocaleString()}</small></span><span class="status-badge status-${esc(item.status)}">${esc(item.status)}</span></summary>
            <div class="recall-trace-detail">${esc(window.t("maintenance.expandToLoad"))}</div>
          </details>
          <div class="recent-recall-actions">
            <button class="btn btn-secondary btn-sm" type="button" data-recent-recall-action="export" data-trace-uid="${esc(item.trace_uid)}">${esc(window.t("common.export"))}</button>
            <button class="btn btn-danger btn-sm" type="button" data-recent-recall-action="delete" data-trace-uid="${esc(item.trace_uid)}">${esc(window.t("common.delete"))}</button>
          </div>
        </div>`).join("") : `<span class="text-tertiary">暂无实际召回记录</span>`;
    } catch (error) {
      target.innerHTML = `<span class="text-tertiary">${esc(error.message || "加载失败")}</span>`;
    }
  }

  async loadRecentRecallDetail(details) {
    const target = details.querySelector(".recall-trace-detail");
    if (!target || target.dataset.loaded === "true") return;
    try {
      const record = await this.topicPage.api.get("recall/traces/detail", { trace_uid: details.dataset.recallTraceUid });
      const injection = record.injection || {};
      const exactInjection = Array.isArray(injection.messages) && injection.messages.length
        ? JSON.stringify(injection.messages, null, 2)
        : injection.content || "无注入内容";
      target.innerHTML = `
        <div class="recall-trace-summary"><span>模式：${esc(injection.actual_method || record.mode || "--")}</span><span>耗时：${Number(record.elapsed_ms || 0).toFixed(0)} ms</span>${record.error ? `<span class="text-danger">${esc(record.error)}</span>` : ""}</div>
        <details class="recall-trace-section"><summary>真实注入内容</summary><pre>${esc(exactInjection)}</pre></details>
        <details class="recall-trace-section"><summary>原始触发</summary><pre>${esc(JSON.stringify(record.request || {}, null, 2))}</pre></details>
        <details class="recall-trace-section"><summary>入选记忆</summary><pre>${esc(JSON.stringify(record.result || {}, null, 2))}</pre></details>
        <details class="recall-trace-section"><summary>完整诊断</summary><pre>${esc(JSON.stringify(record.diagnostics || {}, null, 2))}</pre></details>`;
      target.dataset.loaded = "true";
    } catch (error) {
      target.textContent = error.message || "加载失败";
    }
  }

  async handleRecentRecallAction(action, traceUid, button) {
    if (!traceUid || button.disabled) return;
    button.disabled = true;
    try {
      if (action === "delete") {
        await this.topicPage.api.post("recall/traces/delete", { trace_uid: traceUid, type: "production" });
        await this.loadRecentRecalls();
        this.showToast(window.t("maintenance.recallDeleted"));
        return;
      }
      if (action === "export") {
        const record = await this.topicPage.api.get("recall/traces/detail", { trace_uid: traceUid });
        await this.recallPage.exportJson(record);
      }
    } catch (error) {
      this.showToast(error.message || window.t("maintenance.recallActionFailed"), true);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }

  async setRecallTraceEnabled(enabled) {
    try {
      await this.topicPage.api.post("recall/traces/settings", { production_enabled: enabled });
      this.showToast(enabled ? "已开启实际召回记录" : "已关闭实际召回记录");
    } catch (error) {
      const toggle = document.getElementById("recall-trace-enabled");
      if (toggle) toggle.checked = !enabled;
      this.showToast(error.message || "保存失败", true);
    }
  }

  async clearRecentRecalls() {
    const confirmed = await this.confirmDialog.show({
      title: window.t("maintenance.clearRecallsTitle"),
      message: window.t("maintenance.clearRecallsMessage"),
      confirmLabel: window.t("common.clear"),
    });
    if (!confirmed) return;
    const button = document.getElementById("recent-recall-clear");
    button.disabled = true;
    try {
      await this.topicPage.api.post("recall/traces/clear", { type: "production" });
      await this.loadRecentRecalls();
      this.showToast(window.t("maintenance.recallsCleared"));
    } catch (error) {
      this.showToast(error.message || window.t("maintenance.recallsClearFailed"), true);
    } finally {
      button.disabled = false;
    }
  }

  openSessionMaintenance() {
    const selected = this.selectedSessionIds();
    if (!selected.length) return this.showToast(window.t("maintenance.selectSessions"), true);
    this.sessionPreview = null;
    const selection = document.getElementById("session-maintenance-selection");
    selection.innerHTML = selected.map(id => `<span class="topic-actor-chip">${esc(id)}</span>`).join("");
    const canonical = document.getElementById("session-maintenance-canonical");
    canonical.innerHTML = selected.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join("");
    document.getElementById("session-maintenance-preview").innerHTML = "";
    document.getElementById("session-maintenance-force").checked = false;
    document.getElementById("session-maintenance-submit").textContent = window.t("maintenance.previewChanges");
    this.resetSessionPreview();
    const overlay = document.getElementById("session-maintenance-overlay");
    overlay.classList.add("visible");
    overlay.setAttribute("aria-hidden", "false");
  }

  resetSessionPreview() {
    this.sessionPreview = null;
    const merge = document.getElementById("session-maintenance-operation")?.value === "merge_aliases";
    document.getElementById("session-canonical-field")?.classList.toggle("hidden", !merge);
    document.getElementById("session-force-field")?.classList.add("hidden");
    const preview = document.getElementById("session-maintenance-preview");
    if (preview) preview.innerHTML = "";
    const submit = document.getElementById("session-maintenance-submit");
    if (submit) submit.textContent = window.t("maintenance.previewChanges");
  }

  closeSessionMaintenance() {
    const overlay = document.getElementById("session-maintenance-overlay");
    overlay?.classList.remove("visible");
    overlay?.setAttribute("aria-hidden", "true");
    this.sessionPreview = null;
  }

  sessionMaintenancePayload() {
    const operation = document.getElementById("session-maintenance-operation").value;
    return {
      operation,
      session_ids: this.selectedSessionIds(),
      canonical_session_id: operation === "merge_aliases" ? document.getElementById("session-maintenance-canonical").value : null,
    };
  }

  async submitSessionMaintenance() {
    const submit = document.getElementById("session-maintenance-submit");
    try {
      if (!this.sessionPreview) {
        const payload = this.sessionMaintenancePayload();
        this.sessionPreview = await this.topicPage.api.post("sessions/maintenance/preview", payload);
        const blocked = this.sessionPreview.blocked_reasons || [];
        const warnings = this.sessionPreview.warnings || [];
        const impacts = (this.sessionPreview.items || []).map(item => {
          const messages = this.sessionPreview.operation === "cleanup_summarized"
            ? Number(item.eligible_message_count || 0)
            : Number(item.message_count || 0);
          return `<div class="session-preview-impact"><code>${esc(item.session_id)}</code><small>${esc(window.t("maintenance.previewCounts", messages, Number(item.timeline_count || 0), Number(item.topic_count || 0), Number(item.raw_evidence_reference_count || 0)))}</small></div>`;
        }).join("");
        document.getElementById("session-maintenance-preview").innerHTML = `<strong>${esc(window.t("maintenance.changePreview"))}</strong><p>${esc(window.t("maintenance.sessionImpact", (this.sessionPreview.items || []).length))}</p><div class="session-preview-impacts">${impacts}</div>${warnings.map(item => `<div class="session-preview-warning">${esc(item)}</div>`).join("")}${blocked.map(item => `<div class="session-preview-blocked">${esc(item)}</div>`).join("")}`;
        document.getElementById("session-force-field").classList.toggle("hidden", !this.sessionPreview.requires_force);
        submit.textContent = window.t("maintenance.confirmExecute");
        return;
      }
      const force = document.getElementById("session-maintenance-force").checked;
      if (this.sessionPreview.requires_force && !force) throw new Error(window.t("maintenance.forceRequired"));
      submit.disabled = true;
      const task = await this.topicPage.api.post("sessions/maintenance/start", { ...this.sessionMaintenancePayload(), confirmed: true, force });
      this.showToast(window.t("maintenance.taskStarted"));
      await this.pollSessionTask(task.task_uid);
      this.closeSessionMaintenance();
      await this.loadSessionAudit();
    } catch (error) {
      this.showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  }

  async pollSessionTask(taskUid) {
    for (let attempt = 0; attempt < 300; attempt += 1) {
      const task = await this.topicPage.api.get("sessions/maintenance/task", { task_uid: taskUid });
      const total = Math.max(1, Number((task.source_session_ids || []).length));
      const done = Math.min(total, Number((task.result?.completed_sessions || []).length));
      const progress = task.status === "completed" ? 100 : Math.max(0, Math.min(100, done / total * 100));
      document.getElementById("session-maintenance-preview").innerHTML = `<strong>${esc(window.t("maintenance.taskRunning"))}</strong><p>${esc(task.current_step || task.status)} · ${done} / ${total}</p><div class="topic-progress-track"><span style="width:${progress}%"></span></div>${task.error ? `<div class="session-preview-blocked">${esc(task.error)}</div>` : ""}`;
      if (task.status === "completed") return task;
      if (task.status === "failed") throw new Error(task.error || window.t("maintenance.taskFailed"));
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    throw new Error(window.t("maintenance.taskTimeout"));
  }

  reviewSpace() {
    return document.getElementById("maintenance-topic-space")?.value || "";
  }

  async openReviews() {
    if (!this.reviewSpace()) return this.showToast(window.t("topic.chooseSpace"), true);
    const overlay = document.getElementById("topic-review-overlay");
    overlay?.classList.add("visible");
    overlay?.setAttribute("aria-hidden", "false");
    document.getElementById("topic-review-close")?.focus();
    await this.loadReviews();
  }

  closeReviews() {
    const overlay = document.getElementById("topic-review-overlay");
    overlay?.classList.remove("visible");
    overlay?.setAttribute("aria-hidden", "true");
  }

  async loadReviews() {
    const space = this.reviewSpace();
    const list = document.getElementById("maintenance-review-list");
    const detail = document.getElementById("maintenance-review-detail");
    if (!space) {
      list.innerHTML = `<div class="identity-state">${esc(window.t("maintenance.chooseReviewSpace"))}</div>`;
      detail.innerHTML = `<div class="identity-state">${esc(window.t("maintenance.chooseReview"))}</div>`;
      return;
    }
    list.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
    try {
      const data = await this.topicPage.api.get("topics/reviews", { memory_space_id: space });
      const items = data.items || [];
      this.setTopicMaintenanceCount("maintenance-topic-review-count", Number(data.total ?? items.length));
      list.innerHTML = items.length ? items.map(item => {
        const details = item.details || {};
        const title = details.proposed_title
          || item.candidate_titles?.[0]?.title
          || (item.review_type === "deleted_timeline_source_repair" ? window.t("maintenance.sourceRepairTitle") : window.t("maintenance.reviewUnknown"));
        return `<button class="maintenance-review-item${this.reviewUid === item.review_uid ? " active" : ""}" type="button" data-review-uid="${esc(item.review_uid)}">
          <strong>${esc(title)}</strong><span>${esc(window.t(`maintenance.reviewType.${item.review_type}`))}</span>
          <small>Timeline ${(item.timeline_uids || []).length} · Topic ${(item.topic_uids || []).length}</small>
        </button>`;
      }).join("") : `<div class="identity-state">${esc(window.t("maintenance.reviewEmpty"))}</div>`;
      if (this.reviewUid && items.some(item => item.review_uid === this.reviewUid)) {
        await this.loadReviewDetail(this.reviewUid);
      } else {
        this.reviewUid = null;
        detail.innerHTML = `<div class="identity-state">${esc(window.t("maintenance.chooseReview"))}</div>`;
      }
    } catch (error) {
      list.innerHTML = `<div class="identity-state identity-state-error">${esc(error.message)}</div>`;
    }
  }

  async loadReviewDetail(reviewUid) {
    this.reviewUid = reviewUid;
    const detail = document.getElementById("maintenance-review-detail");
    detail.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
    try {
      const data = await this.topicPage.api.get("topics/reviews/detail", { review_uid: reviewUid });
      const review = data.review || {};
      const fragments = review.fragments || [];
      const candidates = review.candidate_topics || [];
      const timelines = review.timelines || [];
      const canMaterialize = review.review_type === "ambiguous_topic_match";
      const canSyncSources = review.review_type === "deleted_timeline_source_repair";
      const reviewTitle = review.details?.proposed_title
        || (canSyncSources ? window.t("maintenance.sourceRepairTitle") : window.t("maintenance.reviewUnknown"));
      detail.innerHTML = `
        <div class="maintenance-review-detail-head"><div><h3>${esc(reviewTitle)}</h3><p>${esc(review.details?.proposed_summary || (canSyncSources ? window.t("maintenance.sourceRepairDescription") : ""))}</p></div><code>${esc(review.review_uid)}</code></div>
        <section><strong>${esc(window.t("maintenance.sourceFragments"))} (${fragments.length})</strong>
          <div class="maintenance-review-fragments">${fragments.map(fragment => `<details><summary>${esc(fragment.label)} · ${esc(fragment.summary)}</summary>${this.renderTimeRange(fragment.started_at, fragment.ended_at)}<div>${(fragment.facts || []).map(fact => `<p>${esc(fact.content || "")}</p>`).join("")}</div></details>`).join("") || `<span class="text-tertiary">${esc(window.t("topic.none"))}</span>`}</div>
        </section>
        <section><strong>${esc(window.t("maintenance.sourceTimelines"))} (${timelines.length})</strong>
          <div class="maintenance-review-timelines">${timelines.map(timeline => `<div class="maintenance-review-timeline"><p>${esc(timeline.preview || window.t("topic.timelineUnavailable"))}</p>${this.renderTimeRange(timeline.started_at, timeline.ended_at)}<details class="topic-compact-details"><summary>${esc(window.t("topic.identifierDetails"))}</summary><code>${esc(timeline.timeline_uid)}</code><code>Document ${Number(timeline.document_id || 0)} · r${Number(timeline.revision || 0)}</code></details></div>`).join("") || `<span class="text-tertiary">${esc(window.t("topic.none"))}</span>`}</div>
        </section>
        <section><strong>${esc(window.t("maintenance.candidateTopics"))} (${candidates.length})</strong>
          <div class="maintenance-review-candidates">${candidates.map((topic, index) => `<label><input type="radio" name="review-target-topic" value="${esc(topic.topic_uid)}" ${index === 0 ? "checked" : ""}><span><strong>${esc(topic.title)}</strong><small>${Number(topic.score || 0).toFixed(3)} · r${topic.revision}${this.inlineTimeRange(topic.started_at, topic.ended_at)}</small><p>${esc(topic.summary)}</p>${this.renderActorChips(topic.participants || [])}<details><summary>${esc(window.t("topic.actorFacts"))} (${(topic.facts || []).length})</summary>${(topic.facts || []).map(fact => `<div>${esc(fact.content)}</div>`).join("")}</details></span></label>`).join("") || `<span class="text-tertiary">${esc(window.t("topic.none"))}</span>`}</div>
        </section>
        <div class="maintenance-review-actions">
          ${canMaterialize ? `<button class="btn btn-primary" type="button" data-review-action="merge" ${candidates.length ? "" : "disabled"}>${esc(window.t("maintenance.mergeCandidate"))}</button><button class="btn btn-secondary" type="button" data-review-action="new">${esc(window.t("maintenance.createTopic"))}</button>` : ""}
          ${canSyncSources ? `<button class="btn btn-primary" type="button" data-review-action="sync_sources">${esc(window.t("maintenance.syncSourceRepair"))}</button>` : ""}
          <button class="btn btn-secondary" type="button" data-review-action="defer">${esc(window.t("maintenance.defer"))}</button>
          <button class="btn btn-ghost" type="button" data-review-action="ignore">${esc(window.t("maintenance.ignore"))}</button>
        </div>`;
      document.querySelectorAll("#maintenance-review-list [data-review-uid]").forEach(item => item.classList.toggle("active", item.dataset.reviewUid === reviewUid));
    } catch (error) {
      detail.innerHTML = `<div class="identity-state identity-state-error">${esc(error.message)}</div>`;
    }
  }

  async resolveReview(action) {
    if (!this.reviewUid) return;
    const target = document.querySelector('input[name="review-target-topic"]:checked')?.value || "";
    try {
      await this.topicPage.api.post("topics/reviews/resolve", {
        review_uid: this.reviewUid,
        action,
        target_topic_uid: target,
      });
      this.showToast(window.t("maintenance.reviewApplied"));
      this.reviewUid = null;
      await this.topicPage.fetch();
      this.syncTopicSpaces();
      await this.loadReviews();
    } catch (error) {
      this.showToast(error.message || window.t("maintenance.reviewFailed"), true);
    }
  }

  async openGovernance() {
    const space = this.reviewSpace();
    if (!space) return this.showToast(window.t("topic.chooseSpace"), true);
    const overlay = document.getElementById("topic-governance-overlay");
    const body = document.getElementById("topic-governance-body");
    body.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
    overlay.classList.add("visible");
    overlay.setAttribute("aria-hidden", "false");
    try {
      const data = await this.topicPage.api.get("topics", { memory_space_id: space, limit: 500 });
      this.governance = { space, mode: "merge", topics: data.items || [], preview: null, groupCount: 2, confirmed: false };
      this.renderGovernance();
    } catch (error) {
      body.innerHTML = `<div class="identity-state identity-state-error">${esc(error.message)}</div>`;
    }
  }

  renderGovernance() {
    const state = this.governance;
    if (!state) return;
    const body = document.getElementById("topic-governance-body");
    const topicOptions = state.topics.map(topic => `<option value="${esc(topic.topic_uid)}">${esc(topic.title)} · r${topic.revision}</option>`).join("");
    const mode = `<div class="governance-mode"><label><input type="radio" name="governance-mode" value="merge" ${state.mode === "merge" ? "checked" : ""}>${esc(window.t("maintenance.mergeTopics"))}</label><label><input type="radio" name="governance-mode" value="split" ${state.mode === "split" ? "checked" : ""}>${esc(window.t("maintenance.splitTopic"))}</label></div>`;
    if (state.mode === "merge") {
      body.innerHTML = `${mode}<p class="text-secondary">${esc(window.t("maintenance.mergeHelp"))}</p><div class="governance-topic-list">${state.topics.map(topic => `<label><input type="checkbox" data-governance-merge-topic value="${esc(topic.topic_uid)}"><span><strong>${esc(topic.title)}</strong><small>${esc(topic.topic_uid)} · r${topic.revision}</small></span><input type="radio" name="governance-main-topic" value="${esc(topic.topic_uid)}" title="${esc(window.t("maintenance.retainUid"))}"></label>`).join("")}</div>${this.renderGovernanceConfirmation()}`;
    } else {
      body.innerHTML = `${mode}<label class="identity-field"><span>${esc(window.t("maintenance.splitSource"))}</span><select class="input" id="governance-split-topic"><option value="">${esc(window.t("topic.chooseSpace"))}</option>${topicOptions}</select></label><div id="governance-split-fragments">${state.preview ? this.renderSplitFragments() : ""}</div>${this.renderGovernanceConfirmation()}`;
      if (state.selectedTopic) document.getElementById("governance-split-topic").value = state.selectedTopic;
    }
    document.getElementById("topic-governance-submit").textContent = state.confirmed ? window.t("maintenance.confirmExecute") : window.t("maintenance.previewChanges");
  }

  renderSplitFragments() {
    const state = this.governance;
    return `<div class="governance-fragment-head"><strong>${esc(window.t("maintenance.assignFragments"))}</strong><button class="btn btn-ghost btn-sm" type="button" data-governance-add-group>${esc(window.t("maintenance.addGroup"))}</button></div><div class="governance-fragment-list">${(state.preview?.fragments || []).map(fragment => `<label><span><strong>${esc(fragment.label)}</strong><small>${esc(fragment.summary)}</small></span><select class="input input-sm" data-governance-fragment="${esc(fragment.fragment_uid)}">${Array.from({ length: state.groupCount }, (_, index) => `<option value="${index}">${index === 0 ? esc(window.t("maintenance.mainGroup")) : `${esc(window.t("maintenance.newGroup"))} ${index}`}</option>`).join("")}</select></label>`).join("")}</div>`;
  }

  renderGovernanceConfirmation() {
    const state = this.governance;
    if (!state?.confirmed || !state.preview) return "";
    const preview = state.preview;
    const topics = preview.topics || [];
    const fragments = preview.fragments || [];
    return `<div class="governance-confirm"><strong>${esc(window.t("maintenance.changePreview"))}</strong><p>${esc(window.t("maintenance.governancePreview", preview.topic_count || 0, preview.fragment_count || 0, preview.timeline_count || 0))}</p><p>${esc(window.t("maintenance.relationPreview", preview.relation_count || 0))}</p><small>${esc(window.t("maintenance.atomicWarning"))}</small>
      <details class="governance-preview-details"><summary>${esc(window.t("maintenance.previewTopics"))} (${topics.length})</summary>
        <div class="governance-preview-topics">${topics.map(topic => `<div><strong>${esc(topic.title)}</strong><small>${esc(topic.topic_uid)} · r${Number(topic.revision || 0)}</small>${this.renderActorChips([...(topic.participants || []), ...(topic.mentioned_actors || [])])}</div>`).join("")}</div>
      </details>
      <details class="governance-preview-details"><summary>${esc(window.t("maintenance.sourceFragments"))} (${fragments.length})</summary>
        <div class="governance-preview-fragments">${fragments.map(fragment => {
          const actors = [...(fragment.participant_refs || []), ...(fragment.mentioned_actor_refs || [])];
          return `<div><strong>${esc(fragment.label)}</strong><p>${esc(fragment.summary)}</p>${this.renderActorChips(actors)}<details><summary>${esc(window.t("maintenance.previewFacts"))} (${(fragment.facts || []).length})</summary>${(fragment.facts || []).map(fact => `<div class="governance-preview-fact">${esc(fact.content || fact.text || "")}</div>`).join("")}</details><small>${esc(window.t("maintenance.previewTimelines"))}: ${(fragment.timeline_uids || []).map(esc).join(", ")}</small></div>`;
        }).join("")}</div>
      </details>
    </div>`;
  }

  renderActorChips(actors) {
    const unique = new Map();
    (actors || []).forEach(actor => {
      const key = String(actor.actor_id || actor.actor_ref || actor.display_name || actor.name || "").trim();
      if (!key) return;
      const names = Array.isArray(actor.display_names) ? actor.display_names : [];
      const label = actor.display_name || actor.name || names[0] || key;
      unique.set(key, label);
    });
    if (!unique.size) return "";
    return `<div class="governance-actor-chips">${Array.from(unique.values()).map(label => `<span class="topic-actor-chip">${esc(label)}</span>`).join("")}</div>`;
  }

  formatTimestamp(value) {
    const numeric = Number(value || 0);
    if (!numeric) return "";
    const date = new Date(numeric * 1000);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
  }

  inlineTimeRange(startedAt, endedAt) {
    const start = this.formatTimestamp(startedAt);
    const end = this.formatTimestamp(endedAt);
    if (!start && !end) return "";
    return ` · ${esc(start && end && start !== end ? `${start} - ${end}` : start || end)}`;
  }

  renderTimeRange(startedAt, endedAt) {
    const value = this.inlineTimeRange(startedAt, endedAt).replace(/^ · /, "");
    return value ? `<small class="maintenance-review-time">${esc(window.t("maintenance.timeRange"))}: ${value}</small>` : "";
  }

  async handleGovernanceChange(event) {
    const state = this.governance;
    if (!state) return;
    if (event.target.name === "governance-mode") {
      state.mode = event.target.value;
      state.preview = null;
      state.confirmed = false;
      state.pendingPayload = null;
      this.renderGovernance();
      return;
    }
    if (event.target.id === "governance-split-topic") {
      state.selectedTopic = event.target.value;
      state.confirmed = false;
      state.pendingPayload = null;
      if (!state.selectedTopic) return;
      try {
        state.preview = await this.topicPage.api.post("topics/governance/preview", { memory_space_id: state.space, operation: "split", topic_uid: state.selectedTopic });
        this.renderGovernance();
      } catch (error) {
        this.showToast(error.message, true);
      }
      return;
    }
    state.confirmed = false;
    state.pendingPayload = null;
  }

  addGovernanceGroup() {
    if (!this.governance || this.governance.groupCount >= 8) return;
    const assignments = this.splitAssignments();
    this.governance.groupCount += 1;
    document.getElementById("governance-split-fragments").innerHTML = this.renderSplitFragments();
    Object.entries(assignments).forEach(([uid, group]) => {
      const select = document.querySelector(`[data-governance-fragment="${CSS.escape(uid)}"]`);
      if (select) select.value = group;
    });
  }

  splitAssignments() {
    return Object.fromEntries(Array.from(document.querySelectorAll("[data-governance-fragment]")).map(select => [select.dataset.governanceFragment, Number(select.value)]));
  }

  governancePayload() {
    const state = this.governance;
    if (state.mode === "merge") {
      const topicUids = Array.from(document.querySelectorAll("[data-governance-merge-topic]:checked")).map(input => input.value);
      const mainTopicUid = document.querySelector('input[name="governance-main-topic"]:checked')?.value || topicUids[0] || "";
      if (mainTopicUid && !topicUids.includes(mainTopicUid)) throw new Error(window.t("maintenance.mainMustSelected"));
      return { memory_space_id: state.space, operation: "merge", topic_uids: topicUids, main_topic_uid: mainTopicUid };
    }
    const assignments = this.splitAssignments();
    const groups = Array.from({ length: state.groupCount }, () => []);
    Object.entries(assignments).forEach(([uid, group]) => groups[group]?.push(uid));
    return { memory_space_id: state.space, operation: "split", topic_uid: state.selectedTopic, fragment_groups: groups.filter(group => group.length) };
  }

  async submitGovernance() {
    const state = this.governance;
    if (!state) return;
    try {
      const payload = state.confirmed && state.pendingPayload
        ? state.pendingPayload
        : this.governancePayload();
      if (!state.confirmed) {
        if (state.mode === "merge" && (payload.topic_uids || []).length < 2) throw new Error(window.t("maintenance.mergeNeedsTopics"));
        if (state.mode === "merge") state.preview = await this.topicPage.api.post("topics/governance/preview", payload);
        if (state.mode === "split" && (!payload.fragment_groups || payload.fragment_groups.length < 2)) throw new Error(window.t("maintenance.splitNeedsGroups"));
        state.pendingPayload = payload;
        state.confirmed = true;
        this.renderGovernance();
        return;
      }
      await this.topicPage.api.post("topics/governance/execute", { ...payload, confirmed: true });
      this.showToast(window.t("maintenance.governanceCompleted"));
      this.closeGovernance();
      await this.topicPage.fetch();
      this.syncTopicSpaces();
      await this.loadReviews();
    } catch (error) {
      this.showToast(error.message || window.t("maintenance.governanceFailed"), true);
    }
  }

  closeGovernance() {
    document.getElementById("topic-governance-overlay")?.classList.remove("visible");
    document.getElementById("topic-governance-overlay")?.setAttribute("aria-hidden", "true");
    this.governance = null;
  }
}
