# -*- coding: utf-8 -*-
"""
models.py - 插件核心数据模型定义
"""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """记忆事件类型枚举"""

    FACT = "fact"
    PREFERENCE = "preference"
    GOAL = "goal"
    OPINION = "opinion"
    RELATIONSHIP = "relationship"
    OTHER = "other"


class Entity(BaseModel):
    """事件涉及的实体信息"""

    name: str = Field(..., description="实体名称")
    type: str = Field(..., description="实体类型")


class MemoryEvent(BaseModel):
    """结构化记忆事件"""

    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="事件创建的 UTC 时间",
    )
    id: Optional[int] = Field(
        None, description="记忆的唯一整数 ID，由存储后端生成"
    )
    temp_id: str = Field(..., description="由 LLM 生成的临时唯一 ID，用于评分匹配")
    memory_content: str = Field(..., description="对事件的客观描述")
    event_type: EventType = Field(default=EventType.OTHER, description="事件类型")
    entities: List[Entity] = Field(
        default_factory=list, description="事件中涉及的关键实体"
    )
    importance_score: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="记忆的重要性评分 (0.0-1.0)"
    )
    related_event_ids: List[int] = Field(
        default_factory=list, description="与该事件相关的其他事件 ID"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="用于存储附加信息的元数据"
    )


class MemoryEventList(BaseModel):
    """记忆事件列表"""

    events: List[MemoryEvent]


class _LLMExtractionEvent(BaseModel):
    """用于生成提示 Schema：事件提取阶段"""

    temp_id: str = Field(..., description="临时唯一 ID")
    memory_content: str = Field(..., description="记忆内容")
    event_type: EventType = Field(default=EventType.OTHER, description="事件类型")
    entities: List[Entity] = Field(default_factory=list, description="涉及实体列表")
    related_event_ids: List[int] = Field(
        default_factory=list, description="相关事件 ID 列表"
    )
    metadata: Dict[str, Any] = Field(default_factory=dict, description="附加元数据")


class _LLMExtractionEventList(BaseModel):
    """用于生成提示 Schema：事件提取列表"""

    events: List[_LLMExtractionEvent]


class _LLMScoreEvaluation(BaseModel):
    """用于生成提示 Schema：事件评分结果"""

    scores: Dict[str, float] = Field(
        ...,
        description="key 为临时 ID，value 为 0.0-1.0 的重要性得分",
    )

