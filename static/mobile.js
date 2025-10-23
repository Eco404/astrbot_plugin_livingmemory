// 移动版 LivingMemory 控制台交互逻辑

// DOM 元素引用
const dom = {
  // 视图
  loginView: document.getElementById('login-view'),
  mainView: document.getElementById('main-view'),
  listView: document.getElementById('list-view'),
  detailView: document.getElementById('detail-view'),
  settingsView: document.getElementById('settings-view'),
  
  // 登录相关
  loginForm: document.getElementById('login-form'),
  passwordInput: document.getElementById('password-input'),
  loginError: document.getElementById('login-error'),
  
  // 导航相关
  backButton: document.getElementById('back-button'),
  backToList: document.getElementById('back-to-list'),
  backFromSettings: document.getElementById('back-from-settings'),
  refreshButton: document.getElementById('refresh-button'),
  logoutButton: document.getElementById('logout-button'),
  
  // 统计数据
  statTotal: document.getElementById('stat-total'),
  statActive: document.getElementById('stat-active'),
  statArchived: document.getElementById('stat-archived'),
  statDeleted: document.getElementById('stat-deleted'),
  
  // 功能按钮
  btnViewAll: document.getElementById('btn-view-all'),
  btnSearch: document.getElementById('btn-search'),
  btnArchived: document.getElementById('btn-archived'),
  btnSettings: document.getElementById('btn-settings'),
  viewAllRecent: document.getElementById('view-all-recent'),
  
  // 列表相关
  memoriesList: document.getElementById('memories-list'),
  recentMemories: document.getElementById('recent-memories'),
  listTitle: document.getElementById('list-title'),
  searchInput: document.getElementById('search-input'),
  filterTabs: document.querySelectorAll('.filter-tab'),
  loadMore: document.getElementById('load-more'),
  
  // 详情相关
  detailId: document.getElementById('detail-id'),
  detailType: document.getElementById('detail-type'),
  detailStatus: document.getElementById('detail-status'),
  detailImportance: document.getElementById('detail-importance'),
  detailCreated: document.getElementById('detail-created'),
  detailAccess: document.getElementById('detail-access'),
  detailContent: document.getElementById('detail-content'),
  actionButton: document.getElementById('action-button'),
  btnArchive: document.getElementById('btn-archive'),
  btnDelete: document.getElementById('btn-delete'),
  
  // 设置相关
  settingTimeout: document.getElementById('setting-timeout'),
  settingStorage: document.getElementById('setting-storage'),
  btnClearCache: document.getElementById('btn-clear-cache'),
  btnNuke: document.getElementById('btn-nuke'),
  
  // 模态框和菜单
  actionMenu: document.getElementById('action-menu'),
  closeActionMenu: document.getElementById('close-action-menu'),
  actionArchive: document.getElementById('action-archive'),
  actionDelete: document.getElementById('action-delete'),
  confirmDialog: document.getElementById('confirm-dialog'),
  dialogTitle: document.getElementById('dialog-title'),
  dialogMessage: document.getElementById('dialog-message'),
  dialogCancel: document.getElementById('dialog-cancel'),
  dialogConfirm: document.getElementById('dialog-confirm'),
  
  // Toast 提示
  toast: document.getElementById('toast')
};

// 全局状态
let state = {
  token: null,
  currentView: 'login',
  currentMemoryId: null,
  memories: [],
  page: 1,
  hasMore: true,
  filter: 'all',
  searchQuery: '',
  confirmCallback: null
};

// 初始化应用
function initApp() {
  // 检查本地存储的 token
  const savedToken = localStorage.getItem('lm_token');
  if (savedToken) {
    state.token = savedToken;
    showView('main');
    loadStats();
    loadRecentMemories();
  }
  
  // 绑定事件监听器
  bindEvents();
}

