import { esc } from "./utils.js";

export class TimelinePage {
  constructor(api, showToast) {
    this.api = api;
    this.showToast = showToast;
    this.trigger = null;
    this.data = null;
    this.resetKeys = new Set();
    this.resetAll = false;
  }

  initEventListeners() {
    document.getElementById("timeline-settings")?.addEventListener("click", event => this.open(event.currentTarget));
    document.getElementById("timeline-settings-close")?.addEventListener("click", () => this.close());
    document.getElementById("timeline-settings-cancel")?.addEventListener("click", () => this.close());
    document.getElementById("timeline-settings-save")?.addEventListener("click", () => this.save());
    document.getElementById("timeline-settings-reset-all")?.addEventListener("click", () => this.resetAllValues());
    document.getElementById("timeline-settings-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.close();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && this.isOpen()) this.close();
    });
  }

  isOpen() {
    return document.getElementById("timeline-settings-overlay")?.classList.contains("visible") || false;
  }

  async open(trigger) {
    this.trigger = trigger || null;
    this.resetKeys = new Set();
    this.resetAll = false;
    const overlay = document.getElementById("timeline-settings-overlay");
    overlay?.classList.add("visible");
    overlay?.setAttribute("aria-hidden", "false");
    document.getElementById("timeline-settings-status").textContent = window.t("common.loading");
    document.getElementById("timeline-settings-content").innerHTML = "";
    try {
      this.data = await this.api.get("timeline/settings");
      this.render();
      document.getElementById("timeline-settings-close")?.focus();
    } catch (error) {
      document.getElementById("timeline-settings-status").textContent = error.message || window.t("timeline.settingsLoadFailed");
    }
  }

  close({ restoreFocus = true } = {}) {
    const overlay = document.getElementById("timeline-settings-overlay");
    overlay?.classList.remove("visible");
    overlay?.setAttribute("aria-hidden", "true");
    const trigger = this.trigger;
    this.trigger = null;
    this.data = null;
    this.resetKeys = new Set();
    this.resetAll = false;
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }

  render() {
    const definitions = this.data?.definitions || {};
    const effective = this.data?.effective || {};
    const overrides = this.data?.overrides || {};
    document.getElementById("timeline-settings-status").textContent = "";
    document.getElementById("timeline-settings-save").disabled = false;
    document.getElementById("timeline-settings-reset-all").disabled = false;
    const categories = ["recall", "generation", "isolation", "lifecycle", "performance"];
    const content = document.getElementById("timeline-settings-content");
    content.innerHTML = categories.map(category => {
      const rows = Object.entries(definitions).filter(([, item]) => item.category === category).map(([key, definition]) => {
        const customized = Object.prototype.hasOwnProperty.call(overrides, key);
        const value = effective[key];
        let input;
        if (definition.type === "bool") {
          input = `<input class="timeline-setting-input" data-setting-key="${esc(key)}" type="checkbox" ${value ? "checked" : ""}>`;
        } else if (definition.type === "select") {
          input = `<select class="select input timeline-setting-input" data-setting-key="${esc(key)}">${(definition.options || []).map(option => `<option value="${esc(option)}" ${option === value ? "selected" : ""}>${esc(definition.option_labels?.[option] || option)}</option>`).join("")}</select>`;
        } else {
          input = `<input class="input timeline-setting-input" data-setting-key="${esc(key)}" type="number" value="${esc(value)}" min="${esc(definition.min)}" max="${esc(definition.max)}" step="${esc(definition.step || 1)}">`;
        }
        return `<div class="topic-setting-row" data-timeline-setting-row="${esc(key)}">
          <div class="topic-setting-copy"><strong>${esc(definition.label || key)}</strong><small class="topic-setting-description text-secondary">${esc(definition.description || "")}</small><small class="text-tertiary text-mono">${esc(key)}</small></div>
          <div class="topic-setting-control">${input}<span class="topic-setting-source ${customized ? "is-custom" : ""}" data-setting-source>${esc(window.t(customized ? "topic.customValue" : "topic.defaultValue"))}</span><button class="btn btn-ghost btn-sm" type="button" data-reset-timeline-setting="${esc(key)}" ${customized ? "" : "disabled"}>${esc(window.t("topic.resetDefault"))}</button></div>
          <div class="topic-setting-default text-tertiary">${esc(window.t("topic.codeDefault"))}: ${esc(definition.default)}</div>
        </div>`;
      }).join("");
      return `<section class="topic-settings-section"><h3>${esc(window.t(`timeline.settingsCategory.${category}`))}</h3>${rows}</section>`;
    }).join("");
    content.querySelectorAll("[data-reset-timeline-setting]").forEach(button => {
      button.addEventListener("click", () => this.resetOne(button.dataset.resetTimelineSetting));
    });
  }

  resetOne(key) {
    const definition = this.data?.definitions?.[key];
    const input = document.querySelector(`.timeline-setting-input[data-setting-key="${key}"]`);
    if (!definition || !input) return;
    if (definition.type === "bool") input.checked = Boolean(definition.default);
    else input.value = definition.default;
    this.resetKeys.add(key);
    const row = document.querySelector(`[data-timeline-setting-row="${key}"]`);
    row?.querySelector("[data-setting-source]")?.classList.remove("is-custom");
    if (row?.querySelector("[data-setting-source]")) row.querySelector("[data-setting-source]").textContent = window.t("topic.defaultValue");
    if (row?.querySelector("[data-reset-timeline-setting]")) row.querySelector("[data-reset-timeline-setting]").disabled = true;
  }

  resetAllValues() {
    this.resetAll = true;
    Object.keys(this.data?.definitions || {}).forEach(key => this.resetOne(key));
  }

  async save() {
    if (!this.data) return;
    const changes = {};
    try {
      document.querySelectorAll(".timeline-setting-input[data-setting-key]").forEach(input => {
        const key = input.dataset.settingKey;
        const definition = this.data.definitions[key];
        let value;
        if (definition.type === "bool") value = input.checked;
        else if (definition.type === "select") value = input.value;
        else value = definition.type === "int" ? Number.parseInt(input.value, 10) : Number.parseFloat(input.value);
        if (!['bool', 'select'].includes(definition.type) && !Number.isFinite(value)) throw new Error(`${definition.label}: ${window.t("topic.invalidSetting")}`);
        if (value === definition.default) this.resetKeys.add(key);
        else changes[key] = value;
      });
      document.getElementById("timeline-settings-save").disabled = true;
      await this.api.post("timeline/settings/update", { changes, reset_keys: Array.from(this.resetKeys), reset_all: this.resetAll });
      this.close();
      this.showToast(window.t("timeline.settingsSaved"));
    } catch (error) {
      this.showToast(error.message || window.t("timeline.settingsSaveFailed"), true);
      const save = document.getElementById("timeline-settings-save");
      if (save) save.disabled = false;
    }
  }
}
