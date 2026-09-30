"""并发更新下的读取一致性测试。

覆盖：
- 反复原子更新期间，读到的永远是某一份"完整快照"，绝不出现半合并状态；
- 非法批次整批拒绝，旧配置继续生效，版本号不前进；
- 同一请求上下文内多次评估结果稳定，即使请求处理途中发生更新；
- 高并发更新后版本号严格连续、无丢失。
"""

from __future__ import annotations

import threading

import pytest

from app.flags.errors import ConfigError
from app.flags.manager import FlagManager, new_request_scope
from app.flags.models import ErrorCode, EvaluationContext
from app.flags.sources import definition_to_raw


def _check_snapshot_coherent(snapshot) -> None:
    """快照必须自洽：每个开关字段类型/取值都在合法域内。"""
    for name, defn in snapshot.flags.items():
        assert defn.name == name
        assert defn.status.value in ("on", "off")
        assert 0 <= defn.rollout_percentage <= 100
        assert isinstance(defn.allowed_callers, frozenset)
        assert isinstance(defn.denied_callers, frozenset)


def test_invalid_batch_rejected_keeps_old_config(manager):
    v_before = manager.snapshot().version
    with pytest.raises(ConfigError) as ei:
        manager.update(
            {
                "dark_mode": {"rollout_percentage": 101},  # 越界
                "new_checkout": {"status": "on"},         # 这一项本身合法
            }
        )
    assert ei.value.code is ErrorCode.VALUE_OUT_OF_RANGE
    snap = manager.snapshot()
    # 整批拒绝：版本不变，两项都维持旧值
    assert snap.version == v_before
    assert snap.flags["dark_mode"].rollout_percentage == 100
    assert snap.flags["new_checkout"].status.value == "off"


def test_partial_vs_full_replace(manager):
    # partial 更新保留未提及的开关
    manager.update({"dark_mode": {"rollout_percentage": 10}})
    assert "new_checkout" in manager.snapshot().flags

    # 全量替换：只保留本次给出的开关
    snap = manager.update(
        {"only_flag": {"status": "on", "rollout_percentage": 50}}, partial=False
    )
    assert set(snap.flags) == {"only_flag"}

    # 全量替换缺 status -> 整批拒绝，旧配置保留
    with pytest.raises(ConfigError) as ei:
        manager.update({"only_flag": {"rollout_percentage": 1}}, partial=False)
    assert ei.value.code is ErrorCode.MISSING_VALUE
    assert set(manager.snapshot().flags) == {"only_flag"}


def test_concurrent_updates_never_observe_torn_state(manager):
    stop = threading.Event()
    errors: list[Exception] = []

    def writer(start: int) -> None:
        i = start
        try:
            while not stop.is_set():
                # 与 start 错开奇偶，制造两个线程交替写不同值
                value = (i + start) % 101
                manager.update(
                    {"dark_mode": {"rollout_percentage": value, "status": "on"}}
                )
                i += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                snap = manager.snapshot()
                # 核心断言：任何时刻拿到的都是完整自洽的一份
                _check_snapshot_coherent(snap)
                # 已声明开关必须可评估，且不抛异常
                for name in snap.flags:
                    manager.explain(name, EvaluationContext(caller=f"u{snap.version % 7}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(s,)) for s in (0, 3)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    timer = threading.Timer(0.6, stop.set)
    timer.start()
    for t in threads:
        t.join(timeout=5)
    timer.join()
    assert not errors, errors
    assert not any(t.is_alive() for t in threads)


def test_concurrent_valid_updates_version_strictly_increases(manager):
    iterations = 200
    n_threads = 4
    barrier = threading.Barrier(n_threads)
    seen_versions: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        for i in range(iterations):
            snap = manager.update(
                {"dark_mode": {"rollout_percentage": (i % 100), "status": "on"}}
            )
            with lock:
                seen_versions.append(snap.version)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected_total = iterations * n_threads
    assert len(seen_versions) == expected_total
    # 版本从 1 起严格连续，无重复无跳跃 -> 每次合法更新都恰好原子生效一次
    assert sorted(seen_versions) == list(range(2, 2 + expected_total))


def test_request_scope_pins_result_across_midrequest_update(manager):
    manager.update({"dark_mode": {"rollout_percentage": 0, "status": "on"}}, partial=False)
    with new_request_scope():
        ctx = EvaluationContext(caller="alice", request_id="req-x")
        first = manager.explain("dark_mode", ctx)
        # 请求处理途中把比例改成 100
        manager.update({"dark_mode": {"rollout_percentage": 100, "status": "on"}})
        second = manager.explain("dark_mode", ctx)
        third = manager.is_enabled("dark_mode", ctx)
        assert first.enabled == second.enabled is third is False
        assert first.reason == second.reason == "rollout_out_bucket"

    # 请求结束后，新请求看到的是更新后的完整配置
    with new_request_scope():
        after = manager.explain("dark_mode", EvaluationContext(caller="alice"))
        assert after.enabled is True
        assert after.reason == "rollout_in_bucket"