// 绑定事件监听器
function bindEvents() {
  // 登录表单提交
  dom.loginForm.addEventListener('submit', handleLogin);
  
  // 导航按钮
  dom.backButton.addEventListener('click', () => showView('main'));
  dom.backToList.addEventListener('click', () => showView('list'));
  dom.backFromSettings.addEventListener('click', () => showView('main'));
  dom.refreshButton.addEventListener('click', refreshData);
  dom.logoutButton.addEventListener('click', showLogoutConfirm);
  
  // 功能菜单点击
  dom.btnViewAll.addEventListener('click', () => {
    state.filter = 'all';
    dom.listTitle.textContent = '所有记忆';
    showView('list');
    loadMemories(true);
  });
  
  dom.btnSearch.addEventListener('click', () => {
    state.filter = 'all';
    dom.listTitle.textContent = '搜索结果';
    showView('list');
    dom.searchInput.focus();
  });
  
  dom.btnArchived.addEventListener('click', () => {
    state.filter = 'archived';
    dom.listTitle.textContent = '已归档记忆';
    showView('list');
    loadMemories(true);
  });
  
  dom.btnSettings.addEventListener('click', () => {
    showView('settings');
    loadSettings();
  });
  
  dom.viewAllRecent.addEventListener('click', () => {
    state.filter = 'all';
    dom.listTitle.textContent = '最近记忆';
    showView('list');
    loadMemories(true);
  });
  
  // 搜索和筛选
  dom.searchInput.addEventListener('input', handleSearch);
  dom.filterTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      state.filter = tab.dataset.status;
      dom.filterTabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      loadMemories(true);
    });
  });
  
  // 加载更多
  dom.loadMore.addEventListener('click', loadMoreMemories);
  
  // 详情页操作
  dom.actionButton.addEventListener('click', () => {
    dom.actionMenu.classList.remove('hidden');
  });
  
  dom.closeActionMenu.addEventListener('click', () => {
    dom.actionMenu.classList.add('hidden');
  });
  
  dom.actionArchive.addEventListener('click', () => {
    dom.actionMenu.classList.add('hidden');
    archiveMemory(state.currentMemoryId);
  });
  
  dom.actionDelete.addEventListener('click', () => {
    dom.actionMenu.classList.add('hidden');
    showDeleteConfirm(state.currentMemoryId);
  });
  
  dom.btnArchive.addEventListener('click', () => {
    archiveMemory(state.currentMemoryId);
  });
  
  dom.btnDelete.addEventListener('click', () => {
    showDeleteConfirm(state.currentMemoryId);
  });
  
  // 设置页操作
  dom.btnClearCache.addEventListener('click', showClearCacheConfirm);
  dom.btnNuke.addEventListener('click', showNukeConfirm);
  
  // 确认对话框
  dom.dialogCancel.addEventListener('click', closeDialog);
  dom.dialogConfirm.addEventListener('click', confirmDialog);
  
  // 点击菜单外部关闭
  dom.actionMenu.addEventListener('click', (e) => {
    if (e.target === dom.actionMenu) {
      dom.actionMenu.classList.add('hidden');
    }
  });
  
  dom.confirmDialog.addEventListener('click', (e) => {
    if (e.target === dom.confirmDialog) {
      closeDialog();
    }
  });
}

// 显示视图
function showView(viewName) {
  // 隐藏所有视图
  [dom.loginView, dom.mainView, dom.listView, dom.detailView, dom.settingsView]
    .forEach(view => view.classList.remove('active'));
  
  // 显示指定视图
  state.currentView = viewName;
  
  switch(viewName) {
    case 'login':
      dom.loginView.classList.add('active');
      break;
    case 'main':
      dom.mainView.classList.add('active');
      break;
    case 'list':
      dom.listView.classList.add('active');
      // 更新当前筛选标签状态
      dom.filterTabs.forEach(tab => {
        if (tab.dataset.status === state.filter) {
          tab.classList.add('active');
        } else {
          tab.classList.remove('active');
        }
      });
      break;
    case 'detail':
      dom.detailView.classList.add('active');
      break;
    case 'settings':
      dom.settingsView.classList.add('active');
      break;
  }
  
  // 关闭所有模态框和菜单
  dom.actionMenu.classList.add('hidden');
  dom.confirmDialog.classList.add('hidden');
}

// 处理登录
async function handleLogin(e) {
  e.preventDefault();
  
  const password = dom.passwordInput.value;
  if (!password) {
    showError('请输入密码');
    return;
  }
  
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ password })
    });
    
    const data = await response.json();
    
    if (response.ok && data.success) {
      state.token = data.token;
      localStorage.setItem('lm_token', data.token);
      dom.passwordInput.value = '';
      dom.loginError.textContent = '';
      showView('main');
      loadStats();
      loadRecentMemories();
    } else {
      showError(data.message || '登录失败，请检查密码');
    }
  } catch (error) {
    console.error('登录错误:', error);
    showError('网络错误，请稍后重试');
  }
}

