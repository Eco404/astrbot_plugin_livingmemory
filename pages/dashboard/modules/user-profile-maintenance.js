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
  }

  initEventListeners() {
    document.getElementById("user-profile-search")?.addEventListener("input", () => {
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(() => this.loadList(), 250);
    });
    document.getElementById("user-profile-status")?.addEventListener("change", () => this.loadList());
    document.getElementById("user-profile-refresh")?.addEventListener("click", () => this.refresh());
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
    });
  }

  async activate() {
    if (!this.items.length) await this.loadList();
    else if (this.selectedScopeUid && !this.detail) await this.loadDetail();
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
      list.innerHTML = `<div class="identity-state">${esc(window.t("profile.empty"))}</div>`;
      return;
    }
    list.innerHTML = this.items.map(item => {
      const status = item.auto_enable_blocked ? "deleted" : item.enabled ? "enabled" : "disabled";
      const alerts = Number(item.conflict_fact_count || 0) + Number(item.running_task_count || 0) + (item.has_gap ? 1 : 0);
      return `<button class="user-profile-list-item${item.profile_scope_uid === this.selectedScopeUid ? " active" : ""}" type="button" data-profile-scope="${esc(item.profile_scope_uid)}">
        <span class="user-profile-list-main"><strong>${esc(item.display_name || item.actor_id || item.logical_user_uid)}</strong><small>${esc(item.platform || "")} · ${esc(item.stable_user_id || "")}</small></span>
        <span class="user-profile-list-meta"><span class="status-badge ${esc(status)}">${esc(window.t(`profile.status.${status}`))}</span><small>${esc(item.bot_account)} / ${esc(item.persona_id || "-")}</small></span>
        <span class="user-profile-list-counts"><span>${Number(item.active_fact_count || 0)} ${esc(window.t("profile.factsShort"))}</span><span${alerts ? ' class="warning"' : ""}>${alerts}</span></span>
      </button>`;
    }).join("");
  }

  async select(scopeUid) {
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
    root.innerHTML = `
      <div class="user-profile-detail-head">
        <div><h2>${esc(accounts[0]?.last_observed_name || accounts[0]?.actor_id || scope.logical_user_uid)}</h2><p>${esc(scope.bot_account)} / ${esc(scope.persona_id || "-")} · ${esc(scope.profile_scope_uid)}</p></div>
        <div class="user-profile-head-actions">
          <button class="btn btn-secondary btn-sm" type="button" data-profile-action="${scope.enabled ? "disable" : "enable"}">${esc(window.t(scope.enabled ? "profile.disable" : "profile.enable"))}</button>
          <button class="btn btn-secondary btn-sm" type="button" data-profile-action="rebuild">${esc(window.t("profile.rebuild"))}</button>
          <button class="btn btn-danger btn-sm" type="button" data-profile-action="reset">${esc(window.t("profile.reset"))}</button>
          <button class="btn btn-danger btn-sm" type="button" data-profile-action="delete-disable">${esc(window.t("profile.deleteDisable"))}</button>
        </div>
      </div>
      <label class="user-profile-rebuild-option"><input id="profile-rebuild-clear-overrides" type="checkbox"> <span>${esc(window.t("profile.clearOverrides"))}</span></label>
      ${scope.has_gap || data.gap?.has_gap ? `<div class="user-profile-alert">${esc(window.t("profile.gap", data.gap?.pending_count || 0))}</div>` : ""}
      <section class="user-profile-section">
        <div class="user-profile-section-head"><h3>${esc(window.t("profile.injectionPreview"))}</h3><span>${Number(preview?.total_chars || 0)}</span></div>
        <pre class="user-profile-preview">${esc(preview?.content || window.t("profile.noInjection"))}</pre>
      </section>
      ${this.renderFactSection(window.t("profile.activeFacts"), activeFacts, true)}
      ${this.renderFactSection(window.t("profile.pendingConflicts"), pendingFacts, true)}
      ${this.renderConflicts(data.conflicts || [])}
      ${this.renderFactSection(window.t("profile.historyFacts"), historicalFacts, false)}
      ${this.renderIdentityReviews(data.identity_reviews || [], accounts)}
      ${this.renderRelationship(relationship, data.relationship_revisions || [], scope)}
      ${this.renderAccounts(accounts)}
      ${this.renderSharing(data.share_group, scope)}
      ${this.renderTasks(data.tasks || [])}`;
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
        <div class="user-profile-fact-main"><span class="status-badge">${esc(window.t(`profile.category.${fact.category}`))}</span><strong>${esc(fact.raw_fact || "")}</strong><small>${esc(window.t(`profile.factStatus.${fact.status}`))} · ${Math.round(Number(fact.confidence || 0) * 100)}% · ${Math.round(Number(fact.importance || 0) * 100)}%</small></div>
        ${source ? `<a class="user-profile-source" href="#" data-open-timeline="${esc(source.timeline_uid)}">${esc(source.timeline_uid)} r${Number(source.timeline_revision || 1)}</a>` : ""}
        ${controls}
      </div>`;
    }).join("") : `<div class="identity-state compact">${esc(window.t("common.noData"))}</div>`;
    return `<section class="user-profile-section"><h3>${esc(title)}</h3><div class="user-profile-facts">${rows}</div></section>`;
  }

  renderConflicts(conflicts) {
    const open = conflicts.filter(item => item.status === "open");
    if (!open.length) return "";
    const facts = new Map((this.detail.facts || []).map(item => [item.profile_fact_uid, item]));
    return `<section class="user-profile-section"><h3>${esc(window.t("profile.conflicts"))}</h3>${open.map(conflict => `<div class="user-profile-conflict">
      <strong>${esc(conflict.conflict_key || conflict.conflict_uid)}</strong>
      <p>${esc(conflict.resolution_reason || "")}</p>
      <div class="user-profile-conflict-options">${(conflict.fact_uids || []).map(uid => `<button class="btn btn-secondary btn-sm" data-conflict-action="select" data-conflict-uid="${esc(conflict.conflict_uid)}" data-fact-uid="${esc(uid)}">${esc(facts.get(uid)?.raw_fact || uid)}</button>`).join("")}</div>
      <div class="user-profile-fact-actions"><button class="btn btn-secondary btn-sm" data-conflict-action="pause" data-conflict-uid="${esc(conflict.conflict_uid)}">${esc(window.t("profile.keepPaused"))}</button><button class="btn btn-danger btn-sm" data-conflict-action="exclude" data-conflict-uid="${esc(conflict.conflict_uid)}">${esc(window.t("profile.excludeConflict"))}</button></div>
    </div>`).join("")}</section>`;
  }

  renderRelationship(relationship, revisions, scope) {
    const dimensions = DIMENSIONS.map(key => `<label class="relationship-dimension"><span>${esc(window.t(`profile.dimension.${key}`))}</span><input type="range" min="0" max="100" value="${Math.round(Number(relationship?.[key] || 0) * 100)}" data-relationship-dimension="${key}"><output>${Math.round(Number(relationship?.[key] || 0) * 100)}</output></label>`).join("");
    const revisionRows = revisions.slice(0, 12).map(item => `<div class="relationship-revision"><span>r${Number(item.revision)} · ${esc(item.operation)} · ${this.formatTime(item.created_at)}</span>${item.full_snapshot ? `<button class="btn btn-secondary btn-sm" data-relationship-rollback="${Number(item.revision)}">${esc(window.t("profile.rollback"))}</button>` : ""}</div>`).join("");
    return `<section class="user-profile-section"><div class="user-profile-section-head"><h3>${esc(window.t("profile.relationship"))}</h3><button class="btn btn-secondary btn-sm" data-relationship-action="freeze">${esc(window.t(scope.relationship_frozen ? "profile.unfreeze" : "profile.freeze"))}</button></div>
      <div class="relationship-dimensions">${dimensions}</div>
      <label class="field"><span>${esc(window.t("profile.stanceTags"))}</span><input class="input" id="relationship-tags" value="${esc((relationship?.stance_tags || []).join(", "))}"></label>
      <label class="field"><span>${esc(window.t("profile.subjectiveSummary"))}</span><textarea class="input" id="relationship-summary" rows="3">${esc(relationship?.subjective_summary || "")}</textarea></label>
      <div class="relationship-overrides"><label class="field"><span>${esc(window.t("profile.sensitivity"))}</span><select class="select input" id="relationship-sensitivity"><option value="">${esc(window.t("profile.useGlobal"))}</option>${["very_slow", "slow", "balanced", "fast", "very_fast"].map(value => `<option value="${value}"${scope.relationship_sensitivity_override === value ? " selected" : ""}>${esc(window.t(`profile.sensitivityOption.${value}`))}</option>`).join("")}</select></label><label class="field"><span>${esc(window.t("profile.behaviorMode"))}</span><select class="select input" id="relationship-behavior"><option value="">${esc(window.t("profile.useGlobal"))}</option>${["restrained", "natural", "high_autonomy", "unrestricted"].map(value => `<option value="${value}"${scope.relationship_behavior_override === value ? " selected" : ""}>${esc(window.t(`profile.behaviorOption.${value}`))}</option>`).join("")}</select></label></div>
      <div class="user-profile-fact-actions"><button class="btn btn-primary btn-sm" data-relationship-action="save">${esc(window.t("common.save"))}</button><button class="btn btn-danger btn-sm" data-relationship-action="reset">${esc(window.t("profile.resetRelationship"))}</button><button class="btn btn-secondary btn-sm" data-relationship-action="rebuild">${esc(window.t("profile.rebuildRelationship"))}</button></div>
      <div class="relationship-revisions">${revisionRows || `<div class="identity-state compact">${esc(window.t("common.noData"))}</div>`}</div>
    </section>`;
  }

  renderIdentityReviews(items, accounts) {
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
    return `<section class="user-profile-section"><div class="user-profile-section-head"><h3>${esc(window.t("profile.identityReviews"))}</h3><button class="btn btn-secondary btn-sm" data-identity-review-scan>${esc(window.t("profile.scanIdentityReviews"))}</button></div>${rows || `<div class="identity-state compact">${esc(window.t("profile.noIdentityReviews"))}</div>`}</section>`;
  }

  renderAccounts(accounts) {
    const rows = accounts.map(account => `<div class="user-profile-account"><div><strong>${esc(account.actor_id)}</strong><small>${esc((account.observed_names || []).join(" · "))}</small></div>${accounts.length > 1 ? `<button class="btn btn-secondary btn-sm" data-account-unbind="${esc(account.actor_id)}">${esc(window.t("profile.unbind"))}</button>` : ""}</div>`).join("");
    return `<section class="user-profile-section"><h3>${esc(window.t("profile.accounts"))}</h3>${rows}<div class="user-profile-inline-form"><input class="input" id="profile-bind-target" placeholder="${esc(window.t("profile.bindTargetPlaceholder"))}"><input class="input" id="profile-bind-actors" placeholder="${esc(window.t("profile.bindActorsPlaceholder"))}"><button class="btn btn-secondary btn-sm" data-account-action="bind">${esc(window.t("profile.bind"))}</button></div></section>`;
  }

  renderSharing(group, scope) {
    const members = group?.members || [];
    return `<section class="user-profile-section"><h3>${esc(window.t("profile.sharing"))}</h3>${group ? `<p>${esc(group.name)} · ${members.map(item => `${item.bot_account}/${item.persona_id}`).join(", ")}</p>` : `<p class="text-secondary">${esc(window.t("profile.notShared"))}</p>`}<div class="user-profile-inline-form"><input class="input" id="profile-share-name" value="${esc(group?.name || "")}" placeholder="${esc(window.t("profile.shareName"))}"><input class="input" id="profile-share-scopes" value="${esc((members.length ? members.map(item => item.profile_scope_uid) : [scope.profile_scope_uid]).join(", "))}" placeholder="${esc(window.t("profile.shareScopesPlaceholder"))}"><button class="btn btn-secondary btn-sm" data-share-action="save">${esc(window.t("profile.saveSharing"))}</button></div></section>`;
  }

  renderTasks(tasks) {
    const rows = tasks.map(task => `<div class="user-profile-task"><div><strong>${esc(task.status)}</strong><small>${this.formatTime(task.updated_at)}${task.error ? ` · ${esc(task.error)}` : ""}</small></div>${["failed", "facts_failed", "facts_completed"].includes(task.status) ? `<button class="btn btn-secondary btn-sm" data-task-retry="${esc(task.task_uid)}">${esc(window.t("profile.retry"))}</button>` : ""}</div>`).join("");
    return `<section class="user-profile-section"><h3>${esc(window.t("profile.tasks"))}</h3>${rows || `<div class="identity-state compact">${esc(window.t("common.noData"))}</div>`}</section>`;
  }

  async handleDetailClick(event) {
    const source = event.target.closest("[data-open-timeline]");
    if (source) {
      event.preventDefault();
      window.dispatchEvent(new CustomEvent("livingmemory:open-timeline-source", { detail: { timelineUid: source.dataset.openTimeline } }));
      return;
    }
    const action = event.target.closest("[data-profile-action]");
    if (action) return this.profileAction(action.dataset.profileAction);
    const fact = event.target.closest("[data-fact-action]");
    if (fact) return this.factAction(fact.dataset.factUid, fact.dataset.factAction);
    const conflict = event.target.closest("[data-conflict-action]");
    if (conflict) return this.conflictAction(conflict.dataset.conflictUid, conflict.dataset.conflictAction, conflict.dataset.factUid || null);
    const relationship = event.target.closest("[data-relationship-action]");
    if (relationship) return this.relationshipAction(relationship.dataset.relationshipAction);
    const rollback = event.target.closest("[data-relationship-rollback]");
    if (rollback) return this.relationshipRollback(Number(rollback.dataset.relationshipRollback));
    const unbind = event.target.closest("[data-account-unbind]");
    if (unbind) return this.unbindAccount(unbind.dataset.accountUnbind);
    if (event.target.closest('[data-account-action="bind"]')) return this.bindAccounts();
    if (event.target.closest('[data-share-action="save"]')) return this.saveSharing();
    if (event.target.closest("[data-identity-review-scan]")) return this.scanIdentityReviews();
    const identityReview = event.target.closest("[data-identity-review-action]");
    if (identityReview) return this.identityReviewAction(identityReview);
    const retry = event.target.closest("[data-task-retry]");
    if (retry) return this.retryTask(retry.dataset.taskRetry);
  }

  async profileAction(action) {
    const body = { profile_scope_uid: this.selectedScopeUid };
    if (["reset", "delete-disable"].includes(action)) {
      const confirmed = await this.confirmDialog.show({ title: window.t(`profile.${action === "reset" ? "reset" : "deleteDisable"}`), message: window.t(`profile.confirm.${action}`), confirmLabel: window.t("common.confirm") });
      if (!confirmed) return;
      body.fingerprint = this.detail.fingerprint;
    }
    try {
      if (action === "rebuild") return this.rebuildProfile();
      await this.api.post(`user-profiles/${action}`, body);
      this.showToast(window.t("profile.operationDone"), "success");
      await this.refresh();
      if (action === "enable" && this.detail?.gap?.has_gap) await this.offerRebuild();
    } catch (error) { this.showToast(error.message, "error"); }
  }

  async offerRebuild() {
    const confirmed = await this.confirmDialog.show({ title: window.t("profile.gapTitle"), message: window.t("profile.gapRebuildPrompt"), confirmLabel: window.t("profile.rebuild"), danger: false });
    if (confirmed) await this.rebuildProfile();
  }

  async rebuildProfile() {
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
      await this.api.post("user-profiles/rebuild/start", { profile_scope_uid: this.selectedScopeUid, fingerprint: preview.fingerprint, history_fingerprint: preview.history_fingerprint, clear_overrides: clear });
      this.showToast(window.t("profile.rebuildScheduled"), "success");
      await this.loadDetail();
    } catch (error) { this.showToast(error.message, "error"); }
  }

  async factAction(uid, action) {
    try {
      await this.api.post("user-profiles/facts/action", { profile_scope_uid: this.selectedScopeUid, profile_fact_uid: uid, action, expected_revision: this.detail.fact_revision });
      await this.loadDetail();
    } catch (error) { this.showToast(error.message, "error"); }
  }

  async conflictAction(uid, resolution, factUid) {
    try {
      await this.api.post("user-profiles/conflicts/resolve", { profile_scope_uid: this.selectedScopeUid, conflict_uid: uid, resolution, selected_fact_uid: factUid, expected_revision: this.detail.fact_revision });
      await this.loadDetail();
    } catch (error) { this.showToast(error.message, "error"); }
  }

  async relationshipAction(action) {
    try {
      if (action === "save") {
        const changes = {};
        document.querySelectorAll("[data-relationship-dimension]").forEach(input => { changes[input.dataset.relationshipDimension] = Number(input.value); });
        changes.stance_tags = (document.getElementById("relationship-tags")?.value || "").split(",").map(value => value.trim()).filter(Boolean);
        changes.subjective_summary = document.getElementById("relationship-summary")?.value || "";
        await this.api.post("user-profiles/relationship/update", { profile_scope_uid: this.selectedScopeUid, expected_revision: this.detail.relationship?.revision || 0, changes, sensitivity_override: document.getElementById("relationship-sensitivity")?.value || null, behavior_override: document.getElementById("relationship-behavior")?.value || null });
      } else if (action === "freeze") {
        await this.api.post("user-profiles/relationship/freeze", { profile_scope_uid: this.selectedScopeUid, frozen: !this.detail.scope.relationship_frozen });
      } else {
        const confirmed = await this.confirmDialog.show({ title: window.t(`profile.${action === "reset" ? "resetRelationship" : "rebuildRelationship"}`), message: window.t(`profile.confirm.relationship-${action}`), confirmLabel: window.t("common.confirm") });
        if (!confirmed) return;
        await this.api.post(`user-profiles/relationship/${action}`, { profile_scope_uid: this.selectedScopeUid, fingerprint: this.detail.fingerprint, use_all_history: true });
      }
      await this.loadDetail();
    } catch (error) { this.showToast(error.message, "error"); }
  }

  async relationshipRollback(revision) {
    const confirmed = await this.confirmDialog.show({ title: window.t("profile.rollback"), message: window.t("profile.confirm.rollback", revision), confirmLabel: window.t("profile.rollback") });
    if (!confirmed) return;
    try { await this.api.post("user-profiles/relationship/rollback", { profile_scope_uid: this.selectedScopeUid, revision }); await this.loadDetail(); } catch (error) { this.showToast(error.message, "error"); }
  }

  async bindAccounts() {
    const target_actor_id = document.getElementById("profile-bind-target")?.value.trim() || "";
    const actor_ids = (document.getElementById("profile-bind-actors")?.value || "").split(",").map(value => value.trim()).filter(Boolean);
    try {
      const preview = await this.api.post("user-profiles/accounts/bind/preview", { target_actor_id, actor_ids });
      if (preview.blocked_reason) throw new Error(preview.blocked_reason);
      const confirmed = await this.confirmDialog.show({ title: window.t("profile.bind"), message: window.t("profile.bindImpact", preview.moved_account_count, preview.fact_count, preview.scope_collisions.length), confirmLabel: window.t("profile.bind") });
      if (!confirmed) return;
      await this.api.post("user-profiles/accounts/bind", { target_actor_id, actor_ids, fingerprint: preview.fingerprint });
      await this.refresh();
    } catch (error) { this.showToast(error.message, "error"); }
  }

  async unbindAccount(actor_id) {
    try {
      const preview = await this.api.post("user-profiles/accounts/unbind/preview", { actor_id });
      if (preview.blocked_reason) throw new Error(preview.blocked_reason);
      const confirmed = await this.confirmDialog.show({ title: window.t("profile.unbind"), message: window.t("profile.unbindImpact", preview.source_count, preview.new_scope_count), confirmLabel: window.t("profile.unbind") });
      if (!confirmed) return;
      await this.api.post("user-profiles/accounts/unbind", { actor_id, fingerprint: preview.fingerprint });
      await this.refresh();
    } catch (error) { this.showToast(error.message, "error"); }
  }

  async saveSharing() {
    const name = document.getElementById("profile-share-name")?.value.trim() || "";
    const profile_scope_uids = (document.getElementById("profile-share-scopes")?.value || "").split(",").map(value => value.trim()).filter(Boolean);
    const share_group_uid = this.detail.share_group?.share_group_uid || null;
    try {
      const preview = await this.api.post("user-profiles/share-groups/preview", { profile_scope_uids, share_group_uid });
      const confirmed = await this.confirmDialog.show({ title: window.t("profile.sharing"), message: window.t("profile.shareImpact", preview.fact_count, preview.potential_conflict_categories.length), confirmLabel: window.t("common.save") });
      if (!confirmed) return;
      await this.api.post("user-profiles/share-groups/save", { name, profile_scope_uids, share_group_uid, fingerprint: preview.fingerprint });
      await this.refresh();
    } catch (error) { this.showToast(error.message, "error"); }
  }

  async retryTask(task_uid) {
    try { await this.api.post("user-profiles/tasks/retry", { task_uid }); await this.loadDetail(); } catch (error) { this.showToast(error.message, "error"); }
  }

  async scanIdentityReviews() {
    try {
      const data = await this.api.post("user-profiles/identity-reviews/scan", { profile_scope_uid: this.selectedScopeUid });
      if (this.detail) this.detail.identity_reviews = data.items || [];
      this.renderDetail();
      this.showToast(window.t("profile.identityScanDone", data.diagnostics?.legacy_auto_resolved_count || 0, data.diagnostics?.pending_review_count || 0), "success");
    } catch (error) { this.showToast(error.message, "error"); }
  }

  async identityReviewAction(button) {
    const action = button.dataset.identityReviewAction;
    const confirmed = await this.confirmDialog.show({ title: window.t(`profile.identityAction.${action}`), message: window.t(`profile.confirm.identity-${action}`), confirmLabel: window.t("common.confirm") });
    if (!confirmed) return;
    const row = button.closest("[data-identity-review-row]");
    const actor_id = action === "bind" ? row?.querySelector("[data-identity-actor]")?.value || "" : null;
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
    } catch (error) { this.showToast(error.message, "error"); }
  }

  formatTime(value) {
    const numeric = Number(value || 0);
    if (!numeric) return "-";
    return new Date(numeric * 1000).toLocaleString();
  }
}
