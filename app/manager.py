"""运行时配置管理：原子快照 + 锁。

读请求拿到的是不可变快照引用（:class:`~app.models.FeatureFlagsConfig`
本身是 frozen dataclass），拿到后可以无锁使用。更新时**先在锁外**把新配置
解析、校验完整，全部通过后才在锁内做一次引用替换——任何正在处理的请求看到的
要么是旧完整配置、要么是新完整配置，绝不会看到更新一半的中间状态。

更新语义是**整份替换**（不是打补丁）：提交内容必须包含每个开关的全部字段，
任何一条不合法都会整批拒绝并继续沿用旧配置。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from app.models import FeatureFlagsConfig


@dataclass(frozen=True)
class ConfigSnapshot:
    """配置快照 + 单调递增版本号（便于日志 / 响应说明当前用的是哪一版）。"""

    config: FeatureFlagsConfig
    version: int


class ConfigManager:
    """持有当前 :class:`FeatureFlagsConfig` 快照并支持原子替换。"""

    def __init__(self, initial: FeatureFlagsConfig) -> None:
        self._lock = threading.RLock()
        self._snapshot = ConfigSnapshot(config=initial, version=1)

    def snapshot(self) -> ConfigSnapshot:
        """获取当前完整配置快照（不可变，可安全跨请求持有）。"""
        # 取一个引用是原子的；加锁只为内存可见性上的严格性。
        with self._lock:
            return self._snapshot

    def update(self, raw: dict) -> ConfigSnapshot:
        """校验并原子替换整份配置。

        ``raw`` 形如 ``{"flags": {...}}``，与配置文件格式一致。
        校验在锁外完成；任何错误抛出 :class:`ConfigValidationErrors`，
        当前快照（旧配置）保持不变。成功后返回新版本快照。
        """
        from app.errors import ConfigValidationError, ConfigValidationErrors

        if not isinstance(raw, dict) or "flags" not in raw:
            raise ConfigValidationErrors([
                ConfigValidationError(
                    "请求体必须是包含 'flags' 字段的对象",
                    path="body",
                    reason="invalid_structure",
                )
            ])

        # 锁外完成解析与校验（可能较慢，但绝不触碰当前生效配置）。
        new_config = FeatureFlagsConfig.from_dict(
            raw["flags"], path="flags", partial=False
        )

        with self._lock:
            new_snapshot = ConfigSnapshot(
                config=new_config, version=self._snapshot.version + 1
            )
            self._snapshot = new_snapshot
            return new_snapshot

    @property
    def version(self) -> int:
        return self.snapshot().version
