"""HTTP 接口集成测试：判定、查询、整批更新、请求内一致性。"""

from __future__ import annotations

import threading

from .conftest import LOGGER, log_case


def valid_put_body(percentage, *, enabled=True, allowed=None, denied=None):
    return {"flags": {"new_checkout": {
        "enabled": enabled,
        "allowed_callers": allowed or [],
        "denied_callers": denied or [],
        "rollout_percentage": percentage,
    }}}


def test_healthz_and_list(client):
    assert client.get("/healthz").json()["status"] == "ok"
    listing = client.get("/flags").json()
    assert listing["version"] == 1
    assert "new_checkout" in listing["flags"]


def test_check_flag_explains_basis(client):
    log_case("接口判定-黑名单", caller="mallory")
    resp = client.get("/flags/new_checkout", headers={"x-caller-id": "mallory"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["on"] is False and body["matched"] == "denied_caller"
    assert body["caller_id"] == "mallory"
    assert len(body["basis"]) >= 3  # 规则 + 上下文 + 命中依据
    LOGGER.info("判定依据链: %s", body["basis"])


def test_caller_context_from_query_param_overrides_header(client):
    # 查询参数优先于请求头。
    resp = client.get(
        "/flags/new_checkout?caller_id=alice",
        headers={"x-caller-id": "mallory"},
    )
    assert resp.json()["matched"] == "allowed_caller"


def test_unknown_flag_returns_404(client):
    resp = client.get("/flags/nope", headers={"x-caller-id": "x"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "unknown_flag"


def test_put_config_accepts_and_takes_effect(client):
    resp = client.put("/admin/config", json=valid_put_body(100))
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert resp.json()["version"] == 2

    resp = client.get("/flags/new_checkout", headers={"x-caller-id": "bob"})
    assert resp.json()["matched"] == "full_rollout"
    assert resp.json()["version"] == 2


def test_put_invalid_config_rejected_with_details(client):
    resp = client.put("/admin/config", json={"flags": {
        "new_checkout": {"enabled": "yes", "rollout_percentage": 500}}})
    assert resp.status_code == 422
    body = resp.json()
    assert body["rejected"] is True
    reasons = {d["reason"] for d in body["details"]}
    assert {"invalid_type", "out_of_range", "missing_value"} <= reasons
    # 旧配置仍在。
    assert client.get("/admin/config").json()["version"] == 1
    LOGGER.info("422 明细: %s", body["details"])


def test_put_malformed_json_returns_400(client):
    resp = client.put("/admin/config", content="{oops",
                      headers={"content-type": "application/json"})
    assert resp.status_code == 400


def test_same_request_sees_single_version_even_during_update(client):
    """同一 HTTP 请求内多次查询，version 与结论必须恒定。

    请求中间件在请求开始时绑定快照；这里通过同一次请求内串行两次调用模拟
    “处理过程中外部更新了配置”，验证 request state 的缓存语义（单元层），
    并通过并发测试覆盖 HTTP 层。
    """
    from app.evaluator import RequestFlagCache

    app_state = client.app.state.config_manager
    snap = app_state.snapshot()
    cache = RequestFlagCache(snap.config)
    first = cache.check("new_checkout", caller_id="bob", request_id="r")
    client.put("/admin/config", json=valid_put_body(100))
    second = cache.check("new_checkout", caller_id="bob", request_id="r")
    assert first == second


def test_concurrent_http_updates_and_reads_are_consistent(client):
    """HTTP 层并发：读线程读到的每版配置都必须是完整合法的快照。"""
    stop = threading.Event()
    errors: list[str] = []
    observed: set[int] = set()
    lock = threading.Lock()

    def reader():
        while not stop.is_set():
            resp = client.get("/admin/config")
            if resp.status_code != 200:
                errors.append(f"status={resp.status_code}")
                continue
            body = resp.json()
            rule = body["flags"]["new_checkout"]
            # 整批写入的合法取值范围；任何越界/类型异常都说明读到中间态。
            if not isinstance(rule["enabled"], bool):
                errors.append(f"enabled 类型异常: {rule}")
            if not 0 <= rule["rollout_percentage"] <= 100:
                errors.append(f"比例越界: {rule}")
            with lock:
                observed.add(body["version"])

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()

    accepted = rejected = 0
    for pct in range(1, 41):
        ok = client.put("/admin/config", json=valid_put_body(pct))
        accepted += ok.status_code == 200
        bad = client.put("/admin/config", json={"flags": {"new_checkout": {
            "enabled": True, "rollout_percentage": 101,
            "allowed_callers": [], "denied_callers": []}}})
        rejected += bad.status_code == 422

    stop.set()
    for t in readers:
        t.join()

    LOGGER.info("HTTP 并发: accepted=%d rejected=%d versions=%s errors=%s",
                accepted, rejected, sorted(observed), errors)
    assert accepted == 40 and rejected == 40
    assert not errors
    assert client.get("/admin/config").json()["version"] == 41
