// WebUI 脚本文件

// 全局变量
let currentPage = 1;
let selectedMemories = new Set();
let allMemories = [];
let isLoading = false;

// 初始化
document.addEventListener('DOMContentLoaded', function() {
    loadStats();
    loadMemories();
    setupEventListeners();
});

// 设置事件监听器
function setupEventListeners() {
    // 搜索框回车事件
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.addEventListener('keypress', handleSearch);
    }
    
    // 模态框外部点击关闭
    const modal = document.getElementById('memoryModal');
    if (modal) {
        modal.addEventListener('click', function(event) {
            if (event.target === modal) {
                closeModal();
            }
        });
    }
    
    // ESC键关闭模态框
    document.addEventListener('keydown', function(event) {
        if (event.key === 'Escape') {
            closeModal();
        }
    });
}

// 加载统计信息
async function loadStats() {
    try {
        const response = await fetch('/api/stats');
        const data = await response.json();
        
        if (data.success) {
            renderStats(data.data);
        } else {
            showError('加载统计信息失败: ' + data.error);
        }
    } catch (error) {
        console.error('加载统计信息失败:', error);
        showError('加载统计信息失败，请重试');
    }
}

// 渲染统计信息
function renderStats(stats) {
    const statsGrid = document.getElementById('statsGrid');
    if (!statsGrid) return;
    
    statsGrid.innerHTML = `
        <div class="stat-card fade-in">
            <h3>总记忆数</h3>
            <div class="value">${stats.total_memories}</div>
        </div>
        <div class="stat-card fade-in">
            <h3>事实记忆</h3>
            <div class="value">${stats.fact_memories}</div>
        </div>
        <div class="stat-card fade-in">
            <h3>反思记忆</h3>
            <div class="value">${stats.reflection_memories}</div>
        </div>
        <div class="stat-card fade-in">
            <h3>最后更新</h3>
            <div class="value">${new Date(stats.last_updated).toLocaleString()}</div>
        </div>
    `;
}

// 加载记忆列表
async function loadMemories(page = 1) {
    if (isLoading) return;
    
    isLoading = true;
    currentPage = page;
    
    const searchQuery = document.getElementById('searchInput')?.value || '';
    const typeFilter = document.getElementById('typeFilter')?.value || 'all';
    
    const params = new URLSearchParams({
        page: page,
        per_page: 20
    });
    
    if (searchQuery) params.append('q', searchQuery);
    if (typeFilter !== 'all') params.append('type', typeFilter);

    try {
        const response = await fetch(`/api/memories?${params}`);
        const data = await response.json();
        
        if (data.success) {
            allMemories = data.data.memories;
            renderMemories(data.data);
        } else {
            showError('加载记忆失败: ' + data.error);
        }
    } catch (error) {
        console.error('加载记忆失败:', error);
        showError('加载记忆失败，请重试');
    } finally {
        isLoading = false;
    }
}

// 渲染记忆列表
function renderMemories(data) {
    const grid = document.getElementById('memoriesGrid');
    if (!grid) return;
    
    if (data.memories.length === 0) {
        grid.innerHTML = '<div class="loading">暂无记忆</div>';
        return;
    }
    
    grid.innerHTML = data.memories.map(memory => `
        <div class="memory-card fade-in">
            <div class="memory-header">
                <span class="memory-type">${getMemoryTypeLabel(memory.memory_type)}</span>
                <span class="memory-timestamp">${new Date(memory.timestamp).toLocaleString()}</span>
            </div>
            <div class="memory-content">
                ${memory.content || '无内容'}
            </div>
            <div class="memory-actions">
                <button class="btn btn-primary btn-small" onclick="viewMemoryDetail('${memory.id}')">查看详情</button>
                <button class="btn btn-danger btn-small" onclick="deleteMemory('${memory.id}')">删除</button>
                <input type="checkbox" ${selectedMemories.has(memory.id) ? 'checked' : ''} 
                       onchange="toggleMemorySelection('${memory.id}')" title="选择此项">
            </div>
        </div>
    `).join('');
    
    // 添加分页控件
    if (data.total_pages > 1) {
        renderPagination(data);
    }
}

