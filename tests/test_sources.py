"""配置合并优先级与来源校验的测试（默认值 < 文件 < 环境变量）。"""

from __future__ import annotations

import json

import pytest

from app.errors import ConfigSourceError, ConfigValidationErrors
from app.sources import DEFAULT_CONFIG, build_config, load_env, merge_layers

from .conftest import LOGGER, log_case


def test_defaults_used_when_no_override():
    log_case("仅默认值", file_path=None, environ={})
    cfg, provenance = build_config(file_path=None, environ={})
    assert cfg.get("new_checkout").enabled is False
    assert provenance["new_checkout.enabled"] == "defaults"
    LOGGER.info("结论: new_checkout.enabled 来源=%s", provenance["new_checkout.enabled"])


def test_file_overrides_defaults_partial_fields(example_file):
    log_case("文件部分覆盖", file=example_file, env={})
    cfg, provenance = build_config(file_path=example_file, environ={})
    rule = cfg.get("new_checkout")
    assert rule.enabled is True
    assert rule.rollout_percentage == 25
    assert rule.allowed_callers == ("alice", "bob")
    assert provenance["new_checkout.enabled"] == "file"
    assert provenance["new_checkout.denied_callers"] == "file"
    LOGGER.info("来源标注: %s", provenance)


def test_file_unspecified_fields_keep_defaults(tmp_path):
    # 文件只给部分字段时，没给出的字段（如 denied_callers）保持默认值来源。
    path = tmp_path / "partial.json"
    path.write_text(
        '{"flags": {"new_checkout": {"enabled": true}}}', encoding="utf-8"
    )
    cfg, provenance = build_config(file_path=str(path), environ={})
    assert cfg.get("new_checkout").enabled is True
    assert cfg.get("new_checkout").rollout_percentage == 0
    assert provenance["new_checkout.enabled"] == "file"
    assert provenance["new_checkout.denied_callers"] == "defaults"
    assert provenance["new_checkout.rollout_percentage"] == "defaults"


def test_env_overrides_file_and_defaults(example_file):
    env = {
        "FF_NEW_CHECKOUT_ENABLED": "false",
        "FF_NEW_CHECKOUT_ROLLOUT_PERCENTAGE": "50",
    }
    log_case("环境变量最高优先", file=example_file, env=env)
    cfg, provenance = build_config(file_path=example_file, environ=env)
    rule = cfg.get("new_checkout")
    assert rule.enabled is False  # env 覆盖了文件的 true
    assert rule.rollout_percentage == 50  # env 覆盖了文件的 25
    assert rule.allowed_callers == ("alice", "bob")  # 文件保留
    assert provenance["new_checkout.enabled"] == "env"
    assert provenance["new_checkout.rollout_percentage"] == "env"
    assert provenance["new_checkout.allowed_callers"] == "file"
    LOGGER.info("合并后规则: %s", rule)


def test_env_can_define_new_flag_not_in_file():
    env = {"FF_BRAND_NEW_ENABLED": "true",
           "FF_BRAND_NEW_ROLLOUT_PERCENTAGE": "10",
           "FF_BRAND_NEW_ALLOWED_CALLERS": "alice",
           "FF_BRAND_NEW_DENIED_CALLERS": "bob"}
    cfg, _ = build_config(environ=env)
    assert cfg.get("brand_new").rollout_percentage == 10
    # 默认配置里的开关依旧存在。
    assert set(cfg.flags) >= {"new_checkout", "beta_search", "brand_new"}


def test_field_level_merge_does_not_whole_replace():
    layers = [
        {"flags": {"f": {"enabled": True, "rollout_percentage": 10}}},
        {"flags": {"f": {"rollout_percentage": 80}}},
    ]
    merged = merge_layers(*layers)
    assert merged == {"flags": {"f": {"enabled": True,
                                      "rollout_percentage": 80}}}


def test_missing_file_is_skipped(tmp_path):
    missing = str(tmp_path / "nope.json")
    cfg, provenance = build_config(file_path=missing, environ={})
    assert cfg.get("new_checkout").enabled is False
    assert provenance["new_checkout.enabled"] == "defaults"


def test_broken_json_file_raises_source_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigSourceError) as exc:
        build_config(file_path=str(path), environ={})
    LOGGER.info("损坏文件错误: %s", exc.value)


@pytest.mark.parametrize(
    "env,expected_reason",
    [
        ({"FF_NEW_CHECKOUT_ENABLED": "maybe"}, "invalid_type"),
        ({"FF_NEW_CHECKOUT_ROLLOUT_PERCENTAGE": "200"}, "out_of_range"),
        ({"FF_NEW_CHECKOUT_ENABLED": ""}, "invalid_type"),
        ({"FF_WHAT_IS_THIS": "1"}, "unknown_env_variable"),
        ({"FF_NEW_CHECKOUT_ALLOWED_CALLERS": "alice,,bob"}, "invalid_element"),
    ],
)
def test_invalid_env_is_rejected_with_distinct_reason(env, expected_reason):
    log_case("非法环境变量", env=env, 期望原因=expected_reason)
    with pytest.raises(ConfigValidationErrors) as exc:
        build_config(environ=env)
    reasons = {e.reason for e in exc.value.errors}
    assert expected_reason in reasons
    LOGGER.info("收集到的原因: %s", reasons)


def test_cross_layer_list_conflict_is_rejected(example_file):
    # 文件里 alice 在白名单；环境变量把 alice 放进黑名单 -> 跨层冲突。
    env = {"FF_NEW_CHECKOUT_DENIED_CALLERS": "alice"}
    with pytest.raises(ConfigValidationErrors) as exc:
        build_config(file_path=example_file, environ=env)
    assert {e.reason for e in exc.value.errors} == {"rules_conflict"}
    LOGGER.info("跨层冲突: %s", exc.value.errors[0])


def test_defaults_layer_is_itself_complete():
    # 默认层必须能通过完整校验（每个开关四字段齐全）。
    raw = json.loads(json.dumps(DEFAULT_CONFIG))
    from app.models import FeatureFlagsConfig

    config = FeatureFlagsConfig.from_dict(raw["flags"], partial=False)
    assert {"new_checkout", "beta_search"} <= set(config.flags)


def test_load_env_parses_lists():
    parsed = load_env({"FF_F_ALLOWED_CALLERS": "a, b ,c",
                       "FF_F_ENABLED": "TRUE"})
    assert parsed == {"flags": {"f": {"allowed_callers": ["a", "b", "c"],
                                      "enabled": True}}}
