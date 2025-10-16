# -*- coding: utf-8 -*-
"""
webui_handler.py - WebUI处理器
负责处理WebUI相关的逻辑，包括记忆查看、删除等功能。
"""

import os
import json
import time
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from astrbot.api import logger
from ..core.utils import safe_parse_metadata
from ..storage.faiss_manager import FaissManager


class WebUIHandler:
    """WebUI处理器类，负责处理WebUI相关的逻辑"""
    
    def __init__(self, context, config: Dict[str, Any], faiss_manager: FaissManager):
        """
        初始化WebUI处理器
        
        Args:
            context: AstrBot上下文
            config: 插件配置
            faiss_manager: Faiss管理器实例
        """
        self.context = context
        self.config = config
        self.faiss_manager = faiss_manager
        self.webui_config = config.get("webui", {})
        
    def verify_password(self, password: str) -> bool:
        """
        验证访问密码
        
        Args:
            password: 用户输入的密码
            
        Returns:
            bool: 密码是否正确
        """
        stored_password = self.webui_config.get("access_password", "")
        
        # 如果没有设置密码，则直接通过
        if not stored_password:
            return True
            
        return password == stored_password
    
    async def get_all_memories(self, page: int = 1, items_per_page: int = 20) -> Dict[str, Any]:
        """
        获取所有记忆，支持分页
        
        Args:
            page: 页码，从1开始
            items_per_page: 每页显示的项目数
            
        Returns:
            Dict: 包含记忆列表和分页信息的字典
        """
        try:
            # 获取所有记忆
            all_memories = await self.faiss_manager.get_all_memories()
            
            # 对记忆按时间倒序排序
            all_memories.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            
            # 计算分页
            total_items = len(all_memories)
            total_pages = (total_items + items_per_page - 1) // items_per_page
            
            # 确保页码在有效范围内
            page = max(1, min(page, total_pages)) if total_pages > 0 else 1
            
            # 获取当前页的记忆
            start_idx = (page - 1) * items_per_page
            end_idx = min(start_idx + items_per_page, total_items)
            page_memories = all_memories[start_idx:end_idx]
            
            # 格式化记忆数据
            formatted_memories = []
            for memory in page_memories:
                # 解析元数据
                metadata = memory.get("metadata", "{}")
                if isinstance(metadata, str):
                    metadata = safe_parse_metadata(metadata)
                
                # 格式化时间戳
                created_at = memory.get("created_at", 0)
                updated_at = memory.get("updated_at", 0)
                
                formatted_memory = {
                    "id": memory.get("id", ""),
                    "content": memory.get("content", ""),
                    "importance": memory.get("importance", 0.0),
                    "type": metadata.get("type", "OTHER"),
                    "session_id": metadata.get("session_id", ""),
                    "persona_id": metadata.get("persona_id", ""),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "created_at_formatted": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S") if created_at else "未知",
                    "updated_at_formatted": datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d %H:%M:%S") if updated_at else "未知",
                }
                formatted_memories.append(formatted_memory)
            
            return {
                "memories": formatted_memories,
                "pagination": {
                    "current_page": page,
                    "total_pages": total_pages,
                    "total_items": total_items,
                    "items_per_page": items_per_page,
                    "has_prev": page > 1,
                    "has_next": page < total_pages
                }
            }
        except Exception as e:
            logger.error(f"获取记忆列表失败: {e}", exc_info=True)
            return {
                "memories": [],
                "pagination": {
                    "current_page": 1,
                    "total_pages": 0,
                    "total_items": 0,
                    "items_per_page": items_per_page,
                    "has_prev": False,
                    "has_next": False
                },
                "error": str(e)
            }
    
    async def get_memory_details(self, memory_id: str) -> Dict[str, Any]:
        """
        获取记忆的详细信息
        
        Args:
            memory_id: 记忆ID
            
        Returns:
            Dict: 记忆的详细信息
        """
        try:
            memory = await self.faiss_manager.get_memory_by_id(memory_id)
            if not memory:
                return {"error": "记忆不存在"}
            
            # 解析元数据
            metadata = memory.get("metadata", "{}")
            if isinstance(metadata, str):
                metadata = safe_parse_metadata(metadata)
            
            # 格式化时间戳
            created_at = memory.get("created_at", 0)
            updated_at = memory.get("updated_at", 0)
            
            return {
                "id": memory.get("id", ""),
                "content": memory.get("content", ""),
                "importance": memory.get("importance", 0.0),
                "type": metadata.get("type", "OTHER"),
                "session_id": metadata.get("session_id", ""),
                "persona_id": metadata.get("persona_id", ""),
                "created_at": created_at,
                "updated_at": updated_at,
                "created_at_formatted": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S") if created_at else "未知",
                "updated_at_formatted": datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d %H:%M:%S") if updated_at else "未知",
                "raw_metadata": metadata
            }
        except Exception as e:
            logger.error(f"获取记忆详情失败: {e}", exc_info=True)
            return {"error": str(e)}
    
    async def delete_memory(self, memory_id: str) -> Dict[str, Any]:
        """
        删除单条记忆
        
        Args:
            memory_id: 记忆ID
            
        Returns:
            Dict: 操作结果
        """
        try:
            success = await self.faiss_manager.delete_memory(memory_id)
            if success:
                return {"success": True, "message": f"记忆 {memory_id} 已成功删除"}
            else:
                return {"success": False, "message": f"删除记忆 {memory_id} 失败"}
        except Exception as e:
            logger.error(f"删除记忆失败: {e}", exc_info=True)
            return {"success": False, "message": f"删除记忆失败: {str(e)}"}
    
    async def batch_delete_memories(self, memory_ids: List[str]) -> Dict[str, Any]:
        """
        批量删除记忆
        
        Args:
            memory_ids: 记忆ID列表
            
        Returns:
            Dict: 操作结果
        """
        try:
            if not memory_ids:
                return {"success": False, "message": "未选择要删除的记忆"}
            
            success_count = 0
            failed_ids = []
            
            for memory_id in memory_ids:
                success = await self.faiss_manager.delete_memory(memory_id)
                if success:
                    success_count += 1
                else:
                    failed_ids.append(memory_id)
            
            if len(failed_ids) == 0:
                return {
                    "success": True, 
                    "message": f"成功删除 {success_count} 条记忆",
                    "success_count": success_count,
                    "failed_count": 0
                }
            else:
                return {
                    "success": False, 
                    "message": f"删除完成，成功 {success_count} 条，失败 {len(failed_ids)} 条",
                    "success_count": success_count,
                    "failed_count": len(failed_ids),
                    "failed_ids": failed_ids
                }
        except Exception as e:
            logger.error(f"批量删除记忆失败: {e}", exc_info=True)
            return {"success": False, "message": f"批量删除失败: {str(e)}"}
    
    async def search_memories(self, query: str, page: int = 1, items_per_page: int = 20) -> Dict[str, Any]:
        """
        搜索记忆
        
        Args:
            query: 搜索查询
            page: 页码，从1开始
            items_per_page: 每页显示的项目数
            
        Returns:
            Dict: 包含搜索结果和分页信息的字典
        """
        try:
            # 使用faiss_manager搜索记忆
            search_results = await self.faiss_manager.search_memories(query, k=100)  # 先获取较多结果
            
            # 计算分页
            total_items = len(search_results)
            total_pages = (total_items + items_per_page - 1) // items_per_page
            
            # 确保页码在有效范围内
            page = max(1, min(page, total_pages)) if total_pages > 0 else 1
            
            # 获取当前页的记忆
            start_idx = (page - 1) * items_per_page
            end_idx = min(start_idx + items_per_page, total_items)
            page_memories = search_results[start_idx:end_idx]
            
            # 格式化记忆数据
            formatted_memories = []
            for memory in page_memories:
                # 解析元数据
                metadata = memory.get("metadata", "{}")
                if isinstance(metadata, str):
                    metadata = safe_parse_metadata(metadata)
                
                # 格式化时间戳
                created_at = memory.get("created_at", 0)
                updated_at = memory.get("updated_at", 0)
                
                formatted_memory = {
                    "id": memory.get("id", ""),
                    "content": memory.get("content", ""),
                    "importance": memory.get("importance", 0.0),
                    "similarity": memory.get("similarity", 0.0),
                    "type": metadata.get("type", "OTHER"),
                    "session_id": metadata.get("session_id", ""),
                    "persona_id": metadata.get("persona_id", ""),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "created_at_formatted": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S") if created_at else "未知",
                    "updated_at_formatted": datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d %H:%M:%S") if updated_at else "未知",
                }
                formatted_memories.append(formatted_memory)
            
            return {
                "memories": formatted_memories,
                "pagination": {
                    "current_page": page,
                    "total_pages": total_pages,
                    "total_items": total_items,
                    "items_per_page": items_per_page,
                    "has_prev": page > 1,
                    "has_next": page < total_pages
                },
                "query": query
            }
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}", exc_info=True)
            return {
                "memories": [],
                "pagination": {
                    "current_page": 1,
                    "total_pages": 0,
                    "total_items": 0,
                    "items_per_page": items_per_page,
                    "has_prev": False,
                    "has_next": False
                },
                "query": query,
                "error": str(e)
            }
    
    async def get_memory_statistics(self) -> Dict[str, Any]:
        """
        获取记忆统计信息
        
        Returns:
            Dict: 记忆统计信息
        """
        try:
            # 获取所有记忆
            all_memories = await self.faiss_manager.get_all_memories()
            
            # 统计信息
            total_count = len(all_memories)
            
            # 按类型统计
            type_counts = {}
            importance_sum = 0.0
            
            for memory in all_memories:
                # 解析元数据
                metadata = memory.get("metadata", "{}")
                if isinstance(metadata, str):
                    metadata = safe_parse_metadata(metadata)
                
                mem_type = metadata.get("type", "OTHER")
                type_counts[mem_type] = type_counts.get(mem_type, 0) + 1
                
                importance_sum += memory.get("importance", 0.0)
            
            # 计算平均重要性
            avg_importance = importance_sum / total_count if total_count > 0 else 0.0
            
            # 按重要性分组
            importance_groups = {
                "high": 0,    # 0.7 - 1.0
                "medium": 0,  # 0.4 - 0.7
                "low": 0      # 0.0 - 0.4
            }
            
            for memory in all_memories:
                importance = memory.get("importance", 0.0)
                if importance >= 0.7:
                    importance_groups["high"] += 1
                elif importance >= 0.4:
                    importance_groups["medium"] += 1
                else:
                    importance_groups["low"] += 1
            
            return {
                "total_count": total_count,
                "type_distribution": type_counts,
                "average_importance": round(avg_importance, 4),
                "importance_distribution": importance_groups
            }
        except Exception as e:
            logger.error(f"获取记忆统计失败: {e}", exc_info=True)
            return {"error": str(e)}