// 渲染分页控件
function renderPagination(data) {
    const grid = document.getElementById('memoriesGrid');
    if (!grid) return;
    
    const pagination = document.createElement('div');
    pagination.className = 'pagination';
    pagination.style.cssText = `
        display: flex;
        justify-content: center;
        gap: 0.5rem;
        margin-top: 2rem;
        flex-wrap: wrap;
    `;
    
    // 上一页
    if (data.page > 1) {
        const prevBtn = document.createElement('button');
        prevBtn.className = 'btn btn-secondary btn-small';
        prevBtn.textContent = '上一页';
        prevBtn.onclick = () => loadMemories(data.page - 1);
        pagination.appendChild(prevBtn);
    }
    
    // 页码
    const startPage = Math.max(1, data.page - 2);
    const endPage = Math.min(data.total_pages, data.page + 2);
    
    for (let i = startPage; i <= endPage; i++) {
        const pageBtn = document.createElement('button');
        pageBtn.className = i === data.page ? 'btn btn-primary btn-small' : 'btn btn-secondary btn-small';
        pageBtn.textContent = i;
        pageBtn.onclick = () => loadMemories(i);
        pagination.appendChild(pageBtn);
    }
    
    // 下一页
    if (data.page < data.total_pages) {
        const nextBtn = document.createElement('button');
        nextBtn.className = 'btn btn-secondary btn-small';
        nextBtn.textContent = '下一页';
        nextBtn.onclick = () => loadMemories(data.page + 1);
        pagination.appendChild(nextBtn);
    }
    
    grid.appendChild(pagination);
}

// 获取记忆类型标签
function getMemoryTypeLabel(type) {
    const labels = {
        'fact': '事实记忆',
        'reflection': '反思记忆',
        'event': '事件记忆',
        'preference': '偏好记忆',
        'goal': '目标记忆',
        'opinion': '观点记忆',
        'relationship': '关系记忆',
        'other': '其他记忆'
    };
    return labels[type] || type;
}

// 搜索处理
function handleSearch(event) {
    if (event.key === 'Enter') {
        loadMemories(1);
    }
}

// 筛选处理
function filterMemories() {
    loadMemories(1);
}

// 切换记忆选择
function toggleMemorySelection(memoryId) {
    if (selectedMemories.has(memoryId)) {
        selectedMemories.delete(memoryId);
    } else {
        selectedMemories.add(memoryId);
    }
    
    updateSelectionUI();
}

// 更新选择UI
function updateSelectionUI() {
    const selectedCount = selectedMemories.size;
    const deleteBtn = document.querySelector('.controls .btn-danger');
    if (deleteBtn) {
        deleteBtn.textContent = selectedCount > 0 ? `删除选中 (${selectedCount})` : '删除选中';
        deleteBtn.disabled = selectedCount === 0;
    }
}

// 批量删除选中的记忆
async function deleteSelected() {
    if (selectedMemories.size === 0) {
        showMessage('请先选择要删除的记忆', 'warning');
        return;
    }
    
    if (!confirm(`确定要删除选中的 ${selectedMemories.size} 条记忆吗？此操作不可恢复。`)) {
        return;
    }
    
    try {
        const response = await fetch('/api/memories', {
            method: 'DELETE',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                memory_ids: Array.from(selectedMemories)
            })
        });
        
        const data = await response.json();
        
        if (data.success) {
            showMessage(`成功删除 ${data.data.deleted_count} 条记忆`, 'success');
            selectedMemories.clear();
            updateSelectionUI();
            loadMemories();
            loadStats();
        } else {
            showMessage('删除失败: ' + data.error, 'error');
        }
    } catch (error) {
        console.error('批量删除失败:', error);
        showMessage('删除失败，请重试', 'error');
    }
}

// 删除单个记忆
async function deleteMemory(memoryId) {
    if (!confirm('确定要删除这条记忆吗？此操作不可恢复。')) {
        return;
    }
    
    try {
        const response = await fetch(`/api/memory/${memoryId}`, {
            method: 'DELETE'
        });
        
        const data = await response.json();
        
        if (data.success) {
            showMessage('记忆删除成功', 'success');
            loadMemories();
            loadStats();
        } else {
            showMessage('删除失败: ' + data.error, 'error');
        }
    } catch (error) {
        console.error('删除记忆失败:', error);
        showMessage('删除失败，请重试', 'error');
    }
}

