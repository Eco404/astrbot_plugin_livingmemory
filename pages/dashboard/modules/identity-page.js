import { esc } from "./utils.js";

export class IdentityPage {
  constructor(api, showToast) {
    this.api = api;
    this.showToast = showToast;
    this.profiles = [];
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
      const button = event.target.closest("[data-identity-remove]");
      if (button) this.remove(Number(button.dataset.identityRemove));
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
    this.profiles.push(this.normalize());
    this.dirty = true;
    this.render();
    document.querySelector("#identity-list .identity-card:last-child input")?.focus();
  }

  remove(index) {
    if (!Number.isInteger(index) || !this.profiles[index]) return;
    this.profiles.splice(index, 1);
    this.dirty = true;
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
    const field = (name, label, value, placeholder = "") => `
      <label class="identity-field">
        <span>${esc(window.t(label))}</span>
        <input class="input" type="text" value="${this.attr(value)}" placeholder="${this.attr(window.t(placeholder))}"
          data-identity-index="${index}" data-identity-field="${name}" />
      </label>`;
    return `
      <article class="identity-card">
        <div class="identity-card-header">
          <strong>${esc(profile.display_name || profile.user_id || window.t("identity.newProfile"))}</strong>
          <button class="btn btn-danger btn-sm" type="button" data-identity-remove="${index}">${esc(window.t("identity.remove"))}</button>
        </div>
        <div class="identity-fields">
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
        </div>
      </article>`;
  }

  updateSaveState() {
    const button = document.getElementById("identity-save");
    if (button) button.disabled = !this.dirty;
    const marker = document.getElementById("identity-dirty");
    if (marker) marker.classList.toggle("hidden", !this.dirty);
  }
}
