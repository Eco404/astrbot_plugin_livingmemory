(() => {
  const state = {
    token: localStorage.getItem("lmem_token") || "",
    page: 1,
    pageSize: 20,
    total: 0,
    hasMore: false,
    items: [],
    filters: {
      status: "all",
      keyword: "",
    },
    selected: new Set(),
  };

  const dom = {
    loginView: document.getElementById("login-view"),
    dashboardView: document.getElementById("dashboard-view"),
    loginForm: document.getElementById("login-form"),
    loginError: document.getElementById("login-error"),
    passwordInput: document.getElementById("password-input"),
    refreshButton: document.getElementById("refresh-button"),
    logoutButton: document.getElementById("logout-button"),
    stats: {
      total: document.getElementById("stat-total"),
      active: document.getElementById("stat-active"),
      archived: document.getElementById("stat-archived"),
      deleted: document.getElementById("stat-deleted"),
      sessions: document.getElementById("stat-sessions"),
    },
    keywordInput: document.getElementById("keyword-input"),
    statusFilter: document.getElementById("status-filter"),
    applyFilter: document.getElementById("apply-filter"),
    selectAll: document.getElementById("select-all"),
    deleteSelected: document.getElementById("delete-selected"),
    tableBody: document.getElementById("memories-body"),
    paginationInfo: document.getElementById("pagination-info"),
    prevPage: document.getElementById("prev-page"),
    nextPage: document.getElementById("next-page"),
    pageSize: document.getElementById("page-size"),
    toast: document.getElementById("toast"),
  };

  function init() {
    dom.loginForm.addEventListener("submit", onLoginSubmit);
    dom.refreshButton.addEventListener("click", () => {
      fetchAll();
    });
    dom.logoutButton.addEventListener("click", logout);
    dom.prevPage.addEventListener("click", goPrevPage);
    dom.nextPage.addEventListener("click", goNextPage);
    dom.pageSize.addEventListener("change", onPageSizeChange);
    dom.applyFilter.addEventListener("click", applyFilters);
    dom.selectAll.addEventListener("change", toggleSelectAll);
    dom.deleteSelected.addEventListener("click", deleteSelectedMemories);

    dom.keywordInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        applyFilters();
      }
    });

    if (state.token) {
      switchView("dashboard");
      fetchAll();
    } else {
      switchView("login");
    }
  }

  async function onLoginSubmit(event) {
    event.preventDefault();
    const password = dom.passwordInput.value.trim();
    dom.loginError.textContent = "";

    if (!password) {
      dom.loginError.textContent = "请输入密码";
      return;
    }

    try {
      const result = await apiRequest("/api/login", {
        method: "POST",
        body: { password },
        skipAuth: true,
      });
      state.token = result.token;
      localStorage.setItem("lmem_token", state.token);
      dom.passwordInput.value = "";
      switchView("dashboard");
      showToast("登录成功，正在加载数据…");
      fetchAll();
    } catch (error) {
      dom.loginError.textContent = error.message || "登录失败，请重试";
    }
  }

  function switchView(view) {
    if (view === "dashboard") {
      dom.loginView.classList.remove("active");
      dom.dashboardView.classList.add("active");
    } else {
      dom.dashboardView.classList.remove("active");
      dom.loginView.classList.add("active");
    }
  }

  async function fetchAll() {
    state.selected.clear();
    dom.selectAll.checked = false;
    dom.deleteSelected.disabled = true;
    await Promise.all([fetchStats(), fetchMemories()]);
  }

  async function fetchStats() {
    try {
      const stats = await apiRequest("/api/stats");
      dom.stats.total.textContent = stats.total_memories ?? "--";
      dom.stats.active.textContent = stats.status_breakdown?.active ?? 0;
      dom.stats.archived.textContent = stats.status_breakdown?.archived ?? 0;
      dom.stats.deleted.textContent = stats.status_breakdown?.deleted ?? 0;
      dom.stats.sessions.textContent = stats.active_sessions ?? 0;
    } catch (error) {
      showToast(error.message || "无法获取统计信息", true);
    }
  }

  async function fetchMemories() {
    try {
      const params = new URLSearchParams({
        page: String(state.page),
        page_size: String(state.pageSize),
      });
      const result = await apiRequest(`/api/memories?${params.toString()}`);
      state.total = result.total || 0;
      state.hasMore = Boolean(result.has_more);
      state.items = Array.isArray(result.items) ? result.items : [];
      renderTable();
      updatePagination();
    } catch (error) {
      renderEmptyTable(error.message || "加载失败");
      showToast(error.message || "加载记忆失败", true);
    }
  }

  function getFilteredItems() {
    let items = [...state.items];
    if (state.filters.status !== "all") {
      items = items.filter((item) => item.status === state.filters.status);
    }
    if (state.filters.keyword) {
      const keyword = state.filters.keyword.toLowerCase();
      items = items.filter((item) => {
        const base = `${item.content || ""} ${item.session_id || ""}`.toLowerCase();
        return base.includes(keyword);
      });
    }
    return items;
  }

  function renderTable() {
    const items = getFilteredItems();
    if (!items.length) {
      renderEmptyTable("暂无数据");
      return;
    }

    const rows = items
      .map((item) => {
        const id = String(item.id);
        const checked = state.selected.has(id) ? "checked" : "";
        const statusClass = `status-pill ${item.status || "active"}`;
        const importance = item.importance !== undefined && item.importance !== null
          ? Number(item.importance).toFixed(2)
          : "--";
        const preview = escapeHTML(item.preview || item.content || "")
          .replace(/\s+/g, " ")
          .slice(0, 140);

        return `
          <tr data-id="${id}" class="${state.selected.has(id) ? "selected" : ""}">
            <td>
              <input type="checkbox" class="row-select" data-id="${id}" ${checked} />
            </td>
            <td>${id}</td>
            <td>
              <div class="preview" title="${preview}">
                ${preview || "<span class='muted'>（无内容）</span>"}
              </div>
            </td>
            <td>${importance}</td>
            <td><span class="${statusClass}">${statusLabel(item.status)}</span></td>
            <td>${formatDate(item.create_time) || "--"}</td>
            <td>${formatDate(item.last_access_time) || "--"}</td>
          </tr>
        `;
      })
      .join("");

    dom.tableBody.innerHTML = rows;
    dom.tableBody.querySelectorAll(".row-select").forEach((checkbox) => {
      checkbox.addEventListener("change", onRowSelect);
    });
  }

  function renderEmptyTable(message) {
    dom.tableBody.innerHTML = `
      <tr>
        <td colspan="7" class="empty">${escapeHTML(message)}</td>
      </tr>
    `;
  }

  function onRowSelect(event) {
    const checkbox = event.target;
    const id = String(checkbox.dataset.id);
    if (checkbox.checked) {
      state.selected.add(id);
    } else {
      state.selected.delete(id);
    }
    const row = checkbox.closest("tr");
    if (row) {
      row.classList.toggle("selected", checkbox.checked);
    }
    updateSelectionState();
  }

  function toggleSelectAll(event) {
    const checked = event.target.checked;
    const items = getFilteredItems();
    items.forEach((item) => {
      const id = String(item.id);
      if (checked) {
        state.selected.add(id);
      } else {
        state.selected.delete(id);
      }
    });
    renderTable();
    updateSelectionState();
  }

  function updateSelectionState() {
    dom.deleteSelected.disabled = state.selected.size === 0;

    const items = getFilteredItems();
    const allSelected = items.length > 0 && items.every((item) => state.selected.has(String(item.id)));
    dom.selectAll.checked = allSelected;
  }

  function applyFilters() {
    state.filters.status = dom.statusFilter.value;
    state.filters.keyword = dom.keywordInput.value.trim();
    state.selected.clear();
    renderTable();
    updateSelectionState();
  }

  function goPrevPage() {
    if (state.page > 1) {
      state.page -= 1;
      fetchMemories();
    }
  }

  function goNextPage() {
    if (state.hasMore) {
      state.page += 1;
      fetchMemories();
    }
  }

  function onPageSizeChange() {
    state.pageSize = Number(dom.pageSize.value) || 20;
    state.page = 1;
    fetchMemories();
  }

  function updatePagination() {
    const totalPages = state.total ? Math.max(1, Math.ceil(state.total / state.pageSize)) : 1;
    const filteredCount = getFilteredItems().length;
    dom.paginationInfo.textContent = `第 ${state.page} 页 · 共 ${totalPages} 页 · 当前 ${filteredCount} 条`;
    dom.prevPage.disabled = state.page <= 1;
    dom.nextPage.disabled = !state.hasMore;
  }

  async function deleteSelectedMemories() {
    if (state.selected.size === 0) {
      return;
    }
    const count = state.selected.size;
    const confirmed = window.confirm(`确认删除选中的 ${count} 条记忆？该操作不可撤销。`);
    if (!confirmed) {
      return;
    }

    try {
      const ids = Array.from(state.selected).map((id) => Number(id));
      await apiRequest("/api/memories", {
        method: "DELETE",
        body: { ids },
      });
      showToast(`已删除 ${count} 条记忆`);
      state.selected.clear();
      fetchMemories();
      fetchStats();
    } catch (error) {
      showToast(error.message || "删除失败", true);
    }
  }

  function logout() {
    state.token = "";
    state.selected.clear();
    localStorage.removeItem("lmem_token");
    switchView("login");
    showToast("已退出登录");
  }

  async function apiRequest(path, options = {}) {
    const { method = "GET", body, skipAuth = false } = options;
    const headers = new Headers(options.headers || {});

    if (body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }

    if (!skipAuth) {
      if (!state.token) {
        handleAuthFailure();
        throw new Error("尚未登录");
      }
      headers.set("Authorization", `Bearer ${state.token}`);
    }

    const fetchOptions = {
      method,
      headers,
      credentials: "same-origin",
    };

    if (body !== undefined) {
      fetchOptions.body = typeof body === "string" ? body : JSON.stringify(body);
    }

    const response = await fetch(path, fetchOptions);

    if (response.status === 401) {
      handleAuthFailure();
      throw new Error("会话已过期，请重新登录");
    }

    let data;
    try {
      data = await response.json();
    } catch (error) {
      throw new Error("服务器返回格式错误");
    }

    if (!response.ok) {
      const message = (data && (data.detail || data.message)) || "请求失败";
      throw new Error(message);
    }

    return data;
  }

  function handleAuthFailure() {
    state.token = "";
    localStorage.removeItem("lmem_token");
    switchView("login");
  }

  function showToast(message, isError = false) {
    dom.toast.textContent = message;
    dom.toast.classList.remove("hidden", "error");
    if (isError) {
      dom.toast.classList.add("error");
    }
    dom.toast.classList.add("visible");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => {
      dom.toast.classList.remove("visible");
    }, 3200);
  }

  function statusLabel(status) {
    switch ((status || "").toLowerCase()) {
      case "archived":
        return "已归档";
      case "deleted":
        return "已删除";
      default:
        return "活跃";
    }
  }

  function formatDate(value) {
    if (!value || value === "None") {
      return "";
    }
    try {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) {
        return value;
      }
      return date.toLocaleString("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (error) {
      return value;
    }
  }

  function escapeHTML(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  document.addEventListener("DOMContentLoaded", init);
})();

