import { esc } from "./utils.js";

export class TopicPage {
  constructor(api, showToast) {
    this.api = api;
    this.showToast = showToast;
    this.pollTimer = null;
    this.pollInFlight = false;
    this.activeJobUid = null;
    this.buildEnabled = false;
    this.topicCount = 0;
    this.detailRequestId = 0;
    this.detailTrigger = null;
    this.fullBuildTrigger = null;
    this.maintenanceTrigger = null;
    this.maintenanceRequestId = 0;
    this.maintenanceItems = [];
    this.discardBuildTrigger = null;
    this.discardRun = null;
    this.settingsTrigger = null;
    this.settingsData = null;
    this.settingsResetKeys = new Set();
    this.settingsResetAll = false;
  }

  initEventListeners() {
    document.getElementById("topic-refresh")?.addEventListener("click", () => this.fetch());
    document.getElementById("topic-space")?.addEventListener("change", () => this.fetch());
    document.getElementById("topic-build-full")?.addEventListener("click", event => this.requestFullBuild(event.currentTarget));
    document.getElementById("topic-maintenance")?.addEventListener("click", event => this.openMaintenance(event.currentTarget));
    document.getElementById("topic-settings")?.addEventListener("click", event => this.openSettings(event.currentTarget));
    document.getElementById("topic-settings-close")?.addEventListener("click", () => this.closeSettings());
    document.getElementById("topic-settings-cancel")?.addEventListener("click", () => this.closeSettings());
    document.getElementById("topic-settings-save")?.addEventListener("click", () => this.saveSettings());
    document.getElementById("topic-settings-reset-all")?.addEventListener("click", () => this.resetAllSettings());
    document.getElementById("topic-settings-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.closeSettings();
    });
    document.getElementById("topic-maintenance-close")?.addEventListener("click", () => this.closeMaintenance());
    document.getElementById("topic-maintenance-cancel")?.addEventListener("click", () => this.closeMaintenance());
    document.getElementById("topic-maintenance-detect")?.addEventListener("click", () => this.detectUnindexedTimelines());
    document.getElementById("topic-maintenance-select-all")?.addEventListener("change", event => {
      document.querySelectorAll("#topic-maintenance-list .topic-maintenance-checkbox").forEach(input => {
        input.checked = event.currentTarget.checked;
      });
      this.updateMaintenanceSelection();
    });
    document.getElementById("topic-maintenance-submit")?.addEventListener("click", () => this.startSelectedMaintenance());
    document.getElementById("topic-maintenance-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.closeMaintenance();
    });
    document.getElementById("topic-detail-close")?.addEventListener("click", () => this.closeDetail());
    document.getElementById("topic-detail-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.closeDetail();
    });
    document.getElementById("topic-full-build-confirm-close")?.addEventListener("click", () => this.closeFullBuildConfirm());
    document.getElementById("topic-full-build-confirm-cancel")?.addEventListener("click", () => this.closeFullBuildConfirm());
    document.querySelectorAll('input[name="topic-full-build-mode"]').forEach(input => {
      input.addEventListener("change", () => this.updateFullBuildChoice());
    });
    document.getElementById("topic-full-build-confirm-submit")?.addEventListener("click", () => {
      const resetTopics = document.querySelector('input[name="topic-full-build-mode"]:checked')?.value === "clear";
      this.closeFullBuildConfirm({ restoreFocus: false });
      this.startBuild("full", { resetTopics });
    });
    document.getElementById("topic-full-build-confirm-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.closeFullBuildConfirm();
    });
    document.getElementById("topic-discard-build-close")?.addEventListener("click", () => this.closeDiscardBuildConfirm());
    document.getElementById("topic-discard-build-cancel")?.addEventListener("click", () => this.closeDiscardBuildConfirm());
    document.getElementById("topic-discard-build-submit")?.addEventListener("click", () => this.discardBuild());
    document.getElementById("topic-discard-build-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.closeDiscardBuildConfirm();
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      if (this.discardBuildConfirmIsOpen()) this.closeDiscardBuildConfirm();
      else if (this.fullBuildConfirmIsOpen()) this.closeFullBuildConfirm();
      else if (this.settingsIsOpen()) this.closeSettings();
      else if (this.maintenanceIsOpen()) this.closeMaintenance();
      else if (this.detailIsOpen()) this.closeDetail();
    });
  }

  currentSpace() {
    return document.getElementById("topic-space")?.value || "";
  }

  async fetch() {
    try {
      const data = await this.api.get("topics/overview", { memory_space_id: this.currentSpace() });
      this.renderOverview(data);
      await this.fetchTopics();
    } catch (e) {
      this.showToast(e.message || "Topic 数据加载失败", true);
    }
  }

  renderOverview(data) {
    const spaces = data.memory_spaces || [];
    const activeJobs = data.active_jobs || [];
    const resumableRun = data.resumable_run || null;
    const select = document.getElementById("topic-space");
    let selected = select.value;
    if (!selected && activeJobs.length) {
      selected = activeJobs[0].memory_space_id || "";
    } else if (!selected && resumableRun) {
      selected = resumableRun.memory_space_id || "";
    }
    select.innerHTML = `<option value="">${esc(window.t("topic.space"))}</option>` + spaces.map(item =>
      `<option value="${esc(item.memory_space_id)}">${esc(item.memory_space_id)} · ${item.timeline_count} Timeline / ${item.topic_count} Topic</option>`
    ).join("");
    if (spaces.some(item => item.memory_space_id === selected)) select.value = selected;
    this.buildEnabled = Boolean(data.enabled);
    this.topicCount = Math.max(0, Number(data.topic_count || 0));
    document.getElementById("topic-total").textContent = this.topicCount;
    document.getElementById("topic-active").textContent = data.status_counts?.active || 0;
    document.getElementById("topic-atoms").textContent = data.atom_count || 0;
    document.getElementById("topic-links").textContent = data.timeline_link_count || 0;
    document.getElementById("topic-relations").textContent = data.relation_count || 0;
    const flags = document.getElementById("topic-flags");
    flags.textContent = [
      window.t(data.enabled ? "topic.buildEnabled" : "topic.buildDisabled"),
      window.t(data.auto_maintenance ? "topic.autoOn" : "topic.autoOff"),
      data.rerank_available
        ? `${window.t("topic.rerankOn")} (${data.rerank_backend || "configured"})`
        : window.t("topic.rerankOff"),
    ].join(" · ");

    const activeJob = activeJobs[0] || null;
    const settingsButton = document.getElementById("topic-settings");
    if (settingsButton) settingsButton.disabled = Boolean(activeJob);
    this.setBuildButtonsDisabled(
      !this.currentSpace() || !this.buildEnabled || Boolean(activeJob)
    );
    if (activeJob) {
      this.renderProgress(activeJob);
      this.resumePolling(activeJob.job_uid);
    } else {
      this.stopPolling();
      if (resumableRun && resumableRun.memory_space_id === this.currentSpace()) {
        this.renderProgress(resumableRun);
      } else {
        document.getElementById("topic-progress")?.classList.add("hidden");
      }
    }
  }

  async fetchTopics() {
    const space = this.currentSpace();
    const body = document.getElementById("topics-body");
    this.closeDetail({ restoreFocus: false });
    if (!space) {
      body.innerHTML = `<tr><td colspan="6" class="table-empty">${esc(window.t("topic.chooseSpace"))}</td></tr>`;
      return;
    }
    try {
      const data = await this.api.get("topics", { memory_space_id: space, limit: 200 });
      const items = data.items || [];
      body.innerHTML = items.length ? items.map(item => `
        <tr class="topic-row" data-topic-uid="${esc(item.topic_uid)}" tabindex="0" role="button" aria-label="${esc(`${window.t("topic.viewDetails")}: ${item.title}`)}">
          <td class="text-mono">${esc(item.topic_uid.slice(0, 12))}</td>
          <td class="topic-title-cell" title="${esc(item.title)}"><strong>${esc(item.title)}</strong></td>
          <td>${esc(item.status)}</td>
          <td>${Number(item.importance || 0).toFixed(2)}</td>
          <td>${item.support?.time_cluster_count || 0} / ${item.support?.timeline_count || 0}</td>
          <td>r${item.revision}</td>
      </tr>`).join("") : `<tr><td colspan="6" class="table-empty">${esc(window.t("topic.empty"))}</td></tr>`;
      body.querySelectorAll(".topic-row").forEach(row => {
        row.addEventListener("click", () => this.showDetail(row.dataset.topicUid));
        row.addEventListener("keydown", event => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          this.showDetail(row.dataset.topicUid);
        });
      });
    } catch (e) {
      body.innerHTML = `<tr><td colspan="6" class="table-empty">${esc(e.message)}</td></tr>`;
    }
  }

  async showDetail(topicUid) {
    const requestId = ++this.detailRequestId;
    const overlay = document.getElementById("topic-detail-overlay");
    const title = document.getElementById("topic-detail-title");
    const body = document.getElementById("topic-detail-body");
    this.detailTrigger = Array.from(document.querySelectorAll("#topics-body .topic-row"))
      .find(row => row.dataset.topicUid === topicUid) || null;
    document.querySelectorAll("#topics-body .topic-row").forEach(row => {
      row.classList.toggle("is-selected", row.dataset.topicUid === topicUid);
    });
    title.textContent = window.t("common.loading");
    body.innerHTML = `<div class="topic-detail-loading text-tertiary">${esc(window.t("common.loading"))}</div>`;
    overlay.classList.add("visible");
    overlay.setAttribute("aria-hidden", "false");
    document.getElementById("topic-detail-close")?.focus();

    try {
      const data = await this.api.get("topics/detail", { topic_uid: topicUid });
      if (requestId !== this.detailRequestId || !this.detailIsOpen()) return;
      const topic = data.topic;
      const provenance = data.provenance || {};
      const relatedTopics = data.related_topics || [];
      const sourcesByAtom = {};
      (provenance.atom_sources || []).forEach(source => {
        (sourcesByAtom[source.topic_atom_uid] ||= []).push(source);
      });
      title.innerHTML = `${esc(topic.title)} <span class="topic-detail-readonly text-tertiary">（${esc(window.t("topic.readOnly"))}）</span>`;
      body.innerHTML = `
        <p class="topic-detail-summary">${esc(topic.summary)}</p>
        <div class="topic-related-section">
          <strong>${esc(window.t("topic.relatedTopics"))} (${relatedTopics.length})</strong>
          <div class="topic-related-list">${relatedTopics.length ? relatedTopics.map(item => `
            <button class="topic-related-item" type="button" data-related-topic-uid="${esc(item.related_topic_uid)}">
              <span>${esc(item.related_title)}</span><small>${Number(item.semantic_similarity || 0).toFixed(3)}</small>
            </button>`).join("") : `<span class="text-tertiary">${esc(window.t("topic.none"))}</span>`}</div>
        </div>
        <div class="topic-detail-grid">
          <div><strong>${esc(window.t("topic.topicAtoms"))} (${(provenance.atoms || []).length})</strong>${(provenance.atoms || []).map(atom => `<div class="topic-source-item"><div>${esc(atom.content)}</div>${(sourcesByAtom[atom.atom_uid] || []).map(source => `<div class="text-tertiary text-mono">↳ ${esc(source.timeline_uid)} · ${esc(source.source_kind)} · ${esc(source.source_atom_fingerprint || "")}</div>`).join("")}</div>`).join("") || `<div class='text-tertiary'>${esc(window.t("topic.none"))}</div>`}</div>
          <div><strong>${esc(window.t("topic.sources"))} (${(provenance.links || []).length})</strong>${(provenance.links || []).map(link => `<div class="topic-source-item text-mono">${esc(link.timeline_uid)} · ${esc(link.time_cluster_key)}</div>`).join("") || `<div class='text-tertiary'>${esc(window.t("topic.none"))}</div>`}</div>
        </div>`;
      body.querySelectorAll("[data-related-topic-uid]").forEach(button => {
        button.addEventListener("click", () => this.showDetail(button.dataset.relatedTopicUid));
      });
    } catch (e) {
      if (requestId !== this.detailRequestId) return;
      this.closeDetail();
      this.showToast(e.message || "Topic 详情加载失败", true);
    }
  }

  detailIsOpen() {
    return document.getElementById("topic-detail-overlay")?.classList.contains("visible") || false;
  }

  closeDetail({ restoreFocus = true } = {}) {
    if (!this.detailIsOpen() && !this.detailTrigger) return;
    this.detailRequestId += 1;
    const overlay = document.getElementById("topic-detail-overlay");
    overlay?.classList.remove("visible");
    overlay?.setAttribute("aria-hidden", "true");
    document.querySelectorAll("#topics-body .topic-row").forEach(row => {
      row.classList.remove("is-selected");
    });
    const trigger = this.detailTrigger;
    this.detailTrigger = null;
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }

  async openSettings(trigger) {
    if (this.activeJobUid) return;
    this.settingsTrigger = trigger || null;
    this.settingsResetKeys = new Set();
    this.settingsResetAll = false;
    const overlay = document.getElementById("topic-settings-overlay");
    const status = document.getElementById("topic-settings-status");
    const content = document.getElementById("topic-settings-content");
    overlay?.classList.add("visible");
    overlay?.setAttribute("aria-hidden", "false");
    status.textContent = window.t("common.loading");
    content.innerHTML = "";
    try {
      this.settingsData = await this.api.get("topics/settings");
      this.renderSettings();
      document.getElementById("topic-settings-close")?.focus();
    } catch (e) {
      status.textContent = e.message || window.t("topic.settingsLoadFailed");
    }
  }

  settingsIsOpen() {
    return document.getElementById("topic-settings-overlay")?.classList.contains("visible") || false;
  }

  closeSettings({ restoreFocus = true } = {}) {
    if (!this.settingsIsOpen() && !this.settingsTrigger) return;
    const overlay = document.getElementById("topic-settings-overlay");
    overlay?.classList.remove("visible");
    overlay?.setAttribute("aria-hidden", "true");
    const trigger = this.settingsTrigger;
    this.settingsTrigger = null;
    this.settingsData = null;
    this.settingsResetKeys = new Set();
    this.settingsResetAll = false;
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }

  renderSettings() {
    const data = this.settingsData || {};
    const definitions = data.definitions || {};
    const effective = data.effective || {};
    const overrides = data.overrides || {};
    const status = document.getElementById("topic-settings-status");
    status.textContent = data.build_active ? window.t("topic.settingsBuildActive") : "";
    const categories = ["recall", "build", "performance"];
    const content = document.getElementById("topic-settings-content");
    content.innerHTML = categories.map(category => {
      const rows = Object.entries(definitions)
        .filter(([, definition]) => definition.category === category && !definition.deprecated)
        .map(([key, definition]) => {
          const customized = Object.prototype.hasOwnProperty.call(overrides, key);
          const value = effective[key];
          const input = definition.type === "bool"
            ? `<input class="topic-setting-input" data-setting-key="${esc(key)}" type="checkbox" ${value ? "checked" : ""}>`
            : `<input class="input topic-setting-input" data-setting-key="${esc(key)}" type="number" value="${esc(value)}" min="${esc(definition.min)}" max="${esc(definition.max)}" step="${esc(definition.step || 1)}">`;
          return `<div class="topic-setting-row" data-setting-row="${esc(key)}">
            <div class="topic-setting-copy"><strong>${esc(definition.label || key)}</strong><small class="text-tertiary text-mono">${esc(key)}</small></div>
            <div class="topic-setting-control">${input}<span class="topic-setting-source ${customized ? "is-custom" : ""}" data-setting-source>${esc(window.t(customized ? "topic.customValue" : "topic.defaultValue"))}</span><button class="btn btn-ghost btn-sm topic-setting-reset" type="button" data-reset-setting="${esc(key)}" ${customized ? "" : "disabled"}>${esc(window.t("topic.resetDefault"))}</button></div>
            <div class="topic-setting-default text-tertiary">${esc(window.t("topic.codeDefault"))}: ${esc(definition.default)}</div>
          </div>`;
        }).join("");
      return `<section class="topic-settings-section"><h3>${esc(window.t(`topic.settingsCategory.${category}`))}</h3>${rows}</section>`;
    }).join("");
    content.querySelectorAll("[data-reset-setting]").forEach(button => {
      button.addEventListener("click", () => this.resetSetting(button.dataset.resetSetting));
    });
    ["topic-settings-save", "topic-settings-reset-all"].forEach(id => {
      const button = document.getElementById(id);
      if (button) button.disabled = Boolean(data.build_active);
    });
  }

  resetSetting(key) {
    const definition = this.settingsData?.definitions?.[key];
    const input = document.querySelector(`.topic-setting-input[data-setting-key="${key}"]`);
    if (!definition || !input) return;
    if (definition.type === "bool") input.checked = Boolean(definition.default);
    else input.value = definition.default;
    this.settingsResetKeys.add(key);
    const row = document.querySelector(`[data-setting-row="${key}"]`);
    row?.querySelector("[data-setting-source]")?.classList.remove("is-custom");
    const source = row?.querySelector("[data-setting-source]");
    if (source) source.textContent = window.t("topic.defaultValue");
    const button = row?.querySelector("[data-reset-setting]");
    if (button) button.disabled = true;
  }

  resetAllSettings() {
    this.settingsResetAll = true;
    Object.keys(this.settingsData?.definitions || {}).forEach(key => this.resetSetting(key));
  }

  async saveSettings() {
    if (!this.settingsData || this.settingsData.build_active) return;
    const changes = {};
    try {
      document.querySelectorAll(".topic-setting-input[data-setting-key]").forEach(input => {
        const key = input.dataset.settingKey;
        const definition = this.settingsData.definitions[key];
        const value = definition.type === "bool" ? input.checked
          : definition.type === "int" ? Number.parseInt(input.value, 10)
          : Number.parseFloat(input.value);
        if (!Number.isFinite(value) && definition.type !== "bool") throw new Error(`${definition.label}: ${window.t("topic.invalidSetting")}`);
        if (value === definition.default) this.settingsResetKeys.add(key);
        else changes[key] = value;
      });
      const save = document.getElementById("topic-settings-save");
      save.disabled = true;
      const result = await this.api.post("topics/settings/update", {
        changes,
        reset_keys: Array.from(this.settingsResetKeys),
        reset_all: this.settingsResetAll,
      });
      this.settingsData = result;
      this.settingsResetKeys = new Set();
      this.settingsResetAll = false;
      this.renderSettings();
      this.showToast(window.t("topic.settingsSaved"));
    } catch (e) {
      this.showToast(e.message || window.t("topic.settingsSaveFailed"), true);
      const save = document.getElementById("topic-settings-save");
      if (save) save.disabled = false;
    }
  }

  openMaintenance(trigger) {
    if (!this.currentSpace() || !this.buildEnabled || this.activeJobUid) return;
    this.maintenanceTrigger = trigger || null;
    const overlay = document.getElementById("topic-maintenance-overlay");
    overlay?.classList.add("visible");
    overlay?.setAttribute("aria-hidden", "false");
    document.getElementById("topic-maintenance-close")?.focus();
    this.detectUnindexedTimelines();
  }

  maintenanceIsOpen() {
    return document.getElementById("topic-maintenance-overlay")?.classList.contains("visible") || false;
  }

  closeMaintenance({ restoreFocus = true } = {}) {
    if (!this.maintenanceIsOpen() && !this.maintenanceTrigger) return;
    const overlay = document.getElementById("topic-maintenance-overlay");
    overlay?.classList.remove("visible");
    overlay?.setAttribute("aria-hidden", "true");
    this.maintenanceRequestId += 1;
    this.maintenanceItems = [];
    const trigger = this.maintenanceTrigger;
    this.maintenanceTrigger = null;
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }

  async detectUnindexedTimelines() {
    if (!this.maintenanceIsOpen()) return;
    const requestId = ++this.maintenanceRequestId;
    const detectButton = document.getElementById("topic-maintenance-detect");
    const submitButton = document.getElementById("topic-maintenance-submit");
    const selection = document.getElementById("topic-maintenance-selection");
    const status = document.getElementById("topic-maintenance-status");
    const list = document.getElementById("topic-maintenance-list");
    detectButton.disabled = true;
    submitButton.disabled = true;
    selection.classList.add("hidden");
    status.textContent = window.t("topic.detectingUnindexed");
    list.innerHTML = `<div class="topic-maintenance-empty text-tertiary">${esc(window.t("common.loading"))}</div>`;
    try {
      const data = await this.api.get("topics/maintenance/unindexed", {
        memory_space_id: this.currentSpace(),
      });
      if (requestId !== this.maintenanceRequestId || !this.maintenanceIsOpen()) return;
      this.maintenanceItems = data.items || [];
      status.textContent = window.t("topic.unindexedDetected", Number(data.total || 0));
      if (!this.maintenanceItems.length) {
        list.innerHTML = `<div class="topic-maintenance-empty text-tertiary">${esc(window.t("topic.noUnindexed"))}</div>`;
        return;
      }
      selection.classList.remove("hidden");
      document.getElementById("topic-maintenance-select-all").checked = true;
      list.innerHTML = this.maintenanceItems.map(item => `
        <label class="topic-maintenance-item">
          <input class="topic-maintenance-checkbox" type="checkbox" value="${esc(item.timeline_uid)}" checked>
          <span class="topic-maintenance-item-content">
            <span class="topic-maintenance-item-meta">
              <strong class="text-mono">${esc(item.timeline_uid)}</strong>
              <small>r${Number(item.revision || 1)}${item.started_at ? ` · ${esc(this.formatTimestamp(item.started_at))}` : ""}</small>
            </span>
            <span class="topic-maintenance-item-summary">${esc(item.summary || window.t("topic.noTimelineSummary"))}</span>
          </span>
        </label>
      `).join("");
      list.querySelectorAll(".topic-maintenance-checkbox").forEach(input => {
        input.addEventListener("change", () => this.updateMaintenanceSelection());
      });
      this.updateMaintenanceSelection();
    } catch (e) {
      if (requestId !== this.maintenanceRequestId) return;
      this.maintenanceItems = [];
      status.textContent = e.message || window.t("topic.detectUnindexedFailed");
      list.innerHTML = "";
      this.showToast(e.message || window.t("topic.detectUnindexedFailed"), true);
    } finally {
      if (requestId === this.maintenanceRequestId) detectButton.disabled = false;
    }
  }

  updateMaintenanceSelection() {
    const checkboxes = Array.from(document.querySelectorAll("#topic-maintenance-list .topic-maintenance-checkbox"));
    const selected = checkboxes.filter(input => input.checked).length;
    const selectAll = document.getElementById("topic-maintenance-select-all");
    if (selectAll) {
      selectAll.checked = Boolean(checkboxes.length) && selected === checkboxes.length;
      selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
    }
    const selectedCount = document.getElementById("topic-maintenance-selected-count");
    if (selectedCount) selectedCount.textContent = window.t("topic.selectedUnindexed", selected, checkboxes.length);
    const submit = document.getElementById("topic-maintenance-submit");
    if (submit) submit.disabled = selected === 0 || Boolean(this.activeJobUid);
  }

  startSelectedMaintenance() {
    const timelineUids = Array.from(
      document.querySelectorAll("#topic-maintenance-list .topic-maintenance-checkbox:checked")
    ).map(input => input.value);
    if (!timelineUids.length || this.activeJobUid) return;
    this.closeMaintenance({ restoreFocus: false });
    this.startBuild("incremental", { timelineUids });
  }

  requestFullBuild(trigger) {
    if (!this.currentSpace() || !this.buildEnabled || this.activeJobUid) {
      this.startBuild("full");
      return;
    }
    if (this.topicCount <= 0) {
      this.startBuild("full");
      return;
    }
    this.fullBuildTrigger = trigger || null;
    const overlay = document.getElementById("topic-full-build-confirm-overlay");
    const message = document.getElementById("topic-full-build-confirm-message");
    message.textContent = window.t(
      "topic.fullBuildConfirmMessage",
      this.topicCount,
      this.currentSpace(),
    );
    const preserve = document.querySelector('input[name="topic-full-build-mode"][value="preserve"]');
    if (preserve) preserve.checked = true;
    this.updateFullBuildChoice();
    overlay.classList.add("visible");
    overlay.setAttribute("aria-hidden", "false");
    document.getElementById("topic-full-build-confirm-cancel")?.focus();
  }

  updateFullBuildChoice() {
    const resetTopics = document.querySelector('input[name="topic-full-build-mode"]:checked')?.value === "clear";
    document.getElementById("topic-full-build-reset-risk")?.classList.toggle("hidden", !resetTopics);
    const submit = document.getElementById("topic-full-build-confirm-submit");
    if (submit) {
      submit.textContent = window.t(resetTopics ? "topic.fullBuildClearSubmit" : "topic.fullBuildConfirmSubmit");
    }
  }

  fullBuildConfirmIsOpen() {
    return document.getElementById("topic-full-build-confirm-overlay")?.classList.contains("visible") || false;
  }

  closeFullBuildConfirm({ restoreFocus = true } = {}) {
    if (!this.fullBuildConfirmIsOpen() && !this.fullBuildTrigger) return;
    const overlay = document.getElementById("topic-full-build-confirm-overlay");
    overlay?.classList.remove("visible");
    overlay?.setAttribute("aria-hidden", "true");
    const trigger = this.fullBuildTrigger;
    this.fullBuildTrigger = null;
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }

  async startBuild(mode, { resetTopics = false, timelineUids = null } = {}) {
    const memorySpaceId = this.currentSpace();
    if (!memorySpaceId) {
      this.showToast("请先选择记忆空间", true);
      return;
    }
    if (this.activeJobUid) {
      this.showToast(window.t("topic.buildAlreadyRunning"), true);
      return;
    }
    this.setBuildButtonsDisabled(true);
    try {
      const job = await this.api.post("topics/build/start", {
        memory_space_id: memorySpaceId,
        mode,
        reset_topics: Boolean(resetTopics),
        ...(Array.isArray(timelineUids) ? { timeline_uids: timelineUids } : {}),
      });
      this.renderProgress(job);
      if (job.reset_topics && !job.already_running) this.renderResetBuildState();
      if (job.already_running) {
        this.showToast(window.t("topic.buildAlreadyRunning"));
      }
      this.resumePolling(job.job_uid);
    } catch (e) {
      this.setBuildButtonsDisabled(!this.currentSpace() || !this.buildEnabled);
      this.showToast(e.message || "无法启动 Topic 构建", true);
    }
  }

  renderResetBuildState() {
    this.topicCount = 0;
    ["topic-total", "topic-active", "topic-atoms", "topic-links", "topic-relations"].forEach(id => {
      const element = document.getElementById(id);
      if (element) element.textContent = "0";
    });
    const body = document.getElementById("topics-body");
    if (body) {
      body.innerHTML = `<tr><td colspan="6" class="table-empty">${esc(window.t("topic.resetBuildStarted"))}</td></tr>`;
    }
  }

  async resumeBuild(runUid) {
    if (!runUid || this.activeJobUid) return;
    this.setBuildButtonsDisabled(true);
    try {
      const job = await this.api.post("topics/build/start", {
        memory_space_id: this.currentSpace(),
        resume_run_uid: runUid,
      });
      this.renderProgress(job);
      this.resumePolling(job.job_uid);
      this.showToast(window.t("topic.resumeStarted"));
    } catch (e) {
      this.setBuildButtonsDisabled(!this.currentSpace() || !this.buildEnabled);
      this.showToast(e.message || window.t("topic.resumeFailed"), true);
    }
  }

  requestDiscardBuild(job, trigger) {
    if (!job?.run_uid || this.activeJobUid) return;
    this.discardRun = {
      run_uid: job.run_uid,
      memory_space_id: job.memory_space_id || this.currentSpace(),
    };
    this.discardBuildTrigger = trigger || null;
    const overlay = document.getElementById("topic-discard-build-overlay");
    const message = document.getElementById("topic-discard-build-message");
    message.textContent = window.t("topic.discardBuildMessage", job.run_uid);
    overlay.classList.add("visible");
    overlay.setAttribute("aria-hidden", "false");
    document.getElementById("topic-discard-build-cancel")?.focus();
  }

  discardBuildConfirmIsOpen() {
    return document.getElementById("topic-discard-build-overlay")?.classList.contains("visible") || false;
  }

  closeDiscardBuildConfirm({ restoreFocus = true } = {}) {
    if (!this.discardBuildConfirmIsOpen() && !this.discardRun) return;
    const overlay = document.getElementById("topic-discard-build-overlay");
    overlay?.classList.remove("visible");
    overlay?.setAttribute("aria-hidden", "true");
    const trigger = this.discardBuildTrigger;
    this.discardBuildTrigger = null;
    this.discardRun = null;
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }

  async discardBuild() {
    const run = this.discardRun;
    if (!run?.run_uid || this.activeJobUid) return;
    const submit = document.getElementById("topic-discard-build-submit");
    const cancel = document.getElementById("topic-discard-build-cancel");
    if (submit) submit.disabled = true;
    if (cancel) cancel.disabled = true;
    try {
      const result = await this.api.post("topics/build/discard", run);
      this.closeDiscardBuildConfirm({ restoreFocus: false });
      document.getElementById("topic-progress")?.classList.add("hidden");
      this.showToast(window.t(
        "topic.discardBuildSuccess",
        Number(result.deleted_intermediate_items || 0),
      ));
      await this.fetch();
    } catch (e) {
      this.showToast(e.message || window.t("topic.discardBuildFailed"), true);
    } finally {
      if (submit) submit.disabled = false;
      if (cancel) cancel.disabled = false;
    }
  }

  resumePolling(jobUid) {
    if (!jobUid) return;
    if (this.activeJobUid === jobUid && (this.pollTimer || this.pollInFlight)) return;
    clearTimeout(this.pollTimer);
    this.activeJobUid = jobUid;
    this.pollTimer = setTimeout(() => this.poll(jobUid), 200);
  }

  stopPolling() {
    clearTimeout(this.pollTimer);
    this.pollTimer = null;
    this.activeJobUid = null;
  }

  setBuildButtonsDisabled(disabled) {
    ["topic-build-full", "topic-maintenance"].forEach(id => {
      const button = document.getElementById(id);
      if (button) button.disabled = Boolean(disabled);
    });
  }

  async poll(jobUid) {
    if (this.pollInFlight) return;
    this.pollTimer = null;
    this.pollInFlight = true;
    try {
      const job = await this.api.get("topics/build/progress", { job_uid: jobUid });
      this.renderProgress(job);
      if (["completed", "failed", "cancelled"].includes(job.status)) {
        this.stopPolling();
        this.setBuildButtonsDisabled(!this.currentSpace() || !this.buildEnabled);
        if (job.status === "completed" || job.reset_topics) await this.fetch();
        if (job.status === "failed") this.showToast(job.error || "Topic 构建失败", true);
        return;
      }
      this.pollTimer = setTimeout(() => this.poll(jobUid), 1200);
    } catch (e) {
      this.showToast(e.message || "构建进度查询失败", true);
      if (this.activeJobUid === jobUid) {
        this.pollTimer = setTimeout(() => this.poll(jobUid), 3000);
      }
    } finally {
      this.pollInFlight = false;
    }
  }

  renderProgress(job) {
    const el = document.getElementById("topic-progress");
    const total = Number(job.total || 0);
    const current = Number(job.current || 0);
    const stagePercent = total ? Math.min(100, Math.round(current * 100 / total)) : 0;
    const overallPercent = Math.max(0, Math.min(100, Number(job.overall_percent || 0)));
    const stageKey = `topic.stage.${job.stage || "pending"}`;
    const stageLabel = window.t(stageKey);
    const now = Date.now() / 1000;
    const elapsed = this.formatDuration(Math.max(0, now - Number(job.created_at || now)));
    const updatedAgo = this.formatDuration(Math.max(0, now - Number(job.last_progress_at || now)));
    const detail = this.progressDetail(job);
    el.classList.remove("hidden");
    el.innerHTML = `
      <div class="topic-progress-header"><strong>${esc(window.t("topic.overallProgress"))}</strong><strong>${overallPercent.toFixed(1)}%</strong></div>
      <div class="topic-progress-track topic-progress-overall"><span style="width:${overallPercent}%"></span></div>
      <div class="topic-progress-header"><span>${esc(stageLabel)}</span><span>${current} / ${total}</span></div>
      <div class="topic-progress-track"><span style="width:${stagePercent}%"></span></div>
      ${detail ? `<div class="topic-progress-detail">${esc(detail)}</div>` : ""}
      <div class="text-tertiary">${esc(window.t("topic.progress.elapsed"))} ${esc(elapsed)} · ${esc(window.t("topic.progress.updated"))} ${esc(updatedAgo)}${job.error ? ` · ${esc(job.error)}` : ""}</div>
      <div class="text-tertiary">${esc(job.status)} · ${esc(job.memory_space_id || "")}</div>
      ${job.run_uid && ["failed", "cancelled", "pending"].includes(job.status)
        ? `<div class="topic-progress-actions">
            <button id="topic-resume-build" class="btn btn-primary">${esc(window.t("topic.resumeBuild"))}</button>
            <button id="topic-discard-build" class="btn btn-danger">${esc(window.t("topic.discardBuild"))}</button>
          </div>`
        : ""}`;
    document.getElementById("topic-resume-build")?.addEventListener(
      "click",
      () => this.resumeBuild(job.run_uid),
    );
    document.getElementById("topic-discard-build")?.addEventListener(
      "click",
      event => this.requestDiscardBuild(job, event.currentTarget),
    );
  }

  progressDetail(job) {
    if (job.status === "failed" && job.failed_stage) {
      return `${window.t("topic.progress.failedAt")} ${window.t(`topic.stage.${job.failed_stage}`)}`;
    }
    if (!["pending", "running"].includes(job.status)) return "";
    const callTotal = Number(job.llm_call_total || 0);
    const completed = Number(job.llm_call_current || 0);
    const callText = callTotal
      ? ` · ${window.t("topic.progress.llmCall")} ${Math.min(callTotal, completed)} / ${callTotal}`
        + ` · ${window.t("topic.progress.concurrency")} ${Number(job.llm_concurrency || 1)}`
      : "";
    if (job.item_kind === "rerank_query") {
      const rerankTotal = Number(job.rerank_call_total || job.item_total || 0);
      const rerankCompleted = Math.min(
        rerankTotal,
        Number(job.rerank_call_current || 0),
      );
      return `${window.t("topic.progress.rerankCompleted")} ${rerankCompleted} / ${rerankTotal}`
        + ` · ${window.t("topic.progress.rerankActive")} ${Number(job.active_rerank_count || 0)}`
        + ` · ${window.t("topic.progress.concurrency")} ${Number(job.rerank_concurrency || 1)}`;
    }
    if (job.item_kind === "candidate_group") {
      const groupCompleted = Number(job.completed_groups ?? job.current ?? 0);
      const groupTotal = Number(job.item_total || job.total || 0);
      const activeGroups = Number(job.active_group_count || 0);
      const groupConcurrency = Number(job.group_concurrency || job.llm_concurrency || 1);
      const aggregate = `${window.t("topic.progress.groupsCompleted")} ${groupCompleted} / ${groupTotal}`
        + ` · ${window.t("topic.progress.activeGroups")} ${activeGroups} / ${groupConcurrency}`;
      if (job.activity !== "llm_call") return aggregate;
      return aggregate
        + ` · ${window.t("topic.progress.extractingGroup")} #${Number(job.item_index || 0)}`
        + ` · ${window.t("topic.progress.groupTimelines")} ${Number(job.group_timeline_count || job.timeline_count || 0)}`
        + ` · ${window.t("topic.progress.currentBatch")} ${Number(job.batch_index || 1)} / ${Number(job.batch_total || 1)}`
        + ` (${window.t("topic.progress.timelines")} ${Number(job.timeline_count || 0)})`
        + callText;
    }
    if (job.item_kind === "topic_component" && job.activity === "llm_call") {
      const level = Number(job.synthesis_level || 1);
      return `${window.t("topic.progress.synthesizingComponent")} ${Number(job.item_index || 0)} / ${Number(job.item_total || 0)}`
        + ` · ${window.t("topic.progress.fragments")} ${Number(job.fragment_count || 0)}`
        + ` · ${window.t("topic.progress.currentBatch")} ${Number(job.batch_fragment_count || 0)}`
        + ` · ${window.t("topic.progress.level")} ${level}`
        + callText;
    }
    if (job.item_kind === "component_review") {
      const reviewed = Number(job.reviewed_components ?? job.current ?? 0);
      const total = Number(job.item_total || job.total || 0);
      const active = Number(job.active_component_review_count || 0);
      const concurrency = Number(job.component_review_concurrency || job.llm_concurrency || 1);
      const groups = Number(job.review_output_groups || 0);
      return `${window.t("topic.progress.reviewedComponents")} ${reviewed} / ${total}`
        + ` · ${window.t("topic.progress.reviewingComponent")} #${Number(job.item_index || 0)}`
        + ` · ${window.t("topic.progress.fragments")} ${Number(job.fragment_count || 0)}`
        + (groups ? ` · ${window.t("topic.progress.reviewOutputGroups")} ${groups}` : "")
        + ` · ${window.t("topic.progress.concurrency")} ${active} / ${concurrency}`;
    }
    return callText.replace(/^ · /, "");
  }

  formatDuration(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds || 0)));
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const secs = value % 60;
    if (hours) return `${hours}h ${minutes}m ${secs}s`;
    if (minutes) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  }

  formatTimestamp(timestamp) {
    const date = new Date(Number(timestamp || 0) * 1000);
    return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString();
  }
}
