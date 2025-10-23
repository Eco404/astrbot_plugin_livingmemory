# -*- coding: utf-8 -*-
"""
稀疏检索器 - 基于 SQLite FTS5 和 BM25 的全文检索
"""

import json
import sqlite3
import math
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
import asyncio
import aiosqlite

from astrbot.api import logger

try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False
    logger.warning("jieba not available, Chinese tokenization disabled")


@dataclass
class SparseResult:
    """稀疏检索结果"""
    doc_id: int
    score: float
    content: str
    metadata: Dict[str, Any]


class FTSManager:
    """FTS5 索引管理器"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.fts_table_name = "documents_fts"
        
    async def initialize(self):
        """初始化 FTS5 索引"""
        async with aiosqlite.connect(self.db_path) as db:
            # 启用 FTS5 扩展
            await db.execute("PRAGMA foreign_keys = ON")
            
            # 创建 FTS5 虚拟表
            await db.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table_name} 
                USING fts5(content, doc_id, tokenize='unicode61')
            """)
            
            # 创建触发器，保持同步
            await self._create_triggers(db)
            
            await db.commit()
            logger.info(f"FTS5 index initialized: {self.fts_table_name}")
    
    async def _create_triggers(self, db: aiosqlite.Connection):
        """创建数据同步触发器"""
        # 插入触发器
        await db.execute(f"""
            CREATE TRIGGER IF NOT EXISTS documents_ai 
            AFTER INSERT ON documents BEGIN
                INSERT INTO {self.fts_table_name}(doc_id, content) 
                VALUES (new.id, new.text);
            END;
        """)
        
        # 删除触发器
        await db.execute(f"""
            CREATE TRIGGER IF NOT EXISTS documents_ad 
            AFTER DELETE ON documents BEGIN
                DELETE FROM {self.fts_table_name} WHERE doc_id = old.id;
            END;
        """)
        
        # 更新触发器
        await db.execute(f"""
            CREATE TRIGGER IF NOT EXISTS documents_au 
            AFTER UPDATE ON documents BEGIN
                DELETE FROM {self.fts_table_name} WHERE doc_id = old.id;
                INSERT INTO {self.fts_table_name}(doc_id, content) 
                VALUES (new.id, new.text);
            END;
        """)
    
    async def rebuild_index(self):
        """重建索引"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"DELETE FROM {self.fts_table_name}")
            await db.execute(f"""
                INSERT INTO {self.fts_table_name}(doc_id, content)
                SELECT id, text FROM documents
            """)
            await db.commit()
            logger.info("FTS index rebuilt")
    
    async def search(self, query: str, limit: int = 50) -> List[Tuple[int, float]]:
        """执行 BM25 搜索"""
        async with aiosqlite.connect(self.db_path) as db:
            # 将整个查询用双引号包裹，以处理特殊字符并将其作为短语搜索
            # 这是为了防止 FTS5 语法错误，例如 'syntax error near "."'
            safe_query = f'"{query}"'

            # 使用 BM25 算法搜索
            cursor = await db.execute(f"""
                SELECT doc_id, bm25({self.fts_table_name}) as score 
                FROM {self.fts_table_name} 
                WHERE {self.fts_table_name} MATCH ?
                ORDER BY score
                LIMIT ?
            """, (safe_query, limit))
            
            results = await cursor.fetchall()
            return [(row[0], row[1]) for row in results]


class SparseRetriever:
    """稀疏检索器"""
    
    def __init__(self, db_path: str, config: Dict[str, Any] = None):
        self.db_path = db_path
        self.config = config or {}
        self.fts_manager = FTSManager(db_path)
        self.enabled = self.config.get("enabled", True)
        self.use_chinese_tokenizer = self.config.get("use_chinese_tokenizer", JIEBA_AVAILABLE)

        logger.info("SparseRetriever 初始化")
        logger.info(f"  启用状态: {'是' if self.enabled else '否'}")
        logger.info(f"  中文分词: {'是' if self.use_chinese_tokenizer else '否'} (jieba {'可用' if JIEBA_AVAILABLE else '不可用'})")
        logger.info(f"  数据库路径: {db_path}")
        
    async def initialize(self):
        """初始化稀疏检索器"""
        if not self.enabled:
            logger.info("稀疏检索器已禁用，跳过初始化")
            return

        logger.info("开始初始化稀疏检索器...")

        try:
            await self.fts_manager.initialize()
            logger.info("✅ FTS5 索引初始化成功")
        except Exception as e:
            logger.error(f"❌ FTS5 索引初始化失败: {type(e).__name__}: {e}", exc_info=True)
            raise

        # 如果启用中文分词，初始化 jieba
        if self.use_chinese_tokenizer and JIEBA_AVAILABLE:
            logger.debug("jieba 中文分词已启用")
            # 可以添加自定义词典
            pass

        logger.info("✅ 稀疏检索器初始化完成")
    
    def _preprocess_query(self, query: str) -> str:
        """
        预处理查询，包括分词和安全转义。

        Args:
            query: 原始查询字符串

        Returns:
            str: 处理后的安全查询字符串
        """
        query = query.strip()

        # 中文分词
        if self.use_chinese_tokenizer and JIEBA_AVAILABLE:
            # 检查是否包含中文
            if any('\u4e00' <= char <= '\u9fff' for char in query):
                tokens = jieba.cut_for_search(query)
                query = " ".join(tokens)

        # FTS5 安全转义: 双引号需要转义为两个双引号
        # 移除可能导致语法错误的特殊FTS5操作符
        query = query.replace('"', '""')  # FTS5转义规则

        # 移除可能的FTS5特殊字符和操作符
        # FTS5特殊字符: * (通配符), ^ (列过滤), NEAR, AND, OR, NOT
        # 为了安全，我们将查询作为短语搜索，禁用这些操作符
        query = query.replace('*', ' ')  # 移除通配符
        query = query.replace('^', ' ')  # 移除列过滤符

        return query
    
    async def search(
        self,
        query: str,
        limit: int = 50,
        session_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        metadata_filters: Optional[Dict[str, Any]] = None
    ) -> List[SparseResult]:
        """执行稀疏检索"""
        if not self.enabled:
            logger.debug("稀疏检索器未启用，返回空结果")
            return []

        logger.debug(f"稀疏检索: query='{query[:50]}...', limit={limit}")

        try:
            # 预处理查询
            processed_query = self._preprocess_query(query)
            logger.debug(f"  原始查询: '{query[:50]}...'")
            logger.debug(f"  处理后查询: '{processed_query[:50]}...'")

            # 执行 FTS 搜索
            fts_results = await self.fts_manager.search(processed_query, limit)

            if not fts_results:
                logger.debug("  FTS 搜索无结果")
                return []

            logger.debug(f"  FTS 返回 {len(fts_results)} 条原始结果")

            # 获取完整的文档信息
            doc_ids = [doc_id for doc_id, _ in fts_results]
            logger.debug(f"  获取文档详情: {len(doc_ids)} 个 ID")
            documents = await self._get_documents(doc_ids)
            logger.debug(f"  成功获取 {len(documents)} 个文档")

            # 应用过滤器
            filtered_results = []
            for doc_id, bm25_score in fts_results:
                if doc_id in documents:
                    doc = documents[doc_id]

                    # 检查元数据过滤器
                    if self._apply_filters(doc.get("metadata", {}), session_id, persona_id, metadata_filters):
                        result = SparseResult(
                            doc_id=doc_id,
                            score=bm25_score,
                            content=doc["text"],
                            metadata=doc["metadata"]
                        )
                        filtered_results.append(result)

            logger.debug(f"  过滤后剩余 {len(filtered_results)} 条结果")

            # 归一化 BM25 分数（转换为 0-1）
            if filtered_results:
                max_score = max(r.score for r in filtered_results)
                min_score = min(r.score for r in filtered_results)
                score_range = max_score - min_score if max_score != min_score else 1

                logger.debug(f"  归一化分数: min={min_score:.3f}, max={max_score:.3f}, range={score_range:.3f}")

                for result in filtered_results:
                    original_score = result.score
                    result.score = (result.score - min_score) / score_range
                    logger.debug(f"    ID={result.doc_id}: {original_score:.3f} -> {result.score:.3f}")

            logger.info(f"✅ 稀疏检索完成，返回 {len(filtered_results)} 条结果")
            return filtered_results

        except Exception as e:
            logger.error(
                f"❌ 稀疏检索失败: {type(e).__name__}: {e}",
                exc_info=True
            )
            logger.error(f"  失败上下文: query='{query[:50]}...', limit={limit}")
            return []
    
    async def _get_documents(self, doc_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """批量获取文档"""
        async with aiosqlite.connect(self.db_path) as db:
            placeholders = ",".join("?" for _ in doc_ids)
            cursor = await db.execute(f"""
                SELECT id, text, metadata FROM documents WHERE id IN ({placeholders})
            """, doc_ids)
            
            documents = {}
            async for row in cursor:
                metadata = json.loads(row[2]) if isinstance(row[2], str) else row[2]
                documents[row[0]] = {
                    "text": row[1],
                    "metadata": metadata or {}
                }
            
            return documents
    
    def _apply_filters(
        self, 
        metadata: Dict[str, Any], 
        session_id: Optional[str],
        persona_id: Optional[str],
        metadata_filters: Optional[Dict[str, Any]]
    ) -> bool:
        """应用过滤器"""
        # 会话过滤
        if session_id and metadata.get("session_id") != session_id:
            return False
        
        # 人格过滤
        if persona_id and metadata.get("persona_id") != persona_id:
            return False
        
        # 自定义元数据过滤
        if metadata_filters:
            for key, value in metadata_filters.items():
                if metadata.get(key) != value:
                    return False
        
        return True
    
    async def rebuild_index(self):
        """重建索引"""
        if not self.enabled:
            logger.warning("稀疏检索器未启用，无法重建索引")
            return

        logger.info("🔄 开始重建 FTS5 索引...")

        try:
            await self.fts_manager.rebuild_index()
            logger.info("✅ FTS5 索引重建成功")
        except Exception as e:
            logger.error(
                f"❌ 重建 FTS5 索引失败: {type(e).__name__}: {e}",
                exc_info=True
            )
            raise