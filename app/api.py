"""FastAPI 路由：开关判定、配置查询与运行时更新。

接口一览：

- ``GET /healthz``：存活探针；
- ``GET /flags``：列出所有开关的当前规则与配置版本；
- ``GET /flags/{name}``：按请求上下文评估开关，返回结论与完整判定依据；
- ``GET /admin/config``：查看当前整份配置（含版本号）；
- ``PUT /admin/config``：整份替换配置，非法时 422 整批拒绝、沿用旧配置。

上下文（``caller_id`` / ``request_id``）优先取查询参数，缺省时取请求头
``X-Caller-Id`` / ``X-Request-Id``。每个请求进入时把一个
:class:`~app.evaluator.RequestFlagCache` 绑定到当时的配置快照，因此同一请求内
多次评估同一开关结果一致，且不会受到请求处理期间配置更新的影响。
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse

from app.errors import (
    ConfigError,
    ConfigValidationErrors,
    UnknownFlagError,
)
from app.evaluator import RequestFlagCache
from app.manager import ConfigManager

logger = logging.getLogger("feature_flags")

router = APIRouter()


def _context_value(request: Request, query_name: str, header_name: str) -> str | None:
    value = request.query_params.get(query_name)
    if value is None or not value.strip():
        value = request.headers.get(header_name)
    if value is not None and not value.strip():
        return None
    return value


@router.get("/healthz")
def healthz(request: Request) -> dict:
    manager: ConfigManager = request.app.state.config_manager
    snap = manager.snapshot()
    return {"status": "ok", "config_version": snap.version}


@router.get("/flags")
def list_flags(request: Request) -> dict:
    manager: ConfigManager = request.app.state.config_manager
    snap = manager.snapshot()
    return {"version": snap.version, "flags": snap.config.to_dict()}


@router.get("/flags/{name}")
def check_flag(name: str, request: Request) -> dict:
    caller_id = _context_value(request, "caller_id", "x-caller-id")
    request_id = _context_value(request, "request_id", "x-request-id")
    cache: RequestFlagCache = request.state.flag_cache
    try:
        evaluation = cache.check(
            name, caller_id=caller_id, request_id=request_id
        )
    except UnknownFlagError as exc:
        logger.info("flag=%s 判定=unknown caller_id=%r request_id=%r",
                    name, caller_id, request_id)
        return JSONResponse(
            status_code=404,
            content={"error": exc.reason, "detail": str(exc)},
        )

    logger.info(
        "flag=%s 判定=%s caller_id=%r request_id=%r 依据=%s",
        name, evaluation.on, caller_id, request_id, list(evaluation.basis),
    )
    return {
        "version": request.state.config_version,
        **evaluation.to_dict(caller_id=caller_id, request_id=request_id),
    }


@router.get("/admin/config")
def get_config(request: Request) -> dict:
    manager: ConfigManager = request.app.state.config_manager
    snap = manager.snapshot()
    return {"version": snap.version, "flags": snap.config.to_dict()}


@router.put("/admin/config")
async def replace_config(request: Request) -> JSONResponse:
    raw_body = await request.body()
    if not raw_body:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "detail": "请求体不能为空"},
        )
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_json",
                "detail": (
                    f"请求体不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）: "
                    f"{exc.msg}"
                ),
            },
        )
    manager: ConfigManager = request.app.state.config_manager
    old_version = manager.version
    try:
        snap = manager.update(body)
    except ConfigValidationErrors as exc:
        logger.warning("配置更新被拒绝（沿用 version=%s）：%s", old_version, exc)
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_failed",
                "reason": "请求中的配置不合法，已整批拒绝并继续沿用旧配置",
                "rejected": True,
                "current_version": old_version,
                "details": [
                    {"path": err.path, "reason": err.reason, "message": str(err)}
                    for err in exc.errors
                ],
            },
        )

    logger.info("配置更新成功 version %s -> %s", old_version, snap.version)
    return JSONResponse(
        status_code=200,
        content={
            "accepted": True,
            "old_version": old_version,
            "version": snap.version,
            "flags": snap.config.to_dict(),
        },
    )


def create_app(manager: ConfigManager) -> FastAPI:
    """构造 FastAPI 应用：装配管理器、请求级快照绑定与错误处理器。"""
    app = FastAPI(title="Runtime Feature Flags", version="0.1.0")
    app.state.config_manager = manager
    app.include_router(router)

    @app.middleware("http")
    async def bind_snapshot(request: Request, call_next):
        # 请求进入时一次性确定本请求看到的配置快照，处理期间不再变化。
        snap = manager.snapshot()
        request.state.flag_cache = RequestFlagCache(snap.config)
        request.state.config_version = snap.version
        return await call_next(request)

    @app.exception_handler(ConfigValidationErrors)
    async def handle_validation_errors(_: Request, exc: ConfigValidationErrors):
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_failed",
                "details": [
                    {"path": err.path, "reason": err.reason, "message": str(err)}
                    for err in exc.errors
                ],
            },
        )

    @app.exception_handler(UnknownFlagError)
    async def handle_unknown_flag(_: Request, exc: UnknownFlagError):
        return JSONResponse(
            status_code=404, content={"error": exc.reason, "detail": str(exc)}
        )

    @app.exception_handler(ConfigError)
    async def handle_config_error(_: Request, exc: ConfigError):
        return JSONResponse(
            status_code=400,
            content={"error": exc.reason, "detail": str(exc)},
        )

    return app
