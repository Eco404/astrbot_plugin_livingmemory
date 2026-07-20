import { esc } from "./utils.js";

export class IdentityPage {
  constructor(api, showToast) {
    this.api = api;
    this.showToast = showToast;
    this.profiles = [];
    this.expandedProfiles = new Set();
    this.dirty = false;
    this.loaded = false;
  }

  initEventListeners() {
    document.getElementById("identity-refresh")?.addEventListener("click", () => this.fetch(true));
    document.getElementById("identity-add")?.addEventListener("click", () => this.add());
    document.getElementById("identity-save")?.addEventListener("click", () => this.save());
    const container = document.getElementById("identity-list");
    container?.addEventListener("input", event => this.updateField(event));
    container?.addEventListener("click", event => {
      const removeButton = event.target.closest("[data-identity-remove]");
      if (removeButton) {
        this.remove(Number(removeButton.dataset.identityRemove));
        return;
      }
      const toggleButton = event.target.closest("[data-identity-toggle]");
      if (toggleButton) this.toggle(Number(toggleButton.dataset.identityToggle));
    });
  }

  async fetch(force = false) {
    if (this.dirty) {
      this.showToast(window.t("identity.unsavedFirst"), true);
      return;
    }
    if (this.loaded && !force) return;
    const container = document.getElementById("identity-list");
    if (!container) return;
    container.innerHTML = `<div class="identity-state">${esc(window.t("common.loading"))}</div>`;
    try {
      const data = await this.api.get("identities");
      this.profiles = Array.isArray(data.profiles)
        ? data.profiles.map(profile => this.normalize(profile))
        : [];
      this.expandedProfiles.clear();
      this.dirty = false;
      this.loaded = true;
      this.render(data.load_error || "");
    } catch (error) {
      container.innerHTML = `<div class="identity-state identity-state-error">${esc(error.message || window.t("identity.loadFailed"))}</div>`;
      this.showToast(error.message || window.t("identity.loadFailed"), true);
    }
  }

  normalize(profile = {}) {
    return {
      platform: String(profile.platform || ""),
      user_id: String(profile.user_id || ""),
      display_name: String(profile.display_name || ""),
      aliases: Array.isArray(profile.aliases) ? profile.aliases.map(String) : [],
      gender: String(profile.gender || ""),
      pronouns: Array.isArray(profile.pronouns) ? profile.pronouns.map(String) : [],
      notes: String(profile.notes || ""),
    };
  }

  add() {
    const profile = this.normalize();
    this.profiles.push(profile);
    this.expandedProfiles.add(profile);
    this.dirty = true;
    this.render();
    document.querySelector("#identity-list .identity-card:last-child input")?.focus();
  }

  remove(index) {
    if (!Number.isInteger(index) || !this.profiles[index]) return;
    this.expandedProfiles.delete(this.profiles[index]);
    this.profiles.splice(index, 1);
    this.dirty = true;
    this.render();
  }

  toggle(index) {
    const profile = this.profiles[index];
    if (!Number.isInteger(index) || !profile) return;
    if (this.expandedProfiles.has(profile)) {
      this.expandedProfiles.delete(profile);
    } else {
      this.expandedProfiles.add(profile);
    }
    this.render();
  }

  updateField(event) {
    const input = event.target.closest("[data-identity-field]");
    if (!input) return;
    const index = Number(input.dataset.identityIndex);
    const field = input.dataset.identityField;
    if (!this.profiles[index] || !field) return;
    if (field === "aliases" || field === "pronouns") {
      this.profiles[index][field] = this.splitList(input.value);
    } else {
      this.profiles[index][field] = input.value;
    }
    this.dirty = true;
    this.updateSaveState();
  }

  splitList(value) {
    return [...new Set(String(value || "").split(/[，,、/\n]+/).map(item => item.trim()).filter(Boolean))];
  }

