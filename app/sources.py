"""配置来源与合并。

三层来源，优先级从低到高：

1. ``defaults``：代码内置默认值（:data:`DEFAULT_CONFIG`），永远生效；
2. ``file``：JSON 配置文件（路径由参数 / ``FF_CONFIG_FILE`` 指定；
   文件不存在视为该层整体缺省；文件存在但内容非法则报错，不悄悄忽略）；
3. ``env``：环境变量，命名为 ``FF_<开关名>_<字段>``（开关名大写、
   下划线分隔），例如 ``FF_NEW_CHECKOUT_ENABLED=true``、
   ``FF_NEW_CHECKOUT_ROLLOUT_PERCENTAGE=25``、
   ``FF_NEW_CHECKOUT_ALLOWED_CALLERS=alice,bob``。

合并规则是**字段级**的：高层只覆盖它显式给出的字段，没给出的字段
沿用低层。合并产出的整份配置必须通过完整校验（含跨层名单冲突检查）。
"""

from __future__ import annotations

import json
import os

from app.errors import (
    ConfigSourceError,
    ConfigValidationError,
    ConfigValidationErrors,
)
from app.models import FeatureFlagsConfig

#: 默认配置层（最低优先级）。文件 / 环境变量没有给出的字段以此为准。
DEFAULT_CONFIG: dict = {
    "flags": {
        "new_checkout": {
            "enabled": False,
            "allowed_callers": [],
            "denied_callers": [],
            "rollout_percentage": 0,
        },
        "beta_search": {
            "enabled": False,
            "allowed_callers": [],
            "denied_callers": [],
            "rollout_percentage": 0,
        },
    }
}

#: 各层的优先级数字，越大越优先。
LAYER_PRIORITY = {"defaults": 10, "file": 20, "env": 30}

_ENV_FIELDS = ("enabled", "allowed_callers", "denied_callers", "rollout_percentage")
# 环境变量后缀按长度降序匹配，避免 ``ALLOWED`` 前缀类歧义。
_ENV_FIELD_SUFFIXES = sorted(
    ("ENABLED", "ALLOWED_CALLERS", "DENIED_CALLERS", "ROLLOUT_PERCENTAGE")
)