// 显示错误信息
function showError(message) {
  dom.loginError.textContent = message;
  dom.loginError.style.display = 'block';
}

// 加载统计数据
async function loadStats() {
  try {
    const response = await fetch('/api/stats', {
      headers: {
        'Authorization': `Bearer ${state.token}`
      }
    });
    
    if (response.ok) {
      const data = await response.json();
      dom.statTotal.textContent = data.total || 0;
      dom.statActive.textContent = data.active || 0;
      dom.statArchived.textContent = data.archived || 0;
      dom.statDeleted.textContent = data.deleted || 0;
    } else {
      handleAuthError(response);
    }
  } catch (error) {
    console.error('加载统计数据错误:', error);
  }
}

// 加载最近记忆
async function loadRecentMemories() {
  try {
    const response = await fetch('/api/memories?limit=5&sort=recent', {
      headers: {
        'Authorization': `Bearer ${state.token}`
      }
    });
    
    if (response.ok) {
      const data = await response.json();
      renderMemoriesList(data, dom.recentMemories, true);
    } else {
      handleAuthError(response);
    }
  } catch (error) {
    console.error('加载最近记忆错误:', error);
  }
}

// 加载记忆列表
async function loadMemories(reset = false) {
  if (reset) {
    state.page = 1;
    state.memories = [];
    state.hasMore = true;
    dom.memoriesList.innerHTML = '<div class="empty-state"><p class="empty-text">加载中...</p></div>';
  }
  
  if (!state.hasMore) return;
  
  try {
    let url = `/api/memories?page=${state.page}&limit=20`;
    
    if (state.filter !== 'all') {
      url += `&status=${state.filter}`;
    }
    
    if (state.searchQuery) {
      url += `&search=${encodeURIComponent(state.searchQuery)}`;
    }
    
    const response = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${state.token}`
      }
    });
    
    if (response.ok) {
      const data = await response.json();
      
      if (data.memories && data.memories.length > 0) {
        state.memories = [...state.memories, ...data.memories];
        state.hasMore = data.hasMore;
        state.page++;
        renderMemoriesList(state.memories, dom.memoriesList);
      } else if (state.page === 1) {
        dom.memoriesList.innerHTML = `
          <div class="empty-state">
            <div class="empty-icon">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" stroke-width="2" stroke-opacity="0.3"/>
                <path d="M16 3H8C6.89543 3 6 3.89543 6 5V19C6 20.1046 6.89543 21 8 21H16C17.1046 21 18 20.1046 18 19V5C18 3.89543 17.1046 3 16 3Z" stroke="currentColor" stroke-width="2" stroke-opacity="0.3"/>
              </svg>
            </div>
            <p class="empty-text">暂无记忆数据</p>
          </div>
        `;
      }
      
      // 更新加载更多按钮状态
      dom.loadMore.style.display = state.hasMore ? 'block' : 'none';
    } else {
      handleAuthError(response);
    }
  } catch (error) {
    console.error('加载记忆列表错误:', error);
    showToast('加载失败，请重试');
  }
}

// 加载更多记忆
function loadMoreMemories() {
  if (!state.hasMore) return;
  loadMemories();
}

// 渲染记忆列表
function renderMemoriesList(memories, container, isRecent = false) {
  container.innerHTML = '';
  
  if (!memories || memories.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" stroke-width="2" stroke-opacity="0.3"/>
            <path d="M16 3H8C6.89543 3 6 3.89543 6 5V19C6 20.1046 6.89543 21 8 21H16C17.1046 21 18 20.1046 18 19V5C18 3.89543 17.1046 3 16 3Z" stroke="currentColor" stroke-width="2" stroke-opacity="0.3"/>
          </svg>
        </div>
        <p class="empty-text">暂无记忆数据</p>
      </div>
    `;
    return;
  }
  
  memories.forEach(memory => {
    const item = document.createElement('div');
    item.className = 'memory-item';
    item.dataset.id = memory.id;
    
    // 创建状态徽章
    let statusClass = '';
    let statusText = '活跃';
    
    if (memory.status === 'archived') {
      statusClass = ' archived';
      statusText = '已归档';
    } else if (memory.status === 'deleted') {
      statusClass = ' deleted';
      statusText = '已删除';
    }
    
    // 提取内容摘要，限制为100个字符
    const contentPreview = memory.content.length > 100 
      ? memory.content.substring(0, 100) + '...' 
      : memory.content;
    
    item.innerHTML = `
      <div class="memory-item-header">
        <h3 class="memory-item-title">${contentPreview}</h3>
        <span class="status-badge${statusClass}">${statusText}</span>
      </div>
      <div class="memory-item-meta">
        <span>${formatDate(memory.created_at)}</span>
        <span>重要性: ${memory.importance || '普通'}</span>
      </div>
    `;
    
    item.addEventListener('click', () => {
      state.currentMemoryId = memory.id;
      loadMemoryDetail(memory.id);
      showView('detail');
    });
    
    container.appendChild(item);
  });
}

