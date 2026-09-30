"""配置来源：默认值 / 配置文件 / 环境变量。

优先级（从低到高）：``defaults < file < env``。

设计要点：
- 每个来源各自解析为"每开关的字段级原始片段"，字段缺失不算错误
  （字段可以由更低优先级的来源提供）；
- 但**只要某字段出现**，就必须类型正确、取值合法，否则抛
  :class:`ConfigError`，错误码区分 类型错误 / 取值越界，绝不静默回退；
- 最终某开关仍缺少必填字段（status）时，由合并阶段抛 MISSING_VALUE。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from .errors import ConfigError
from .models import ErrorCode, FlagDefinition, FlagStatus

# 环境变量前缀：
#   FEATURE_FLAG_<NAME>                 = on/off          -> status
#   FEATURE_FLAG_<NAME>_PERCENTAGE      = 0..100           -> rollout_percentage
#   FEATURE_FLAG_<NAME>_ALLOW           = a,b              -> allowed_callers
#   FEATURE_FLAG_<NAME>_DENY            = a,b              -> denied_callers
#   FEATURE_FLAG_<NAME>_DESC            = 文本              -> description
ENV_PREFIX = "FEATURE_FLAG_"

# 规范化后的字段名 -> 期望类型
_INT_FIELDS = ("rollout_percentage",)
_STR_LIST_FIELDS = ("allowed_callers", "denied_callers")
_STR_FIELDS = ("status", "description")
_ALL_FIELDS = ("status", "rollout_percentage", "allowed_callers", "denied_callers", "description")


def default_definitions() -> dict[str, FlagDefinition]:
    """内置默认配置（最低优先级），在代码里显式声明，受版本管理。"""
    return {
        "new_checkout": FlagDefinition(
            name="new_checkout",
            status=FlagStatus.OFF,
            rollout_percentage=0,
            allowed_callers=frozenset(),
            denied_callers=frozenset(),
            description="新版结算链路",
        ),
        "dark_mode": FlagDefinition(
            name="dark_mode",
            status=FlagStatus.ON,
            rollout_percentage=100,
            allowed_callers=frozenset(),
            denied_callers=frozenset(),
            description="暗色模式",
        ),
    }


def definition_to_raw(defn: FlagDefinition) -> dict[str, Any]:
    """把代码里的默认定义转成与文件/环境变量同构的规范化片段，便于统一合并。"""
    return {
        "status": defn.status,
        "rollout_percentage": defn.rollout_percentage,
        "allowed_callers": sorted(defn.allowed_callers),
        "denied_callers": sorted(defn.denied_callers),
        "description": defn.description,
    }


def _validate_field(flag: str, key: str, value: Any, *, source: str) -> Any:
    """校验单个字段；类型错误与取值越界分别抛不同错误码。"""
    path = f"{flag}.{key}"
    if key == "status":
        if not isinstance(value, str):
            raise ConfigError(
                ErrorCode.TYPE_ERROR,
                f"字段 {path} 应为字符串 on/off，实际类型为 {type(value).__name__}",
                source=source,
                path=path,
            )
        try:
            return FlagStatus(value)
        except ValueError:
            raise ConfigError(
                ErrorCode.VALUE_OUT_OF_RANGE,
                f"字段 {path} 取值越界：{value!r} 不在允许集合 {{on, off}} 内",
                source=source,
                path=path,
            ) from None
    if key == "rollout_percentage":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(
                ErrorCode.TYPE_ERROR,
                f"字段 {path} 应为整数，实际类型为 {type(value).__name__}",
                source=source,
                path=path,
            )
        if not 0 <= value <= 100:
            raise ConfigError(
                ErrorCode.VALUE_OUT_OF_RANGE,
                f"字段 {path} 取值越界：{value} 不在 [0, 100] 内",
                source=source,
                path=path,
            )
        return value
    if key in ("allowed_callers", "denied_callers"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(
                ErrorCode.TYPE_ERROR,
                f"字段 {path} 应为字符串列表，实际为 {value!r}",
                source=source,
                path=path,
            )
        return value
    if key == "description":
        if not isinstance(value, str):
            raise ConfigError(
                ErrorCode.TYPE_ERROR,
                f"字段 {path} 应为字符串，实际类型为 {type(value).__name__}",
                source=source,
                path=path,
            )
        return value
    raise ConfigError(
        ErrorCode.TYPE_ERROR,
        f"开关 {flag} 含未知字段 {key!r}，允许的字段：{', '.join(_ALL_FIELDS)}",
        source=source,
        path=path,
    )


def build_definition(name: str, raw: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """把单个开关的原始片段逐字段校验，返回规范化片段（只含出现过的字段）。

    任何字段类型错误或越界都立即抛 :class:`ConfigError`，不做静默默认。
    """
    if not isinstance(raw, Mapping):
        raise ConfigError(
            ErrorCode.TYPE_ERROR,
            f"开关 {name} 的配置应为对象/映射，实际类型为 {type(raw).__name__}",
            source=source,
            path=name,
        )
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _ALL_FIELDS:
            raise ConfigError(
                ErrorCode.TYPE_ERROR,
                f"开关 {name} 含未知字段 {key!r}，允许的字段：{', '.join(_ALL_FIELDS)}",
                source=source,
                path=f"{name}.{key}",
            )
        normalized[key] = _validate_field(name, key, value, source=source)
    return normalized


def load_file_config(path: str) -> dict[str, dict[str, Any]]:
    """读取 JSON 配置文件，返回逐字段校验后的片段。

    文件不存在或 JSON 非法时抛 INVALID_SOURCE，而不是静默当成"没有该来源"。
    """
    source = f"file:{path}"
    if not os.path.isfile(path):
        raise ConfigError(
            ErrorCode.INVALID_SOURCE,
            f"配置文件不存在：{path}",
            source=source,
            path=path,
        )
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            ErrorCode.INVALID_SOURCE,
            f"配置文件不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）",
            source=source,
            path=path,
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            ErrorCode.TYPE_ERROR,
            f"配置文件顶层应为对象，实际类型为 {type(data).__name__}",
            source=source,
            path="",
        )
    result: dict[str, dict[str, Any]] = {}
    for name, raw in data.items():
        result[name] = build_definition(name, raw, source=source)
    return result


def _parse_env_int(flag: str, suffix: str, raw_value: str, *, source: str) -> int:
    path = f"{flag}.{suffix.lower()}"
    try:
        value = int(raw_value)
    except ValueError:
        raise ConfigError(
            ErrorCode.TYPE_ERROR,
            f"环境变量对应字段 {path} 应为整数，实际为 {raw_value!r}",
            source=source,
            path=path,
        ) from None
    if not 0 <= value <= 100:
        raise ConfigError(
            ErrorCode.VALUE_OUT_OF_RANGE,
            f"环境变量对应字段 {path} 取值越界：{value} 不在 [0, 100] 内",
            source=source,
            path=path,
        )
    return value


def load_env_config(environ: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """从环境变量解析原始配置片段并逐字段校验。

    开关名取环境变量名的小写形式；非法取值按 TYPE_ERROR / OUT_OF_RANGE 区分。
    """
    env = os.environ if environ is None else environ
    source = "env"
    collected: dict[str, dict[str, Any]] = {}
    for env_name, raw_value in env.items():
        if not env_name.startswith(ENV_PREFIX):
            continue
        body = env_name[len(ENV_PREFIX) :].lower()
        if not body:
            continue
        if body.endswith("_percentage"):
            flag = body[: -len("_percentage")]
            collected.setdefault(flag, {})["rollout_percentage"] = _parse_env_int(
                flag, "PERCENTAGE", raw_value, source=source
            )
        elif body.endswith("_allow"):
            flag = body[: -len("_allow")]
            collected.setdefault(flag, {})["allowed_callers"] = [
                p for p in raw_value.split(",") if p
            ]
        elif body.endswith("_deny"):
            flag = body[: -len("_deny")]
            collected.setdefault(flag, {})["denied_callers"] = [
                p for p in raw_value.split(",") if p
            ]
        elif body.endswith("_desc"):
            flag = body[: -len("_desc")]
            collected.setdefault(flag, {})["description"] = raw_value
        else:
            flag = body
            if raw_value not in ("on", "off"):
                raise ConfigError(
                    ErrorCode.VALUE_OUT_OF_RANGE,
                    f"环境变量 {env_name} 取值越界：{raw_value!r} 不在允许集合 {{on, off}} 内",
                    source=source,
                    path=f"{flag}.status",
                )
            collected.setdefault(flag, {})["status"] = FlagStatus(raw_value)
    # 列表字段统一走一遍类型校验（字符串列表天然合法，保持与文件来源同一套入口）
    return {
        name: build_definition(name, raw, source=source) for name, raw in collected.items()
    }
