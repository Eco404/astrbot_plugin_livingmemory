# -*- coding: utf-8 -*-
"""
WebUI 模块 - LivingMemory 插件的Web界面实现
"""
import asyncio
import json
import time
from typing import Dict, Any, Optional
from aiohttp import web
import aiohttp_jinja2
import jinja2
from astrbot.api import logger


class WebUIManager:
    """
    WebUI管理器，负责启动和管理Web服务
    """
    
    def __init__(self, config: Dict[str, Any], faiss_manager):
        """
        初始化WebUI管理器
        
        Args:
            config: 插件配置
            faiss_manager: Faiss管理器实例，用于数据访问
        """
        self.config = config
        self.faiss_manager = faiss_manager
        self.app = None
        self.runner = None
        self.site = None
        self.is_running = False
        self.session_tokens = {}  # 用于存储会话令牌
        self.session_timeout = 3600  # 会话超时时间（秒）
        
        # WebUI配置
        self.webui_config = config.get("webui", {})
        self.enabled = self.webui_config.get("enabled", True)
        self.host = self.webui_config.get("host", "0.0.0.0")
        self.port = self.webui_config.get("port", 8080)
        self.password = self.webui_config.get("password", "livingmemory")
        self.page_size = self.webui_config.get("page_size", 20)
    
    async def start(self):
        """
        启动WebUI服务
        """
        if not self.enabled:
            logger.info("WebUI功能已禁用")
            return
        
        try:
            # 初始化Web应用
            self.app = web.Application(middlewares=[
                self.auth_middleware,
                self.error_middleware
            ])
            
            # 确保必要的目录存在
            os.makedirs(f"{__file__}/../templates", exist_ok=True)
            os.makedirs(f"{__file__}/../static", exist_ok=True)
            
            # 设置模板加载器
            template_dir = f"{__file__}/../templates"
            aiohttp_jinja2.setup(
                self.app,
                loader=jinja2.FileSystemLoader(template_dir),
                context_processors=[self._template_context_processor]
            )
            
            # 添加模板过滤器
            env = aiohttp_jinja2.get_env(self.app)
            env.filters['format_key'] = self._format_key
            env.filters['format_timestamp'] = self._format_timestamp
            
            # 注册静态文件目录
            self.app.router.add_static('/static/', f"{__file__}/../static/")
            
            # 注册路由
            self.register_routes()
            
            # 添加静态文件路由
            self.app.router.add_static('/static/', path=f"{__file__}/../static/")
            
            # 创建runner和site
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, self.host, self.port)
            await self.site.start()
            
            self.is_running = True
            logger.info(f"LivingMemory WebUI已启动，访问地址: http://{self.host}:{self.port}")
            
        except Exception as e:
            logger.error(f"启动WebUI服务失败: {e}")
            self.is_running = False
    
    async def stop(self):
        """
        停止WebUI服务
        """
        if not self.is_running:
            return
        
        try:
            if self.site:
                await self.site.stop()
            if self.runner:
                await self.runner.cleanup()
            self.is_running = False
            logger.info("LivingMemory WebUI已停止")
        except Exception as e:
            logger.error(f"停止WebUI服务失败: {e}")
    
    def register_routes(self):
        """
        注册WebUI路由
        """
        # 无需认证的路由
        self.app.router.add_get('/', self.login_page_handler)
        self.app.router.add_post('/login', self.login_handler)
        self.app.router.add_get('/logout', self.logout_handler)
        
        # 需要认证的路由
        self.app.router.add_get('/dashboard', self.dashboard_handler)
        self.app.router.add_get('/memories', self.memories_list_handler)
        self.app.router.add_get('/memories/data', self.memories_data_handler)
        self.app.router.add_post('/memories/delete', self.memories_delete_handler)
        self.app.router.add_get('/memory/{memory_id}', self.memory_detail_handler)
        self.app.router.add_get('/api/stats', self.api_stats_handler)
    
    @aiohttp_jinja2.template('login.html')
    async def login_page_handler(self, request):
        """
        登录页面处理函数
        """
        return {"title": "LivingMemory - 登录"}
    
    async def login_handler(self, request):
        """
        登录验证处理函数
        """
        try:
            data = await request.post()
            password = data.get("password", "")
            
            if password == self.password:
                # 生成会话令牌
                token = self._generate_token()
                self._add_session(token)
                
                # 设置cookie并重定向到仪表板
                response = web.HTTPFound('/dashboard')
                response.set_cookie('session_token', token)
                return response
            else:
                # 密码错误
                return web.HTTPFound('/?error=密码错误')
                
        except Exception as e:
            logger.error(f"登录失败: {e}")
            return web.HTTPFound('/?error=登录失败')
    
    async def logout_handler(self, request):
        """
        登出处理函数
        """
        token = request.cookies.get('session_token')
        if token:
            self._remove_session(token)
        
        response = web.HTTPFound('/')
        response.del_cookie('session_token')
        return response
    
    @aiohttp_jinja2.template('dashboard.html')
    async def dashboard_handler(self, request):
        """
        仪表板页面处理函数
        """
        # 获取统计信息
        total_memories = await self.faiss_manager.count_total_memories()
        
        return {
            "title": "LivingMemory - 仪表板",
            "total_memories": total_memories,
            "config": self.webui_config
        }
    
    @aiohttp_jinja2.template('memories.html')
    async def memories_list_handler(self, request):
        """
        记忆列表页面处理函数
        """
        return {
            "title": "LivingMemory - 记忆列表",
            "page_size": self.page_size
        }
    
    async def memories_data_handler(self, request):
        """
        记忆数据API，用于AJAX加载
        """
        try:
            # 获取分页参数
            page = int(request.query.get('page', 1))
            page_size = int(request.query.get('page_size', self.page_size))
            offset = (page - 1) * page_size
            
            # 获取记忆数据
            memories = await self.faiss_manager.get_memories_paginated(
                page_size=page_size, offset=offset
            )
            
            # 获取总数
            total = await self.faiss_manager.count_total_memories()
            
            # 处理metadata
            for mem in memories:
                try:
                    if isinstance(mem['metadata'], str):
                        mem['metadata'] = json.loads(mem['metadata'])
                    
                    # 格式化时间戳
                    if 'create_time' in mem['metadata']:
                        mem['create_time'] = self._format_timestamp(mem['metadata']['create_time'])
                    if 'last_access_time' in mem['metadata']:
                        mem['last_access_time'] = self._format_timestamp(mem['metadata']['last_access_time'])
                    
                except Exception as e:
                    logger.error(f"处理记忆元数据失败: {e}")
                    mem['metadata'] = {}
            
            # 构建响应数据
            data = {
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": (total + page_size - 1) // page_size,
                "items": memories
            }
            
            return web.json_response(data)
            
        except Exception as e:
            logger.error(f"获取记忆数据失败: {e}")
            return web.json_response({"error": str(e)}, status=500)
    
    async def memories_delete_handler(self, request):
        """
        批量删除记忆处理函数
        """
        try:
            data = await request.json()
            memory_ids = data.get('ids', [])
            
            if not memory_ids:
                return web.json_response({"error": "未指定要删除的记忆ID"}, status=400)
            
            # 确保ID列表是整数
            memory_ids = [int(mid) for mid in memory_ids]
            
            # 执行删除
            await self.faiss_manager.delete_memories(memory_ids)
            
            return web.json_response({
                "success": True,
                "message": f"成功删除 {len(memory_ids)} 条记忆"
            })
            
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return web.json_response({"error": str(e)}, status=500)
    
    @aiohttp_jinja2.template('memory_detail.html')
    async def memory_detail_handler(self, request):
        """
        记忆详情页面处理函数
        """
        try:
            memory_id = int(request.match_info['memory_id'])
            
            # 获取记忆数据
            memories = await self.faiss_manager.db.document_storage.get_documents(ids=[memory_id])
            
            if not memories:
                return web.HTTPNotFound(text="未找到指定的记忆")
            
            memory = memories[0]
            
            # 处理metadata
            try:
                if isinstance(memory['metadata'], str):
                    memory['metadata'] = json.loads(memory['metadata'])
            except Exception as e:
                logger.error(f"解析记忆元数据失败: {e}")
                memory['metadata'] = {}
            
            return {
                "title": f"LivingMemory - 记忆详情 #{memory_id}",
                "memory": memory
            }
            
        except ValueError:
            return web.HTTPBadRequest(text="无效的记忆ID")
        except Exception as e:
            logger.error(f"获取记忆详情失败: {e}")
            return web.HTTPInternalServerError(text="获取记忆详情时发生错误")
    
    async def api_stats_handler(self, request):
        """
        统计信息API
        """
        try:
            total_memories = await self.faiss_manager.count_total_memories()
            
            # 获取最近的几条记忆
            recent_memories = await self.faiss_manager.get_memories_paginated(page_size=5, offset=0)
            
            # 处理metadata
            for mem in recent_memories:
                try:
                    if isinstance(mem['metadata'], str):
                        mem['metadata'] = json.loads(mem['metadata'])
                except Exception as e:
                    mem['metadata'] = {}
            
            data = {
                "total_memories": total_memories,
                "recent_memories": recent_memories
            }
            
            return web.json_response(data)
            
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return web.json_response({"error": str(e)}, status=500)
    
    async def auth_middleware(self, request, handler):
        """
        认证中间件，用于验证用户是否已登录
        """
        # 无需认证的路由
        if request.path in ['/', '/login', '/static/main.css', '/static/main.js']:
            return await handler(request)
        
        # 验证会话令牌
        token = request.cookies.get('session_token')
        if not token or not self._is_valid_session(token):
            # 会话无效，重定向到登录页面
            return web.HTTPFound('/')
        
        # 更新会话时间
        self._update_session(token)
        
        # 调用原始处理函数
        return await handler(request)
    
    async def error_middleware(self, request, handler):
        """
        错误处理中间件
        """
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            logger.error(f"WebUI错误: {e}")
            return web.HTTPInternalServerError(text=str(e))
    
    def _generate_token(self) -> str:
        """
        生成会话令牌
        """
        import uuid
        return str(uuid.uuid4())
    
    def _add_session(self, token: str):
        """
        添加会话
        """
        self.session_tokens[token] = time.time()
    
    def _remove_session(self, token: str):
        """
        移除会话
        """
        if token in self.session_tokens:
            del self.session_tokens[token]
    
    def _is_valid_session(self, token: str) -> bool:
        """
        检查会话是否有效
        """
        if token not in self.session_tokens:
            return False
        
        # 检查是否超时
        if time.time() - self.session_tokens[token] > self.session_timeout:
            self._remove_session(token)
            return False
        
        return True
    
    def _update_session(self, token: str):
        """
        更新会话时间
        """
        if token in self.session_tokens:
            self.session_tokens[token] = time.time()
    
    def _template_context_processor(self, request):
        """
        模板上下文处理器，为所有模板提供通用变量
        """
        return {
            'config': self.webui_config,
            'json': json
        }
    
    def _format_key(self, key):
        """
        格式化键名（将下划线转换为空格，并首字母大写）
        """
        if not key:
            return ''
        # 将下划线和驼峰命名转换为空格分隔的单词
        formatted = key.replace('_', ' ').replace(r'([a-z])([A-Z])', r'$1 $2')
        # 首字母大写
        return formatted[0].upper() + formatted[1:] if formatted else ''
    
    def _format_timestamp(self, timestamp):
        """
        格式化时间戳
        """
        if not timestamp or not isinstance(timestamp, (int, float)):
            return '未知时间'
        try:
            return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))
        except Exception:
            return '未知时间'