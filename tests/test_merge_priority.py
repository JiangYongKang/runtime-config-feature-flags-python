"""配置合并优先级与严格校验测试。

覆盖：
- 优先级 defaults < file < env，字段级覆盖、列表整体替换；
- 值缺失 / 类型错误 / 取值越界三类失败原因可区分，不静默默认；
- 文件不存在、JSON 非法、未知字段、未知开关等。
"""

from __future__ import annotations

import logging

import pytest

from app.flags.errors import ConfigError
from app.flags.manager import FlagManager
from app.flags.models import ErrorCode, EvaluationContext, FlagStatus
from app.flags.merge import build_snapshot, merge_raw
from app.flags.sources import (
    default_definitions,
    definition_to_raw,
    load_env_config,
    load_file_config,
)


def test_defaults_present():
    defaults = default_definitions()
    assert "dark_mode" in defaults and "new_checkout" in defaults
    assert defaults["dark_mode"].status is FlagStatus.ON


def test_field_level_merge_low_to_high():
    low = {"f": definition_to_raw(default_definitions()["dark_mode"])}
    mid = {"f": {"rollout_percentage": 40}}
    high = {"f": {"rollout_percentage": 70}, "g": {"status": FlagStatus.ON}}
    merged = merge_raw(low, mid, high)
    # 高优先级覆盖低优先级
    assert merged["f"]["rollout_percentage"] == 70
    # 未覆盖字段沿用低层
    assert merged["f"]["status"] is FlagStatus.ON
    # 高层新增开关保留
    assert merged["g"]["status"] is FlagStatus.ON


def test_list_fields_are_replaced_not_concatenated():
    low = {"f": {"status": FlagStatus.ON, "allowed_callers": ["a", "b"]}}
    high = {"f": {"allowed_callers": ["c"]}}
    merged = merge_raw(low, high)
    # 兼容规则：列表整体替换，语义明确不拼接
    assert merged["f"]["allowed_callers"] == ["c"]


def test_priority_defaults_file_env(tmp_config_file):
    path = tmp_config_file(
        '{"dark_mode": {"rollout_percentage": 25}, '
        '"new_checkout": {"status": "on", "rollout_percentage": 10}}'
    )
    env = {
        "FEATURE_FLAGS_CONFIG_FILE": path,
        "FEATURE_FLAG_DARK_MODE_PERCENTAGE": "80",  # env 覆盖文件
    }
    m = FlagManager(config_path=path, environ=env)
    snap = m.snapshot()
    assert snap.flags["dark_mode"].rollout_percentage == 80          # env 最高
    assert snap.flags["new_checkout"].status is FlagStatus.ON        # 文件提供
    assert snap.flags["new_checkout"].rollout_percentage == 10


def test_env_status_override(tmp_config_file):
    path = tmp_config_file('{"new_checkout": {"status": "on"}}')
    m = FlagManager(config_path=path, environ={"FEATURE_FLAG_NEW_CHECKOUT": "off"})
    assert m.snapshot().flags["new_checkout"].status is FlagStatus.OFF


def test_env_lists_and_description():
    raw = load_env_config(
        {
            "FEATURE_FLAG_X": "on",
            "FEATURE_FLAG_X_ALLOW": "alice,bob",
            "FEATURE_FLAG_X_DENY": "mallory",
            "FEATURE_FLAG_X_DESC": "灰度实验 X",
        }
    )
    full = build_snapshot(merge_raw(raw), version=2)
    assert full.flags["x"].status is FlagStatus.ON
    assert full.flags["x"].allowed_callers == frozenset({"alice", "bob"})
    assert full.flags["x"].denied_callers == frozenset({"mallory"})
    assert full.flags["x"].description == "灰度实验 X"


def test_missing_required_status_distinct_error(tmp_config_file):
    # env-only 开关只有 percentage 没有 status（且无默认/文件提供）-> MISSING_VALUE
    with pytest.raises(ConfigError) as ei:
        FlagManager(config_path=None, environ={"FEATURE_FLAG_ORPHAN_PERCENTAGE": "10"})
    assert ei.value.code is ErrorCode.MISSING_VALUE
    assert ei.value.path == "orphan.status"


def test_type_error_is_distinct_from_range_error():
    with pytest.raises(ConfigError) as ei:
        load_env_config({"FEATURE_FLAG_X_PERCENTAGE": "abc"})
    assert ei.value.code is ErrorCode.TYPE_ERROR

    with pytest.raises(ConfigError) as ei:
        load_env_config({"FEATURE_FLAG_X_PERCENTAGE": "101"})
    assert ei.value.code is ErrorCode.VALUE_OUT_OF_RANGE

    with pytest.raises(ConfigError) as ei:
        load_env_config({"FEATURE_FLAG_X": "maybe"})
    assert ei.value.code is ErrorCode.VALUE_OUT_OF_RANGE


def test_file_level_errors(tmp_config_file):
    with pytest.raises(ConfigError) as ei:
        load_file_config(tmp_config_file('{"f": {"status": 1}}'))
    assert ei.value.code is ErrorCode.TYPE_ERROR

    with pytest.raises(ConfigError) as ei:
        load_file_config(tmp_config_file('{"f": {"rollout_percentage": -1}}'))
    assert ei.value.code is ErrorCode.VALUE_OUT_OF_RANGE

    with pytest.raises(ConfigError) as ei:
        load_file_config(tmp_config_file("{not json"))
    assert ei.value.code is ErrorCode.INVALID_SOURCE

    with pytest.raises(ConfigError) as ei:
        load_file_config("/nonexistent/path/flags.json")
    assert ei.value.code is ErrorCode.INVALID_SOURCE

    with pytest.raises(ConfigError) as ei:
        load_file_config(tmp_config_file('{"f": {"unknown_field": 1}}'))
    assert ei.value.code is ErrorCode.TYPE_ERROR


def test_evaluation_logs_input_and_reason(manager, caplog):
    """单测日志里必须打印输入与判定依据。"""
    caplog.set_level(logging.DEBUG, logger="feature_flags")
    ctx = EvaluationContext(caller="alice", request_id="req-1")
    manager.explain("dark_mode", ctx)
    records = [r.message for r in caplog.records if r.name == "feature_flags"]
    assert any("[flag-eval]" in m and "alice" in m and "reason=" in m for m in records), records
