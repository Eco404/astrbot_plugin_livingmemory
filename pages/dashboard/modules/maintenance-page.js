export class MaintenancePage {
  constructor(topicPage, recallPage, modelPage, showToast) {
    this.topicPage = topicPage;
    this.recallPage = recallPage;
    this.modelPage = modelPage;
    this.showToast = showToast;
    this.tab = "topic";
  }

  initEventListeners() {
    document.querySelectorAll("[data-maintenance-tab]").forEach(button => {
      button.addEventListener("click", () => this.selectTab(button.dataset.maintenanceTab));
    });
    document.getElementById("maintenance-open-topic")?.addEventListener("click", event => this.openTopicMaintenance(event.currentTarget));
  }

  async activate() {
    await this.topicPage.fetch();
    this.syncTopicSpaces();
    if (this.tab === "models") this.modelPage.fetch();
    if (this.tab === "recall") this.recallPage.fetchSessions();
  }

  selectTab(tab) {
    if (!tab) return;
    this.tab = tab;
    document.querySelectorAll("[data-maintenance-tab]").forEach(button => {
      const active = button.dataset.maintenanceTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".maintenance-panel").forEach(panel => panel.classList.toggle("active", panel.id === `maintenance-panel-${tab}`));
    if (tab === "models") this.modelPage.fetch();
    if (tab === "recall") this.recallPage.fetchSessions();
  }

  syncTopicSpaces() {
    const source = document.getElementById("topic-space");
    const target = document.getElementById("maintenance-topic-space");
    if (!source || !target) return;
    const previous = target.value;
    target.innerHTML = source.innerHTML;
    target.value = previous && Array.from(target.options).some(option => option.value === previous)
      ? previous
      : source.value;
  }

  async openTopicMaintenance(trigger) {
    const selected = document.getElementById("maintenance-topic-space")?.value || "";
    if (!selected) {
      this.showToast(window.t("topic.chooseSpace"), true);
      return;
    }
    const topicSpace = document.getElementById("topic-space");
    topicSpace.value = selected;
    await this.topicPage.fetch();
    this.topicPage.openMaintenance(trigger);
  }
}
