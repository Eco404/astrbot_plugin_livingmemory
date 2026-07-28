export class ConfirmDialog {
  constructor() {
    this.pending = null;
    this.trigger = null;
  }

  initEventListeners() {
    document.getElementById("action-confirm-close")?.addEventListener("click", () => this.close(false));
    document.getElementById("action-confirm-cancel")?.addEventListener("click", () => this.close(false));
    document.getElementById("action-confirm-submit")?.addEventListener("click", () => this.close(true));
    document.getElementById("action-confirm-overlay")?.addEventListener("click", event => {
      if (event.target === event.currentTarget) this.close(false);
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && this.isOpen()) this.close(false);
    });
  }

  isOpen() {
    return document.getElementById("action-confirm-overlay")?.classList.contains("visible") || false;
  }

  show({ title, message, confirmLabel, danger = true }) {
    if (this.pending) this.close(false);
    this.trigger = document.activeElement;
    document.getElementById("action-confirm-title").textContent = title;
    document.getElementById("action-confirm-message").textContent = message;
    const submit = document.getElementById("action-confirm-submit");
    submit.textContent = confirmLabel || window.t("common.confirm");
    submit.classList.toggle("btn-danger", danger);
    submit.classList.toggle("btn-primary", !danger);
    const overlay = document.getElementById("action-confirm-overlay");
    overlay.classList.add("visible");
    overlay.setAttribute("aria-hidden", "false");
    document.getElementById("action-confirm-cancel")?.focus();
    return new Promise(resolve => { this.pending = resolve; });
  }

  close(result) {
    if (!this.pending) return;
    const overlay = document.getElementById("action-confirm-overlay");
    overlay.classList.remove("visible");
    overlay.setAttribute("aria-hidden", "true");
    const resolve = this.pending;
    this.pending = null;
    resolve(Boolean(result));
    this.trigger?.focus?.();
    this.trigger = null;
  }
}
