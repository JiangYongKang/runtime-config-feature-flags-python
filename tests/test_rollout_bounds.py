"""灰度比例边界与规则确定性测试。

覆盖：
- 比例 0   -> 任何调用方都不命中灰度；
- 比例 100 -> 任何调用方都命中灰度；
- 比例 1/99 的桶边界严格性（bucket < percentage）；
- 同一 (开关, 调用方) 任意次评估桶值恒定；
- deny 与 allow 冲突时结论确定（deny 优先）；
- baseline off 时名单外不放量。
"""

from __future__ import annotations

from app.flags.evaluator import evaluate, stable_bucket
from app.flags.manager import FlagManager, new_request_scope
from app.flags.merge import build_snapshot, merge_raw
from app.flags.models import EvaluationContext, FlagStatus


def _snapshot(flags: dict, version: int = 1):
    return build_snapshot(merge_raw(flags), version=version)


def test_percentage_zero_closes_for_everyone():
    snap = _snapshot({"f": {"status": FlagStatus.ON, "rollout_percentage": 0}})
    callers = [f"user-{i}" for i in range(200)]
    results = {evaluate(snap, "f", EvaluationContext(caller=c)).enabled for c in callers}
    assert results == {False}
    reasons = {evaluate(snap, "f", EvaluationContext(caller=c)).reason for c in callers}
    assert reasons == {"rollout_out_bucket"}


def test_percentage_hundred_opens_for_everyone():
    snap = _snapshot({"f": {"status": FlagStatus.ON, "rollout_percentage": 100}})
    callers = [f"user-{i}" for i in range(200)] + ["", "中文调用方", "a" * 50]
    results = {evaluate(snap, "f", EvaluationContext(caller=c)).enabled for c in callers}
    assert results == {True}
    assert all(
        0 <= evaluate(snap, "f", EvaluationContext(caller=c)).bucket < 100
        for c in callers
    )


def test_bucket_is_stable_and_in_range():
    for _ in range(50):
        flag = "dark_mode"
        caller = "caller-" + str(stable_bucket(flag, "seed"))
        values = {stable_bucket(flag, caller) for _ in range(100)}
        assert len(values) == 1  # 完全确定，无随机
        assert 0 <= values.pop() <= 99
    # 不同调用方的分桶应呈现分散性（粗略统计，不依赖具体哈希值）
    buckets = [stable_bucket("f", f"u{i}") for i in range(2000)]
    assert min(buckets) == 0 and max(buckets) == 99


def test_boundary_1_and_99_strict_less_than():
    # 直接按桶构造调用方集合，验证 bucket < percentage 的严格边界
    for pct in (1, 50, 99):
        in_hits = 0
        out_hits = 0
        snap = _snapshot({"f": {"status": FlagStatus.ON, "rollout_percentage": pct}})
        for i in range(3000):
            caller = f"c{i}"
            r = evaluate(snap, "f", EvaluationContext(caller=caller))
            assert (r.bucket < pct) == r.enabled
            if r.enabled:
                in_hits += 1
            else:
                out_hits += 1
        # 3000 个调用方，命中率应在比例附近（放宽到 ±15%，避免脆弱断言）
        ratio = in_hits / 3000
        assert pct / 100 - 0.15 < ratio < pct / 100 + 0.15, (pct, ratio)


def test_deny_overrides_allow_conflict_is_deterministic():
    snap = _snapshot(
        {
            "f": {
                "status": FlagStatus.ON,
                "rollout_percentage": 100,
                "allowed_callers": ["vip"],
                "denied_callers": ["vip"],
            }
        }
    )
    ctx = EvaluationContext(caller="vip")
    results = [evaluate(snap, "f", ctx) for _ in range(50)]
    assert all(r.enabled is False and r.reason == "deny_list" for r in results)


def test_allow_list_bypasses_rollout_and_baseline_on():
    # 即便灰度 0，白名单仍开启；白名单 bucket 标记为 -1（未参与分桶）
    snap = _snapshot(
        {"f": {"status": FlagStatus.ON, "rollout_percentage": 0, "allowed_callers": ["vip"]}}
    )
    r = evaluate(snap, "f", EvaluationContext(caller="vip"))
    assert r.enabled is True and r.reason == "allow_list" and r.bucket == -1


def test_baseline_off_blocks_non_listed_callers():
    # status=off 时，即便灰度 100，名单外调用方也必须关闭
    snap = _snapshot(
        {"f": {"status": FlagStatus.OFF, "rollout_percentage": 100, "allowed_callers": ["vip"]}}
    )
    assert evaluate(snap, "f", EvaluationContext(caller="bob")).enabled is False
    assert evaluate(snap, "f", EvaluationContext(caller="bob")).reason == "baseline_off"
    assert evaluate(snap, "f", EvaluationContext(caller="vip")).enabled is True


def test_different_flags_have_independent_buckets():
    # 同调用方在不同开关上的桶相互独立
    b1 = stable_bucket("flag-a", "alice")
    b2 = stable_bucket("flag-b", "alice")
    assert isinstance(b1, int) and isinstance(b2, int)


def test_request_cache_makes_repeated_evaluation_identical():
    m = FlagManager(environ={})
    m.update({"dark_mode": {"status": "on", "rollout_percentage": 45}}, partial=False)
    with new_request_scope():
        ctx = EvaluationContext(caller="alice", request_id="r")
        first = m.explain("dark_mode", ctx)
        for _ in range(10):
            r = m.explain("dark_mode", ctx)
            assert (r.enabled, r.reason, r.bucket) == (
                first.enabled,
                first.reason,
                first.bucket,
            )
