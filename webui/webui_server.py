# -*- coding: utf-8 -*-
"""
webui_server.py - WebUI服务器
负责提供Web界面服务，包括路由处理和模板渲染。
"""

import os
import json
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional

try:
    from quart import Quart, render_template, request, jsonify, redirect, url_for, session, abort
    from quart_session import Session
    import hypercorn
    WEBUI_AVAILABLE = True
except ImportError:
    WEBUI_AVAILABLE = False

from astrbot.api import logger

from ..core.handlers.webui_handler import WebUIHandler


class WebUIServer:
    """WebUI服务器类，负责提供Web界面服务"""
    
    def __init__(self, context, db_manager, llm_provider):
        """
        初始化WebUI服务器
        
        Args:
            context: AstrBot上下文
            db_manager: 数据库管理器
            llm_provider: LLM提供者
        """
        if not WEBUI_AVAILABLE:
            logger.error("WebUI依赖项未安装，无法启动WebUI服务。请安装quart、quart-session和hypercorn。")
            raise ImportError("WebUI dependencies not available")
            
        self.context = context
        self.config_manager = context  # 使用context作为配置管理器
        self.db_manager = db_manager
        self.llm_provider = llm_provider
        self.webui_handler = WebUIHandler(self.context, self.config_manager.get("livingmemory", {}), db_manager)
        self.webui_config = self.config_manager.get("webui", {})
        
        # 获取配置
        self.host = self.webui_config.get("host", "127.0.0.1")
        self.port = self.webui_config.get("port", 8080)
        self.items_per_page = self.webui_config.get("items_per_page", 20)
        
        # 创建Quart应用
        self.app = Quart(__name__, 
                         template_folder=os.path.join(os.path.dirname(__file__), "..", "..", "webui", "templates"),
                         static_folder=os.path.join(os.path.dirname(__file__), "..", "..", "webui", "static"))
        
        # 配置密钥
        self.app.secret_key = os.urandom(24)
        
        # 配置Session
        self.app.config['SESSION_TYPE'] = 'filesystem'
        self.app.config['SESSION_FILE_DIR'] = os.path.join(os.path.dirname(__file__), "..", "..", "data", "sessions")
        self.app.config['SESSION_FILE_THRESHOLD'] = 100
        Session(self.app)
        
        # 注册路由
        self._register_routes()
        
        # 服务器任务
        self._server_task: Optional[asyncio.Task] = None
        self._running = False
    
    def _register_routes(self):
        """注册路由"""
        
        @self.app.before_request
        async def check_auth():
            """检查用户认证"""
            # 登录页面和静态资源不需要认证
            if request.path.startswith('/static/') or request.path == '/login':
                return
                
            # 检查是否已登录
            if not session.get('logged_in'):
                # 如果没有设置密码，则自动登录
                if not self.webui_config.get("access_password", ""):
                    session['logged_in'] = True
                    return
                    
                # 否则重定向到登录页面
                return redirect(url_for('login'))
        
        @self.app.route('/')
        async def index():
            """首页"""
            return await render_template('index.html')
        
        @self.app.route('/login', methods=['GET', 'POST'])
        async def login():
            """登录页面"""
            if request.method == 'POST':
                form = await request.form
                password = form.get('password', '')
                
                if self.webui_handler.verify_password(password):
                    session['logged_in'] = True
                    return redirect(url_for('index'))
                else:
                    return await render_template('login.html', error="密码错误")
                    
            return await render_template('login.html')
        
        @self.app.route('/logout')
        async def logout():
            """登出"""
            session.pop('logged_in', None)
            return redirect(url_for('login'))
        
        @self.app.route('/api/memories')
        async def api_memories():
            """获取记忆列表API"""
            try:
                page = int(request.args.get('page', 1))
                items_per_page = int(request.args.get('items_per_page', self.items_per_page))
                
                result = await self.webui_handler.get_all_memories(page, items_per_page)
                return jsonify(result)
            except Exception as e:
                logger.error(f"获取记忆列表API错误: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/api/memories/<memory_id>')
        async def api_memory_detail(memory_id):
            """获取记忆详情API"""
            try:
                result = await self.webui_handler.get_memory_details(memory_id)
                return jsonify(result)
            except Exception as e:
                logger.error(f"获取记忆详情API错误: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/api/memories/<memory_id>', methods=['DELETE'])
        async def api_delete_memory(memory_id):
            """删除记忆API"""
            try:
                result = await self.webui_handler.delete_memory(memory_id)
                return jsonify(result)
            except Exception as e:
                logger.error(f"删除记忆API错误: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/api/memories/batch', methods=['DELETE'])
        async def api_batch_delete_memories():
            """批量删除记忆API"""
            try:
                data = await request.get_json()
                memory_ids = data.get('memory_ids', [])
                
                result = await self.webui_handler.batch_delete_memories(memory_ids)
                return jsonify(result)
            except Exception as e:
                logger.error(f"批量删除记忆API错误: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/api/memories/search')
        async def api_search_memories():
            """搜索记忆API"""
            try:
                query = request.args.get('query', '')
                page = int(request.args.get('page', 1))
                items_per_page = int(request.args.get('items_per_page', self.items_per_page))
                
                if not query:
                    return jsonify({"error": "搜索查询不能为空"}), 400
                
                result = await self.webui_handler.search_memories(query, page, items_per_page)
                return jsonify(result)
            except Exception as e:
                logger.error(f"搜索记忆API错误: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500
        
        @self.app.route('/api/statistics')
        async def api_statistics():
            """获取记忆统计信息API"""
            try:
                result = await self.webui_handler.get_memory_statistics()
                return jsonify(result)
            except Exception as e:
                logger.error(f"获取记忆统计API错误: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500
    
    async def start(self):
        """启动WebUI服务器"""
        if self._running:
            logger.warning("WebUI服务器已经在运行中")
            return
            
        try:
            # 确保session目录存在
            session_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "sessions")
            os.makedirs(session_dir, exist_ok=True)
            
            # 配置Hypercorn
            config = hypercorn.Config()
            config.bind = [f"{self.host}:{self.port}"]
            config.use_reloader = False
            
            # 创建服务器任务
            self._server_task = asyncio.create_task(hypercorn.serve(self.app, config))
            self._running = True
            
            logger.info(f"WebUI服务器已启动，访问地址: http://{self.host}:{self.port}")
            
        except Exception as e:
            logger.error(f"启动WebUI服务器失败: {e}", exc_info=True)
            self._running = False
            raise
    
    async def stop(self):
        """停止WebUI服务器"""
        if not self._running:
            return
            
        try:
            if self._server_task:
                self._server_task.cancel()
                try:
                    await self._server_task
                except asyncio.CancelledError:
                    pass
                
            self._running = False
            logger.info("WebUI服务器已停止")
            
        except Exception as e:
            logger.error(f"停止WebUI服务器失败: {e}", exc_info=True)
    
    def is_running(self) -> bool:
        """检查服务器是否正在运行"""
        return self._running