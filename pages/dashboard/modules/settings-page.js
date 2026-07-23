import { esc } from "./utils.js";

const SCOPES = {
  timeline: {
    get: "timeline/settings",
    update: "timeline/settings/update",
    categories: ["recall", "injection", "generation", "session", "isolation", "graph", "lifecycle", "index", "model", "maintenance", "performance"],
  },
  topic: {
    get: "topics/settings",
    update: "topics/settings/update",
    categories: ["recall", "build", "performance"],
  },
};

export class SettingsPage {
  constructor(api, showToast) {
    this.api = api;
    this.showToast = showToast;
    this.scope = "timeline";
    this.data = null;
    this.resetKeys = new Set();
    this.resetAll = false;
  }

  initEventListeners() {
    document.querySelectorAll("[data-settings-scope]").forEach(button => {
      button.addEventListener("click", () => this.selectScope(button.dataset.settingsScope));
    });
    document.getElementById("settings-save")?.addEventListener("click", () => this.save());
    document.getElementById("settings-reset-all")?.addEventListener("click", () => this.resetAllValues());
  }

  async selectScope(scope) {
    if (!SCOPES[scope] || scope === this.scope) return;
    this.scope = scope;
    document.querySelectorAll("[data-settings-scope]").forEach(button => {
      const active = button.dataset.settingsScope === scope;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    await this.fetch();
  }

  async fetch() {
    const content = document.getElementById("settings-page-content");
    const status = document.getElementById("settings-page-status");
    if (!content || !status) return;
    status.textContent = window.t("common.loading");
    content.innerHTML = "";
    this.resetKeys = new Set();
    this.resetAll = false;
    try {
      this.data = await this.api.get(SCOPES[this.scope].get);
      this.render();
    } catch (error) {
      status.textContent = error.message || window.t("settings.loadFailed");
    }
  }

  categoryLabel(category) {
    const key = this.scope === "topic"
      ? `topic.settingsCategory.${category}`
      : `timeline.settingsCategory.${category}`;
    const translated = window.t(key);
    return translated === key ? category : translated;
  }

  render() {
    const content = document.getElementById("settings-page-content");
    const status = document.getElementById("settings-page-status");
    if (!content || !status) return;
    const definitions = this.data?.definitions || {};
    const effective = this.data?.effective || {};
    const overrides = this.data?.overrides || {};
    status.textContent = this.data?.build_active ? window.t("topic.settingsBuildActive") : "";
    content.innerHTML = SCOPES[this.scope].categories.map(category => {
      const rows = Object.entries(definitions)
        .filter(([, definition]) => definition.category === category && !definition.deprecated)
        .map(([key, definition]) => this.renderRow(key, definition, effective[key], Object.prototype.hasOwnProperty.call(overrides, key)))
        .join("");
      if (!rows) return "";
      return `<section class="settings-page-section"><div class="settings-page-section-header"><h2>${esc(this.categoryLabel(category))}</h2></div>${rows}</section>`;
    }).join("");
    content.querySelectorAll("[data-settings-reset]").forEach(button => {
      button.addEventListener("click", () => this.resetOne(button.dataset.settingsReset));
    });
    const disabled = Boolean(this.data?.build_active);
    document.getElementById("settings-save").disabled = disabled;
    document.getElementById("settings-reset-all").disabled = disabled;
  }

  renderRow(key, definition, value, customized) {
    let input;
    if (definition.type === "bool") {
      input = `<input class="settings-page-input" data-setting-key="${esc(key)}" type="checkbox" ${value ? "checked" : ""}>`;
    } else if (definition.type === "select") {
      input = `<select class="select input settings-page-input" data-setting-key="${esc(key)}">${(definition.options || []).map(option => `<option value="${esc(option)}" ${option === value ? "selected" : ""}>${esc(definition.option_labels?.[option] || option)}</option>`).join("")}</select>`;
    } else {
      input = `<input class="input settings-page-input" data-setting-key="${esc(key)}" type="number" value="${esc(value)}" min="${esc(definition.min)}" max="${esc(definition.max)}" step="${esc(definition.step || 1)}">`;
    }
    return `<div class="settings-page-row" data-settings-row="${esc(key)}">
      <div class="settings-page-copy"><strong>${esc(definition.label || key)}</strong><small>${esc(definition.description || "")}</small><code>${esc(key)}</code></div>
      <div class="settings-page-control">${input}<span class="topic-setting-source ${customized ? "is-custom" : ""}" data-setting-source>${esc(window.t(customized ? "topic.customValue" : "topic.defaultValue"))}</span><button class="btn btn-ghost btn-sm" type="button" data-settings-reset="${esc(key)}" ${customized ? "" : "disabled"}>${esc(window.t("topic.resetDefault"))}</button><small>${esc(window.t("topic.codeDefault"))}: ${esc(definition.default)}</small></div>
    </div>`;
  }

  resetOne(key) {
    const definition = this.data?.definitions?.[key];
    const input = document.querySelector(`.settings-page-input[data-setting-key="${CSS.escape(key)}"]`);
    if (!definition || !input) return;
    if (definition.type === "bool") input.checked = Boolean(definition.default);
    else input.value = definition.default;
    this.resetKeys.add(key);
    const row = document.querySelector(`[data-settings-row="${CSS.escape(key)}"]`);
    const source = row?.querySelector("[data-setting-source]");
    if (source) {
      source.classList.remove("is-custom");
      source.textContent = window.t("topic.defaultValue");
    }
    const reset = row?.querySelector("[data-settings-reset]");
    if (reset) reset.disabled = true;
  }

  resetAllValues() {
    this.resetAll = true;
    Object.keys(this.data?.definitions || {}).forEach(key => this.resetOne(key));
  }

  async save() {
    if (!this.data || this.data.build_active) return;
    const changes = {};
    try {
      document.querySelectorAll(".settings-page-input[data-setting-key]").forEach(input => {
        const key = input.dataset.settingKey;
        const definition = this.data.definitions[key];
        const value = definition.type === "bool" ? input.checked
          : definition.type === "select" ? input.value
          : definition.type === "int" ? Number.parseInt(input.value, 10)
          : Number.parseFloat(input.value);
        if (!["bool", "select"].includes(definition.type) && !Number.isFinite(value)) throw new Error(`${definition.label}: ${window.t("topic.invalidSetting")}`);
        if (value === definition.default) this.resetKeys.add(key);
        else changes[key] = value;
      });
      document.getElementById("settings-save").disabled = true;
      await this.api.post(SCOPES[this.scope].update, {
        changes,
        reset_keys: Array.from(this.resetKeys),
        reset_all: this.resetAll,
      });
      this.showToast(window.t("settings.saved"));
      await this.fetch();
    } catch (error) {
      this.showToast(error.message || window.t("settings.saveFailed"), true);
      document.getElementById("settings-save").disabled = false;
    }
  }
}

