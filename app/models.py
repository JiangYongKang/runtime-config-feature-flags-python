"""功能开关相关的数据模型。

- :class:`FlagRule`：单个开关的规则（默认值、调用方名单、灰度比例）。
- :class:`FeatureFlagsConfig`：一份完整、不可变的开关配置。
- :class:`Evaluation`：一次开关判定的结论与可解释的依据链。

校验错误使用 :class:`~app.errors.ConfigValidationError`，并通过 ``reason``
区分失败原因（缺失 / 类型错误 / 越界 / 未知字段 / 规则冲突），
不会用默认值把问题悄悄盖过去。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.errors import ConfigValidationError, ConfigValidationErrors

_FLAG_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_KNOWN_FIELDS = ("enabled", "allowed_callers", "denied_callers", "rollout_percentage")


def _require_mapping(raw: object, path: str) -> dict:
    if not isinstance(raw, dict):
        raise ConfigValidationErrors([
            ConfigValidationError(
                f"期望是对象(dict)，实际是 {type(raw).__name__}",
                path=path,
                reason="invalid_type",
            )
        ])
    return raw


def _parse_bool(raw: object, path: str) -> bool:
    # 注意：bool 是 int 的子类，必须先判 bool，且不接受 0/1 这种“隐式布尔”。
    if not isinstance(raw, bool):
        raise ConfigValidationError(
            f"期望是布尔值(true/false)，实际是 {type(raw).__name__}: {raw!r}",
            path=path,
            reason="invalid_type",
        )
    return raw


def _parse_percentage(raw: object, path: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigValidationError(
            f"期望是 0-100 的整数，实际是 {type(raw).__name__}: {raw!r}",
            path=path,
            reason="invalid_type",
        )
    if not 0 <= raw <= 100:
        raise ConfigValidationError(
            f"灰度比例必须在 0-100 之间，实际是 {raw}",
            path=path,
            reason="out_of_range",
        )
    return raw


def _parse_caller_list(raw: object, path: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ConfigValidationError(
            f"期望是字符串数组(list)，实际是 {type(raw).__name__}: {raw!r}",
            path=path,
            reason="invalid_type",
        )
    result: list[str] = []
    errors: list[ConfigValidationError] = []
    for index, item in enumerate(raw):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str) or not item.strip():
            errors.append(
                ConfigValidationError(
                    f"调用方标识必须是非空字符串，实际是 {type(item).__name__}: {item!r}",
                    path=item_path,
                    reason="invalid_element",
                )
            )
            continue
        result.append(item.strip())
    if errors:
        raise ConfigValidationErrors(errors)
    return tuple(result)


@dataclass(frozen=True)
class FlagRule:
    """单个功能开关的判定规则。

    属性:
        enabled: 开关的总闸。关闭时只有 denied 名单相关的短路逻辑不生效，
            整体评估见 :mod:`app.evaluator` 的确定性优先级。
        allowed_callers: 强制开启的调用方白名单。
        denied_callers: 强制关闭的调用方黑名单（优先级高于白名单）。
        rollout_percentage: 灰度比例 0-100，按调用方稳定哈希落点。
    """

    enabled: bool = False
    allowed_callers: tuple[str, ...] = ()
    denied_callers: tuple[str, ...] = ()
    rollout_percentage: int = 0

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "allowed_callers": list(self.allowed_callers),
            "denied_callers": list(self.denied_callers),
            "rollout_percentage": self.rollout_percentage,
        }

    @classmethod
    def from_dict(cls, raw: object, *, path: str, partial: bool = False) -> "FlagRule":
        """从普通 dict 严格构造规则。

        ``partial=True`` 时允许字段缺失（用于单个配置层，缺的字段留给
        更高优先级层或默认值）；``False`` 时缺字段报 ``missing_value``。
        校验错误一次性收集，通过 :class:`ConfigValidationErrors` 批量抛出。
        """
        data = _require_mapping(raw, path)
        errors: list[ConfigValidationError] = []

        for key in data:
            if key not in _KNOWN_FIELDS:
                errors.append(
                    ConfigValidationError(
                        f"未知字段 {key!r}，允许的字段: {', '.join(_KNOWN_FIELDS)}",
                        path=f"{path}.{key}",
                        reason="unknown_field",
                    )
                )

        def take(name: str):
            if name not in data:
                if not partial:
                    errors.append(
                        ConfigValidationError(
                            f"缺少必填字段 {name!r}",
                            path=f"{path}.{name}",
                            reason="missing_value",
                        )
                    )
                return None, False
            return data[name], True

        enabled = False
        raw_enabled, present = take("enabled")
        if present:
            try:
                enabled = _parse_bool(raw_enabled, f"{path}.enabled")
            except ConfigValidationError as exc:
                errors.append(exc)

        percentage = 0
        raw_pct, present = take("rollout_percentage")
        if present:
            try:
                percentage = _parse_percentage(raw_pct, f"{path}.rollout_percentage")
            except ConfigValidationError as exc:
                errors.append(exc)

        allowed: tuple[str, ...] = ()
        raw_allowed, present = take("allowed_callers")
        if present:
            try:
                allowed = _parse_caller_list(raw_allowed, f"{path}.allowed_callers")
            except ConfigValidationErrors as exc:
                errors.extend(exc.errors)

        denied: tuple[str, ...] = ()
        raw_denied, present = take("denied_callers")
        if present:
            try:
                denied = _parse_caller_list(raw_denied, f"{path}.denied_callers")
            except ConfigValidationErrors as exc:
                errors.extend(exc.errors)

        overlap = set(allowed) & set(denied)
        if overlap:
                errors.append(
                    ConfigValidationError(
                        "同一调用方不能同时出现在 allowed_callers 和 denied_callers 中: "
                        + ", ".join(sorted(overlap)),
                        path=path,
                        reason="rules_conflict",
                    )
                )

        if errors:
            raise ConfigValidationErrors(errors)

        if partial:
            # 部分层：用默认值补齐缺省字段，合并阶段再做字段级覆盖。
            return cls(
                enabled=enabled if "enabled" in data else cls.enabled,
                allowed_callers=allowed if "allowed_callers" in data else (),
                denied_callers=denied if "denied_callers" in data else (),
                rollout_percentage=percentage if "rollout_percentage" in data else 0,
            )
        return cls(
            enabled=enabled,
            allowed_callers=allowed,
            denied_callers=denied,
            rollout_percentage=percentage,
        )


@dataclass(frozen=True)
class FeatureFlagsConfig:
    """某一时刻完整的功能开关集合（不可变快照）。"""

    flags: dict[str, FlagRule] = field(default_factory=dict)

    def get(self, name: str) -> FlagRule | None:
        return self.flags.get(name)

    def to_dict(self) -> dict:
        return {name: rule.to_dict() for name, rule in sorted(self.flags.items())}

    @classmethod
    def from_dict(
        cls, raw: object, *, path: str = "flags", partial: bool = False
    ) -> "FeatureFlagsConfig":
        """从普通 dict 严格构造整份配置。

        ``partial=True`` 用于解析单个配置层：允许只写部分开关、每个开关只写
        部分字段。无论是否 partial，类型/越界/未知字段/名单冲突都会立即报错。
        """
        data = _require_mapping(raw, path)
        errors: list[ConfigValidationError] = []
        rules: dict[str, FlagRule] = {}

        for name, raw_rule in data.items():
            flag_path = f"{path}.{name}"
            if not isinstance(name, str) or not _FLAG_NAME_RE.match(name):
                errors.append(
                    ConfigValidationError(
                        "开关名必须匹配 ^[a-z][a-z0-9_]*$",
                        path=flag_path,
                        reason="invalid_flag_name",
                    )
                )
                continue
            try:
                rules[name] = FlagRule.from_dict(
                    raw_rule, path=flag_path, partial=partial
                )
            except ConfigValidationErrors as exc:
                errors.extend(exc.errors)

        if errors:
            raise ConfigValidationErrors(errors)
        return cls(flags=rules)


@dataclass(frozen=True)
class Evaluation:
    """一次开关评估的结果。

    ``matched`` 是最终命中的规则，``basis`` 按检查顺序记录每一步判定依据，
    便于接口把“为什么是这个结果”解释清楚。
    """

    flag: str
    on: bool
    matched: str
    basis: tuple[str, ...] = ()

    def to_dict(self, caller_id: str | None, request_id: str | None) -> dict:
        return {
            "flag": self.flag,
            "on": self.on,
            "matched": self.matched,
            "caller_id": caller_id,
            "request_id": request_id,
            "basis": list(self.basis),
        }
