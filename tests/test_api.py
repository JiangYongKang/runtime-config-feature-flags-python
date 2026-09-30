"""HTTP 接口端到端测试（TestClient）。"""

from __future__ import annotations

import pytest

# 在导入 main 前确保不读宿主配置
import os

os.environ.pop("FEATURE_FLAGS_CONFIG_FILE", None)

from fastapi.testclient import TestClient

import main


@pytest.fixture()
def client():
    # startup 事件里构造 manager（默认配置 + 空环境）
    with TestClient(main.app) as c:
        yield c


def test_root_and_list_flags(client):
    r = client.get("/")
    assert r.status_code == 200
    r = client.get("/flags")
    body = r.json()
    assert body["version"] == 1
    assert set(body["flags"]) == {"dark_mode", "new_checkout"}
    # 输出字段完整、可解释
    f = body["flags"]["dark_mode"]
    assert set(f) == {
        "status",
        "rollout_percentage",
        "allowed_callers",
        "denied_callers",
        "description",
    }


def test_evaluate_flag_returns_reason(client):
    r = client.get("/flags/dark_mode", headers={"X-Caller": "alice", "X-Request-Id": "t1"})
    assert r.status_code == 200
    body = r.json()
    assert body["flag"] == "dark_mode"
    assert body["reason"] in {
        "rollout_in_bucket",
        "rollout_out_bucket",
        "allow_list",
        "deny_list",
        "baseline_off",
    }
    assert 0 <= body["bucket"] <= 99
    assert body["request_id"] == "t1"


def test_unknown_flag_404(client):
    r = client.get("/flags/not_there")
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_flag"


def test_admin_invalid_batch_rejected_with_distinct_code(client):
    before = client.get("/flags").json()
    r = client.patch(
        "/admin/flags",
        json={"flags": {"dark_mode": {"rollout_percentage": 101}}},
    )
    assert r.status_code == 400
    err = r.json()
    assert err["error"] == "value_out_of_range"
    assert err["path"] == "dark_mode.rollout_percentage"
    # 旧配置沿用，版本不变
    after = client.get("/flags").json()
    assert after["version"] == before["version"]
    assert after["flags"] == before["flags"]


def test_admin_type_error_code(client):
    r = client.patch("/admin/flags", json={"flags": {"dark_mode": {"status": 3}}})
    assert r.status_code == 400
    assert r.json()["error"] == "type_error"


def test_admin_valid_partial_update(client):
    r = client.patch(
        "/admin/flags",
        json={"flags": {"new_checkout": {"status": "on", "rollout_percentage": 100}}},
    )
    assert r.status_code == 200
    assert r.json()["version"] == 2
    # 未提及的 dark_mode 仍在
    assert "dark_mode" in r.json()["flags"]
    eval_r = client.get("/flags/new_checkout", headers={"X-Caller": "anyone"})
    assert eval_r.json()["enabled"] is True
    assert eval_r.json()["reason"] == "rollout_in_bucket"


def test_admin_full_replace(client):
    r = client.patch(
        "/admin/flags",
        json={
            "partial": False,
            "flags": {"only": {"status": "off"}},
        },
    )
    assert r.status_code == 200
    assert set(r.json()["flags"]) == {"only"}


def test_malformed_body(client):
    r = client.patch("/admin/flags", json={"nope": 1})
    assert r.status_code == 400
    assert r.json()["error"] == "type_error"
