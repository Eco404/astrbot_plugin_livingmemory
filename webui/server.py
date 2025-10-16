# -*- coding: utf-8 -*-
"""
server.py - LivingMemory WebUI backend
Provides authentication, memory browsing, detail view and bulk deletion APIs built on FastAPI.
"""


import asyncio
import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from astrbot.api import logger

from ..core.utils import safe_parse_metadata
from ..storage.memory_storage import MemoryStorage

if TYPE_CHECKING:
    from ..storage.faiss_manager import FaissManager
    from ..main import SessionManager


class WebUIServer:
    """
    Helper class responsible for starting and managing the LivingMemory WebUI service.
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

        self.memory_storage: Optional[MemoryStorage] = None
        self._storage_prepared = False

        self._app = FastAPI(title="LivingMemory 控制台", version="1.1.0")
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

        await self._prepare_storage()

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

    async def _prepare_storage(self):
        """
        初始化自定义记忆存储（如可用）。
        """
        if self._storage_prepared:
            return

        connection = None
        try:
            doc_storage = getattr(self.faiss_manager.db, "document_storage", None)
            connection = getattr(doc_storage, "connection", None)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"获取文档存储连接失败: {exc}")

        if connection:
            try:
                storage = MemoryStorage(connection)
                await storage.initialize_schema()
                self.memory_storage = storage
                logger.info("WebUI 已接入插件自定义的记忆存储（SQLite）")
            except Exception as exc:
                logger.warning(f"初始化 MemoryStorage 失败，将回退至文档存储: {exc}")
                self.memory_storage = None
        else:
            logger.debug("未获取到 MemoryStorage 连接，将仅使用 Faiss 文档存储接口")

        self._storage_prepared = True

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
            query = request.query_params
            keyword = query.get("keyword", "").strip()
            status_filter = query.get("status", "all").strip() or "all"
            load_all = query.get("all", "false").lower() == "true"

            if load_all:
                page = 1
                page_size = 0
                offset = 0
            else:
                page = max(1, int(query.get("page", 1)))
                page_size = query.get("page_size")
                page_size = min(200, max(1, int(page_size))) if page_size else 50
                offset = (page - 1) * page_size

            try:
                total, items = await self._fetch_memories(
                    page=page,
                    page_size=page_size,
                    offset=offset,
                    status_filter=status_filter,
                    keyword=keyword,
                    load_all=load_all,
                )
            except Exception as exc:
                logger.error(f"获取记忆列表失败: {exc}", exc_info=True)
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR, detail="读取记忆失败"
                ) from exc

            has_more = False if load_all else offset + len(items) < total
            effective_page_size = page_size if page_size else len(items)

            return {
                "items": items,
                "page": page,
                "page_size": effective_page_size,
                "total": total,
                "has_more": has_more,
            }

        @self._app.get("/api/memories/{memory_id}")
        async def memory_detail(
            memory_id: str, token: str = Depends(self._auth_dependency())
        ):
            detail = await self._get_memory_detail(memory_id)
            if not detail:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="未找到记忆记录")
            return detail

        @self._app.delete("/api/memories")
        async def delete_memories(
            payload: Dict[str, Any],
            token: str = Depends(self._auth_dependency()),
        ):
            doc_ids = payload.get("doc_ids") or payload.get("ids") or []
            memory_ids = payload.get("memory_ids") or []

            if not doc_ids and not memory_ids:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, detail="需要提供待删除的记忆ID列表"
                )

            deleted_docs = 0
            deleted_memories = 0

            if doc_ids:
                try:
                    doc_ids_int = [int(x) for x in doc_ids]
                    await self.faiss_manager.delete_memories(doc_ids_int)
                    deleted_docs = len(doc_ids_int)
                except Exception as exc:
                    logger.error(f"删除 Faiss 记忆失败: {exc}", exc_info=True)
                    raise HTTPException(
                        status.HTTP_500_INTERNAL_SERVER_ERROR, detail="向量记忆删除失败"
                    ) from exc

            if memory_ids and self.memory_storage:
                try:
                    ids = [str(x) for x in memory_ids]
                    await self.memory_storage.delete_memories_by_memory_ids(ids)
                    deleted_memories = len(ids)
                except Exception as exc:
                    logger.error(f"删除结构化记忆失败: {exc}", exc_info=True)
                    raise HTTPException(
                        status.HTTP_500_INTERNAL_SERVER_ERROR, detail="结构化记忆删除失败"
                    ) from exc

            return {
                "deleted_doc_count": deleted_docs,
                "deleted_memory_count": deleted_memories,
            }

        @self._app.get("/api/stats")
        async def stats(token: str = Depends(self._auth_dependency())):
            total, status_counts = await self._gather_statistics()
            active_sessions = (
                self.session_manager.get_session_count()
                if self.session_manager
                else 0
            )

            return {
                "total_memories": total,
                "status_breakdown": status_counts,
                "active_sessions": active_sessions,
                "session_timeout": self.session_timeout,
            }

        @self._app.get("/api/health")
        async def health():
            return {"status": "ok"}

    async def _fetch_memories(
        self,
        page: int,
        page_size: int,
        offset: int,
        status_filter: str,
        keyword: str,
        load_all: bool,
    ) -> Tuple[int, list]:
        if self.memory_storage:
            if load_all:
                records = await self.memory_storage.get_memories_paginated(
                    page_size=0,
                    offset=0,
                    status=status_filter,
                    keyword=keyword,
                )
                total = len(records)
            else:
                total = await self.memory_storage.count_memories(
                    status=status_filter, keyword=keyword
                )
                records = await self.memory_storage.get_memories_paginated(
                    page_size=page_size,
                    offset=offset,
                    status=status_filter,
                    keyword=keyword,
                )
            items = [self._format_memory(record, source="storage") for record in records]
            return total, items

        # fallback: 使用 Faiss 文档存储
        total = await self.faiss_manager.count_total_memories()
        fetch_size = page_size if page_size else max(total, 1)
        records = await self.faiss_manager.get_memories_paginated(
            page_size=fetch_size, offset=offset
        )
        items = [self._format_memory(record, source="faiss") for record in records]
        return total, items

    async def _get_memory_detail(self, memory_id: str) -> Optional[Dict[str, Any]]:
        if self.memory_storage:
            record = await self.memory_storage.get_memory_by_memory_id(memory_id)
            if record:
                return self._format_memory(record, source="storage")

        # fallback: 尝试按文档ID查询
        try:
            doc_id = int(memory_id)
        except ValueError:
            return None

        try:
            docs = await self.faiss_manager.db.document_storage.get_documents(
                ids=[doc_id]
            )
        except Exception as exc:  # pragma: no cover
            logger.error(f"查询文档存储失败: {exc}")
            return None

        if not docs:
            return None

        return self._format_memory(docs[0], source="faiss")

    def _format_memory(self, raw: Dict[str, Any], source: str) -> Dict[str, Any]:
        if source == "storage":
            memory_json = raw.get("memory_data") or "{}"
            parsed = self._safe_json_loads(memory_json)
            metadata = parsed.get("metadata", {})
            access_info = metadata.get("access_info", {})

            summary = (
                parsed.get("summary")
                or parsed.get("description")
                or parsed.get("memory_content")
                or ""
            )
            created_at = parsed.get("timestamp") or raw.get("timestamp")
            last_access = access_info.get("last_accessed_timestamp")

            return {
                "doc_id": None,
                "memory_id": raw.get("memory_id"),
                "summary": summary,
                "memory_type": raw.get("memory_type"),
                "importance": raw.get("importance_score"),
                "status": raw.get("status"),
                "created_at": self._format_timestamp(created_at),
                "last_access": self._format_timestamp(last_access),
                "source": "storage",
                "metadata": metadata,
                "raw": parsed,
                "raw_json": memory_json,
            }

        metadata = safe_parse_metadata(raw.get("metadata"))
        summary = metadata.get("memory_content") or raw.get("content") or ""
        importance = metadata.get("importance")
        event_type = metadata.get("event_type")
        status = metadata.get("status", "active")
        created_at = metadata.get("create_time")
        last_access = metadata.get("last_access_time")

        return {
            "doc_id": raw.get("id"),
            "memory_id": metadata.get("memory_id"),
            "summary": summary,
            "memory_type": event_type,
            "importance": importance,
            "status": status,
            "created_at": self._format_timestamp(created_at),
            "last_access": self._format_timestamp(last_access),
            "source": "faiss",
            "metadata": metadata,
            "raw": {
                "content": raw.get("content"),
                "metadata": metadata,
            },
            "raw_json": json.dumps(metadata, ensure_ascii=False),
        }

    async def _gather_statistics(self) -> Tuple[int, Dict[str, int]]:
        if self.memory_storage:
            total = await self.memory_storage.count_memories()
            counts = {
                "active": await self.memory_storage.count_memories(status="active"),
                "archived": await self.memory_storage.count_memories(status="archived"),
                "deleted": await self.memory_storage.count_memories(status="deleted"),
            }
            return total, counts

        total = await self.faiss_manager.count_total_memories()
        counts = await self._collect_status_counts()
        return total, counts

    async def _collect_status_counts(self) -> Dict[str, int]:
        """
        针对 Faiss 文档存储统计不同状态的记忆数量。
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

    @staticmethod
    def _safe_json_loads(payload: str) -> Dict[str, Any]:
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _format_timestamp(value: Any) -> Optional[str]:
        if not value:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value).isoformat(sep=" ", timespec="seconds")
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(
                    sep=" ", timespec="seconds"
                )
            except ValueError:
                return value
        return str(value)
