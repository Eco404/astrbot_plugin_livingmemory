# -*- coding: utf-8 -*-
"""
test_fusion_handler.py - 融合处理器测试
"""

import pytest
from unittest.mock import Mock

from core.handlers.fusion_handler import FusionHandler
from tests.conftest import TEST_CONFIG


class TestFusionHandler:
    """融合处理器测试类"""

    def setup_method(self):
        """测试前设置"""
        self.mock_context = Mock()
        self.config = TEST_CONFIG.copy()

        # 确保 fusion 配置存在
        if "fusion" not in self.config:
            self.config["fusion"] = {
                "strategy": "hybrid_rrf",
                "dense_weight": 0.7,
                "sparse_weight": 0.3
            }

        self.handler = FusionHandler(
            self.mock_context,
            self.config
        )

    @pytest.mark.asyncio
    async def test_set_fusion_param_dense_weight_valid(self):
        """测试设置 dense_weight（有效值）"""
        result = await self.handler.set_fusion_param("weighted", "dense_weight", "0.6")

        assert result["success"] is True
        assert self.config["fusion"]["dense_weight"] == 0.6

    @pytest.mark.asyncio
    async def test_set_fusion_param_weight_sum_exceeds_limit(self):
        """测试权重总和超过 1.0"""
        # 设置初始 sparse_weight
        self.config["fusion"]["sparse_weight"] = 0.4

        # 尝试设置 dense_weight = 0.7，总和 = 1.1
        result = await self.handler.set_fusion_param("weighted", "dense_weight", "0.7")

        assert result["success"] is False
        assert "权重总和不能超过 1.0" in result["message"]
        assert "1.1" in result["message"]

    @pytest.mark.asyncio
    async def test_set_fusion_param_weight_sum_exactly_one(self):
        """测试权重总和恰好为 1.0"""
        # 设置初始 sparse_weight
        self.config["fusion"]["sparse_weight"] = 0.3

        # 设置 dense_weight = 0.7，总和 = 1.0
        result = await self.handler.set_fusion_param("weighted", "dense_weight", "0.7")

        assert result["success"] is True
        assert self.config["fusion"]["dense_weight"] == 0.7

    @pytest.mark.asyncio
    async def test_set_fusion_param_weight_for_non_weight_strategy(self):
        """测试对不使用权重的策略设置权重参数"""
        # rrf 策略不使用权重参数
        result = await self.handler.set_fusion_param("rrf", "dense_weight", "0.5")

        assert result["success"] is False
        assert "参数 dense_weight 不适用于策略 rrf" in result["message"]

    @pytest.mark.asyncio
    async def test_set_fusion_param_sparse_weight_valid(self):
        """测试设置 sparse_weight（有效值）"""
        # 设置初始 dense_weight
        self.config["fusion"]["dense_weight"] = 0.6

        result = await self.handler.set_fusion_param("weighted", "sparse_weight", "0.4")

        assert result["success"] is True
        assert self.config["fusion"]["sparse_weight"] == 0.4

    @pytest.mark.asyncio
    async def test_set_fusion_param_multiple_weight_strategies(self):
        """测试多种需要权重的策略"""
        strategies = ["weighted", "convex", "rank_fusion", "score_fusion", "cascade", "adaptive"]

        for strategy in strategies:
            # 重置配置
            self.config["fusion"]["sparse_weight"] = 0.3

            # 测试设置 dense_weight
            result = await self.handler.set_fusion_param(strategy, "dense_weight", "0.7")

            assert result["success"] is True, f"策略 {strategy} 应该支持权重参数"
            assert self.config["fusion"]["dense_weight"] == 0.7

    @pytest.mark.asyncio
    async def test_set_fusion_param_invalid_param_name(self):
        """测试设置无效的参数名"""
        result = await self.handler.set_fusion_param("weighted", "invalid_param", "0.5")

        assert result["success"] is False
        assert "无效的参数名: invalid_param" in result["message"]

    @pytest.mark.asyncio
    async def test_set_fusion_param_param_out_of_range(self):
        """测试参数值超出范围"""
        # 测试 dense_weight > 1.0
        result = await self.handler.set_fusion_param("weighted", "dense_weight", "1.5")

        assert result["success"] is False
        assert "必须在 0.0-1.0 范围内" in result["message"]

        # 测试 dense_weight < 0.0
        result = await self.handler.set_fusion_param("weighted", "dense_weight", "-0.1")

        assert result["success"] is False
        assert "必须在 0.0-1.0 范围内" in result["message"]

    @pytest.mark.asyncio
    async def test_set_fusion_param_invalid_value_type(self):
        """测试设置无效的值类型"""
        result = await self.handler.set_fusion_param("weighted", "dense_weight", "not_a_number")

        assert result["success"] is False
        assert "参数 dense_weight 的值类型无效" in result["message"]
