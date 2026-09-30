"""运行时配置存储：不可变快照 + 原子替换。

一致性保证：
- :class:`~app.flags.models.FlagSnapshot` 本身不可变，读方拿到的是一整份
  冻结配置，更新过程不可能读到"改了一半"的中间态；
- 更新采用"先在锁外构造好新快照，再在锁内一次性替换引用"的方式，
  临界区只做一次指针赋值；
- 任何一次读要么拿到旧快照、要么拿到新快照，二者都是完整配置。
"""

from __future__ import annotations

import threading

from .models import FlagSnapshot


class FlagStore:
    def __init__(self, initial: FlagSnapshot | None = None) -> None:
        self._lock = threading.RLock()
        self._snapshot = initial if initial is not None else FlagSnapshot()

    def current(self) -> FlagSnapshot:
        """返回当前生效的完整快照（始终非空；未初始化时为空快照 v0）。"""
        with self._lock:
            return self._snapshot

    def replace(self, snapshot: FlagSnapshot) -> None:
        """原子替换整份快照。调用方必须已完成全部校验后才调用本方法。"""
        with self._lock:
            self._snapshot = snapshot
