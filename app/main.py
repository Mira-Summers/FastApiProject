"""FastAPI 应用工厂。

硬性约束：**本模块 import 时不得建立任何 k8s 连接**。
这既是能在没有集群的机器上跑测试的前提，也是原 k8s_client.py 在 import 期调用
load_config() 导致进程直接崩掉的教训。所有 k8s 客户端一律懒加载。
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request, Response

from app.api.routes import health
from app.config import Settings, get_settings
from app.logging_setup import (
    configure_logging,
    get_request_id,
    reset_request_id,
    sanitize_request_id,
    set_request_id,
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

    _register_request_context(app)
    app.include_router(health.router)

    return app


app = create_app()
