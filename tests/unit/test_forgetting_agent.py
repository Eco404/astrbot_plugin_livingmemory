# -*- coding: utf-8 -*-
"""
test_forgetting_agent.py - 遗忘代理测试
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock

from core.engines.forgetting_agent import ForgettingAgent
from tests.conftest import TEST_CONFIG


class TestForgettingAgent:
    """遗忘代理测试类"""

    def setup_method(self):
        """测试前设置"""
        self.mock_context = Mock()
        self.mock_faiss_manager = Mock()

        # 设置配置
        self.config = TEST_CONFIG.get("forgetting_agent", {})

        self.agent = ForgettingAgent(
            self.mock_context,
            self.config,
            self.mock_faiss_manager
        )

    @pytest.mark.asyncio
    async def test_trigger_manual_run_success(self):
        """测试手动触发遗忘任务（成功）"""
        # 模拟 _prune_memories 方法
        self.agent._prune_memories = AsyncMock()

        result = await self.agent.trigger_manual_run()

        assert result["success"] is True
        assert result["message"] == "遗忘代理任务执行完毕"
        self.agent._prune_memories.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_manual_run_concurrent_protection(self):
        """测试手动触发遗忘任务的并发保护"""
        # 模拟一个长时间运行的 _prune_memories
        async def slow_prune():
            await asyncio.sleep(0.1)

        self.agent._prune_memories = slow_prune

        # 启动第一个任务
        task1 = asyncio.create_task(self.agent.trigger_manual_run())

        # 等待一小段时间确保第一个任务开始执行
        await asyncio.sleep(0.01)

        # 尝试启动第二个任务（应该被拒绝）
        result2 = await self.agent.trigger_manual_run()

        assert result2["success"] is False
        assert "正在运行中" in result2["message"]

        # 等待第一个任务完成
        result1 = await task1
        assert result1["success"] is True

    @pytest.mark.asyncio
    async def test_trigger_manual_run_exception_handling(self):
        """测试手动触发遗忘任务的异常处理"""
        # 模拟 _prune_memories 抛出异常
        self.agent._prune_memories = AsyncMock(side_effect=Exception("测试异常"))

        result = await self.agent.trigger_manual_run()

        assert result["success"] is False
        assert "遗忘任务执行失败" in result["message"]
        assert "测试异常" in result["message"]

    @pytest.mark.asyncio
    async def test_trigger_manual_run_multiple_sequential(self):
        """测试顺序执行多个手动任务"""
        self.agent._prune_memories = AsyncMock()

        # 执行第一个任务
        result1 = await self.agent.trigger_manual_run()
        assert result1["success"] is True

        # 执行第二个任务（第一个已完成，应该成功）
        result2 = await self.agent.trigger_manual_run()
        assert result2["success"] is True

        # 验证 _prune_memories 被调用了两次
        assert self.agent._prune_memories.call_count == 2
