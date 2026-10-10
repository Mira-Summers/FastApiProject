"""FastAPI 应用工厂。

硬性约束：**本模块 import 时不得建立任何 k8s 连接**。
这既是能在没有集群的机器上跑测试的前提，也是原 k8s_client.py 在 import 期调用
load_config() 导致进程直接崩掉的教训。所有 k8s 客户端一律懒加载。
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager, nullcontext
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request, Response

from app.api.routes import health, helm, logs, pods
from app.api.routes import metrics as metrics_routes
from app.audit import AuditLogger
from app.config import Settings, get_settings
from app.errors import register_exception_handlers
from app.k8s.readiness import ReadinessProbe
from app.logging_setup import (
    configure_logging,
    get_request_id,
    reset_request_id,
    sanitize_request_id,
    set_request_id,
)
from app.metrics import (
    is_metered_route,
    is_scrape,
    observe_request,
    route_template,
    track_in_progress,
)

logger = logging.getLogger("app.request")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "应用启动",
        extra={
            "event": "startup",
            "env": settings.app_env,
            "version": settings.app_version,
            "auth_mode": settings.auth_mode,
        },
    )
    if settings.auth_disabled:
        # 这条日志必须刺眼：免认证模式下面向网络开放等于把回滚权限公开。
        logger.warning(
            "认证已关闭，任何人只要能访问本端口即可执行回滚等变更操作——仅限本地开发使用",
            extra={"event": "insecure_mode_enabled"},
        )
    yield
    logger.info("应用关闭", extra={"event": "shutdown"})


def _register_request_context(app: FastAPI) -> None:
    """给每个请求挂上 request_id，并记录一条结构化的访问日志。"""

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        # 复用调用方传来的 X-Request-ID 以便跨服务串联，但要先收敛字符集，
        # 否则可以用它注入换行来伪造日志行。
        token = set_request_id(sanitize_request_id(request.headers.get("X-Request-ID")))
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "请求处理抛出未捕获异常",
                extra={
                    "event": "request_failed",
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(elapsed_ms, 2),
                },
            )
            raise
        else:
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers["X-Request-ID"] = get_request_id()
            logger.info(
                "请求完成",
                extra={
                    "event": "request",
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(elapsed_ms, 2),
                    "client": request.client.host if request.client else None,
                },
            )
            return response
        finally:
            reset_request_id(token)


def _register_metrics(app: FastAPI) -> None:
    """HTTP 指标。刻意注册在请求上下文中间件之后，从而位于最外层。

    Starlette 的 `add_middleware` 是 insert(0)，后注册的在更外层。放最外层意味着
    这个中间件度量的是调用方真实感受到的端到端耗时，也意味着它能看到
    内层任何中间的异常。
    """

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next) -> Response:
        method = request.method
        started = time.perf_counter()
        # 先按 500 记账：call_next 抛异常时下面的 finally 会按这个值记录。
        status_code = 500
        # 抓取请求不计入在途数，否则 gauge 永远读到 1（详见 app.metrics.is_scrape）。
        tracker = nullcontext() if is_scrape(request.url.path) else track_in_progress(method)
        try:
            with tracker:
                response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            # 路由模板要等 call_next 返回后才写进 scope，所以只能在这里读。
            route = route_template(request.scope)
            if is_metered_route(route):
                observe_request(
                    method=method,
                    route=route,
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                )


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """应用工厂。测试里传入自定义 Settings 即可完全隔离环境。"""
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_output=bool(settings.log_json))

    app = FastAPI(
        title="K8s 运维助手 API",
        description="基于 FastAPI + kubernetes Python SDK 的轻量级运维工具",
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = settings
    # 审计器与探活缓存挂在 app 上：它们都有进程内状态（文件锁、TTL 缓存），
    # 测试里每个 app 各一份，天然隔离。
    app.state.audit = AuditLogger(settings.audit_log_path)
    app.state.readiness = ReadinessProbe(ttl_seconds=settings.k8s_ready_cache_seconds)

    # 异常处理必须在挂路由之前注册，保证所有路由的报错都收敛成同一种响应结构。
    register_exception_handlers(app)
    _register_request_context(app)
    # 后注册的在更外层，指标要包住全部中间件，所以放最后。
    _register_metrics(app)

    app.include_router(health.router)
    app.include_router(pods.router)
    app.include_router(logs.router)
    app.include_router(helm.router)
    app.include_router(metrics_routes.router)

    return app


app = create_app()