// 查看记忆详情
async function viewMemoryDetail(memoryId) {
    try {
        const response = await fetch(`/api/memory/${memoryId}`);
        const data = await response.json();
        
        if (data.success) {
            showMemoryDetail(data.data);
        } else {
            showMessage('获取记忆详情失败: ' + data.error, 'error');
        }
    } catch (error) {
        console.error('获取记忆详情失败:', error);
        showMessage('获取记忆详情失败，请重试', 'error');
    }
}

// 显示记忆详情模态框
function showMemoryDetail(memory) {
    const modal = document.getElementById('memoryModal');
    const modalBody = document.getElementById('modalBody');
    
    if (!modal || !modalBody) return;
    
    modalBody.innerHTML = `
        <div class="memory-detail">
            <p><strong>ID:</strong> ${memory.id}</p>
            <p><strong>类型:</strong> ${getMemoryTypeLabel(memory.memory_type)}</p>
            <p><strong>时间:</strong> ${new Date(memory.timestamp).toLocaleString()}</p>
            <p><strong>重要性:</strong> ${memory.importance || 'N/A'}</p>
            <p><strong>内容:</strong></p>
            <div style="background: #f5f5f5; padding: 1rem; border-radius: 5px; margin: 1rem 0; line-height: 1.6;">
                ${memory.content || '无内容'}
            </div>
            ${memory.metadata ? `
                <p><strong>元数据:</strong></p>
                <pre style="background: #f5f5f5; padding: 1rem; border-radius: 5px; overflow-x: auto; font-family: 'Courier New', monospace; font-size: 0.9rem;">${JSON.stringify(memory.metadata, null, 2)}</pre>
            ` : ''}
        </div>
    `;
    
    modal.style.display = 'block';
}

// 关闭模态框
function closeModal() {
    const modal = document.getElementById('memoryModal');
    if (modal) {
        modal.style.display = 'none';
    }
}

// 刷新数据
async function refreshData() {
    await loadStats();
    await loadMemories();
    showMessage('数据已刷新', 'info');
}

// 登出
async function logout() {
    try {
        await fetch('/api/logout', { method: 'POST' });
        window.location.href = '/login';
    } catch (error) {
        console.error('登出失败:', error);
        showMessage('登出失败，请重试', 'error');
    }
}

// 显示消息
function showMessage(message, type = 'info') {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message message-${type}`;
    messageDiv.textContent = message;
    messageDiv.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 1rem 1.5rem;
        border-radius: 5px;
        color: white;
        font-weight: 500;
        z-index: 10000;
        animation: slideInRight 0.3s ease;
        max-width: 300px;
        word-wrap: break-word;
    `;
    
    const colors = {
        info: '#2196F3',
        success: '#4CAF50',
        warning: '#FF9800',
        error: '#f44336'
    };
    
    messageDiv.style.backgroundColor = colors[type] || colors.info;
    
    document.body.appendChild(messageDiv);
    
    setTimeout(() => {
        messageDiv.style.animation = 'slideOutRight 0.3s ease';
        setTimeout(() => {
            if (messageDiv.parentNode) {
                messageDiv.parentNode.removeChild(messageDiv);
            }
        }, 300);
    }, 3000);
}

// 显示错误
function showError(message) {
    const grid = document.getElementById('memoriesGrid') || document.getElementById('statsGrid');
    if (grid) {
        grid.innerHTML = `<div class="error">${message}</div>`;
    }
}

// 添加CSS动画
const style = document.createElement('style');
style.textContent = `
    @keyframes slideInRight {
        from {
            transform: translateX(100%);
            opacity: 0;
        }
        to {
            transform: translateX(0);
            opacity: 1;
        }
    }
    
    @keyframes slideOutRight {
        from {
            transform: translateX(0);
            opacity: 1;
        }
        to {
            transform: translateX(100%);
            opacity: 0;
        }
    }
    
    .memory-detail {
        line-height: 1.6;
    }
    
    .memory-detail p {
        margin-bottom: 0.5rem;
    }
    
    .memory-detail strong {
        color: #333;
    }
    
    .message {
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    }
`;
document.head.appendChild(style);