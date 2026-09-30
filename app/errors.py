"""配置加载 / 更新过程中可能出现的错误类型。

设计原则：值缺失、类型不对、取值越界不能被默认值悄悄掩盖，
调用方需要能通过具体的异常类型与 ``reason`` 区分失败原因。
"""

from __future__ import annotations


class ConfigError(Exception):
    """所有配置相关错误的基类。"""

    reason: str = "config_error"

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        if path is not None:
            message = f"{path}: {message}"
        super().__init__(message)


class ConfigValidationError(ConfigError):
    """单条配置项校验失败（聚合到 :class:`ConfigValidationErrors` 中批量上报）。"""

    def __init__(self, message: str, *, path: str | None = None, reason: str) -> None:
        self.reason = reason
        super().__init__(message, path=path)


class ConfigValidationErrors(ConfigError):
    """一次加载 / 更新中收集到的全部校验错误（整批拒绝的依据）。"""

    reason = "validation_errors"

    def __init__(self, errors: list[ConfigValidationError]) -> None:
        self.errors = errors
        detail = "; ".join(str(e) for e in errors)
        super().__init__(f"配置校验失败，共 {len(errors)} 项：{detail}")


class UnknownFlagError(ConfigError):
    """请求评估了一个配置中不存在的开关名。"""

    reason = "unknown_flag"


class ConfigSourceError(ConfigError):
    """配置来源本身不可用（例如配置文件不存在 / 不是合法 JSON）。"""

    reason = "source_error"
