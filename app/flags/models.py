"""运行时功能开关系统的数据模型。

骨架阶段：仅包含类型定义，不含任何业务逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FlagStatus(str, Enum):
    """开关基线状态。"""

    ON = "on"
    OFF = "off"


class ErrorCode(str, Enum):
    """配置错误原因码：用于区分"值缺失/类型错误/取值越界"等不同失败原因。"""

    MISSING_VALUE = "missing_value"
    TYPE_ERROR = "type_error"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    UNKNOWN_FLAG = "unknown_flag"
    INVALID_SOURCE = "invalid_source"


@dataclass(frozen=True)
class FlagDefinition:
    """单个功能开关的完整定义。骨架阶段字段为空默认值。"""

    name: str = ""
    status: FlagStatus = FlagStatus.OFF
    rollout_percentage: int = 0
    allowed_callers: frozenset[str] = field(default_factory=frozenset)
    denied_callers: frozenset[str] = field(default_factory=frozenset)
    description: str = ""


@dataclass(frozen=True)
class FlagSnapshot:
    """某一时刻不可变的完整配置快照（名称 -> 开关定义）。"""

    flags: dict[str, FlagDefinition] = field(default_factory=dict)
    version: int = 0


@dataclass(frozen=True)
class EvaluationContext:
    """一次请求的评估上下文。"""

    caller: str = ""
    request_id: str = ""


@dataclass(frozen=True)
class Evaluation:
    """一次开关评估的结果及其可解释依据。"""

    flag: str = ""
    enabled: bool = False
    reason: str = ""
    bucket: int = -1