// 加载记忆详情
async function loadMemoryDetail(id) {
  try {
    const response = await fetch(`/api/memories/${id}`, {
      headers: {
        'Authorization': `Bearer ${state.token}`
      }
    });
    
    if (response.ok) {
      const data = await response.json();
      renderMemoryDetail(data);
    } else {
      handleAuthError(response);
    }
  } catch (error) {
    console.error('加载记忆详情错误:', error);
    dom.detailContent.innerHTML = '<p style="color: var(--text-light); text-align: center; padding: 20px;">加载失败</p>';
  }
}

// 渲染记忆详情
function renderMemoryDetail(memory) {
  // 设置元数据
  dom.detailId.textContent = memory.id;
  dom.detailType.textContent = memory.type || '默认';
  
  // 设置状态显示
  let statusText = '活跃';
  if (memory.status === 'archived') {
    statusText = '已归档';
  } else if (memory.status === 'deleted') {
    statusText = '已删除';
  }
  dom.detailStatus.textContent = statusText;
  
  dom.detailImportance.textContent = memory.importance || '普通';
  dom.detailCreated.textContent = formatDate(memory.created_at);
  dom.detailAccess.textContent = memory.last_accessed ? formatDate(memory.last_accessed) : '从未访问';
  
  // 显示内容
  dom.detailContent.textContent = memory.content;
}

// 处理搜索
function handleSearch(e) {
  state.searchQuery = e.target.value;
  state.page = 1;
  state.memories = [];
  
  // 防抖处理，避免频繁请求
  clearTimeout(state.searchTimeout);
  state.searchTimeout = setTimeout(() => {
    loadMemories(true);
  }, 300);
}

// 归档记忆
async function archiveMemory(id) {
  try {
    const response = await fetch(`/api/memories/${id}/archive`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${state.token}`,
        'Content-Type': 'application/json'
      }
    });
    
    if (response.ok) {
      showToast('归档成功');
      // 刷新当前列表和统计数据
      if (state.currentView === 'detail') {
        showView('list');
        loadMemories(true);
      } else {
        loadMemories(true);
        loadStats();
      }
    } else {
      handleAuthError(response);
      showToast('归档失败');
    }
  } catch (error) {
    console.error('归档记忆错误:', error);
    showToast('操作失败，请重试');
  }
}

// 删除记忆
async function deleteMemory(id) {
  try {
    const response = await fetch(`/api/memories/${id}/delete`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${state.token}`,
        'Content-Type': 'application/json'
      }
    });
    
    if (response.ok) {
      showToast('删除成功');
      // 刷新当前列表和统计数据
      if (state.currentView === 'detail') {
        showView('list');
        loadMemories(true);
      } else {
        loadMemories(true);
        loadStats();
      }
    } else {
      handleAuthError(response);
      showToast('删除失败');
    }
  } catch (error) {
    console.error('删除记忆错误:', error);
    showToast('操作失败，请重试');
  }
}

// 加载设置
async function loadSettings() {
  try {
    const response = await fetch('/api/settings', {
      headers: {
        'Authorization': `Bearer ${state.token}`
      }
    });
    
    if (response.ok) {
      const data = await response.json();
      dom.settingTimeout.textContent = `${data.timeout || 30} 分钟`;
      dom.settingStorage.textContent = data.storage_type || '默认存储';
    } else {
      handleAuthError(response);
    }
  } catch (error) {
    console.error('加载设置错误:', error);
  }
}

