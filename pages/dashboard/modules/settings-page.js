import { esc } from "./utils.js";
import { bindSettingDependencies } from "./setting-dependencies.js";

export class SettingsPage {
  constructor(api, showToast) {
    this.api = api;
    this.showToast = showToast;
    this.category = "recall";
    this.data = {};
    this.resetKeys = new Set();
  }

  initEventListeners() {
    document.querySelectorAll("[data-settings-category]").forEach(button => {
      button.addEventListener("click", () => this.selectCategory(button.dataset.settingsCategory));
    });
    document.getElementById("settings-save")?.addEventListener("click", () => this.save());
    document.getElementById("settings-reset-all")?.addEventListener("click", () => this.resetAllValues());
  }

  selectCategory(nextCategory) {
    if (nextCategory === this.category) return;
    this.category = nextCategory;
    this.resetKeys = new Set();
    document.querySelectorAll("[data-settings-category]").forEach(button => {
      const active = button.dataset.settingsCategory === nextCategory;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    this.render();
  }

  async fetch() {
    const content = document.getElementById("settings-page-content");
    const status = document.getElementById("settings-page-status");
    if (!content || !status) return;
    status.textContent = window.t("common.loading");
    content.innerHTML = "";
    this.resetKeys = new Set();
    try {
      this.data = await this.api.get("settings");
      this.render();
    } catch (error) {
      status.textContent = error.message || window.t("settings.loadFailed");
    }
  }

  currentCategory() {
    return (this.data.categories || []).find(item => item.id === this.category) || null;
  }

  render() {
    const content = document.getElementById("settings-page-content");
    const status = document.getElementById("settings-page-status");
    if (!content || !status) return;
    const category = this.currentCategory();
    const definitions = this.data.definitions || {};
    const effective = this.data.effective || {};
    const overrides = this.data.overrides || {};
    const locked = (category?.groups || []).some(group => (
      group.keys.some(key => definitions[key]?.locked)
    ));
    status.textContent = locked ? window.t("topic.settingsBuildActive") : "";
    content.innerHTML = (category?.groups || []).map(group => {
      const rows = group.keys
        .filter(key => definitions[key] && !definitions[key].deprecated)
        .map(key => this.renderRow(
          key,
          definitions[key],
          effective[key],
          Object.prototype.hasOwnProperty.call(overrides, key),
        )).join("");
      return `<section class="settings-page-section"><div class="settings-page-section-header"><h2>${esc(window.t(group.label))}</h2></div>${rows}</section>`;
    }).join("");
    content.querySelectorAll("[data-settings-reset]").forEach(button => {
      button.addEventListener("click", () => this.resetOne(button.dataset.settingsReset));
    });
    this.refreshVisibility = bindSettingDependencies({
      root: content,
      definitions,
      effectiveValues: effective,
      inputSelector: ".settings-page-input",
      rowSelector: ".settings-page-row",
      sectionSelector: ".settings-page-section",
    });
    const hasEditable = Boolean(content.querySelector(".settings-page-input:not(:disabled)"));
    document.getElementById("settings-save").disabled = !hasEditable;
    document.getElementById("settings-reset-all").disabled = !hasEditable;
  }

  renderRow(key, definition, value, customized) {
    const disabled = definition.locked ? "disabled" : "";
    let input;
    if (definition.type === "bool") {
      input = `<input class="settings-page-input" data-setting-key="${esc(key)}" type="checkbox" ${value ? "checked" : ""} ${disabled}>`;
    } else if (definition.type === "select") {
      input = `<select class="select input settings-page-input" data-setting-key="${esc(key)}" ${disabled}>${(definition.options || []).map(option => `<option value="${esc(option)}" ${option === value ? "selected" : ""}>${esc(definition.option_labels?.[option] || option)}</option>`).join("")}</select>`;
    } else if (definition.type === "string") {
      input = `<input class="input settings-page-input" data-setting-key="${esc(key)}" type="text" value="${esc(value || "")}" ${disabled}>`;
    } else {
      input = `<input class="input settings-page-input" data-setting-key="${esc(key)}" type="number" value="${esc(value)}" min="${esc(definition.min)}" max="${esc(definition.max)}" step="${esc(definition.step || 1)}" ${disabled}>`;
    }
    return `<div class="settings-page-row" data-settings-row="${esc(key)}">
      <div class="settings-page-copy"><strong>${esc(definition.label || key)}</strong><small>${esc(definition.description || "")}</small><code>${esc(key)}</code></div>
      <div class="settings-page-control">${input}<span class="topic-setting-source ${customized ? "is-custom" : ""}" data-setting-source-label>${esc(window.t(customized ? "topic.customValue" : "topic.defaultValue"))}</span><button class="btn btn-ghost btn-sm" type="button" data-settings-reset="${esc(key)}" ${customized && !definition.locked ? "" : "disabled"}>${esc(window.t("topic.resetDefault"))}</button><small>${esc(window.t("topic.codeDefault"))}: ${esc(definition.default)}</small></div>
    </div>`;
  }

  row(key) {
    return document.querySelector(`[data-settings-row="${CSS.escape(key)}"]`);
  }

  resetOne(key) {
    const definition = this.data.definitions?.[key];
    const input = this.row(key)?.querySelector(".settings-page-input");
    if (!definition || !input || input.disabled) return;
    if (definition.type === "bool") input.checked = Boolean(definition.default);
    else input.value = definition.default;
    this.resetKeys.add(key);
    const row = this.row(key);
    const sourceLabel = row?.querySelector("[data-setting-source-label]");
    if (sourceLabel) {
      sourceLabel.classList.remove("is-custom");
      sourceLabel.textContent = window.t("topic.defaultValue");
    }
    const reset = row?.querySelector("[data-settings-reset]");
    if (reset) reset.disabled = true;
    this.refreshVisibility?.();
  }

  resetAllValues() {
    document.querySelectorAll(".settings-page-input:not(:disabled)").forEach(input => {
      this.resetOne(input.dataset.settingKey);
    });
  }

  async save() {
    const changes = {};
    try {
      document.querySelectorAll(".settings-page-input[data-setting-key]:not(:disabled)").forEach(input => {
        const key = input.dataset.settingKey;
        const definition = this.data.definitions[key];
        const value = definition.type === "bool" ? input.checked
          : ["select", "string"].includes(definition.type) ? input.value
          : definition.type === "int" ? Number.parseInt(input.value, 10)
          : Number.parseFloat(input.value);
        if (!["bool", "select", "string"].includes(definition.type) && !Number.isFinite(value)) {
          throw new Error(`${definition.label}: ${window.t("topic.invalidSetting")}`);
        }
        if (value === definition.default) this.resetKeys.add(key);
        else changes[key] = value;
      });
      if (!Object.keys(changes).length && !this.resetKeys.size) return;
      document.getElementById("settings-save").disabled = true;
      await this.api.post("settings/update", {
        changes,
        reset_keys: Array.from(this.resetKeys),
        reset_all: false,
      });
      this.showToast(window.t("settings.saved"));
      await this.fetch();
    } catch (error) {
      this.showToast(error.message || window.t("settings.saveFailed"), true);
      document.getElementById("settings-save").disabled = false;
    }
  }
}
