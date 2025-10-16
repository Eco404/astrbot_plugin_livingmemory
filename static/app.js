(() => {
  const state = {
    token: localStorage.getItem("lmem_token") || "",
    page: 1,
    pageSize: 20,
    total: 0,
    hasMore: false,
    loadAll: false,
    filters: {
      status: "all",
      keyword: "",
    },
    items: [],
    selected: new Set(),
  };

  const dom = {
    loginView: document.getElementById("login-view"),
    dashboardView: document.getElementById("dashboard-view"),
    loginForm: document.getElementById("login-form"),
    loginError: document.getElementById("login-error"),
    passwordInput: document.getElementById("password-input"),
    refreshButton: document.getElementById("refresh-button"),
    loadAllButton: document.getElementById("load-all-button"),
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
    drawer: document.getElementById("detail-drawer"),
    drawerClose: document.getElementById("drawer-close"),
    detail: {
      memoryId: document.getElementById("detail-memory-id"),
      source: document.getElementById("detail-source"),
      status: document.getElementById("detail-status"),
      importance: document.getElementById("detail-importance"),
      type: document.getElementById("detail-type"),
      created: document.getElementById("detail-created"),
      access: document.getElementById("detail-access"),
      json: document.getElementById("detail-json"),
    },
  };

  function init() {
    dom.loginForm.addEventListener("submit", onLoginSubmit);
    dom.refreshButton.addEventListener("click", fetchAll);
    dom.loadAllButton.addEventListener("click", onLoadAll);
    dom.logoutButton.addEventListener("click", logout);
    dom.prevPage.addEventListener("click", goPrevPage);
    dom.nextPage.addEventListener("click", goNextPage);
    dom.pageSize.addEventListener("change", onPageSizeChange);
    dom.applyFilter.addEventListener("click", applyFilters);
    dom.selectAll.addEventListener("change", toggleSelectAll);
    dom.deleteSelected.addEventListener("click", deleteSelectedMemories);
    dom.drawerClose.addEventListener("click", closeDetailDrawer);
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
    await Promise.all([fetchStats(), fetchMemories()]);
  }

  async function fetchStats() {
    try {
      const stats = await apiRequest("/api/stats");
      dom.stats.total.textContent = stats.total_memories ?? "--";
      const breakdown = stats.status_breakdown || {};
      dom.stats.active.textContent = breakdown.active ?? 0;
      dom.stats.archived.textContent = breakdown.archived ?? 0;
      dom.stats.deleted.textContent = breakdown.deleted ?? 0;
      dom.stats.sessions.textContent = stats.active_sessions ?? 0;
    } catch (error) {
      showToast(error.message || "无法获取统计信息", true);
    }
  }

  async function fetchMemories() {
    const params = new URLSearchParams();
    if (!state.loadAll) {
      params.set("page", String(state.page));
      params.set("page_size", String(state.pageSize));
    } else {
      params.set("all", "true");
    }
    if (state.filters.status && state.filters.status !== "all") {
      params.set("status", state.filters.status);
    }
    if (state.filters.keyword) {
      params.set("keyword", state.filters.keyword);
    }

    try {
      const data = await apiRequest(`/api/memories?${params.toString()}`);
      state.items = Array.isArray(data.items) ? data.items : [];
      state.total = data.total ?? state.items.length;
      state.hasMore = Boolean(data.has_more);
      state.page = data.page ?? state.page;
      if (!state.loadAll && data.page_size) {
        state.pageSize = data.page_size;
      }
      state.selected.clear();
      dom.selectAll.checked = false;
      dom.deleteSelected.disabled = true;
      renderTable();
      updatePagination();
    } catch (error) {
      renderEmptyTable(error.message || "加载失败");
      showToast(error.message || "加载记忆失败", true);
    }
  }

  function renderTable() {
    if (!state.items.length) {
      renderEmptyTable("暂无数据");
      return;
    }

    const rows = state.items
      .map((item) => {
        const key = getItemKey(item);
        const checked = state.selected.has(key) ? "checked" : "";
        const rowClass = state.selected.has(key) ? "selected" : "";
        const importance =
          item.importance !== undefined && item.importance !== null
            ? Number(item.importance).toFixed(2)
            : "--";
        const statusPill = formatStatus(item.status);

        return `
          <tr data-key="${escapeHTML(key)}" class="${rowClass}">
            <td>
              <input type="checkbox" class="row-select" data-key="${escapeHTML(
                key
              )}" ${checked} />
            </td>
            <td class="mono">${escapeHTML(item.memory_id || item.doc_id || "-")}</td>
            <td class="summary-cell" title="${escapeHTML(item.summary || "")}">
              ${escapeHTML(item.summary || "（无摘要）")}
            </td>
            <td>${escapeHTML(item.memory_type || "--")}</td>
            <td>${importance}</td>
            <td>${statusPill}</td>
            <td>${escapeHTML(item.created_at || "--")}</td>
            <td>${escapeHTML(item.last_access || "--")}</td>
            <td>
              <div class="table-actions">
                <button class="ghost detail-btn" data-key="${escapeHTML(
                  key
                )}">详情</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");

    dom.tableBody.innerHTML = rows;

    dom.tableBody.querySelectorAll(".row-select").forEach((checkbox) => {
      checkbox.addEventListener("change", onRowSelect);
    });
    dom.tableBody.querySelectorAll(".detail-btn").forEach((btn) => {
      btn.addEventListener("click", onDetailClick);
    });
  }

  function renderEmptyTable(message) {
    dom.tableBody.innerHTML = `
      <tr>
        <td colspan="9" class="empty">${escapeHTML(message)}</td>
      </tr>
    `;
  }

  function onRowSelect(event) {
    const checkbox = event.target;
    const key = checkbox.dataset.key;
    if (!key) return;

    if (checkbox.checked) {
      state.selected.add(key);
    } else {
      state.selected.delete(key);
    }

    const row = checkbox.closest("tr");
    if (row) {
      row.classList.toggle("selected", checkbox.checked);
    }
    updateSelectionState();
  }

  function toggleSelectAll(event) {
    const checked = event.target.checked;
    if (!state.items.length) {
      event.target.checked = false;
      return;
    }
    state.items.forEach((item) => {
      const key = getItemKey(item);
      if (checked) {
        state.selected.add(key);
      } else {
        state.selected.delete(key);
      }
    });
    renderTable();
    updateSelectionState();
  }

  function updateSelectionState() {
    dom.deleteSelected.disabled = state.selected.size === 0;
    if (!state.items.length) {
      dom.selectAll.checked = false;
      return;
    }
    const allSelected = state.items.every((item) =>
      state.selected.has(getItemKey(item))
    );
    dom.selectAll.checked = allSelected;
  }

  function applyFilters() {
    state.filters.status = dom.statusFilter.value;
    state.filters.keyword = dom.keywordInput.value.trim();
    state.page = 1;
    state.loadAll = false;
    dom.loadAllButton.classList.remove("active");
    fetchMemories();
  }

  function onPageSizeChange() {
    state.pageSize = Number(dom.pageSize.value) || 20;
    state.page = 1;
    state.loadAll = false;
    dom.loadAllButton.classList.remove("active");
    fetchMemories();
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

  function onLoadAll() {
    state.loadAll = !state.loadAll;
    dom.loadAllButton.classList.toggle("active", state.loadAll);
    state.page = 1;
    fetchMemories();
  }

  function updatePagination() {
    if (state.loadAll) {
      dom.paginationInfo.textContent = `共 ${state.items.length} 条记录`;
    } else {
      const totalPages = state.total
        ? Math.max(1, Math.ceil(state.total / state.pageSize))
        : 1;
      dom.paginationInfo.textContent = `第 ${state.page} / ${totalPages} 页 · 共 ${state.total} 条`;
    }
    dom.prevPage.disabled = state.loadAll || state.page <= 1;
    dom.nextPage.disabled = state.loadAll || !state.hasMore;
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

    const docIds = [];
    const memoryIds = [];
    state.items.forEach((item) => {
      const key = getItemKey(item);
      if (state.selected.has(key)) {
        if (item.doc_id !== null && item.doc_id !== undefined) {
          docIds.push(item.doc_id);
        }
        if (item.memory_id) {
          memoryIds.push(item.memory_id);
        }
      }
    });

    try {
      await apiRequest("/api/memories", {
        method: "DELETE",
        body: {
          doc_ids: docIds,
          memory_ids: memoryIds,
        },
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

  function onDetailClick(event) {
    const key = event.target.dataset.key;
    if (!key) return;
    const item = state.items.find((record) => getItemKey(record) === key);
    if (!item) {
      showToast("未找到对应的记录", true);
      return;
    }
    openDetailDrawer(item);
  }

  function openDetailDrawer(item) {
    dom.detail.memoryId.textContent = item.memory_id || item.doc_id || "--";
    dom.detail.source.textContent =
      item.source === "storage" ? "自定义存储" : "向量存储";
    dom.detail.status.textContent = item.status || "--";
    dom.detail.importance.textContent =
      item.importance !== undefined && item.importance !== null
        ? Number(item.importance).toFixed(2)
        : "--";
    dom.detail.type.textContent = item.memory_type || "--";
    dom.detail.created.textContent = item.created_at || "--";
    dom.detail.access.textContent = item.last_access || "--";
    dom.detail.json.textContent = item.raw_json || JSON.stringify(item.raw, null, 2);
    dom.drawer.classList.remove("hidden");
  }

  function closeDetailDrawer() {
    dom.drawer.classList.add("hidden");
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
      const message =
        (data && (data.detail || data.message || data.error)) || "请求失败";
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
    }, 3000);
  }

  function getItemKey(item) {
    if (item.doc_id !== null && item.doc_id !== undefined) {
      return `doc:${item.doc_id}`;
    }
    if (item.memory_id) {
      return `mem:${item.memory_id}`;
    }
    return `row:${state.items.indexOf(item)}`;
  }

  function formatStatus(status) {
    const value = (status || "active").toLowerCase();
    let text = "活跃";
    let cls = "status-pill";
    if (value === "archived") {
      text = "已归档";
      cls += " archived";
    } else if (value === "deleted") {
      text = "已删除";
      cls += " deleted";
    }
    return `<span class="${cls}">${text}</span>`;
  }

  function escapeHTML(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  document.addEventListener("DOMContentLoaded", init);
})();