// 清除缓存
async function clearCache() {
  try {
    const response = await fetch('/api/cache/clear', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${state.token}`
      }
    });
    
    if (response.ok) {
      showToast('缓存已清除');
      refreshData();
    } else {
      handleAuthError(response);
      showToast('清除缓存失败');
    }
  } catch (error) {
    console.error('清除缓存错误:', error);
    showToast('操作失败，请重试');
  }
}

// 核爆清除
async function nukeMemories() {
  try {
    const response = await fetch('/api/memories/nuke', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${state.token}`
      }
    });
    
    if (response.ok) {
      showToast('已清空所有数据');
      refreshData();
    } else {
      handleAuthError(response);
      showToast('操作失败');
    }
  } catch (error) {
    console.error('核爆清除错误:', error);
    showToast('操作失败，请重试');
  }
}

// 刷新数据
function refreshData() {
  loadStats();
  
  if (state.currentView === 'main') {
    loadRecentMemories();
  } else if (state.currentView === 'list') {
    loadMemories(true);
  } else if (state.currentView === 'detail' && state.currentMemoryId) {
    loadMemoryDetail(state.currentMemoryId);
  }
  
  showToast('已刷新');
}

// 处理认证错误
async function handleAuthError(response) {
  if (response.status === 401) {
    // 认证失败，清除 token 并返回登录页
    localStorage.removeItem('lm_token');
    state.token = null;
    showView('login');
    showToast('登录已过期，请重新登录');
  }
}

// 显示退出确认
function showLogoutConfirm() {
  showConfirmDialog(
    '退出登录',
    '确定要退出登录吗？',
    () => {
      localStorage.removeItem('lm_token');
      state.token = null;
      showView('login');
    }
  );
}

// 显示删除确认
function showDeleteConfirm(id) {
  showConfirmDialog(
    '删除记忆',
    '确定要删除这条记忆吗？此操作不可撤销。',
    () => deleteMemory(id)
  );
}

// 显示清除缓存确认
function showClearCacheConfirm() {
  showConfirmDialog(
    '清除缓存',
    '确定要清除所有缓存数据吗？',
    clearCache
  );
}

// 显示核爆确认
function showNukeConfirm() {
  showConfirmDialog(
    '核爆清除',
    '此操作将删除所有记忆数据！确定要继续吗？',
    nukeMemories,
    true
  );
}

// 显示确认对话框
function showConfirmDialog(title, message, callback, isDanger = false) {
  dom.dialogTitle.textContent = title;
  dom.dialogMessage.textContent = message;
  state.confirmCallback = callback;
  
  // 设置确认按钮样式
  if (isDanger) {
    dom.dialogConfirm.classList.remove('btn-primary');
    dom.dialogConfirm.classList.add('btn-danger');
    dom.dialogConfirm.textContent = '确认清除';
  } else {
    dom.dialogConfirm.classList.remove('btn-danger');
    dom.dialogConfirm.classList.add('btn-primary');
    dom.dialogConfirm.textContent = '确认';
  }
  
  dom.confirmDialog.classList.remove('hidden');
}

// 关闭对话框
function closeDialog() {
  dom.confirmDialog.classList.add('hidden');
  state.confirmCallback = null;
}

// 确认对话框操作
function confirmDialog() {
  if (state.confirmCallback) {
    state.confirmCallback();
  }
  closeDialog();
}

// 显示 Toast 提示
function showToast(message) {
  dom.toast.textContent = message;
  dom.toast.classList.remove('hidden');
  
  setTimeout(() => {
    dom.toast.classList.add('hidden');
  }, 2000);
}

// 格式化日期
function formatDate(dateString) {
  const date = new Date(dateString);
  const now = new Date();
  const diff = now - date;
  const days = Math.floor(diff / (1000 * 60 * 60 * 24));
  
  if (days === 0) {
    // 今天，显示时间
    return date.toLocaleTimeString('zh-CN', { 
      hour: '2-digit', 
      minute: '2-digit' 
    });
  } else if (days === 1) {
    // 昨天
    return '昨天 ' + date.toLocaleTimeString('zh-CN', { 
      hour: '2-digit', 
      minute: '2-digit' 
    });
  } else if (days < 7) {
    // 一周内，显示星期几
    const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
    return '周' + weekdays[date.getDay()];
  } else if (date.getFullYear() === now.getFullYear()) {
    // 今年，显示月日
    return `${date.getMonth() + 1}月${date.getDate()}日`;
  } else {
    // 其他，显示完整日期
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }
}

// 初始化应用
initApp();