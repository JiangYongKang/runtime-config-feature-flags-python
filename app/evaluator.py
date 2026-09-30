"""按请求上下文评估功能开关。

确定性优先级（结论唯一、不随机、不依赖时钟）：

1. 调用方在 ``denied_callers`` 黑名单 -> **关闭**（黑名单优先于一切）；
2. 调用方在 ``allowed_callers`` 白名单 -> **开启**；
3. ``enabled is False`` -> **关闭**（总闸没开，灰度也不生效）；
4. ``rollout_percentage >= 100`` -> **开启**（全量放开）；
5. ``rollout_percentage == 0`` -> **关闭**；
6. 灰度：对稳定键（``caller_id``，缺失时退化为 ``request_id``）做
   ``int(sha256(flag + ':' + key).hexdigest()[:12], 16) % 100``，
   桶号 ``< rollout_percentage`` 则开启。同一调用方在同一开关上落点恒定；
   两个稳定键都缺失时，无法做稳定灰度，结论为关闭并在依据中说明。

每一步检查都会写进 ``basis``，因此响应里能完整解释判定来源。
"""

from __future__ import annotations

import hashlib
import threading

from app.errors import UnknownFlagError
from app.models import Evaluation, FeatureFlagsConfig

#: 取哈希摘要前 12 个十六进制字符（48 bit）再取模，分布足够均匀且确定性强。
_HASH_PREFIX_LEN = 12
_BUCKET_COUNT = 100


def _bucket(flag: str, key: str) -> int:
    """把（开关名, 稳定键）哈希到 [0, 99] 的桶，纯函数、跨进程稳定。"""
    digest = hashlib.sha256(f"{flag}:{key}".encode("utf-8")).hexdigest()
    return int(digest[:_HASH_PREFIX_LEN], 16) % _BUCKET_COUNT


def evaluate(
    config: FeatureFlagsConfig,
    flag: str,
    *,
    caller_id: str | None,
    request_id: str | None,
) -> Evaluation:
    """无状态地评估单个开关（纯函数，便于单测）。

    开关名不存在时抛 :class:`UnknownFlagError`，由接口层映射为 404。
    """
    rule = config.get(flag)
    if rule is None:
        raise UnknownFlagError(f"未知开关 {flag!r}", path=f"flags.{flag}")

    basis: list[str] = []
    basis.append(
        f"规则: enabled={rule.enabled}, rollout_percentage={rule.rollout_percentage}, "
        f"allowed={list(rule.allowed_callers)}, denied={list(rule.denied_callers)}"
    )

    caller = caller_id.strip() if isinstance(caller_id, str) and caller_id.strip() else None
    request = (
        request_id.strip()
        if isinstance(request_id, str) and request_id.strip()
        else None
    )
    basis.append(
        f"上下文: caller_id={caller!r}, request_id={request!r}"
    )

    if caller is not None and caller in rule.denied_callers:
        basis.append("命中 denied_callers 黑名单，黑名单优先级最高")
        return Evaluation(flag=flag, on=False, matched="denied_caller",
                          basis=tuple(basis))

    if caller is not None and caller in rule.allowed_callers:
        basis.append("命中 allowed_callers 白名单，直接开启")
        return Evaluation(flag=flag, on=True, matched="allowed_caller",
                          basis=tuple(basis))

    if not rule.enabled:
        basis.append("未命中名单且 enabled=false，总闸关闭")
        return Evaluation(flag=flag, on=False, matched="disabled",
                          basis=tuple(basis))

    if rule.rollout_percentage >= 100:
        basis.append("enabled=true 且 rollout_percentage=100，全量开启")
        return Evaluation(flag=flag, on=True, matched="full_rollout",
                          basis=tuple(basis))

    if rule.rollout_percentage <= 0:
        basis.append("enabled=true 但 rollout_percentage=0，灰度未放开")
        return Evaluation(flag=flag, on=False, matched="zero_rollout",
                          basis=tuple(basis))

    stable_key = caller if caller is not None else request
    key_kind = "caller_id" if caller is not None else (
        "request_id" if request is not None else None
    )
    if stable_key is None:
        basis.append(
            "进入灰度但 caller_id/request_id 均缺失，无法做稳定灰度，按关闭处理"
        )
        return Evaluation(flag=flag, on=False, matched="no_stable_key",
                          basis=tuple(basis))

    bucket = _bucket(flag, stable_key)
    on = bucket < rule.rollout_percentage
    basis.append(
        f"灰度判定: 稳定键来源={key_kind} 值={stable_key!r} -> 桶号 {bucket}; "
        f"{bucket} < {rule.rollout_percentage} 为 {str(on).lower()}"
    )
    return Evaluation(
        flag=flag,
        on=on,
        matched="rollout_in" if on else "rollout_out",
        basis=tuple(basis),
    )


class RequestFlagCache:
    """请求级评估缓存。

    绑定一个固定的配置快照，以开关名为键缓存评估结果，保证同一请求内
    重复评估同一个开关结论恒定（即便期间配置发生了更新）。
    """

    def __init__(self, config: FeatureFlagsConfig) -> None:
        self._config = config
        self._cache: dict[str, Evaluation] = {}
        self._lock = threading.Lock()

    @property
    def config(self) -> FeatureFlagsConfig:
        return self._config

    def check(
        self,
        flag: str,
        *,
        caller_id: str | None,
        request_id: str | None,
    ) -> Evaluation:
        with self._lock:
            cached = self._cache.get(flag)
            if cached is not None:
                return cached
            result = evaluate(
                self._config,
                flag,
                caller_id=caller_id,
                request_id=request_id,
            )
            self._cache[flag] = result
            return result
