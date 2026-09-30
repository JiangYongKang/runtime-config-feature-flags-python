"""FastAPI 依赖：暴露 manager 与按请求构造的评估上下文。"""

from __future__ import annotations

import uuid

from fastapi import Request

from .flags.manager import FlagManager
from .flags.models import EvaluationContext


def get_manager(request: Request) -> FlagManager:
    """取出应用启动时装配好的同一个 FlagManager 单例。"""
    manager: FlagManager = request.app.state.flag_manager
    return manager


def get_context(request: Request) -> EvaluationContext:
    """从请求头构造评估上下文。

    - ``X-Caller``：调用方标识（用于名单与灰度分桶）；
    - ``X-Request-Id``：请求标识，缺省时生成一个，仅用于日志与请求级缓存。
    """
    caller = request.headers.get("x-caller", "")
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    return EvaluationContext(caller=caller, request_id=request_id)
