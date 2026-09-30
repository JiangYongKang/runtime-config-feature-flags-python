"""开关评估的确定性、优先级与灰度比例边界测试。"""

from __future__ import annotations

import pytest

from app.errors import UnknownFlagError
from app.evaluator import RequestFlagCache, _bucket, evaluate
from app.models import FeatureFlagsConfig

from .conftest import log_evaluation, log_case


def make(enabled=True, percentage=0, allowed=(), denied=(), name="f"):
    return FeatureFlagsConfig.from_dict({name: {
        "enabled": enabled,
        "rollout_percentage": percentage,
        "allowed_callers": list(allowed),
        "denied_callers": list(denied),
    }})


def test_denied_overrides_everything():
    # 黑名单优先级最高：即便全量灰度开启，黑名单调用方仍被关闭。
    cfg = make(percentage=100, denied=("mallory",))
    log_case("黑名单最高优先", caller="mallory", percentage=100)
    result = evaluate(cfg, "f", caller_id="mallory", request_id="r")
    log_evaluation("黑名单最高优先", result)
    assert result.on is False and result.matched == "denied_caller"


def test_allowed_overrides_disabled():
    # 白名单优先于总闸：enabled=false 时白名单调用方仍开启。
    cfg = make(enabled=False, percentage=0, allowed=("zoe",))
    result = evaluate(cfg, "f", caller_id="zoe", request_id="r")
    log_evaluation("白名单优先于总闸", result)
    assert result.on is True and result.matched == "allowed_caller"


def test_disabled_gate_closes_even_at_full_rollout():
    cfg = make(enabled=False, percentage=100)
    result = evaluate(cfg, "f", caller_id="bob", request_id="r")
    assert result.on is False and result.matched == "disabled"


@pytest.mark.parametrize("percentage,matched", [
    (0, "zero_rollout"),
    (100, "full_rollout"),
])
def test_rollout_extremes(percentage, matched):
    cfg = make(percentage=percentage)
    log_case("灰度极值", percentage=percentage, caller="anyone")
    result = evaluate(cfg, "f", caller_id="anyone", request_id="r")
    log_evaluation(f"灰度极值 {percentage}", result)
    assert result.matched == matched
    assert result.on is (percentage == 100)


def test_bucket_is_stable_and_in_range():
    for key in ["alice", "bob", "carol", "", "用户-001"]:
        first = _bucket("f", key)
        assert _bucket("f", key) == first  # 同键同桶
        assert 0 <= first <= 99  # 桶范围恒为 [0, 99]


def test_boundary_percentage_1_includes_only_bucket_zero():
    """1% 灰度的边界语义：只有桶号 0 命中（bucket < 1）。

    枚举大量调用方，找到桶号为 0 的调用方 -> 必须开启；
    桶号为 1 的调用方 -> 必须关闭。
    """
    cfg = make(percentage=1)
    keys = [f"user-{i}" for i in range(2000)]
    buckets = {key: _bucket("f", key) for key in keys}
    zero_keys = [k for k, b in buckets.items() if b == 0]
    one_keys = [k for k, b in buckets.items() if b == 1]
    assert zero_keys and one_keys, "测试样本应同时覆盖桶 0 和桶 1"

    for key in zero_keys[:3]:
        result = evaluate(cfg, "f", caller_id=key, request_id="r")
        assert result.on is True and result.matched == "rollout_in", key
        log_evaluation(f"桶0边界 {key}", result)
    for key in one_keys[:3]:
        result = evaluate(cfg, "f", caller_id=key, request_id="r")
        assert result.on is False and result.matched == "rollout_out", key


def test_boundary_percentage_99_excludes_only_bucket_99():
    """99% 灰度的边界语义：桶号 0..98 开启，桶号 99 关闭（bucket < 99）。"""
    cfg = make(percentage=99)
    keys = [f"user-{i}" for i in range(3000)]
    buckets = {key: _bucket("f", key) for key in keys}
    out_keys = [k for k, b in buckets.items() if b == 99]
    in_keys = [k for k, b in buckets.items() if b == 98]
    assert out_keys and in_keys

    assert all(
        evaluate(cfg, "f", caller_id=k, request_id="r").on is False
        for k in out_keys[:3]
    )
    assert all(
        evaluate(cfg, "f", caller_id=k, request_id="r").on is True
        for k in in_keys[:3]
    )
    LOGGER = pytest.importorskip("logging").getLogger("feature_flags.tests")
    LOGGER.info("99%% 边界: 桶99关闭样本=%s, 桶98开启样本=%s",
                out_keys[:3], in_keys[:3])


def test_same_caller_consistent_across_requests():
    # 同一调用方不同 request_id：落点只取决于 (flag, caller_id)，结论一致。
    cfg = make(percentage=37)
    results = {
        evaluate(cfg, "f", caller_id="bob", request_id=f"req-{i}").on
        for i in range(20)
    }
    assert results == {evaluate(cfg, "f", caller_id="bob", request_id="x").on}
    assert len(results) == 1


def test_different_flags_hash_independently():
    # 同一调用方在不同开关上的桶可以不同（flag 名参与哈希）。
    # 只验证稳定性，不强制必须不同。
    assert _bucket("flag_a", "bob") == _bucket("flag_a", "bob")
    # 绝大多数调用方在两个开关上的桶不相关——抽样验证确实存在分歧样本。
    flags = ("flag_a", "flag_b")
    split = [
        k for k in (f"u{i}" for i in range(500))
        if (_bucket(flags[0], k) < 50) != (_bucket(flags[1], k) < 50)
    ]
    assert split, "不同开关的灰度应当独立，样本中应存在分歧"


def test_request_cache_pins_result_within_request(sample_config):
    cache = RequestFlagCache(sample_config)
    first = cache.check("new_checkout", caller_id="carol", request_id="req-1")
    # 同一请求里即便换了 request_id（缓存键只看开关名），结论也必须一致。
    second = cache.check("new_checkout", caller_id="carol", request_id="req-2")
    assert first is second
    log_evaluation("请求内缓存", first)


def test_missing_stable_key_falls_back_to_request_id():
    cfg = make(percentage=100)  # 100% 不需要稳定键
    assert evaluate(cfg, "f", caller_id=None, request_id=None).matched == "full_rollout"

    cfg_partial = make(percentage=10)
    no_key = evaluate(cfg_partial, "f", caller_id=None, request_id=None)
    assert no_key.matched == "no_stable_key" and no_key.on is False

    with_request = evaluate(cfg_partial, "f", caller_id=None, request_id="req-9")
    assert with_request.matched in ("rollout_in", "rollout_out")
    # request_id 退化键也要稳定。
    again = evaluate(cfg_partial, "f", caller_id=None, request_id="req-9")
    assert again.on == with_request.on


def test_unknown_flag_raises():
    cfg = make()
    with pytest.raises(UnknownFlagError):
        evaluate(cfg, "nope", caller_id="x", request_id="r")
