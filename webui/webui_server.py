# -*- coding: utf-8 -*-
"""
webui_server.py - WebUI服务器
提供记忆插件的Web界面功能
"""

import asyncio
import json
import time
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from urllib.parse import unquote

from aiohttp import web, web_request, web_response
import aiohttp_cors
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

try:
    from ..storage.faiss_manager import FaissManager
    from ..core.models.memory_models import Memory, MemoryType, MemoryEntry
except ImportError:
    # 降级处理，使用模拟类
    class FaissManager:
        async def get_all_memories(self):
            return []
        
        async def delete_memory(self, memory_id: str):
            return True
        
        async def search_memories(self, query: str, k: int = 5):
            return []
    
    class Memory:
        pass
    
    class MemoryType:
        pass
    
    class MemoryEntry:
        pass


class WebUIServer:
    def __init__(self, config: Dict[str, Any], faiss_manager: FaissManager):
        """
        初始化WebUI服务器
        
        Args:
            config: WebUI配置
            faiss_manager: FAISS管理器
        """
        self.config = config.get("webui_settings", {})
        self.enabled = self.config.get("enabled", True)
        self.host = self.config.get("host", "127.0.0.1")
        self.port = self.config.get("port", 8080)
        self.access_password = self.config.get("access_password", "")
        self.session_timeout = self.config.get("session_timeout", 3600)
        
        self.faiss_manager = faiss_manager
        self.app = web.Application()
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.site = None
        
        # 设置CORS
        self._setup_cors()
        
        # 注册路由
        self._register_routes()
        
        logger.info(f"WebUI服务器初始化完成，配置: host={self.host}, port={self.port}, enabled={self.enabled}")
    
    def _setup_cors(self):
        """设置CORS"""
        cors = aiohttp_cors.setup(self.app, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
                allow_methods="*"
            )
        })
        
        # 为所有路由添加CORS
        for route in list(self.app.router.routes()):
            cors.add(route)
    
    def _register_routes(self):
        """注册路由"""
        self.app.router.add_get('/', self.index_handler)
        self.app.router.add_get('/login', self.login_page_handler)
        self.app.router.add_post('/api/login', self.login_handler)
        self.app.router.add_post('/api/logout', self.logout_handler)
        self.app.router.add_get('/api/memories', self.get_memories_handler)
        self.app.router.add_delete('/api/memories', self.delete_memories_handler)
        self.app.router.add_get('/api/memory/{memory_id}', self.get_memory_detail_handler)
        self.app.router.add_delete('/api/memory/{memory_id}', self.delete_memory_handler)
        self.app.router.add_get('/api/stats', self.get_stats_handler)
        self.app.router.add_get('/api/search', self.search_memories_handler)
        self.app.router.add_static('/static', 'webui/static')
    
    def _generate_session_id(self) -> str:
        """生成会话ID"""
        return secrets.token_urlsafe(32)
    
    def _hash_password(self, password: str) -> str:
        """哈希密码"""
        return hashlib.sha256(password.encode()).hexdigest()
    
    def _is_session_valid(self, session_id: str) -> bool:
        """检查会话是否有效"""
        if session_id not in self.sessions:
            return False
        
        session = self.sessions[session_id]
        expire_time = session.get('expire_time', 0)
        
        if time.time() > expire_time:
            # 会话过期，删除
            del self.sessions[session_id]
            return False
        
        return True
    
    def _require_auth(self, handler):
        """认证装饰器"""
        async def wrapper(request):
            session_id = request.cookies.get('session_id')
            if not session_id or not self._is_session_valid(session_id):
                return web.json_response({
                    'success': False,
                    'error': '未认证或会话已过期',
                    'code': 'UNAUTHORIZED'
                }, status=401)
            
            return await handler(request)
        return wrapper
    
    async def index_handler(self, request):
        """首页处理器"""
        session_id = request.cookies.get('session_id')
        if not session_id or not self._is_session_valid(session_id):
            # 未认证，重定向到登录页
            raise web.HTTPFound('/login')
        
        # 返回主页面
        html_content = self._get_main_page_html()
        return web.Response(text=html_content, content_type='text/html')
    
    async def login_page_handler(self, request):
        """登录页面处理器"""
        session_id = request.cookies.get('session_id')
        if session_id and self._is_session_valid(session_id):
            # 已认证，重定向到首页
            raise web.HTTPFound('/')
        
        # 返回登录页面
        html_content = self._get_login_page_html()
        return web.Response(text=html_content, content_type='text/html')
    
    async def login_handler(self, request):
        """登录API处理器"""
        try:
            data = await request.json()
            password = data.get('password', '')
            
            # 验证密码
            if not self.access_password:
                # 如果没有设置密码，直接允许访问
                session_id = self._generate_session_id()
                self.sessions[session_id] = {
                    'expire_time': time.time() + self.session_timeout,
                    'login_time': time.time()
                }
                
                response = web.json_response({
                    'success': True,
                    'message': '登录成功'
                })
                response.set_cookie('session_id', session_id, max_age=self.session_timeout)
                return response
            
            # 验证密码
            if self._hash_password(password) == self._hash_password(self.access_password):
                session_id = self._generate_session_id()
                self.sessions[session_id] = {
                    'expire_time': time.time() + self.session_timeout,
                    'login_time': time.time()
                }
                
                response = web.json_response({
                    'success': True,
                    'message': '登录成功'
                })
                response.set_cookie('session_id', session_id, max_age=self.session_timeout)
                return response
            else:
                return web.json_response({
                    'success': False,
                    'error': '密码错误'
                }, status=401)
                
        except Exception as e:
            logger.error(f"登录处理失败: {e}")
            return web.json_response({
                'success': False,
                'error': '登录失败'
            }, status=500)
    
    async def logout_handler(self, request):
        """登出API处理器"""
        session_id = request.cookies.get('session_id')
        if session_id and session_id in self.sessions:
            del self.sessions[session_id]
        
        response = web.json_response({
            'success': True,
            'message': '登出成功'
        })
        response.del_cookie('session_id')
        return response
    
    async def get_memories_handler(self, request):
        """获取记忆列表处理器"""
        try:
            # 获取查询参数
            page = int(request.query.get('page', 1))
            per_page = int(request.query.get('per_page', 20))
            memory_type = request.query.get('type', 'all')
            search_query = request.query.get('q', '')
            
            # 获取所有记忆
            memories = await self._get_all_memories()
            
            # 过滤
            if memory_type != 'all':
                memories = [m for m in memories if m.get('memory_type') == memory_type]
            
            if search_query:
                memories = [m for m in memories if search_query.lower() in json.dumps(m).lower()]
            
            # 分页
            total = len(memories)
            start = (page - 1) * per_page
            end = start + per_page
            memories_page = memories[start:end]
            
            return web.json_response({
                'success': True,
                'data': {
                    'memories': memories_page,
                    'total': total,
                    'page': page,
                    'per_page': per_page,
                    'total_pages': (total + per_page - 1) // per_page
                }
            })
            
        except Exception as e:
            logger.error(f"获取记忆列表失败: {e}")
            return web.json_response({
                'success': False,
                'error': '获取记忆列表失败'
            }, status=500)
    
    async def delete_memories_handler(self, request):
        """批量删除记忆处理器"""
        try:
            data = await request.json()
            memory_ids = data.get('memory_ids', [])
            
            if not memory_ids:
                return web.json_response({
                    'success': False,
                    'error': '未提供要删除的记忆ID'
                }, status=400)
            
            deleted_count = 0
            errors = []
            
            for memory_id in memory_ids:
                try:
                    success = await self._delete_memory(memory_id)
                    if success:
                        deleted_count += 1
                    else:
                        errors.append(f"记忆 {memory_id} 不存在")
                except Exception as e:
                    errors.append(f"删除记忆 {memory_id} 失败: {str(e)}")
            
            return web.json_response({
                'success': True,
                'data': {
                    'deleted_count': deleted_count,
                    'total_requested': len(memory_ids),
                    'errors': errors
                }
            })
            
        except Exception as e:
            logger.error(f"批量删除记忆失败: {e}")
            return web.json_response({
                'success': False,
                'error': '批量删除记忆失败'
            }, status=500)
    
    async def get_memory_detail_handler(self, request):
        """获取记忆详情处理器"""
        try:
            memory_id = request.match_info.get('memory_id')
            if not memory_id:
                return web.json_response({
                    'success': False,
                    'error': '未提供记忆ID'
                }, status=400)
            
            memory = await self._get_memory_detail(memory_id)
            if not memory:
                return web.json_response({
                    'success': False,
                    'error': '记忆不存在'
                }, status=404)
            
            return web.json_response({
                'success': True,
                'data': memory
            })
            
        except Exception as e:
            logger.error(f"获取记忆详情失败: {e}")
            return web.json_response({
                'success': False,
                'error': '获取记忆详情失败'
            }, status=500)
    
    async def delete_memory_handler(self, request):
        """删除单个记忆处理器"""
        try:
            memory_id = request.match_info.get('memory_id')
            if not memory_id:
                return web.json_response({
                    'success': False,
                    'error': '未提供记忆ID'
                }, status=400)
            
            success = await self._delete_memory(memory_id)
            if success:
                return web.json_response({
                    'success': True,
                    'message': '记忆删除成功'
                })
            else:
                return web.json_response({
                    'success': False,
                    'error': '记忆不存在'
                }, status=404)
                
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return web.json_response({
                'success': False,
                'error': '删除记忆失败'
            }, status=500)
    
    async def get_stats_handler(self, request):
        """获取统计信息处理器"""
        try:
            stats = await self._get_stats()
            return web.json_response({
                'success': True,
                'data': stats
            })
            
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return web.json_response({
                'success': False,
                'error': '获取统计信息失败'
            }, status=500)
    
    async def search_memories_handler(self, request):
        """搜索记忆处理器"""
        try:
            query = request.query.get('q', '')
            if not query:
                return web.json_response({
                    'success': False,
                    'error': '未提供搜索关键词'
                }, status=400)
            
            results = await self._search_memories(query)
            return web.json_response({
                'success': True,
                'data': {
                    'results': results,
                    'query': query
                }
            })
            
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return web.json_response({
                'success': False,
                'error': '搜索记忆失败'
            }, status=500)
    
    async def _get_all_memories(self) -> List[Dict[str, Any]]:
        """获取所有记忆"""
        try:
            # 这里需要从FAISS管理器获取所有记忆
            # 由于FAISS管理器可能没有直接获取所有记忆的方法，
            # 我们需要实现一个方法来遍历所有记忆
            memories = []
            
            # 获取数据库中的所有向量ID
            if hasattr(self.faiss_manager, 'get_all_memory_ids'):
                memory_ids = await self.faiss_manager.get_all_memory_ids()
            else:
                # 如果没有直接的方法，我们可以尝试从数据库中获取
                # 这里需要根据实际的FAISS管理器实现来调整
                memory_ids = []
            
            for memory_id in memory_ids:
                try:
                    memory = await self._get_memory_detail(memory_id)
                    if memory:
                        memories.append(memory)
                except Exception as e:
                    logger.warning(f"获取记忆 {memory_id} 失败: {e}")
            
            # 按时间倒序排列
            memories.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            return memories
            
        except Exception as e:
            logger.error(f"获取所有记忆失败: {e}")
            return []
    
    async def _get_memory_detail(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """获取记忆详情"""
        try:
            # 这里需要根据实际的FAISS管理器实现来获取记忆详情
            if hasattr(self.faiss_manager, 'get_memory_by_id'):
                memory = await self.faiss_manager.get_memory_by_id(memory_id)
                return memory
            else:
                # 如果没有直接的方法，返回一个模拟的记忆数据
                logger.warning(f"FAISS管理器没有get_memory_by_id方法，返回模拟数据")
                return {
                    'id': memory_id,
                    'content': f'记忆内容 {memory_id}',
                    'memory_type': 'fact',
                    'timestamp': datetime.now().isoformat(),
                    'importance': 0.5,
                    'metadata': {}
                }
                
        except Exception as e:
            logger.error(f"获取记忆详情失败 {memory_id}: {e}")
            return None
    
    async def _delete_memory(self, memory_id: str) -> bool:
        """删除记忆"""
        try:
            # 这里需要根据实际的FAISS管理器实现来删除记忆
            if hasattr(self.faiss_manager, 'delete_memory_by_id'):
                success = await self.faiss_manager.delete_memory_by_id(memory_id)
                return success
            else:
                logger.warning(f"FAISS管理器没有delete_memory_by_id方法")
                return False
                
        except Exception as e:
            logger.error(f"删除记忆失败 {memory_id}: {e}")
            return False
    
    async def _get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        try:
            memories = await self._get_all_memories()
            
            total_memories = len(memories)
            fact_memories = len([m for m in memories if m.get('memory_type') == 'fact'])
            reflection_memories = len([m for m in memories if m.get('memory_type') == 'reflection'])
            
            # 按日期统计
            date_stats = {}
            for memory in memories:
                date = memory.get('timestamp', '')[:10]  # 获取日期部分
                if date:
                    date_stats[date] = date_stats.get(date, 0) + 1
            
            return {
                'total_memories': total_memories,
                'fact_memories': fact_memories,
                'reflection_memories': reflection_memories,
                'date_stats': date_stats,
                'last_updated': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {
                'total_memories': 0,
                'fact_memories': 0,
                'reflection_memories': 0,
                'date_stats': {},
                'last_updated': datetime.now().isoformat()
            }
    
    async def _search_memories(self, query: str) -> List[Dict[str, Any]]:
        """搜索记忆"""
        try:
            # 这里需要根据实际的FAISS管理器实现来搜索记忆
            if hasattr(self.faiss_manager, 'search_memories'):
                results = await self.faiss_manager.search_memories(query)
                return results
            else:
                # 如果没有直接的方法，使用简单的文本搜索
                memories = await self._get_all_memories()
                results = []
                
                for memory in memories:
                    content = json.dumps(memory).lower()
                    if query.lower() in content:
                        results.append(memory)
                
                return results[:10]  # 限制返回结果数量
                
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return []
    
    def _get_login_page_html(self) -> str:
        """获取登录页面HTML"""
        return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LivingMemory WebUI - 登录</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .login-container {
            background: white;
            padding: 2rem;
            border-radius: 10px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
            width: 100%;
            max-width: 400px;
        }
        
        .login-header {
            text-align: center;
            margin-bottom: 2rem;
        }
        
        .login-header h1 {
            color: #333;
            margin-bottom: 0.5rem;
        }
        
        .login-header p {
            color: #666;
            font-size: 0.9rem;
        }
        
        .form-group {
            margin-bottom: 1.5rem;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 0.5rem;
            color: #333;
            font-weight: 500;
        }
        
        .form-group input {
            width: 100%;
            padding: 0.75rem;
            border: 2px solid #e1e1e1;
            border-radius: 5px;
            font-size: 1rem;
            transition: border-color 0.3s;
        }
        
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .btn {
            width: 100%;
            padding: 0.75rem;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 5px;
            font-size: 1rem;
            font-weight: 500;
            cursor: pointer;
            transition: background 0.3s;
        }
        
        .btn:hover {
            background: #5a6fd8;
        }
        
        .error-message {
            color: #e74c3c;
            font-size: 0.9rem;
            margin-top: 0.5rem;
            display: none;
        }
        
        .error-message.show {
            display: block;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="login-header">
            <h1>LivingMemory</h1>
            <p>智能长期记忆插件</p>
        </div>
        <form id="loginForm">
            <div class="form-group">
                <label for="password">访问密码</label>
                <input type="password" id="password" name="password" required>
            </div>
            <button type="submit" class="btn">登录</button>
            <div id="errorMessage" class="error-message"></div>
        </form>
    </div>

    <script>
        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const password = document.getElementById('password').value;
            const errorMessage = document.getElementById('errorMessage');
            
            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ password })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    window.location.href = '/';
                } else {
                    errorMessage.textContent = data.error || '登录失败';
                    errorMessage.classList.add('show');
                }
            } catch (error) {
                errorMessage.textContent = '网络错误，请重试';
                errorMessage.classList.add('show');
            }
        });
    </script>
