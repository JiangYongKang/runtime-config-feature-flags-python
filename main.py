"""FastAPI 入口：装配功能开关管理器并暴露 HTTP 接口。

接口：
- GET  /                 健康检查
- GET  /flags            查看当前生效配置（含版本号）
- GET  /flags/{name}     按请求上下文评估单个开关，返回结论与判定依据
- PATCH /admin/flags     运行时整批更新；任一项不合法则整批拒绝、沿用旧配置
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.deps import get_context, get_manager
from app.flags.errors import ConfigError, UnknownFlagError
from app.flags.manager import FlagManager, new_request_scope
from app.flags.models import EvaluationContext, FlagDefinition

CONFIG_PATH = os.environ.get("FEATURE_FLAGS_CONFIG_FILE")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时严格加载：配置非法应直接启动失败，而不是带着错误默认值运行。
    app.state.flag_manager = FlagManager(config_path=CONFIG_PATH)
    yield


app = FastAPI(title="runtime-config-feature-flags", lifespan=lifespan)


@app.middleware("http")
async def request_scope_middleware(request: Request, call_next):
    """每个请求建立独立的开关评估缓存：同请求内判定稳定，跨请求互不影响。"""
    with new_request_scope():
        return await call_next(request)


@app.exception_handler(ConfigError)
def _config_error_handler(request: Request, exc: ConfigError) -> JSONResponse:
    # 错误码区分 值缺失/类型错误/取值越界/来源非法，调用方可据此分别处理。
    return JSONResponse(
        status_code=400,
        content={
            "error": exc.code.value,
            "message": str(exc.args[0]) if exc.args else "",
            "source": exc.source,
            "path": exc.path,
        },
    )


@app.exception_handler(UnknownFlagError)
def _unknown_flag_handler(request: Request, exc: UnknownFlagError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "unknown_flag", "message": str(exc)})


def _serialize(defn: FlagDefinition) -> dict[str, Any]:
    return {
        "status": defn.status.value,
        "rollout_percentage": defn.rollout_percentage,
        "allowed_callers": sorted(defn.allowed_callers),
        "denied_callers": sorted(defn.denied_callers),
        "description": defn.description,
    }


@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "feature-flags", "status": "ok"}


@app.get("/flags")
def list_flags(request: Request) -> dict[str, Any]:
    manager = get_manager(request)
    snap = manager.snapshot()
    return {
        "version": snap.version,
        "flags": {name: _serialize(defn) for name, defn in sorted(snap.flags.items())},
    }


@app.get("/flags/{name}")
def get_flag(
    name: str,
    request: Request,
    ctx: EvaluationContext = Depends(get_context),
) -> dict[str, Any]:
    manager = get_manager(request)
    result = manager.explain(name, ctx)
    return {
        "flag": name,
        "enabled": result.enabled,
        "reason": result.reason,
        "bucket": result.bucket,
        "caller": ctx.caller,
        "request_id": ctx.request_id,
        "version": manager.snapshot().version,
    }


@app.patch("/admin/flags")
async def update_flags(request: Request) -> dict[str, Any]:
    """整批更新配置。

    请求体：{"flags": {...}, "partial": true}
    - partial=true  字段级合并进当前配置（默认）；
    - partial=false 请求体必须是完整配置，整体替换。
    任何一项不合法 -> 400 且整批拒绝，响应里带当前版本号，旧配置继续生效。
    """
    body = await request.json()
    if not isinstance(body, dict) or "flags" not in body:
        return JSONResponse(
            status_code=400,
            content={
                "error": "type_error",
                "message": "请求体应为对象且包含 'flags' 字段",
                "source": "admin",
                "path": "",
            },
        )
    partial = bool(body.get("partial", True))
    manager = get_manager(request)
    snapshot = manager.update(body["flags"], partial=partial)
    return {
        "accepted": True,
        "version": snapshot.version,
        "flags": {name: _serialize(defn) for name, defn in sorted(snapshot.flags.items())},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
