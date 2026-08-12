import { esc } from "./utils.js";

const DIMENSIONS = ["familiarity", "trust", "warmth", "ease", "tension", "concern"];

export class UserProfilePage {
  constructor(api) {
    this.api = api;
    this.items = [];
    this.detail = null;
    this.selectedScopeUid = "";
    this.searchTimer = null;
  }

  initEventListeners() {
    document.getElementById("profile-page-search")?.addEventListener("input", () => {
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(() => this.loadList(), 250);
    });
    document.getElementById("profile-page-status")?.addEventListener("change", () => this.loadList());
    document.getElementById("profile-page-refresh")?.addEventListener("click", () => this.refresh());
    document.getElementById("profile-page-list")?.addEventListener("click", event => {
      const item = event.target.closest("[data-profile-view-scope]");
      if (item) this.select(item.dataset.profileViewScope);
    });
    document.getElementById("profile-page-detail")?.addEventListener("click", event => {
      const source = event.target.closest("[data-open-timeline]");
      if (!source) return;
      event.preventDefault();
      window.dispatchEvent(new CustomEvent("livingmemory:open-timeline-source", {
        detail: { timelineUid: source.dataset.openTimeline },
      }));
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
    const list = document.getElementById("profile-page-list");
    if (list) list.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
    try {
      const data = await this.api.get("user-profiles", {
        search: document.getElementById("profile-page-search")?.value || "",
        status: document.getElementById("profile-page-status")?.value || "",
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
    const list = document.getElementById("profile-page-list");
    if (!list) return;
    if (!this.items.length) {
      list.innerHTML = `<div class="identity-state">${esc(window.t("profile.viewEmpty"))}</div>`;
      return;
    }
    list.innerHTML = this.items.map(item => {
      const status = item.auto_enable_blocked ? "deleted" : item.enabled ? "enabled" : "disabled";
      return `<button class="user-profile-list-item${item.profile_scope_uid === this.selectedScopeUid ? " active" : ""}" type="button" data-profile-view-scope="${esc(item.profile_scope_uid)}">
        <span class="user-profile-list-main"><strong>${esc(item.display_name || item.actor_id || item.logical_user_uid)}</strong><small>${esc(item.platform || "")} · ${esc(item.stable_user_id || "")}</small></span>
        <span class="user-profile-list-meta"><span class="status-badge ${esc(status)}">${esc(window.t(`profile.status.${status}`))}</span><small>${esc(item.bot_account)} / ${esc(item.persona_id || "-")}</small></span>
        <span class="user-profile-list-counts"><span>${Number(item.active_fact_count || 0)} ${esc(window.t("profile.factsShort"))}</span></span>
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
    try {
      this.detail = await this.api.get("user-profiles/detail", {
        profile_scope_uid: this.selectedScopeUid,
      });
      this.renderDetail();
    } catch (error) {
      const root = document.getElementById("profile-page-detail");
      if (root) root.innerHTML = `<div class="identity-state error">${esc(error.message)}</div>`;
    }
  }

  renderDetail(loading = false) {
    const root = document.getElementById("profile-page-detail");
    if (!root) return;
    if (!this.selectedScopeUid) {
      root.innerHTML = `<div class="identity-state">${esc(window.t("profile.viewChoose"))}</div>`;
      return;
    }
    if (loading || !this.detail) {
      root.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
      return;
    }
    const data = this.detail;
    const scope = data.scope || {};
    const account = (data.accounts || [])[0] || {};
    const activeFacts = (data.facts || []).filter(item => item.status === "active");
    const reviewFacts = (data.facts || []).filter(item => ["pending", "conflict", "stale"].includes(item.status));
    const preview = data.injection_preview;
    root.innerHTML = `<div class="profile-view-detail-head">
        <div><h2>${esc(account.last_observed_name || account.actor_id || scope.logical_user_uid)}</h2><p>${esc(scope.bot_account)} / ${esc(scope.persona_id || "-")} · ${esc(scope.profile_scope_uid)}</p></div>
        <span class="status-badge ${scope.enabled ? "enabled" : "disabled"}">${esc(window.t(`profile.status.${scope.enabled ? "enabled" : "disabled"}`))}</span>
      </div>
      <section class="profile-view-summary">
        <div><span>${esc(window.t("profile.viewActiveFacts"))}</span><strong>${activeFacts.length}</strong></div>
        <div><span>${esc(window.t("profile.viewReviewFacts"))}</span><strong>${reviewFacts.length}</strong></div>
        <div><span>${esc(window.t("profile.viewFactRevision"))}</span><strong>r${Number(data.fact_revision || 0)}</strong></div>
        <div><span>${esc(window.t("profile.viewRelationshipRevision"))}</span><strong>${data.relationship ? `r${Number(data.relationship.revision || 0)}` : "-"}</strong></div>
      </section>
      <section class="user-profile-section">
        <div class="user-profile-section-head"><h3>${esc(window.t("profile.injectionPreview"))}</h3><span>${esc(window.t("profile.injectionMeta", Number(preview?.total_chars || 0), Number(preview?.fact_count || 0), window.t(preview?.relationship_included ? "profile.included" : "profile.notIncluded")))}</span></div>
        <pre class="user-profile-preview">${esc(preview?.content || window.t("profile.noInjection"))}</pre>
      </section>
      ${this.renderFacts(window.t("profile.activeFacts"), activeFacts)}
      ${this.renderFacts(window.t("profile.pendingConflicts"), reviewFacts)}
      ${this.renderRelationship(data.relationship, scope)}`;
  }

  renderFacts(title, facts) {
    const rows = facts.map(fact => {
      const source = fact.sources?.[0];
      return `<div class="user-profile-fact">
        <div class="user-profile-fact-main"><span class="status-badge">${esc(window.t(`profile.category.${fact.category}`))}</span><strong>${esc(fact.raw_fact || "")}</strong><small>${esc(window.t(`profile.factStatus.${fact.status}`))} · ${Math.round(Number(fact.confidence || 0) * 100)}% · ${Math.round(Number(fact.importance || 0) * 100)}%</small></div>
        ${source ? `<a class="user-profile-source" href="#" data-open-timeline="${esc(source.timeline_uid)}">${esc(source.timeline_uid)} r${Number(source.timeline_revision || 1)}</a>` : ""}
      </div>`;
    }).join("");
    return `<section class="user-profile-section"><h3>${esc(title)}</h3><div class="user-profile-facts">${rows || `<div class="identity-state compact">${esc(window.t("common.noData"))}</div>`}</div></section>`;
  }

  renderRelationship(relationship, scope) {
    const dimensions = DIMENSIONS.map(key => `<div class="profile-view-dimension"><span>${esc(window.t(`profile.dimension.${key}`))}</span><div class="profile-view-meter"><i style="width:${Math.round(Number(relationship?.[key] || 0) * 100)}%"></i></div><strong>${Math.round(Number(relationship?.[key] || 0) * 100)}</strong></div>`).join("");
    return `<section class="user-profile-section"><div class="user-profile-section-head"><h3>${esc(window.t("profile.relationship"))}</h3><span>${esc(window.t(scope.relationship_frozen ? "profile.viewFrozen" : "profile.viewLive"))}</span></div>
      <div class="profile-view-dimensions">${dimensions}</div>
      <div class="profile-view-narrative"><span>${esc(window.t("profile.stanceTags"))}</span><p>${esc((relationship?.stance_tags || []).join(" · ") || window.t("common.noData"))}</p></div>
      <div class="profile-view-narrative"><span>${esc(window.t("profile.subjectiveSummary"))}</span><p>${esc(relationship?.subjective_summary || window.t("common.noData"))}</p></div>
    </section>`;
  }
}
