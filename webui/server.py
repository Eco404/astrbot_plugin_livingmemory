# -*- coding: utf-8 -*-
"""
server.py - LivingMemory WebUI 服务
提供基于 FastAPI 的管理界面后端，包括登录、记忆浏览与批量删除功能。
"""

import asyncio
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from astrbot.api import logger

from ..core.utils import safe_parse_metadata

if TYPE_CHECKING:
    from ..storage.faiss_manager import FaissManager
    from ..main import SessionManager


class WebUIServer:
    """
    负责启动和管理 WebUI 服务的类。
    """

    def __init__(
        self,
        config: Dict[str, Any],
        faiss_manager: "FaissManager",
        session_manager: Optional["SessionManager"] = None,
    ):
        self.config = config
        self.faiss_manager = faiss_manager
        self.session_manager = session_manager

        self.host = str(config.get("host", "127.0.0.1"))
        self.port = int(config.get("port", 8080))
        self.session_timeout = max(60, int(config.get("session_timeout", 3600)))
        self._access_password = str(config.get("access_password", "")).strip()

        self._tokens: Dict[str, float] = {}
        self._token_lock = asyncio.Lock()
        self._server: Optional[uvicorn.Server] = None
        self._server_task: Optional[asyncio.Task] = None

        self._app = FastAPI(title="LivingMemory 控制台", version="1.0.0")
        self._setup_routes()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self):
        """
        启动 WebUI 服务。
        """
        if self._server_task and not self._server_task.done():
            logger.warning("WebUI 服务已经在运行")
            return

        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level="info",
            loop="asyncio",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())

        # 等待服务启动
        for _ in range(50):
            if getattr(self._server, "started", False):
                logger.info(f"WebUI 已启动: http://{self.host}:{self.port}")
                return
            if self._server_task.done():
                error = self._server_task.exception()
                raise RuntimeError(f"WebUI 启动失败: {error}") from error
            await asyncio.sleep(0.1)

        logger.warning("WebUI 启动耗时较长，仍在后台启动中")

    async def stop(self):
        """
        停止 WebUI 服务。
        """
        if self._server:
            self._server.should_exit = True
        if self._server_task:
            await self._server_task
        self._server = None
        self._server_task = None
        logger.info("WebUI 已停止")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _setup_routes(self):
        """
        初始化 FastAPI 路由与静态资源。
        """
        static_dir = Path(__file__).resolve().parent.parent / "static"
        index_path = static_dir / "index.html"
        if not index_path.exists():
            logger.warning("未找到 WebUI 前端文件，静态资源目录为空")

        self._app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                f"http://{self.host}:{self.port}",
                "http://localhost",
                "http://127.0.0.1",
            ],
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )

        self._app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @self._app.get("/", response_class=HTMLResponse)
        async def serve_index():
            if not index_path.exists():
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="前端文件缺失")
            return HTMLResponse(index_path.read_text(encoding="utf-8"))

        @self._app.post("/api/login")
        async def login(payload: Dict[str, Any]):
            password = str(payload.get("password", "")).strip()
            if not password:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="密码不能为空")
            if password != self._access_password:
                await asyncio.sleep(0.3)  # 减缓暴力破解
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="认证失败")

            token = secrets.token_urlsafe(32)
            expires_at = time.time() + self.session_timeout
            async with self._token_lock:
                await self._cleanup_tokens_locked()
                self._tokens[token] = expires_at

            return {"token": token, "expires_in": self.session_timeout}

        @self._app.post("/api/logout")
        async def logout(token: str = Depends(self._auth_dependency())):
            async with self._token_lock:
                self._tokens.pop(token, None)
            return {"detail": "已退出登录"}

        @self._app.get("/api/memories")
        async def list_memories(
            request: Request,
            token: str = Depends(self._auth_dependency()),
        ):
            page = max(1, int(request.query_params.get("page", 1)))
            page_size = min(200, max(1, int(request.query_params.get("page_size", 50))))

            offset = (page - 1) * page_size
            try:
                memories_task = asyncio.create_task(
                    self.faiss_manager.get_memories_paginated(
                        page_size=page_size, offset=offset
                    )
                )
                total_task = asyncio.create_task(
                    self.faiss_manager.count_total_memories()
                )

                raw_memories = await memories_task
                total = await total_task
            except Exception as exc:
                logger.error(f"获取记忆列表失败: {exc}", exc_info=True)
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="读取记忆失败")

            items = [self._format_memory(record) for record in raw_memories]

            return {
                "items": items,
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_more": offset + len(items) < total,
            }

        @self._app.delete("/api/memories")
        async def delete_memories(
            payload: Dict[str, Any],
            token: str = Depends(self._auth_dependency()),
        ):
            ids = payload.get("ids") or payload.get("doc_ids")
            if not isinstance(ids, list) or not ids:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="需要提供待删除的记忆ID列表")

            try:
                doc_ids = [int(x) for x in ids]
            except (TypeError, ValueError):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="ID列表必须为整数")

            try:
                await self.faiss_manager.delete_memories(doc_ids)
            except Exception as exc:
                logger.error(f"删除记忆失败: {exc}", exc_info=True)
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="删除记忆失败")

            return {"deleted": len(doc_ids)}

        @self._app.get("/api/stats")
        async def stats(token: str = Depends(self._auth_dependency())):
            total_task = asyncio.create_task(self.faiss_manager.count_total_memories())
            status_task = asyncio.create_task(self._collect_status_counts())

            total = await total_task
            status_counts = await status_task
            active_sessions = self.session_manager.get_session_count() if self.session_manager else 0

            return {
                "total_memories": total,
                "status_breakdown": status_counts,
                "active_sessions": active_sessions,
                "session_timeout": self.session_timeout,
            }

        @self._app.get("/api/health")
        async def health():
            return {"status": "ok"}

    def _auth_dependency(self):
        async def dependency(request: Request) -> str:
            token = self._extract_token(request)
            if not token:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="未授权")
            await self._validate_token(token)
            return token

        return dependency

    async def _validate_token(self, token: str):
        async with self._token_lock:
            await self._cleanup_tokens_locked()
            expiry = self._tokens.get(token)
            if not expiry:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="会话已失效")
            if expiry < time.time():
                self._tokens.pop(token, None)
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="会话已过期")
            self._tokens[token] = time.time() + self.session_timeout

    async def _cleanup_tokens_locked(self):
        now = time.time()
        expired = [token for token, expiry in self._tokens.items() if expiry < now]
        for token in expired:
            self._tokens.pop(token, None)

    def _extract_token(self, request: Request) -> str:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        cookie_token = request.cookies.get("auth_token")
        if cookie_token:
            return cookie_token.strip()
        custom_header = request.headers.get("X-Auth-Token", "")
        return custom_header.strip()

    def _format_memory(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        metadata = safe_parse_metadata(raw.get("metadata"))

        def _fmt(ts: Any) -> Optional[str]:
            if ts in (None, "", 0):
                return None
            if isinstance(ts, (int, float)):
                return datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds")
            try:
                return datetime.fromtimestamp(float(ts)).isoformat(sep=" ", timespec="seconds")
            except (TypeError, ValueError):
                if isinstance(ts, str):
                    return ts
            return None

        content = raw.get("content") or ""
        preview = content.replace("\n", " ")[:120]

        return {
            "id": raw.get("id"),
            "content": content,
            "preview": preview,
            "importance": metadata.get("importance"),
            "event_type": metadata.get("event_type"),
            "status": metadata.get("status", "active"),
            "session_id": metadata.get("session_id"),
            "persona_id": metadata.get("persona_id"),
            "create_time": _fmt(metadata.get("create_time")),
            "last_access_time": _fmt(metadata.get("last_access_time")),
            "last_updated_time": _fmt(metadata.get("last_updated_time")),
        }

    async def _collect_status_counts(self) -> Dict[str, int]:
        """
        统计不同状态的记忆数量。
        """
        counts: Dict[str, int] = {"active": 0, "archived": 0, "deleted": 0}
        try:
            conn = self.faiss_manager.db.document_storage.connection
            async with conn.execute(
                "SELECT json_extract(metadata, '$.status') AS status FROM documents"
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                status_value = row[0] if row and row[0] else "active"
                counts[status_value] = counts.get(status_value, 0) + 1
        except Exception as exc:
            logger.error(f"统计记忆状态失败: {exc}", exc_info=True)
        return counts

