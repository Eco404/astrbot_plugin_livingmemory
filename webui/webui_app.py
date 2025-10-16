# -*- coding: utf-8 -*-
"""
webui_app.py - WebUI应用主文件
实现FastAPI应用的核心功能
"""

import asyncio
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime

from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn

from .auth_manager import AuthManager

logger = logging.getLogger(__name__)


class WebUIApp:
    """
    WebUI应用类
    管理FastAPI应用实例和服务器
    """
    
    def __init__(self, config: Dict[str, Any], faiss_manager, memory_handler, admin_handler, forgetting_agent):
        """
        初始化WebUI应用
        
        Args:
            config: 插件配置
            faiss_manager: Faiss管理器实例
            memory_handler: 记忆处理器实例
            admin_handler: 管理员处理器实例
            forgetting_agent: 遗忘代理实例
        """
        self.config = config
        self.webui_config = config.get("webui_settings", {})
        self.port = self.webui_config.get("port", 6186)
        self.password = self.webui_config.get("password", "")
        
        # 核心组件
        self.faiss_manager = faiss_manager
        self.memory_handler = memory_handler
        self.admin_handler = admin_handler
        self.forgetting_agent = forgetting_agent
        
        # 认证管理器
        self.auth_manager = AuthManager(self.password)
        
        # 创建FastAPI应用
        self.app = FastAPI(
            title="LivingMemory WebUI",
            description="AstrBot LivingMemory 插件管理界面",
            version="1.0.0"
        )
        
        # 设置模板
        self.templates = Jinja2Templates(directory="webui/templates")
        
        # 配置路由
        self._setup_routes()
        
        # 服务器实例
        self.server = None
    
    def _setup_routes(self):
        """
        设置FastAPI路由
        """
        # 主页路由
        @self.app.get("/", response_class=HTMLResponse)
        async def index(request: Request):
            # 检查会话
            session_id = request.cookies.get("session_id")
            if not self.auth_manager.validate_session(session_id):
                return RedirectResponse(url="/login")
            
            # 获取记忆统计信息
            try:
                memories = await self.faiss_manager.get_all_memories_for_forgetting()
                stats = {
                    "total_count": len(memories),
                    "active_count": sum(1 for m in memories if m.get("metadata", {}).get("status", "active") == "active"),
                    "archived_count": sum(1 for m in memories if m.get("metadata", {}).get("status") == "archived")
                }
            except Exception as e:
                logger.error(f"获取记忆统计失败: {e}")
                stats = {"total_count": 0, "active_count": 0, "archived_count": 0}
            
            return self.templates.TemplateResponse(
                "index.html", 
                {"request": request, "stats": stats}
            )
        
        # 登录路由
        @self.app.get("/login", response_class=HTMLResponse)
        async def login_page(request: Request):
            return self.templates.TemplateResponse(
                "login.html", 
                {"request": request, "error": None}
            )
        
        # 登录处理
        @self.app.post("/login")
        async def login(request: Request, password: str = Form(...)):
            if self.auth_manager.verify_password(password):
                session_id = self.auth_manager.generate_session()
                response = RedirectResponse(url="/")
                response.set_cookie("session_id", session_id)
                return response
            else:
                return self.templates.TemplateResponse(
                    "login.html", 
                    {"request": request, "error": "密码错误"}
                )
        
        # 登出路由
        @self.app.get("/logout")
        async def logout(request: Request):
            session_id = request.cookies.get("session_id")
            if session_id:
                self.auth_manager.logout(session_id)
            return RedirectResponse(url="/login")
        
        # 获取所有记忆（分页）
        @self.app.get("/api/memories")
        async def get_memories(request: Request, page: int = 1, page_size: int = 50, status: Optional[str] = None):
            # 验证会话
            session_id = request.cookies.get("session_id")
            if not self.auth_manager.validate_session(session_id):
                raise HTTPException(status_code=401, detail="未授权访问")
            
            try:
                memories = await self.faiss_manager.get_all_memories_for_forgetting()
                
                # 过滤状态
                if status:
                    memories = [m for m in memories if m.get("metadata", {}).get("status", "active") == status]
                
                # 排序（按创建时间倒序）
                memories.sort(key=lambda x: x.get("metadata", {}).get("create_time", 0), reverse=True)
                
                # 分页
                total = len(memories)
                start = (page - 1) * page_size
                end = start + page_size
                paginated_memories = memories[start:end]
                
                # 格式化数据
                formatted_memories = []
                for mem in paginated_memories:
                    metadata = mem.get("metadata", {})
                    formatted_memories.append({
                        "id": mem["id"],
                        "memory_id": metadata.get("memory_id", str(mem["id"])),
                        "content": mem["content"][:100] + "..." if len(mem["content"]) > 100 else mem["content"],
                        "full_content": mem["content"],
                        "importance": metadata.get("importance", 0.5),
                        "status": metadata.get("status", "active"),
                        "create_time": self._format_timestamp(metadata.get("create_time")),
                        "last_access_time": self._format_timestamp(metadata.get("last_access_time")),
                        "session_id": metadata.get("session_id", ""),
                        "event_type": metadata.get("event_type", "OTHER")
                    })
                
                return {
                    "memories": formatted_memories,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "pages": (total + page_size - 1) // page_size
                }
            except Exception as e:
                logger.error(f"获取记忆列表失败: {e}")
                raise HTTPException(status_code=500, detail=f"获取记忆列表失败: {str(e)}")
        
        # 批量删除记忆
        @self.app.delete("/api/memories")
        async def delete_memories(request: Request, memory_ids: list):
            # 验证会话
            session_id = request.cookies.get("session_id")
            if not self.auth_manager.validate_session(session_id):
                raise HTTPException(status_code=401, detail="未授权访问")
            
            try:
                # 确保 memory_ids 是列表
                if not isinstance(memory_ids, list):
                    raise ValueError("memory_ids 必须是列表")
                
                # 转换为整数 ID
                ids_to_delete = []
                for id_str in memory_ids:
                    try:
                        ids_to_delete.append(int(id_str))
                    except ValueError:
                        logger.warning(f"无效的记忆ID: {id_str}")
                
                if not ids_to_delete:
                    return {"success": True, "message": "没有有效的记忆ID可供删除", "deleted_count": 0}
                
                # 执行删除
                # 使用数据库连接直接删除
                connection = self.faiss_manager.db.document_storage.connection
                placeholders = ",".join("?" for _ in ids_to_delete)
                await connection.execute(
                    f"DELETE FROM documents WHERE id IN ({placeholders})",
                    ids_to_delete
                )
                await connection.commit()
                
                # 重新索引（如果需要）
                # 这里简化处理，实际可能需要更多逻辑
                
                logger.info(f"成功删除 {len(ids_to_delete)} 条记忆")
                return {
                    "success": True,
                    "message": f"成功删除 {len(ids_to_delete)} 条记忆",
                    "deleted_count": len(ids_to_delete)
                }
            except Exception as e:
                logger.error(f"批量删除记忆失败: {e}")
                raise HTTPException(status_code=500, detail=f"删除记忆失败: {str(e)}")
        
        # 获取记忆详情
        @self.app.get("/api/memories/{memory_id}")
        async def get_memory_detail(request: Request, memory_id: str):
            # 验证会话
            session_id = request.cookies.get("session_id")
            if not self.auth_manager.validate_session(session_id):
                raise HTTPException(status_code=401, detail="未授权访问")
            
            try:
                result = await self.memory_handler.get_memory_details(memory_id)
                if result.get("success"):
                    return result.get("data", {})
                else:
                    raise HTTPException(status_code=404, detail=result.get("message", "记忆不存在"))
            except Exception as e:
                logger.error(f"获取记忆详情失败: {e}")
                raise HTTPException(status_code=500, detail=f"获取记忆详情失败: {str(e)}")
        
        # 更新记忆
        @self.app.put("/api/memories/{memory_id}")
        async def update_memory(request: Request, memory_id: str, data: dict):
            # 验证会话
            session_id = request.cookies.get("session_id")
            if not self.auth_manager.validate_session(session_id):
                raise HTTPException(status_code=401, detail="未授权访问")
            
            try:
                # 支持更新的字段
                field_mapping = {
                    "content": "content",
                    "importance": "importance",
                    "event_type": "type",
                    "status": "status"
                }
                
                for key, field in field_mapping.items():
                    if key in data:
                        value = data[key]
                        result = await self.memory_handler.edit_memory(memory_id, field, value, "WebUI更新")
                        if not result.get("success"):
                            raise HTTPException(status_code=400, detail=result.get("message"))
                
                return {"success": True, "message": "记忆更新成功"}
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"更新记忆失败: {e}")
                raise HTTPException(status_code=500, detail=f"更新记忆失败: {str(e)}")
        
        # 执行遗忘操作
        @self.app.post("/api/forget")
        async def trigger_forgetting(request: Request):
            # 验证会话
            session_id = request.cookies.get("session_id")
            if not self.auth_manager.validate_session(session_id):
                raise HTTPException(status_code=401, detail="未授权访问")
            
            try:
                if self.forgetting_agent:
                    result = await self.forgetting_agent.run()
                    return {
                        "success": True,
                        "message": "遗忘操作执行成功",
                        "deleted_count": result.get("deleted_count", 0)
                    }
                else:
                    raise HTTPException(status_code=500, detail="遗忘代理未初始化")
            except Exception as e:
                logger.error(f"执行遗忘操作失败: {e}")
                raise HTTPException(status_code=500, detail=f"执行遗忘操作失败: {str(e)}")
    
    def _format_timestamp(self, timestamp):
        """
        格式化时间戳
        
        Args:
            timestamp: 时间戳（秒）或datetime对象
            
        Returns:
            str: 格式化后的时间字符串
        """
        if not timestamp:
            return "未知"
        
        try:
            if isinstance(timestamp, (int, float)):
                dt = datetime.fromtimestamp(timestamp)
            elif isinstance(timestamp, str):
                # 尝试解析ISO格式字符串
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            else:
                return "未知"
            
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "未知"
    
    async def start(self):
        """
        启动WebUI服务器
        """
        try:
            # 启动服务器（非阻塞方式）
            self.server = uvicorn.Server(
                uvicorn.Config(
                    self.app,
                    host="127.0.0.1",
                    port=self.port,
                    log_level="info"
                )
            )
            
            # 在后台运行服务器
            asyncio.create_task(self.server.serve())
            
            logger.info(f"WebUI服务已启动，监听地址: http://127.0.0.1:{self.port}")
            return True
        except Exception as e:
            logger.error(f"启动WebUI服务失败: {e}")
            return False
    
    async def stop(self):
        """
        停止WebUI服务器
        """
        try:
            if self.server:
                self.server.should_exit = True
                logger.info("WebUI服务已停止")
            return True
        except Exception as e:
            logger.error(f"停止WebUI服务失败: {e}")
            return False