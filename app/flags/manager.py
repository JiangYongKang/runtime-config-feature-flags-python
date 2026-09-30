"""开关管理器：串联来源加载、严格校验、快照存储、评估与请求级缓存。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextvars import ContextVar, Token
from typing import Any

from . import evaluator as evaluator_mod
from .errors import ConfigError
from .merge import build_snapshot, merge_raw
from .models import ErrorCode, Evaluation, EvaluationContext, FlagSnapshot
from .sources import (
    build_definition,
    default_definitions,
    definition_to_raw,
    load_env_config,
    load_file_config,
)
from .store import FlagStore

logger = logging.getLogger("feature_flags")

# 请求级评估缓存：同一请求内多次评估同一开关，直接复用首次结论，
# 即使请求处理途中配置被更新，本请求的判定也保持一致。
_request_cache: ContextVar[dict[tuple[str, str], Evaluation] | None] = ContextVar(
    "feature_flag_request_cache", default=None
)


class RequestScope:
    """上下文管理器：进入时建立空缓存，退出时恢复上层缓存。"""

    def __init__(self) -> None:
        self._token: Token | None = None

    def __enter__(self) -> "RequestScope":
        self._token = _request_cache.set({})
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _request_cache.reset(self._token)


def new_request_scope() -> RequestScope:
    """开启一个新的请求级评估缓存上下文。"""
    return RequestScope()


class FlagManager:
    def __init__(
        self,
        config_path: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config_path = config_path
        self._environ = environ
        self._store = FlagStore(self._load_initial())

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def _load_initial(self) -> FlagSnapshot:
        """按 defaults < file < env 拼出首份快照。

        任一来源非法（类型错/越界/缺失）都直接向上抛 ConfigError，
        绝不用默认值把问题糊过去。
        """
        defaults_raw = {
            name: definition_to_raw(defn)
            for name, defn in default_definitions().items()
        }
        layers: list[Mapping[str, Mapping[str, Any]]] = [defaults_raw]
        if self._config_path:
            layers.append(load_file_config(self._config_path))
        layers.append(load_env_config(self._environ))
        merged = merge_raw(*layers)
        snapshot = build_snapshot(merged, version=1, source="startup")
        logger.info(
            "feature flags loaded: version=%s flags=%s sources=[defaults%s,env]",
            snapshot.version,
            sorted(snapshot.flags),
            f",file:{self._config_path}" if self._config_path else "",
        )
        return snapshot

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def snapshot(self) -> FlagSnapshot:
        return self._store.current()

    def explain(self, flag: str, ctx: EvaluationContext) -> Evaluation:
        """评估开关并记录依据；请求内同一开关只真正评估一次（结果稳定）。"""
        cache = _request_cache.get()
        key = (flag, ctx.caller)
        if cache is not None and key in cache:
            result = cache[key]
            logger.debug(
                "[flag-cache-hit] input={flag=%r caller=%r request_id=%r} "
                "enabled=%s reason=%s bucket=%s version=%s",
                flag,
                ctx.caller,
                ctx.request_id,
                result.enabled,
                result.reason,
                result.bucket,
                self._store.current().version,
            )
            return result

        # 在同一次评估内先取快照再评估，保证结论与解释基于同一版本
        snap = self._store.current()
        result = evaluator_mod.evaluate(snap, flag, ctx)
        logger.debug(
            "[flag-eval] input={flag=%r caller=%r request_id=%r} "
            "version=%s enabled=%s reason=%s bucket=%s",
            flag,
            ctx.caller,
            ctx.request_id,
            snap.version,
            result.enabled,
            result.reason,
            result.bucket,
        )
        if cache is not None:
            cache[key] = result
        return result

    def is_enabled(self, flag: str, ctx: EvaluationContext) -> bool:
        return self.explain(flag, ctx).enabled

    # ------------------------------------------------------------------ #
    # 更新
    # ------------------------------------------------------------------ #
    def update(
        self,
        patch: Mapping[str, Mapping[str, Any]],
        *,
        partial: bool = True,
    ) -> FlagSnapshot:
        """整批校验通过后原子替换；任何一项不合法则整批拒绝、沿用旧配置。

        - ``partial=True``（默认）：把 patch 作为高优先级的一层字段级覆盖
          合并到当前配置；
        - ``partial=False``：patch 必须是一份完整配置，整体替换。
        """
        if not isinstance(patch, Mapping):
            raise ConfigError(
                ErrorCode.TYPE_ERROR,
                f"更新内容应为对象，实际类型为 {type(patch).__name__}",
                source="admin",
                path="",
            )

        old = self._store.current()

        # 1) 先把 patch 每一项严格校验一遍（任何异常都不会触碰旧快照）
        validated: dict[str, dict[str, Any]] = {
            name: build_definition(name, raw, source="admin")
            for name, raw in patch.items()
        }

        # 2) 在不修改旧快照的前提下构造候选配置
        if partial:
            old_raw = {
                name: definition_to_raw(defn)
                for name, defn in old.flags.items()
            }
            candidate_raw = merge_raw(old_raw, validated)
        else:
            candidate_raw = validated

        # 3) 完整构造 + 必填字段校验，失败则旧配置继续生效
        candidate = build_snapshot(candidate_raw, version=old.version + 1, source="admin")

        # 4) 全部合法才做一次性原子替换
        self._store.replace(candidate)
        logger.info(
            "feature flags replaced: version=%s->%s partial=%s updated=%s",
            old.version,
            candidate.version,
            partial,
            sorted(validated),
        )
        return candidate
