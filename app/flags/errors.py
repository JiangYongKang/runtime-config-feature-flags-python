"""配置与评估相关的异常类型。骨架阶段仅定义类型。"""

from __future__ import annotations

from .models import ErrorCode


class ConfigError(Exception):
    """配置来源（默认值/文件/环境变量）不合法。

    用 ``code`` 区分：值缺失、类型错误、取值越界等，
    避免静默回退到默认值把问题糊过去。
    """

    def __init__(self, code: ErrorCode, message: str, *, source: str = "", path: str = "") -> None:
        self.code = code
        self.source = source
        self.path = path
        super().__init__(message)


class UnknownFlagError(Exception):
    """评估了一个未在配置中声明的开关。"""