def load_file(path: str) -> dict:
    """读取 JSON 配置文件。

    文件不存在 -> 返回 ``{}``（该层缺省，属正常情况）；
    文件不可读 / 不是合法 JSON -> :class:`ConfigSourceError`；
    JSON 顶层结构错误 -> :class:`ConfigValidationErrors`。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigSourceError(
            f"配置文件无法读取: {exc}", path=path
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigSourceError(
            f"配置文件不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}",
            path=path,
        ) from exc

    if not isinstance(data, dict) or "flags" not in data:
        raise ConfigValidationErrors([
            ConfigValidationError(
                "顶层必须是包含 'flags' 字段的对象",
                path=path,
                reason="invalid_structure",
            )
        ])
    return data


def _parse_env_bool(raw: str, path: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    raise ConfigValidationError(
        f"环境变量布尔值只接受 true/false，实际是 {raw!r}",
        path=path,
        reason="invalid_type",
    )


def _parse_env_int(raw: str, path: str) -> int:
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        raise ConfigValidationError(
            f"环境变量整数值非法，实际是 {raw!r}",
            path=path,
            reason="invalid_type",
        )


def _parse_env_callers(raw: str, path: str) -> list[str]:
    items = [part.strip() for part in raw.split(",")]
    for index, item in enumerate(items):
        if not item:
            raise ConfigValidationError(
                "调用方列表中存在空元素（多个逗号之间或首尾有空段）",
                path=f"{path}[{index}]",
                reason="invalid_element",
            )
    return items


def load_env(environ: dict[str, str] | None = None) -> dict:
    """从环境变量解析出配置层。

    仅识别 ``FF_`` 前缀的变量；任何无法解析为 ``<开关名>_<字段>`` 的
    ``FF_`` 变量都会报 ``unknown_env_variable``，避免拼写错误被静默忽略。
    """
    env = os.environ if environ is None else environ
    flags: dict[str, dict] = {}
    errors: list[ConfigValidationError] = []

    for key, value in env.items():
        if not key.startswith("FF_") or key == "FF_CONFIG_FILE":
            continue
        if value == "":
            # 显式置空视为给了非法值而不是“没配置”，防止 export FF_X= 误关功能。
            errors.append(
                ConfigValidationError(
                    "环境变量值为空；要取消设置请 unset 而不是赋空字符串",
                    path=key,
                    reason="invalid_type",
                )
            )
            continue

        remainder = key[3:]
        matched_suffix = next(
            (suffix for suffix in _ENV_FIELD_SUFFIXES if remainder.endswith("_" + suffix)),
            None,
        )
        if matched_suffix is None:
            errors.append(
                ConfigValidationError(
                    "无法识别的开关环境变量，允许的字段后缀: "
                    + ", ".join(_ENV_FIELD_SUFFIXES),
                    path=key,
                    reason="unknown_env_variable",
                )
            )
            continue

        flag_name = remainder[: -(len(matched_suffix) + 1)].lower()
        field = matched_suffix.lower()
        try:
            if field == "enabled":
                parsed = _parse_env_bool(value, key)
            elif field == "rollout_percentage":
                parsed = _parse_env_int(value, key)
            else:
                parsed = _parse_env_callers(value, key)
        except ConfigValidationError as exc:
            errors.append(exc)
            continue

        flags.setdefault(flag_name, {})[field] = parsed

    if errors:
        raise ConfigValidationErrors(errors)
    return {"flags": flags}


def merge_layers(*layers: dict) -> dict:
    """按入参顺序（低优先级在前）做字段级深度合并（仅合并 flags 这一层）。"""
    merged_flags: dict[str, dict] = {}
    for layer in layers:
        for name, rule in layer.get("flags", {}).items():
            target = merged_flags.setdefault(name, {})
            for field_name, value in rule.items():
                target[field_name] = value
    return {"flags": merged_flags}


def build_provenance(*named_layers: tuple[str, dict]) -> dict[str, str]:
    """记录合并后每个 ``开关.字段`` 的值来自哪一层（取最高优先级来源）。"""
    provenance: dict[str, str] = {}
    for layer_name, layer in named_layers:
        for name, rule in layer.get("flags", {}).items():
            for field_name in rule:
                provenance[f"{name}.{field_name}"] = layer_name
    return provenance


def build_config(
    *,
    file_path: str | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[FeatureFlagsConfig, dict[str, str]]:
    """合并默认值 / 文件 / 环境变量并完整校验。

    返回 ``(配置快照, 来源标注)``。任何一层存在类型错误、越界、未知字段、
    名单冲突等问题，都会在这里抛出 :class:`ConfigValidationErrors`，
    调用方必须显式处理（例如启动失败、更新被拒），而不是退回默认值。
    """
    # 各层先独立解析（partial 允许只写字段），错误路径带来源前缀。
    defaults = FeatureFlagsConfig.from_dict(
        DEFAULT_CONFIG["flags"], path="defaults.flags", partial=True
    ).to_dict()
    defaults_layer = {"flags": defaults}

    named_layers: list[tuple[str, dict]] = [("defaults", defaults_layer)]

    if file_path:
        file_raw = load_file(file_path)
        if file_raw:
            FeatureFlagsConfig.from_dict(
                file_raw["flags"], path="file.flags", partial=True
            )
            named_layers.append(("file", file_raw))

    env_raw = load_env(environ)
    if env_raw["flags"]:
        FeatureFlagsConfig.from_dict(
            env_raw["flags"], path="env.flags", partial=True
        )
        named_layers.append(("env", env_raw))

    merged_raw = merge_layers(*(layer for _, layer in named_layers))
    # 合并后做一次完整校验：字段齐不齐、跨层名单是否冲突都在这一步兜住。
    config = FeatureFlagsConfig.from_dict(
        merged_raw["flags"], path="flags", partial=False
    )
    provenance = build_provenance(*named_layers)
    return config, provenance
