"""配置合并：把多个来源的原始片段按优先级拼成最终配置。

优先级规则：``defaults < file < env``（以及运行时整批更新）。
合并粒度是**字段级**：高优先级来源只需给出它关心的字段，未给出的字段
继续沿用低优先级来源。列表字段（allowed/denied）为整体替换，不做拼接，
保证规则可解释、无歧义。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import ConfigError
from .models import ErrorCode, FlagDefinition, FlagSnapshot, FlagStatus

_LIST_FIELDS = ("allowed_callers", "denied_callers")
_SCALAR_FIELDS = ("status", "rollout_percentage", "description")


def merge_raw(*layers: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """按传入顺序（低优先级 -> 高优先级）做字段级深合并。

    标量字段高优先级覆盖低优先级；列表字段整体替换；不做列表拼接。
    各来源在解析阶段已完成类型/取值校验，这里只负责合并。
    """
    merged: dict[str, dict[str, Any]] = {}
    for layer in layers:
        for flag, raw in layer.items():
            target = merged.setdefault(flag, {})
            for key, value in raw.items():
                target[key] = value
    return merged


def build_snapshot(
    merged_raw: Mapping[str, Mapping[str, Any]],
    *,
    version: int,
    source: str = "merged",
) -> FlagSnapshot:
    """校验合并结果并构造不可变 :class:`FlagSnapshot`。

    合并后仍缺少 ``status`` 的开关属于"值缺失"，抛 MISSING_VALUE
    （各字段的类型/越界校验已在来源解析阶段完成）。
    """
    flags: dict[str, FlagDefinition] = {}
    for name, raw in merged_raw.items():
        if "status" not in raw:
            raise ConfigError(
                ErrorCode.MISSING_VALUE,
                f"开关 {name} 缺少必填字段 status，且没有任何更高优先级来源提供它",
                source=source,
                path=f"{name}.status",
            )
        status = raw["status"]
        if not isinstance(status, FlagStatus):
            raise ConfigError(
                ErrorCode.TYPE_ERROR,
                f"开关 {name}.status 内部状态异常：{status!r}",
                source=source,
                path=f"{name}.status",
            )
        flags[name] = FlagDefinition(
            name=name,
            status=status,
            rollout_percentage=int(raw.get("rollout_percentage", 0)),
            allowed_callers=frozenset(raw.get("allowed_callers", ())),
            denied_callers=frozenset(raw.get("denied_callers", ())),
            description=str(raw.get("description", "")),
        )
    return FlagSnapshot(flags=flags, version=version)
