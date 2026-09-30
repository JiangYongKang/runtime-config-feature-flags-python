"""运行时更新的原子性与整批拒绝测试，包含并发读/写压力场景。"""

from __future__ import annotations

import threading

import pytest

from app.errors import ConfigValidationErrors
from app.models import FeatureFlagsConfig
from app.evaluator import evaluate

from .conftest import LOGGER, log_case


def full_body(percentage: int, *, enabled: bool = True) -> dict:
    return {"flags": {"new_checkout": {
        "enabled": enabled,
        "allowed_callers": [],
        "denied_callers": [],
        "rollout_percentage": percentage,
    }}}


def test_successful_update_bumps_version(manager):
    before = manager.snapshot()
    after = manager.update(full_body(100))
    assert after.version == before.version + 1
    assert after.config.get("new_checkout").rollout_percentage == 100
    LOGGER.info("版本 %s -> %s", before.version, after.version)


def test_invalid_update_is_rejected_and_old_config_kept(manager):
    before = manager.snapshot()
    log_case("非法更新整批拒绝", old_version=before.version)
    with pytest.raises(ConfigValidationErrors) as exc:
        manager.update({"flags": {"new_checkout": {"enabled": "yes",
                                                   "rollout_percentage": 500}}})
    reasons = sorted({e.reason for e in exc.value.errors})
    LOGGER.info("拒绝原因集合: %s", reasons)
    assert "invalid_type" in reasons and "out_of_range" in reasons

    current = manager.snapshot()
    assert current.version == before.version
    assert current.config is before.config  # 旧配置原封不动
    assert current.config.get("new_checkout").rollout_percentage == 50


def test_partial_update_rejected_full_replace_semantics(manager):
    # 整份替换语义：缺字段的提交不能被默认值悄悄补齐。
    with pytest.raises(ConfigValidationErrors) as exc:
        manager.update({"flags": {"new_checkout": {"enabled": True}}})
    assert any(e.reason == "missing_value" for e in exc.value.errors)
    assert manager.version == 1


def test_concurrent_readers_never_see_torn_state(manager):
    """N 个读线程在更新风暴中持续读取，读到的必须始终是某一版“完整配置”。

    校验方式：每次读到的快照都必须能独立通过完整校验，
    且同一次快照内各字段取值必须是某一版整批写入的值（enabled 与
    rollout_percentage 成对出现，没有“半个更新”）。
    """
    stop = threading.Event()
    read_count = 0
    violations: list[str] = []
    seen_versions: set[int] = set()
    lock = threading.Lock()

    writer_versions: list[int] = []

    def reader():
        nonlocal read_count
        while not stop.is_set():
            snap = manager.snapshot()
            # 1) 快照可独立完整校验（不可变对象，直接序列化再反解析）。
            try:
                FeatureFlagsConfig.from_dict(snap.config.to_dict(), partial=False)
            except ConfigValidationErrors as exc:
                violations.append(f"快照损坏: {exc}")
            # 2) 本批测试中每个写入版本的 enabled 恒为 True；
            #    若读到 enabled=False 且版本号 > 1，说明看到了撕裂状态。
            rule = snap.config.get("new_checkout")
            if snap.version > 1 and not rule.enabled:
                violations.append(
                    f"version={snap.version} enabled=False 与更新内容不符"
                )
            with lock:
                seen_versions.add(snap.version)
                read_count += 1

    readers = [threading.Thread(target=reader) for _ in range(8)]
    for t in readers:
        t.start()

    # 写线程：交替提交合法更新（版本递增）和非法更新（必须被拒绝）。
    def writer():
        for pct in range(1, 61):
            try:
                snap = manager.update(full_body(pct))
                with lock:
                    writer_versions.append(snap.version)
            except ConfigValidationErrors:
                pass
            try:
                manager.update({"flags": {"new_checkout": {"enabled": 9}}})
            except ConfigValidationErrors:
                pass

    w = threading.Thread(target=writer)
    w.start()
    w.join()
    stop.set()
    for t in readers:
        t.join()

    LOGGER.info("读取次数=%s 观察到的版本数=%d 成功写入版本数=%d",
                read_count, len(seen_versions), len(writer_versions))
    LOGGER.info("观察到的版本: %s", sorted(seen_versions))
    assert not violations, violations
    # 60 次合法更新全部生效；60 次非法更新一次都没生效。
    assert len(writer_versions) == 60
    assert manager.version == 61
    assert manager.snapshot().config.get("new_checkout").rollout_percentage == 60


def test_inflight_request_keeps_old_snapshot_until_it_finishes(manager):
    """模拟“请求处理期间配置被更新”：请求持有的快照与评估结果保持更新前一致。"""
    from app.evaluator import RequestFlagCache

    snap = manager.snapshot()
    cache = RequestFlagCache(snap.config)

    before = evaluate(snap.config, "new_checkout",
                      caller_id="alice", request_id="req-1")
    # 请求处理到一半，配置被整体替换（甚至删掉/改变规则）。
    manager.update(full_body(100))
    after = cache.check("new_checkout", caller_id="alice", request_id="req-1")

    assert after == before
    LOGGER.info("更新前评估 on=%s matched=%s；请求内复查 on=%s matched=%s",
                before.on, before.matched, after.on, after.matched)