  attr(value) {
    return esc(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }

  validate() {
    const seen = [];
    for (let index = 0; index < this.profiles.length; index += 1) {
      const profile = this.profiles[index];
      profile.user_id = profile.user_id.trim();
      profile.platform = profile.platform.trim();
      if (!profile.user_id) throw new Error(window.t("identity.userIdRequired", index + 1));
      const platform = profile.platform.toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
      const userId = profile.user_id.toLocaleLowerCase();
      const overlaps = seen.some(item => item.userId === userId && (
        !item.platform || !platform || item.platform === platform
        || item.platform.includes(platform) || platform.includes(item.platform)
      ));
      if (overlaps) throw new Error(window.t("identity.duplicate", index + 1));
      seen.push({ platform, userId });
    }
  }

  async save() {
    try {
      this.validate();
    } catch (error) {
      this.showToast(error.message, true);
      return;
    }
    const button = document.getElementById("identity-save");
    if (button) button.disabled = true;
    try {
      const data = await this.api.post("identities/save", { profiles: this.profiles });
      this.profiles = (data.profiles || []).map(profile => this.normalize(profile));
      this.expandedProfiles.clear();
      this.dirty = false;
      this.loaded = true;
      this.render(data.load_error || "");
      this.showToast(window.t("identity.saved"));
    } catch (error) {
      this.showToast(error.message || window.t("identity.saveFailed"), true);
      this.updateSaveState();
    }
  }

  render(loadError = "") {
    const container = document.getElementById("identity-list");
    if (!container) return;
    const error = loadError
      ? `<div class="identity-load-warning">${esc(window.t("identity.fileError", loadError))}</div>`
      : "";
    const cards = this.profiles.length
      ? this.profiles.map((profile, index) => this.renderCard(profile, index)).join("")
      : `<div class="identity-state">${esc(window.t("identity.empty"))}</div>`;
    container.innerHTML = error + cards;
    this.updateSaveState();
  }

  renderCard(profile, index) {
    const expanded = this.expandedProfiles.has(profile);
    const title = profile.display_name || profile.user_id || window.t("identity.newProfile");
    const identityParts = [
      profile.platform && profile.user_id ? `${profile.platform} · ${profile.user_id}` : profile.platform || profile.user_id,
      profile.gender,
      profile.pronouns.join(" / "),
    ].filter(Boolean);
    const field = (name, label, value, placeholder = "") => `
      <label class="identity-field">
        <span>${esc(window.t(label))}</span>
        <input class="input" type="text" value="${this.attr(value)}" placeholder="${this.attr(window.t(placeholder))}"
          data-identity-index="${index}" data-identity-field="${name}" />
      </label>`;
    return `
      <article class="identity-card${expanded ? " is-expanded" : ""}">
        <div class="identity-card-header">
          <button class="identity-card-summary" type="button" data-identity-toggle="${index}"
            aria-expanded="${expanded ? "true" : "false"}">
            <span class="identity-card-summary-text">
              <strong>${esc(title)}</strong>
              ${identityParts.length ? `<span>${esc(identityParts.join(" · "))}</span>` : ""}
            </span>
            <span class="identity-card-chevron" aria-hidden="true">⌄</span>
          </button>
          <button class="btn btn-danger btn-sm" type="button" data-identity-remove="${index}">${esc(window.t("identity.remove"))}</button>
        </div>
        ${expanded ? `<div class="identity-fields">
          ${field("platform", "identity.platform", profile.platform, "identity.platformPh")}
          ${field("user_id", "identity.userId", profile.user_id, "identity.userIdPh")}
          ${field("display_name", "identity.displayName", profile.display_name, "identity.displayNamePh")}
          ${field("aliases", "identity.aliases", profile.aliases.join(", "), "identity.aliasesPh")}
          ${field("gender", "identity.gender", profile.gender, "identity.genderPh")}
          ${field("pronouns", "identity.pronouns", profile.pronouns.join(", "), "identity.pronounsPh")}
          <label class="identity-field identity-field-wide">
            <span>${esc(window.t("identity.notes"))}</span>
            <textarea class="input" rows="3" placeholder="${this.attr(window.t("identity.notesPh"))}"
              data-identity-index="${index}" data-identity-field="notes">${esc(profile.notes)}</textarea>
          </label>
        </div>` : ""}
      </article>`;
  }

  updateSaveState() {
    const button = document.getElementById("identity-save");
    if (button) button.disabled = !this.dirty;
    const marker = document.getElementById("identity-dirty");
    if (marker) marker.classList.toggle("hidden", !this.dirty);
  }
}
