# -*- coding: utf-8 -*-
"""
WebUIServer - LivingMemory插件的Web界面服务
提供记忆管理、查询和删除等功能
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
from astrbot.api import logger

# 创建必要的目录
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')
for directory in [STATIC_DIR, TEMPLATES_DIR]:
    os.makedirs(directory, exist_ok=True)

# 创建FastAPI应用
app = FastAPI(title="LivingMemory WebUI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# 用于会话管理的存储
sessions: Dict[str, Dict[str, Any]] = {}

class WebUIServer:
    def __init__(self, config: Dict[str, Any], faiss_manager):
        """
        初始化WebUIServer
        
        Args:
            config: WebUI配置字典
            faiss_manager: FaissManager实例，用于管理记忆数据
        """
        self.config = config
        self.faiss_manager = faiss_manager
        self.host = config.get("host", "127.0.0.1")
        self.port = config.get("port", 8080)
        self.access_password = config.get("access_password", "")
        self.session_timeout = config.get("session_timeout", 3600)
        self.server = None
        
        # 设置应用的状态
        app.state.faiss_manager = faiss_manager
        app.state.webui_config = config
        
        # 初始化静态文件
        self._create_static_files()
    
    def _create_static_files(self):
        """创建必要的静态文件"""
        # 创建CSS文件
        css_content = """
        /* 全局样式 */
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f5f5f5;
            color: #333;
        }
        
        /* 容器样式 */
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        /* 头部样式 */
        .header {
            background-color: #fff;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 {
            margin: 0;
            color: #2c3e50;
            font-size: 24px;
        }
        
        /* 卡片样式 */
        .card {
            background-color: #fff;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            padding: 20px;
            margin-bottom: 20px;
        }
        
        /* 表格样式 */
        .memory-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }
        
        .memory-table th, .memory-table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #e0e0e0;
        }
        
        .memory-table th {
            background-color: #f8f9fa;
            font-weight: 600;
            color: #495057;
        }
        
        .memory-table tr:hover {
            background-color: #f8f9fa;
        }
        
        /* 按钮样式 */
        .btn {
            display: inline-block;
            padding: 8px 16px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            transition: background-color 0.3s;
            text-decoration: none;
        }
        
        .btn-primary {
            background-color: #007bff;
            color: white;
        }
        
        .btn-primary:hover {
            background-color: #0056b3;
        }
        
        .btn-danger {
            background-color: #dc3545;
            color: white;
        }
        
        .btn-danger:hover {
            background-color: #c82333;
        }
        
        .btn-secondary {
            background-color: #6c757d;
            color: white;
        }
        
        .btn-secondary:hover {
            background-color: #545b62;
        }
        
        /* 表单样式 */
        .form-group {
            margin-bottom: 16px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 4px;
            font-weight: 500;
            color: #495057;
        }
        
        .form-control {
            width: 100%;
            padding: 8px 12px;
            border: 1px solid #ced4da;
            border-radius: 4px;
            font-size: 14px;
        }
        
        .form-control:focus {
            outline: none;
            border-color: #007bff;
        }
        
        /* 分页样式 */
        .pagination {
            display: flex;
            justify-content: center;
            margin-top: 20px;
        }
        
        .pagination button {
            margin: 0 5px;
        }
        
        /* 统计信息样式 */
        .stats {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
        }
        
        .stat-item {
            background-color: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            text-align: center;
            flex: 1;
        }
        
        .stat-value {
            font-size: 28px;
            font-weight: bold;
            color: #007bff;
        }
        
        .stat-label {
            color: #6c757d;
            font-size: 14px;
        }
        
        /* 登录页面样式 */
        .login-container {
            max-width: 400px;
            margin: 100px auto;
            padding: 20px;
            background-color: #fff;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .login-title {
            text-align: center;
            margin-bottom: 20px;
            color: #2c3e50;
        }
        
        /* 操作栏样式 */
        .action-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        
        /* 消息提示样式 */
        .message {
            padding: 10px 15px;
            margin-bottom: 15px;
            border-radius: 4px;
        }
        
        .message-success {
            background-color: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        
        .message-error {
            background-color: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        
        /* 加载动画 */
        .loading {
            text-align: center;
            padding: 20px;
            color: #6c757d;
        }
        
        /* 响应式设计 */
        @media (max-width: 768px) {
            .stats {
                flex-direction: column;
            }
            
            .header {
                flex-direction: column;
                gap: 10px;
            }
            
            .action-bar {
                flex-direction: column;
                gap: 10px;
                align-items: stretch;
            }
        }
        """
        
        with open(os.path.join(STATIC_DIR, 'style.css'), 'w', encoding='utf-8') as f:
            f.write(css_content)
        
        # 创建JavaScript文件
        js_content = """
        // 删除选中的记忆
        async function deleteSelectedMemories() {
            const selectedIds = Array.from(document.querySelectorAll('input[name="memory_ids"]:checked'))
                .map(checkbox => checkbox.value);
            
            if (selectedIds.length === 0) {
                alert('请选择要删除的记忆');
                return;
            }
            
            if (!confirm(`确定要删除选中的 ${selectedIds.length} 条记忆吗？此操作不可撤销。`)) {
                return;
            }
            
            try {
                const response = await fetch('/api/delete-memories', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Session-ID': document.querySelector('meta[name="session-id"]').content
                    },
                    body: JSON.stringify({ ids: selectedIds })
                });
                
                const result = await response.json();
                if (result.success) {
                    // 显示成功消息
                    showMessage('success', `成功删除 ${result.deleted_count} 条记忆`);
                    // 刷新页面
                    setTimeout(() => {
                        window.location.reload();
                    }, 1000);
                } else {
                    showMessage('error', result.message || '删除记忆失败');
                }
            } catch (error) {
                showMessage('error', '网络错误，请稍后重试');
                console.error('删除记忆时出错:', error);
            }
        }
        
        // 全选/取消全选
        function toggleSelectAll() {
            const selectAllCheckbox = document.getElementById('select-all');
            const memoryCheckboxes = document.querySelectorAll('input[name="memory_ids"]');
            
            memoryCheckboxes.forEach(checkbox => {
                checkbox.checked = selectAllCheckbox.checked;
            });
        }
        
        // 显示消息提示
        function showMessage(type, message) {
            const messageContainer = document.getElementById('message-container');
            messageContainer.innerHTML = `
                <div class="message message-${type}">
                    ${message}
                </div>
            `;
            
            // 3秒后自动隐藏消息
            setTimeout(() => {
                messageContainer.innerHTML = '';
            }, 3000);
        }
        
        // 排序功能
        function sortTable(column) {
            const currentSort = document.getElementById('current-sort')?.value || '';
            let sortOrder = 'asc';
            
            if (currentSort.startsWith(column)) {
                sortOrder = currentSort.endsWith('asc') ? 'desc' : 'asc';
            }
            
            // 更新URL并刷新页面
            const url = new URL(window.location);
            url.searchParams.set('sort', column);
            url.searchParams.set('order', sortOrder);
            window.location.href = url.toString();
        }
        
        // 每页显示数量变化
        function changePageSize() {
            const pageSize = document.getElementById('page-size').value;
            const url = new URL(window.location);
            url.searchParams.set('page_size', pageSize);
            url.searchParams.delete('page'); // 重置到第一页
            window.location.href = url.toString();
        }
        """
        
        with open(os.path.join(STATIC_DIR, 'script.js'), 'w', encoding='utf-8') as f:
            f.write(js_content)
    
    async def start(self):
        """
        启动WebUI服务
        """
        try:
            # 创建主页面模板
            self._create_templates()
            
            # 启动uvicorn服务器
            self.server = uvicorn.Server(
                uvicorn.Config(
                    app, 
                    host=self.host, 
                    port=self.port,
                    log_level="info"
                )
            )
            
            logger.info(f"WebUI服务启动在 http://{self.host}:{self.port}")
            await self.server.serve()
            
        except Exception as e:
            logger.error(f"启动WebUI服务时出错: {e}")
            raise
    
    async def stop(self):
        """
        停止WebUI服务
        """
        try:
            if self.server:
                await self.server.shutdown()
                logger.info("WebUI服务已停止")
        except Exception as e:
            logger.error(f"停止WebUI服务时出错: {e}")
    
    def _create_templates(self):
        """创建HTML模板文件"""
        # 登录页面模板
        login_template = """
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>LivingMemory - 登录</title>
            <link rel="stylesheet" href="/static/style.css">
        </head>
        <body>
            <div class="login-container">
                <h1 class="login-title">LivingMemory WebUI</h1>
                
                {% if error %}
                <div class="message message-error">
                    {{ error }}
                </div>
                {% endif %}
                
                <form method="post" action="/login">
                    <div class="form-group">
                        <label for="password">请输入访问密码</label>
                        <input type="password" class="form-control" id="password" name="password" required>
                    </div>
                    <button type="submit" class="btn btn-primary" style="width: 100%;">登录</button>
                </form>
            </div>
        </body>
        </html>
        """
        
        with open(os.path.join(TEMPLATES_DIR, 'login.html'), 'w', encoding='utf-8') as f:
            f.write(login_template)
        
        # 主页面模板
        main_template = """
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <meta name="session-id" content="{{ session_id }}">
            <title>LivingMemory - 记忆管理</title>
            <link rel="stylesheet" href="/static/style.css">
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>LivingMemory 记忆管理</h1>
                    <a href="/logout" class="btn btn-secondary">退出登录</a>
                </div>
                
                <div id="message-container"></div>
                
                <div class="card">
                    <div class="stats">
                        <div class="stat-item">
                            <div class="stat-value">{{ total_memories }}</div>
                            <div class="stat-label">总记忆数</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-value">{{ current_page }}</div>
                            <div class="stat-label">当前页码</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-value">{{ total_pages }}</div>
                            <div class="stat-label">总页数</div>
                        </div>
                    </div>
                </div>
                
                <div class="card">
                    <div class="action-bar">
                        <div>
                            <button class="btn btn-danger" onclick="deleteSelectedMemories()">
                                删除选中的记忆
                            </button>
                        </div>
                        <div>
                            每页显示:
                            <select id="page-size" onchange="changePageSize()">
                                <option value="20" {% if page_size == 20 %}selected{% endif %}>20</option>
                                <option value="50" {% if page_size == 50 %}selected{% endif %}>50</option>
                                <option value="100" {% if page_size == 100 %}selected{% endif %}>100</option>
                            </select>
                        </div>
                    </div>
                    
                    <table class="memory-table">
                        <thead>
                            <tr>
                                <th><input type="checkbox" id="select-all" onclick="toggleSelectAll()"></th>
                                <th onclick="sortTable('id')">ID {% if sort_by == 'id' %}<small>({{ sort_order }})</small>{% endif %}</th>
                                <th onclick="sortTable('content')">记忆内容 {% if sort_by == 'content' %}<small>({{ sort_order }})</small>{% endif %}</th>
                                <th onclick="sortTable('importance')">重要性 {% if sort_by == 'importance' %}<small>({{ sort_order }})</small>{% endif %}</th>
                                <th onclick="sortTable('create_time')">创建时间 {% if sort_by == 'create_time' %}<small>({{ sort_order }})</small>{% endif %}</th>
                                <th onclick="sortTable('last_access_time')">最后访问时间 {% if sort_by == 'last_access_time' %}<small>({{ sort_order }})</small>{% endif %}</th>
                                <th>会话ID</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for memory in memories %}
                            <tr>
                                <td><input type="checkbox" name="memory_ids" value="{{ memory.id }}"></td>
                                <td>{{ memory.id }}</td>
                                <td style="max-width: 300px; word-break: break-all;">{{ memory.content }}</td>
                                <td>{{ "%.2f" | format(memory.metadata.importance if memory.metadata and 'importance' in memory.metadata else 0.0) }}</td>
                                <td>{{ format_datetime(memory.metadata.create_time if memory.metadata and 'create_time' in memory.metadata else 0) }}</td>
                                <td>{{ format_datetime(memory.metadata.last_access_time if memory.metadata and 'last_access_time' in memory.metadata else 0) }}</td>
                                <td style="font-size: 12px;">{{ memory.metadata.session_id if memory.metadata and 'session_id' in memory.metadata else '-' }}</td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="7" style="text-align: center; padding: 20px;">暂无记忆数据</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    
                    <div class="pagination">
                        {% if current_page > 1 %}
                        <a href="?page={{ current_page - 1 }}&page_size={{ page_size }}{% if sort_by %}&sort={{ sort_by }}&order={{ sort_order }}{% endif %}" class="btn btn-secondary">上一页</a>
                        {% endif %}
                        
                        <span style="margin: 0 10px; line-height: 36px;">
                            {{ current_page }} / {{ total_pages }}
                        </span>
                        
                        {% if current_page < total_pages %}
                        <a href="?page={{ current_page + 1 }}&page_size={{ page_size }}{% if sort_by %}&sort={{ sort_by }}&order={{ sort_order }}{% endif %}" class="btn btn-secondary">下一页</a>
                        {% endif %}
                    </div>
                </div>
            </div>
            
            <script src="/static/script.js"></script>
        </body>
        </html>
        """
        
        with open(os.path.join(TEMPLATES_DIR, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(main_template)

# 辅助函数：验证会话
async def verify_session(request: Request) -> bool:
    """
    验证会话是否有效
    """
    session_id = request.cookies.get('session_id') or request.headers.get('X-Session-ID')
    
    if not session_id or session_id not in sessions:
        return False
    
    # 检查会话是否过期
    session_data = sessions[session_id]
    if time.time() - session_data['created_at'] > session_data['timeout']:
        del sessions[session_id]
        return False
    
    # 更新会话时间
    session_data['last_activity'] = time.time()
    return True

# 辅助函数：格式化日期时间
def format_datetime(timestamp: float) -> str:
    """
    将时间戳格式化为可读字符串
    """
    if not timestamp:
        return '-'
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime('%Y-%m-%d %H:%M:%S')
    except:
        return '-'

# 注册过滤器
@app.template_filter('format_datetime')
def template_format_datetime(timestamp: float) -> str:
    return format_datetime(timestamp)

# 登录页面
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """
    主页面，需要登录后访问
    """
    is_authenticated = await verify_session(request)
    
    if not is_authenticated:
        return RedirectResponse(url="/login")
    
    # 获取分页参数
    page = int(request.query_params.get("page", 1))
    page_size = int(request.query_params.get("page_size", 20))
    sort_by = request.query_params.get("sort", "id")
    sort_order = request.query_params.get("order", "asc")
    
    # 获取记忆数据
    faiss_manager = request.app.state.faiss_manager
    offset = (page - 1) * page_size
    
    try:
        # 获取记忆列表
        memories = await faiss_manager.get_memories_paginated(page_size, offset)
        
        # 解析元数据
        for memory in memories:
            try:
                memory['metadata'] = json.loads(memory['metadata']) if isinstance(memory['metadata'], str) else {}
            except:
                memory['metadata'] = {}
        
        # 排序处理
        if sort_by in ['id', 'importance', 'create_time', 'last_access_time']:
            memories.sort(key=lambda x: 
                (x[sort_by] if sort_by == 'id' else 
                 x['metadata'].get(sort_by, 0)), 
                reverse=(sort_order == 'desc')
            )
        
        # 获取总数
        total_memories = await faiss_manager.count_total_memories()
        total_pages = max(1, (total_memories + page_size - 1) // page_size)
        
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "memories": memories,
                "total_memories": total_memories,
                "current_page": page,
                "total_pages": total_pages,
                "page_size": page_size,
                "sort_by": sort_by,
                "sort_order": sort_order,
                "session_id": request.cookies.get('session_id')
            }
        )
    except Exception as e:
        logger.error(f"获取记忆列表时出错: {e}")
        raise HTTPException(status_code=500, detail="获取记忆数据失败")

# 登录页面
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """
    登录页面
    """
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None}
    )

# 登录处理
@app.post("/login", response_class=RedirectResponse)
async def login(request: Request):
    """
    处理登录请求
    """
    form_data = await request.form()
    password = form_data.get("password", "")
    
    # 获取配置的密码
    webui_config = request.app.state.webui_config
    configured_password = webui_config.get("access_password", "")
    
    # 如果没有设置密码，直接登录成功
    if not configured_password:
        # 创建会话
        session_id = os.urandom(16).hex()
        sessions[session_id] = {
            'created_at': time.time(),
            'last_activity': time.time(),
            'timeout': webui_config.get("session_timeout", 3600)
        }
        
        # 创建响应
        response = RedirectResponse(url="/")
        response.set_cookie("session_id", session_id, httponly=True)
        return response
    
    # 验证密码
    if password != configured_password:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "密码错误，请重试"}
        )
    
    # 创建会话
    session_id = os.urandom(16).hex()
    sessions[session_id] = {
        'created_at': time.time(),
        'last_activity': time.time(),
        'timeout': webui_config.get("session_timeout", 3600)
    }
    
    # 创建响应
    response = RedirectResponse(url="/")
    response.set_cookie("session_id", session_id, httponly=True)
    return response

# 退出登录
@app.get("/logout", response_class=RedirectResponse)
async def logout(request: Request):
    """
    退出登录
    """
    session_id = request.cookies.get('session_id')
    if session_id in sessions:
        del sessions[session_id]
    
    response = RedirectResponse(url="/login")
    response.delete_cookie("session_id")
    return response

# API: 删除记忆
@app.post("/api/delete-memories")
async def delete_memories(request: Request):
    """
    批量删除记忆
    """
    # 验证会话
    is_authenticated = await verify_session(request)
    if not is_authenticated:
        raise HTTPException(status_code=401, detail="未授权访问")
    
    # 解析请求数据
    try:
        data = await request.json()
        memory_ids = data.get("ids", [])
        
        # 验证数据
        if not isinstance(memory_ids, list) or not memory_ids:
            raise HTTPException(status_code=400, detail="无效的请求数据")
        
        # 转换为整数ID
        try:
            ids_to_delete = [int(id_str) for id_str in memory_ids]
        except ValueError:
            raise HTTPException(status_code=400, detail="ID格式无效")
        
        # 删除记忆
        faiss_manager = request.app.state.faiss_manager
        await faiss_manager.delete_memories(ids_to_delete)
        
        return {
            "success": True,
            "deleted_count": len(ids_to_delete),
            "message": "记忆删除成功"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除记忆时出错: {e}")
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")

# API: 获取统计信息
@app.get("/api/stats")
async def get_stats(request: Request):
    """
    获取记忆统计信息
    """
    # 验证会话
    is_authenticated = await verify_session(request)
    if not is_authenticated:
        raise HTTPException(status_code=401, detail="未授权访问")
    
    try:
        faiss_manager = request.app.state.faiss_manager
        total_memories = await faiss_manager.count_total_memories()
        
        return {
            "success": True,
            "data": {
                "total_memories": total_memories
            }
        }
    except Exception as e:
        logger.error(f"获取统计信息时出错: {e}")
        raise HTTPException(status_code=500, detail="获取统计信息失败")