</body>
</html>'''
    
    def _get_main_page_html(self) -> str:
        """获取主页面HTML"""
        return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LivingMemory WebUI - 记忆管理</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #f5f5f5;
            color: #333;
        }
        
        .header {
            background: #667eea;
            color: white;
            padding: 1rem 2rem;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }
        
        .header-content {
            display: flex;
            justify-content: space-between;
            align-items: center;
            max-width: 1200px;
            margin: 0 auto;
        }
        
        .header h1 {
            font-size: 1.5rem;
        }
        
        .header-actions {
            display: flex;
            gap: 1rem;
        }
        
        .btn {
            padding: 0.5rem 1rem;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 0.9rem;
            transition: all 0.3s;
        }
        
        .btn-primary {
            background: #4CAF50;
            color: white;
        }
        
        .btn-danger {
            background: #f44336;
            color: white;
        }
        
        .btn-secondary {
            background: #757575;
            color: white;
        }
        
        .btn:hover {
            opacity: 0.9;
        }
        
        .container {
            max-width: 1200px;
            margin: 2rem auto;
            padding: 0 2rem;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }
        
        .stat-card {
            background: white;
            padding: 1.5rem;
            border-radius: 10px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }
        
        .stat-card h3 {
            color: #666;
            font-size: 0.9rem;
            margin-bottom: 0.5rem;
        }
        
        .stat-card .value {
            font-size: 2rem;
            font-weight: bold;
            color: #333;
        }
        
        .controls {
            background: white;
            padding: 1.5rem;
            border-radius: 10px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            margin-bottom: 2rem;
        }
        
        .controls-row {
            display: flex;
            gap: 1rem;
            align-items: center;
            flex-wrap: wrap;
        }
        
        .search-box {
            flex: 1;
            min-width: 200px;
        }
        
        .search-box input {
            width: 100%;
            padding: 0.5rem;
            border: 2px solid #e1e1e1;
            border-radius: 5px;
            font-size: 0.9rem;
        }
        
        .filter-select {
            padding: 0.5rem;
            border: 2px solid #e1e1e1;
            border-radius: 5px;
            font-size: 0.9rem;
        }
        
        .memories-grid {
            display: grid;
            gap: 1rem;
        }
        
        .memory-card {
            background: white;
            padding: 1.5rem;
            border-radius: 10px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            transition: transform 0.2s;
        }
        
        .memory-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
        }
        
        .memory-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1rem;
        }
        
        .memory-type {
            background: #667eea;
            color: white;
            padding: 0.25rem 0.5rem;
            border-radius: 3px;
            font-size: 0.8rem;
        }
        
        .memory-timestamp {
            color: #666;
            font-size: 0.8rem;
        }
        
        .memory-content {
            margin-bottom: 1rem;
            line-height: 1.5;
        }
        
        .memory-actions {
            display: flex;
            gap: 0.5rem;
        }
        
        .btn-small {
            padding: 0.25rem 0.5rem;
            font-size: 0.8rem;
        }
        
        .loading {
            text-align: center;
            padding: 2rem;
            color: #666;
        }
        
        .error {
            background: #fee;
            color: #c33;
            padding: 1rem;
            border-radius: 5px;
            margin: 1rem 0;
        }
        
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.5);
            z-index: 1000;
        }
        
        .modal-content {
            background: white;
            margin: 5% auto;
            padding: 2rem;
            border-radius: 10px;
            width: 90%;
            max-width: 600px;
            max-height: 80vh;
            overflow-y: auto;
        }
        
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }
        
        .close {
            font-size: 1.5rem;
            cursor: pointer;
            color: #666;
        }
        
        @media (max-width: 768px) {
            .controls-row {
                flex-direction: column;
                align-items: stretch;
            }
            
            .header-content {
                flex-direction: column;
                gap: 1rem;
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-content">
            <h1>LivingMemory 记忆管理</h1>
            <div class="header-actions">
                <button class="btn btn-secondary" onclick="refreshData()">刷新</button>
                <button class="btn btn-danger" onclick="logout()">登出</button>
            </div>
        </div>
    </div>

    <div class="container">
        <div class="stats-grid" id="statsGrid">
            <div class="loading">加载统计信息中...</div>
        </div>

        <div class="controls">
            <div class="controls-row">
                <div class="search-box">
                    <input type="text" id="searchInput" placeholder="搜索记忆内容..." onkeyup="handleSearch(event)">
                </div>
                <select id="typeFilter" class="filter-select" onchange="filterMemories()">
                    <option value="all">所有类型</option>
                    <option value="fact">事实记忆</option>
                    <option value="reflection">反思记忆</option>
                </select>
                <button class="btn btn-danger" onclick="deleteSelected()">删除选中</button>
            </div>
        </div>

        <div class="memories-grid" id="memoriesGrid">
            <div class="loading">加载记忆中...</div>
        </div>
    </div>

    <div id="memoryModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>记忆详情</h2>
                <span class="close" onclick="closeModal()">&times;</span>
            </div>
            <div id="modalBody"></div>
        </div>
    </div>

    <script>
        let currentPage = 1;
        let selectedMemories = new Set();
        let allMemories = [];

        async function loadStats() {
            try {
                const response = await fetch('/api/stats');
                const data = await response.json();
                
                if (data.success) {
                    const stats = data.data;
                    document.getElementById('statsGrid').innerHTML = `
                        <div class="stat-card">
                            <h3>总记忆数</h3>
                            <div class="value">${stats.total_memories}</div>
                        </div>
                        <div class="stat-card">
                            <h3>事实记忆</h3>
                            <div class="value">${stats.fact_memories}</div>
                        </div>
                        <div class="stat-card">
                            <h3>反思记忆</h3>
                            <div class="value">${stats.reflection_memories}</div>
                        </div>
                        <div class="stat-card">
                            <h3>最后更新</h3>
                            <div class="value">${new Date(stats.last_updated).toLocaleString()}</div>
                        </div>
                    `;
                }
            } catch (error) {
                console.error('加载统计信息失败:', error);
            }
        }

        async function loadMemories(page = 1) {
            currentPage = page;
            const searchQuery = document.getElementById('searchInput').value;
            const typeFilter = document.getElementById('typeFilter').value;
            
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
                }
            } catch (error) {
                console.error('加载记忆失败:', error);
                document.getElementById('memoriesGrid').innerHTML = 
                    '<div class="error">加载记忆失败，请重试</div>';
            }
        }

        function renderMemories(data) {
            const grid = document.getElementById('memoriesGrid');
            
            if (data.memories.length === 0) {
                grid.innerHTML = '<div class="loading">暂无记忆</div>';
                return;
            }
            
            grid.innerHTML = data.memories.map(memory => `
                <div class="memory-card">
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
                               onchange="toggleMemorySelection('${memory.id}')">
                    </div>
                </div>
            `).join('');
        }

        function getMemoryTypeLabel(type) {
            const labels = {
                'fact': '事实记忆',
                'reflection': '反思记忆',
                'event': '事件记忆'
            };
            return labels[type] || type;
        }

        function handleSearch(event) {
            if (event.key === 'Enter') {
                loadMemories(1);
            }
        }

        function filterMemories() {
            loadMemories(1);
        }

        function toggleMemorySelection(memoryId) {
            if (selectedMemories.has(memoryId)) {
                selectedMemories.delete(memoryId);
            } else {
                selectedMemories.add(memoryId);
            }
        }

        async function deleteSelected() {
            if (selectedMemories.size === 0) {
                alert('请先选择要删除的记忆');
                return;
            }
            
            if (!confirm(`确定要删除选中的 ${selectedMemories.size} 条记忆吗？`)) {
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
                    alert(`成功删除 ${data.data.deleted_count} 条记忆`);
                    selectedMemories.clear();
                    loadMemories();
                    loadStats();
                } else {
                    alert('删除失败: ' + data.error);
                }
            } catch (error) {
                alert('删除失败，请重试');
            }
        }

        async function deleteMemory(memoryId) {
            if (!confirm('确定要删除这条记忆吗？')) {
                return;
            }
            
            try {
                const response = await fetch(`/api/memory/${memoryId}`, {
                    method: 'DELETE'
                });
                
                const data = await response.json();
                
                if (data.success) {
                    loadMemories();
                    loadStats();
                } else {
                    alert('删除失败: ' + data.error);
                }
            } catch (error) {
                alert('删除失败，请重试');
            }
        }

        async function viewMemoryDetail(memoryId) {
            try {
                const response = await fetch(`/api/memory/${memoryId}`);
                const data = await response.json();
                
                if (data.success) {
                    showMemoryDetail(data.data);
                } else {
                    alert('获取记忆详情失败: ' + data.error);
                }
            } catch (error) {
                alert('获取记忆详情失败，请重试');
            }
        }

        function showMemoryDetail(memory) {
            const modal = document.getElementById('memoryModal');
            const modalBody = document.getElementById('modalBody');
            
            modalBody.innerHTML = `
                <div class="memory-detail">
                    <p><strong>ID:</strong> ${memory.id}</p>
                    <p><strong>类型:</strong> ${getMemoryTypeLabel(memory.memory_type)}</p>
                    <p><strong>时间:</strong> ${new Date(memory.timestamp).toLocaleString()}</p>
                    <p><strong>重要性:</strong> ${memory.importance || 'N/A'}</p>
                    <p><strong>内容:</strong></p>
                    <div style="background: #f5f5f5; padding: 1rem; border-radius: 5px; margin: 1rem 0;">
                        ${memory.content || '无内容'}
                    </div>
                    ${memory.metadata ? `
                        <p><strong>元数据:</strong></p>
                        <pre style="background: #f5f5f5; padding: 1rem; border-radius: 5px; overflow-x: auto;">${JSON.stringify(memory.metadata, null, 2)}</pre>
                    ` : ''}
                </div>
            `;
            
            modal.style.display = 'block';
        }

        function closeModal() {
            document.getElementById('memoryModal').style.display = 'none';
        }

        async function refreshData() {
            loadStats();
            loadMemories();
        }

        async function logout() {
            try {
                await fetch('/api/logout', { method: 'POST' });
                window.location.href = '/login';
            } catch (error) {
                console.error('登出失败:', error);
            }
        }

        // 点击模态框外部关闭
        window.onclick = function(event) {
            const modal = document.getElementById('memoryModal');
            if (event.target === modal) {
                closeModal();
            }
        }

        // 页面加载时初始化
        document.addEventListener('DOMContentLoaded', function() {
            loadStats();
            loadMemories();
        });
    </script>
</body>
</html>'''
    
    async def start(self):
        """启动WebUI服务器"""
        if not self.enabled:
            logger.info("WebUI功能已禁用")
            return
        
        try:
            runner = web.AppRunner(self.app)
            await runner.setup()
            
            self.site = web.TCPSite(runner, self.host, self.port)
            await self.site.start()
            
            logger.info(f"WebUI服务器启动成功，访问地址: http://{self.host}:{self.port}")
            
        except Exception as e:
            logger.error(f"WebUI服务器启动失败: {e}")
            raise
    
    async def stop(self):
        """停止WebUI服务器"""
        if self.site:
            await self.site.stop()
            logger.info("WebUI服务器已停止")