import { esc } from "./utils.js";

export class ModelPage {
  constructor(api, showToast) {
    this.api = api;
    this.showToast = showToast;
    this.models = [];
  }

  initEventListeners() {
    document.getElementById("model-refresh")?.addEventListener("click", () => this.fetch());
    document.getElementById("model-cards")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-model-test]");
      if (button) this.test(button.dataset.modelTest, button);
    });
  }

  async fetch() {
    const container = document.getElementById("model-cards");
    if (!container) return;
    container.innerHTML = `<div class="model-page-state">${esc(window.t("common.loading"))}</div>`;
    try {
      const data = await this.api.get("models");
      this.models = Array.isArray(data.models) ? data.models : [];
      this.render();
    } catch (error) {
      container.innerHTML = `<div class="model-page-state model-page-state-error">${esc(error.message || window.t("models.loadFailed"))}</div>`;
      this.showToast(error.message || window.t("models.loadFailed"), true);
    }
  }

  render() {
    const container = document.getElementById("model-cards");
    if (!container) return;
    container.innerHTML = this.models.map(model => this.renderCard(model)).join("");
  }

  renderCard(model) {
    const roleLabel = window.t(`models.role.${model.role}`);
    const available = Boolean(model.available);
    const statusClass = available ? "available" : "unavailable";
    const statusLabel = window.t(
      available
        ? "models.available"
        : model.selection === "unavailable" && model.configured_provider_id
          ? "models.configurationError"
          : "models.unavailable"
    );
    const configured = model.configured_provider_id || window.t("models.notSpecified");
    const rows = [
      [window.t("models.selection"), window.t(`models.selection.${model.selection}`)],
      [window.t("models.configuredProvider"), configured],
      [window.t("models.actualProvider"), model.provider_id || "--"],
      [window.t("models.providerType"), model.provider_type || "--"],
      [window.t("models.modelName"), model.model || "--"],
      [window.t("models.runtimeClass"), model.runtime_class || "--"],
    ];
    if (model.extra?.dimension) rows.push([window.t("models.dimension"), model.extra.dimension]);
    if (model.extra?.base_url) rows.push([window.t("models.baseUrl"), model.extra.base_url]);
    if (model.extra?.fallback_provider_id) rows.push([window.t("models.fallbackProvider"), model.extra.fallback_provider_id]);
    if (model.extra?.initialization_error) rows.push([window.t("models.initializationError"), model.extra.initialization_error]);
    if (model.extra?.account_configured !== undefined) {
      rows.push([
        window.t("models.accountConfigured"),
        window.t(model.extra.account_configured ? "models.yes" : "models.no"),
      ]);
    }
    if (model.extra?.credential_source) {
      rows.push([
        window.t("models.credentialSource"),
        window.t(`models.credential.${model.extra.credential_source}`),
      ]);
    }

    return `
      <article class="model-card" data-model-role="${esc(model.role)}">
        <div class="model-card-header">
          <div>
            <div class="model-role">${esc(roleLabel)}</div>
            <div class="model-card-model">${esc(model.model || model.provider_id || window.t("models.noModel"))}</div>
          </div>
          <span class="model-status ${statusClass}">${esc(statusLabel)}</span>
        </div>
        <dl class="model-details">
          ${rows.map(([label, value]) => `<div class="model-detail-row"><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join("")}
        </dl>
        <div class="model-test-result" id="model-result-${esc(model.role)}"></div>
        <button class="btn btn-primary model-test-button" type="button" data-model-test="${esc(model.role)}" ${model.testable ? "" : "disabled"}>
          ${esc(window.t("models.testConnection"))}
        </button>
      </article>`;
  }

  async test(role, button) {
    const resultElement = document.getElementById(`model-result-${role}`);
    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = window.t("models.testing");
    if (resultElement) {
      resultElement.className = "model-test-result testing";
      resultElement.textContent = window.t("models.testingHint");
    }
    try {
      const result = await this.api.post("models/test", { role });
      const detail = this.formatResultDetails(result.details || {});
      if (resultElement) {
        resultElement.className = "model-test-result success";
        resultElement.textContent = window.t("models.testSuccess", result.latency_ms) + detail;
      }
      this.showToast(window.t("models.testSuccessToast", window.t(`models.role.${role}`)));
    } catch (error) {
      if (resultElement) {
        resultElement.className = "model-test-result error";
        resultElement.textContent = error.message || window.t("models.testFailed");
      }
      this.showToast(error.message || window.t("models.testFailed"), true);
    } finally {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  }

  formatResultDetails(details) {
    const parts = [];
    if (details.dimension) parts.push(window.t("models.resultDimension", details.dimension));
    if (details.result_count) parts.push(window.t("models.resultCount", details.result_count));
    if (details.top_score !== undefined) parts.push(window.t("models.resultScore", details.top_score));
    return parts.length ? ` · ${parts.join(" · ")}` : "";
  }
}
