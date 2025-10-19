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
    nuke: {
      active: false,
      operationId: null,
      secondsLeft: 0,
      timer: null,
    },
  };

  const dom = {
    loginView: document.getElementById("login-view"),
    dashboardView: document.getElementById("dashboard-view"),
    loginForm: document.getElementById("login-form"),
    loginError: document.getElementById("login-error"),
    passwordInput: document.getElementById("password-input"),
    refreshButton: document.getElementById("refresh-button"),
    loadAllButton: document.getElementById("load-all-button"),
    nukeButton: document.getElementById("nuke-button"),
    nukeBanner: document.getElementById("nuke-banner"),
    nukeMessage: document.getElementById("nuke-message"),
    nukeCancel: document.getElementById("nuke-cancel"),
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
    dom.nukeButton.addEventListener("click", onNukeClick);
    dom.logoutButton.addEventListener("click", logout);
    dom.prevPage.addEventListener("click", goPrevPage);
    dom.nextPage.addEventListener("click", goNextPage);
    dom.pageSize.addEventListener("change", onPageSizeChange);
    dom.applyFilter.addEventListener("click", applyFilters);
    dom.selectAll.addEventListener("change", toggleSelectAll);
    dom.deleteSelected.addEventListener("click", deleteSelectedMemories);
    dom.drawerClose.addEventListener("click", closeDetailDrawer);
    dom.nukeCancel.addEventListener("click", onNukeCancel);
    dom.keywordInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        applyFilters();
      }
    });

    if (state.token) {
      switchView("dashboard");
      showToast("Session restored, loading data...");
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
      dom.loginError.textContent = "Please enter the password";
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
      showToast("Login successful, loading data...");
      fetchAll();
    } catch (error) {
      dom.loginError.textContent = error.message || "Login failed, please try again";
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
    await initNukeStatus();
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
      if (!state.loadAll) {
        const totalPages = state.pageSize
          ? Math.max(1, Math.ceil(state.total / state.pageSize))
          : 1;
        if (state.page > totalPages) {
          state.page = totalPages;
          await fetchMemories();
          return;
        }
      }
      state.selected.clear();
      dom.selectAll.checked = false;
      dom.deleteSelected.disabled = true;
      renderTable();
      updatePagination();
    } catch (error) {
      renderEmptyTable(error.message || "加载失败");
      showToast(error.message || "Failed to load memories", true);
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
    const confirmed = window.confirm(`Delete ${count} record(s)? This action cannot be undone.`);
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
      showToast(`Deleted ${count} record(s)`);
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
    showToast("Logged out");
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

  async function initNukeStatus() {
    if (!state.token) {
      return;
    }
    try {
      const status = await apiRequest("/api/memories/nuke");
      if (status && status.pending) {
        startNukeCountdown(status);
      } else {
      }
    } catch (_error) {
    }
  }

  async function onNukeClick() {
    if (state.nuke.active) {
      return;
    }
    dom.nukeButton.disabled = true;
    try {
      const result = await apiRequest("/api/memories/nuke", { method: "POST" });
      if (result && result.pending) {
        startNukeCountdown(result);
        showToast(result.detail || "Memory wipe scheduled");
      } else {
        dom.nukeButton.disabled = false;
      }
    } catch (error) {
      dom.nukeButton.disabled = false;
      showToast(error.message || "Unable to schedule memory wipe", true);
    }
  }

  async function onNukeCancel() {
    if (!state.nuke.active || !state.nuke.operationId) {
      return;
    }
    dom.nukeCancel.disabled = true;
    try {
      await apiRequest(`/api/memories/nuke/${state.nuke.operationId}`, {
        method: "DELETE",
      });
      showToast("Memory wipe cancelled");
    } catch (error) {
      dom.nukeCancel.disabled = false;
      showToast(error.message || "取消失败，请稍后重试", true);
    }
  }

  function startNukeCountdown(info) {
    const seconds = Number.isFinite(Number(info.seconds_left))
      ? Number(info.seconds_left)
      : 30;

    state.nuke.active = true;
    state.nuke.operationId = info.operation_id || null;
    state.nuke.secondsLeft = seconds;

    updateNukeBanner();
    dom.nukeButton.disabled = true;

    state.nuke.timer = setInterval(() => {
      if (state.nuke.secondsLeft > 0) {
        state.nuke.secondsLeft -= 1;
        updateNukeBanner();
        return;
      }

      clearInterval(state.nuke.timer);
      state.nuke.timer = null;
      updateNukeBanner();
      dom.nukeCancel.disabled = true;

      setTimeout(async () => {
        await fetchAll();
        showToast("All memories have been cleared");
      }, 1500);
    }, 1000);
  }

  function stopNukeCountdown() {
    if (state.nuke.timer) {
      clearInterval(state.nuke.timer);
      state.nuke.timer = null;
    }
    state.nuke.active = false;
    state.nuke.operationId = null;
    state.nuke.secondsLeft = 0;
    if (dom.nukeBanner) {
      dom.nukeBanner.classList.add("hidden");
    }
    if (dom.nukeMessage) {
      dom.nukeMessage.textContent = "";
    }
    if (dom.nukeCancel) {
      dom.nukeCancel.disabled = false;
    }
    if (dom.nukeButton) {
      dom.nukeButton.disabled = false;
    }
  }

  function updateNukeBanner() {
    if (!state.nuke.active || !dom.nukeBanner) {
      return;
    }
    dom.nukeBanner.classList.remove("hidden");
    const seconds = Math.max(0, state.nuke.secondsLeft);
    const message =
      seconds > 0
        ? `All memories will be erased in ${seconds}s. Cancel now to abort.`
        : "Erasing all memories... please keep this window open.";
    dom.nukeMessage.textContent = message;
    if (dom.nukeCancel) {
      dom.nukeCancel.disabled = seconds === 0;
    }
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
      throw new Error("Session expired, please sign in again");
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
