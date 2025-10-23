# -*- coding: utf-8 -*-
"""
forgetting_agent.py - 遗忘代理
作为一个后台任务，定期清理陈旧的、不重要的记忆，模拟人类的遗忘曲线。
"""

import asyncio
import json
from typing import Dict, Any, Optional

from astrbot.api import logger
from astrbot.api.star import Context
from ...storage.faiss_manager import FaissManager
from ..utils import get_now_datetime, safe_parse_metadata, validate_timestamp


class ForgettingAgent:
    """
    遗忘代理：作为一个后台任务，定期清理陈旧的、不重要的记忆，模拟人类的遗忘曲线。
    """

    def __init__(
        self, context: Context, config: Dict[str, Any], faiss_manager: FaissManager
    ):
        """
        初始化遗忘代理。

        Args:
            context (Context): AstrBot 的上下文对象。
            config (Dict[str, Any]): 插件配置中 'forgetting_agent' 部分的字典。
            faiss_manager (FaissManager): 数据库管理器实例。
        """
        self.context = context
        self.config = config
        self.faiss_manager = faiss_manager
        self._task: Optional[asyncio.Task] = None
        self._manual_task: Optional[asyncio.Task] = None
        self._operation_lock = asyncio.Lock()

        # 记录配置信息
        enabled = config.get("enabled", True)
        retention_days = config.get("retention_days", 90)
        check_interval_hours = config.get("check_interval_hours", 24)
        decay_rate = config.get("importance_decay_rate", 0.005)
        importance_threshold = config.get("importance_threshold", 0.1)

        logger.info("ForgettingAgent 初始化成功")
        logger.info(f"  启用状态: {'是' if enabled else '否'}")
        logger.info(f"  保留天数: {retention_days} 天")
        logger.info(f"  检查间隔: {check_interval_hours} 小时")
        logger.info(f"  衰减率: {decay_rate}/天")
        logger.info(f"  重要性阈值: {importance_threshold}")

    async def start(self):
        """启动后台遗忘任务。"""
        if not self.config.get("enabled", True):
            logger.info("遗忘代理未启用，不启动后台任务。")
            return

        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_periodically())
            logger.info("遗忘代理后台任务已启动。")

    async def stop(self):
        """停止后台遗忘任务。"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("遗忘代理后台任务已成功取消。")
        self._task = None

    async def trigger_manual_run(self) -> Dict[str, Any]:
        """手动触发遗忘任务的公共接口,使用锁防止竞态条件。

        Returns:
            Dict[str, Any]: 包含 'success' 和 'message' 的响应字典
        """
        async with self._operation_lock:
            # 检查是否有正在运行的手动任务
            if self._manual_task and not self._manual_task.done():
                return {
                    "success": False,
                    "message": "遗忘任务正在运行中,请稍后再试"
                }

            try:
                logger.info("手动触发遗忘代理任务...")
                await self._prune_memories()
                return {
                    "success": True,
                    "message": "遗忘代理任务执行完毕"
                }
            except Exception as e:
                logger.error(f"手动触发遗忘任务失败: {e}", exc_info=True)
                return {
                    "success": False,
                    "message": f"遗忘任务执行失败: {e}"
                }

    async def _run_periodically(self):
        """后台任务的循环体。"""
        interval_hours = self.config.get("check_interval_hours", 24)
        interval_seconds = interval_hours * 3600
        logger.info(f"🕐 遗忘代理定期任务已启动，每 {interval_hours} 小时运行一次")

        while True:
            try:
                logger.debug(f"⏰ 等待 {interval_hours} 小时后执行下一次清理...")
                await asyncio.sleep(interval_seconds)

                logger.info("🧹 开始执行记忆清理任务...")
                await self._prune_memories()
                logger.info("✅ 记忆清理任务执行完毕")

            except asyncio.CancelledError:
                logger.info("🛑 遗忘代理任务已被取消")
                break
            except Exception as e:
                logger.error(
                    f"❌ 遗忘代理后台任务发生错误: {type(e).__name__}: {e}",
                    exc_info=True
                )
                # 即使出错，也等待下一个周期，避免快速失败刷屏
                logger.warning(f"⏳ 等待 60 秒后重试...")
                await asyncio.sleep(60)

    async def _prune_memories(self):
        """执行一次完整的记忆衰减和修剪，使用分页处理避免内存过载。"""
        try:
            # 获取记忆总数
            total_memories = await self.faiss_manager.count_total_memories()
            if total_memories == 0:
                logger.info("📭 数据库中没有记忆，无需清理")
                return

            retention_days = self.config.get("retention_days", 90)
            decay_rate = self.config.get("importance_decay_rate", 0.005)
            importance_threshold = self.config.get("importance_threshold", 0.1)
            current_time = get_now_datetime(self.context).timestamp()

            # 分页处理配置
            page_size = self.config.get("forgetting_batch_size", 1000)  # 每批处理数量

            logger.info(f"📊 清理任务配置:")
            logger.info(f"  总记忆数: {total_memories}")
            logger.info(f"  保留天数: {retention_days}")
            logger.info(f"  衰减率: {decay_rate}/天")
            logger.info(f"  重要性阈值: {importance_threshold}")
            logger.info(f"  批处理大小: {page_size}")

            memories_to_update = []
            ids_to_delete = []
            total_processed = 0
            decay_count = 0

            # 分页处理所有记忆
            batch_num = 0
            for offset in range(0, total_memories, page_size):
                batch_num += 1
                logger.debug(f"📦 处理第 {batch_num} 批 (offset={offset}, size={page_size})...")

                try:
                    batch_memories = await self.faiss_manager.get_memories_paginated(
                        page_size=page_size, offset=offset
                    )
                except Exception as e:
                    logger.error(f"❌ 获取第 {batch_num} 批记忆失败: {e}", exc_info=True)
                    continue

                if not batch_memories:
                    logger.debug(f"  第 {batch_num} 批无数据，结束分页")
                    break

                logger.debug(f"  第 {batch_num} 批加载了 {len(batch_memories)} 条记忆")

                batch_updates = []
                batch_deletes = []

                for mem in batch_memories:
                    # 使用统一的元数据解析函数
                    metadata = safe_parse_metadata(mem["metadata"])
                    if not metadata:
                        logger.warning(f"⚠️ 记忆 {mem['id']} 的元数据解析失败，跳过处理")
                        continue

                    # 1. 重要性衰减
                    create_time = validate_timestamp(metadata.get("create_time"), current_time)
                    days_since_creation = (current_time - create_time) / (24 * 3600)

                    original_importance = metadata.get("importance", 0.5)
                    # 线性衰减
                    decayed_importance = original_importance - (days_since_creation * decay_rate)
                    metadata["importance"] = max(0, decayed_importance)  # 确保不为负

                    if decayed_importance < original_importance:
                        decay_count += 1

                    mem["metadata"] = metadata  # 更新内存中的 metadata
                    batch_updates.append(mem)

                    # 2. 识别待删除项
                    retention_seconds = retention_days * 24 * 3600
                    is_old = (current_time - create_time) > retention_seconds
                    is_unimportant = metadata["importance"] < importance_threshold

                    if is_old and is_unimportant:
                        batch_deletes.append(mem["id"])
                        logger.debug(
                            f"  标记删除: ID={mem['id']}, 天数={days_since_creation:.1f}, "
                            f"重要性={metadata['importance']:.3f}"
                        )

                # 累积到全局列表
                memories_to_update.extend(batch_updates)
                ids_to_delete.extend(batch_deletes)
                total_processed += len(batch_memories)

                # 如果批次数据过多，执行中间提交
                if len(memories_to_update) >= page_size * 2:
                    logger.debug(f"💾 执行中间批次更新，更新 {len(memories_to_update)} 条记忆")
                    try:
                        await self.faiss_manager.update_memories_metadata(memories_to_update)
                        logger.debug(f"  中间批次更新成功")
                    except Exception as e:
                        logger.error(f"❌ 中间批次更新失败: {e}", exc_info=True)
                    memories_to_update.clear()

                logger.debug(f"  批次进度: {total_processed}/{total_memories} ({(total_processed/total_memories)*100:.1f}%)")

            # 3. 执行最终数据库操作
            if memories_to_update:
                logger.info(f"💾 更新 {len(memories_to_update)} 条记忆的重要性得分...")
                try:
                    await self.faiss_manager.update_memories_metadata(memories_to_update)
                    logger.info(f"  ✅ 重要性得分更新成功")
                except Exception as e:
                    logger.error(f"❌ 更新重要性得分失败: {e}", exc_info=True)

            if ids_to_delete:
                logger.info(f"🗑️ 删除 {len(ids_to_delete)} 条陈旧且不重要的记忆...")
                # 分批删除，避免一次删除太多
                delete_batch_size = 100
                deleted_count = 0
                for i in range(0, len(ids_to_delete), delete_batch_size):
                    batch = ids_to_delete[i:i + delete_batch_size]
                    try:
                        await self.faiss_manager.delete_memories(batch)
                        deleted_count += len(batch)
                        logger.debug(f"  删除批次: {deleted_count}/{len(ids_to_delete)}")
                    except Exception as e:
                        logger.error(f"❌ 删除批次 {i//delete_batch_size + 1} 失败: {e}", exc_info=True)

                logger.info(f"  ✅ 成功删除 {deleted_count}/{len(ids_to_delete)} 条记忆")

            # 最终统计
            logger.info(f"📊 清理任务统计:")
            logger.info(f"  处理总数: {total_processed}")
            logger.info(f"  衰减数量: {decay_count}")
            logger.info(f"  删除数量: {len(ids_to_delete)}")
            logger.info(f"  剩余记忆: {total_memories - len(ids_to_delete)}")

        except Exception as e:
            logger.error(
                f"❌ 记忆清理过程发生严重错误: {type(e).__name__}: {e}",
                exc_info=True
            )
