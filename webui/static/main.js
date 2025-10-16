// 全局工具函数

/**
 * 格式化时间戳
 * @param {number} timestamp - 时间戳
 * @returns {string} 格式化后的时间字符串
 */
function formatTimestamp(timestamp) {
    if (!timestamp || typeof timestamp !== 'number') {
        return '未知时间';
    }
    
    const date = new Date(timestamp * 1000);
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const hours = String(date.getHours()).padStart(2, '0');
    const minutes = String(date.getMinutes()).padStart(2, '0');
    const seconds = String(date.getSeconds()).padStart(2, '0');
    
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

/**
 * 格式化键名（将下划线转换为空格，并首字母大写）
 * @param {string} key - 原始键名
 * @returns {string} 格式化后的键名
 */
function formatKey(key) {
    if (!key) return '';
    
    // 将下划线和驼峰命名转换为空格分隔的单词
    const formatted = key
        .replace(/_/g, ' ')
        .replace(/([a-z])([A-Z])/g, '$1 $2');
    
    // 首字母大写
    return formatted.charAt(0).toUpperCase() + formatted.slice(1);
}

/**
 * 截断文本
 * @param {string} text - 原始文本
 * @param {number} maxLength - 最大长度
 * @returns {string} 截断后的文本
 */
function truncateText(text, maxLength) {
    if (!text) return '';
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
}

/**
 * 显示消息提示
 * @param {string} message - 消息内容
 * @param {string} type - 消息类型：success, error, warning, info
 * @param {number} duration - 显示时长（毫秒）
 */
function showToast(message, type = 'success', duration = 3000) {
    // 检查是否已存在toast元素，如果没有则创建
    let toast = document.getElementById('toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'toast';
        toast.className = 'toast';
        toast.style.display = 'none';
        toast.innerHTML = `
            <div class="toast-content">
                <svg id="toastIcon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16"></svg>
                <span id="toastMessage"></span>
            </div>
        `;
        document.body.appendChild(toast);
    }
    
    // 设置消息内容
    document.getElementById('toastMessage').textContent = message;
    
    // 设置图标和样式
    const toastIcon = document.getElementById('toastIcon');
    toast.className = 'toast';
    
    switch (type) {
        case 'success':
            toast.classList.add('toast-success');
            toastIcon.innerHTML = '<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"></path>';
            break;
        case 'error':
            toast.classList.add('toast-error');
            toastIcon.innerHTML = '<path d="M11 15h2v2h-2v-2zm0-8h2v6h-2V7zm.99-5C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8z"></path>';
            break;
        case 'warning':
            toast.classList.add('toast-warning');
            toastIcon.innerHTML = '<path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"></path>';
            break;
        case 'info':
            toast.classList.add('toast-info');
            toastIcon.innerHTML = '<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"></path>';
            break;
    }
    
    // 显示toast
    toast.style.display = 'block';
    
    // 自动隐藏
    setTimeout(() => {
        toast.style.display = 'none';
    }, duration);
}

/**
 * 简单的AJAX请求封装
 * @param {string} url - 请求URL
 * @param {Object} options - 请求选项
 * @returns {Promise} Promise对象
 */
async function fetchJSON(url, options = {}) {
    try {
        const defaultOptions = {
            headers: {
                'Content-Type': 'application/json'
            }
        };
        
        const mergedOptions = {
            ...defaultOptions,
            ...options,
            headers: {
                ...defaultOptions.headers,
                ...options.headers
            }
        };
        
        // 如果有body且是对象，自动序列化
        if (mergedOptions.body && typeof mergedOptions.body === 'object') {
            mergedOptions.body = JSON.stringify(mergedOptions.body);
        }
        
        const response = await fetch(url, mergedOptions);
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        return await response.json();
    } catch (error) {
        console.error('Fetch error:', error);
        showToast('操作失败: ' + error.message, 'error');
        throw error;
    }
}

/**
 * 获取URL参数
 * @param {string} name - 参数名
 * @returns {string|null} 参数值
 */
function getQueryParam(name) {
    const urlParams = new URLSearchParams(window.location.search);
    return urlParams.get(name);
}

/**
 * 防抖函数
 * @param {Function} func - 要防抖的函数
 * @param {number} delay - 延迟时间（毫秒）
 * @returns {Function} 防抖后的函数
 */
function debounce(func, delay) {
    let timeoutId;
    return function (...args) {
        clearTimeout(timeoutId);
        timeoutId = setTimeout(() => func.apply(this, args), delay);
    };
}

/**
 * 节流函数
 * @param {Function} func - 要节流的函数
 * @param {number} limit - 时间限制（毫秒）
 * @returns {Function} 节流后的函数
 */
function throttle(func, limit) {
    let inThrottle;
    return function (...args) {
        if (!inThrottle) {
            func.apply(this, args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
}

/**
 * 复制文本到剪贴板
 * @param {string} text - 要复制的文本
 * @returns {Promise<boolean>} 是否复制成功
 */
async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        showToast('复制成功', 'success');
        return true;
    } catch (err) {
        console.error('复制失败:', err);
        showToast('复制失败', 'error');
        return false;
    }
}

// 页面加载完成后执行的初始化函数
document.addEventListener('DOMContentLoaded', function() {
    // 这里可以添加一些全局的初始化逻辑
    
    // 例如：为所有带tooltip的元素初始化tooltip
    // 或者：设置全局事件监听器
    
    // 检测屏幕宽度，在小屏幕上可能需要额外的处理
    function handleResize() {
        const isMobile = window.innerWidth <= 640;
        // 这里可以添加响应式处理逻辑
    }
    
    // 初始调用一次
    handleResize();
    
    // 监听窗口大小变化
    window.addEventListener('resize', debounce(handleResize, 200));
});