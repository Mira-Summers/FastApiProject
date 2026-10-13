"""领域异常与 HTTP 错误语义。

两条原则：
- **4xx 是调用方的问题**，可以把原因如实告知，帮助排障。
- **5xx 是我们的问题**，绝不回显内部细节（原实现把 `str(e)` 直接丢给调用方，
  既泄露了内部路径与 API Server 地址，又让所有错误码退化成 500）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from kubernetes.client.rest import ApiException
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_setup import get_request_id

logger = logging.getLogger("app.error")

# Starlette 抛出的 HTTPException 也要收敛进统一的错误码体系。
_HTTP_EXCEPTION_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    415: "unsupported_media_type",
}

__all__ = [
    "BadRequest",
    "Conflict",
    "NotFound",
    "OpsError",
    "PermissionDenied",
    "ServiceUnavailable",
    "Unauthorized",
    "UpstreamError",
    "UpstreamTimeout",
    "from_api_exception",
    "register_exception_handlers",
]


class OpsError(Exception):
    """所有可预期错误的基类。子类通过 status_code / code 自我描述。"""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        detail: Any = None,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        # 少数错误需要附加响应头，典型的是 401 的 WWW-Authenticate：
        # RFC 6750 要求 Bearer 认证失败时必须带这个头，客户端（含 kubectl 插件、
        # 各类 SDK）靠它判断该用哪种凭据重试。少了它，调用方只能靠猜。
        self.headers = headers


class BadRequest(OpsError):
    status_code = 400
    code = "bad_request"


class Unauthorized(OpsError):
    status_code = 401
    code = "unauthorized"


class PermissionDenied(OpsError):
    status_code = 403
    code = "forbidden"


class NotFound(OpsError):
    status_code = 404
    code = "not_found"


class Conflict(OpsError):
    status_code = 409
    code = "conflict"


class UpstreamError(OpsError):
    """上游（k8s API Server / helm）出错，不是调用方的问题。"""

    status_code = 502
    code = "upstream_error"


class ServiceUnavailable(OpsError):
    status_code = 503
    code = "service_unavailable"


class UpstreamTimeout(OpsError):
    status_code = 504
    code = "upstream_timeout"


def from_api_exception(exc: ApiException, *, context: str) -> OpsError:
    """把 kubernetes SDK 的 ApiException 映射成有语义的领域异常。

    调用方越权（403）和资源不存在（404）是使用中最常见的两种情况，
    必须如实反映成 403/404，而不是一律 500。
    """
    status = exc.status or 500
    reason = (exc.reason or "").strip()
    suffix = f"（{reason}）" if reason and status < 500 else ""

    if status == 401:
        return Unauthorized(f"{context}：集群拒绝了本服务的身份认证")
    if status == 403:
        return PermissionDenied(f"{context}：本服务账号没有该操作的权限")
    if status == 404:
        return NotFound(f"{context}：目标不存在{suffix}")
    if status == 409:
        return Conflict(f"{context}：资源冲突{suffix}")
    if status == 429:
        return ServiceUnavailable(f"{context}：API Server 正在限流，请稍后重试")
    if status >= 500:
        # 5xx 不把 reason 带出去，避免回显集群内部信息。
        return UpstreamError(f"{context}：API Server 返回 {status}")
    return UpstreamError(f"{context}：集群返回 {status}{suffix}")


def _error_response(
    status_code: int,
    code: str,
    message: str,
    detail: Optional[Any] = None,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "detail": detail,
                "request_id": get_request_id(),
            }
        },
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """把各类异常统一收敛成同一种错误响应结构。"""

    @app.exception_handler(OpsError)
    async def _handle_ops_error(request: Request, exc: OpsError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error(
                "请求失败",
                extra={
                    "event": "request_error",
                    "path": request.url.path,
                    "code": exc.code,
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
            )
        return _error_response(
            exc.status_code, exc.code, exc.message, exc.detail, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 把 pydantic 的报错压成「字段 -> 说明」，直接可读。
        detail = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "message": error.get("msg", ""),
            }
            for error in exc.errors()
        ]
        return _error_response(422, "validation_error", "请求参数不合法", detail)

    @app.exception_handler(ApiException)
    async def _handle_api_exception(request: Request, exc: ApiException) -> JSONResponse:
        # 兜底：正常情况下各 service 已把 ApiException 转成 OpsError，
        # 万一漏了也不至于把 SDK 的原始报错丢给调用方。
        mapped = from_api_exception(exc, context="访问集群失败")
        logger.error(
            "未转换的 ApiException",
            extra={"event": "unconverted_api_exception", "path": request.url.path},
        )
        return _error_response(mapped.status_code, mapped.code, mapped.message)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # 路由未匹配(404)、方法不允许(405) 这类由 Starlette 自己抛 HTTPException，
        # 默认响应体是 {"detail": "Not Found"}，和我们定义的错误结构不一致。
        # 调用方不该为了解析错误而准备两套逻辑——尤其是 405 这种会真实遇到的场景。
        return _error_response(
            exc.status_code,
            _HTTP_EXCEPTION_CODES.get(exc.status_code, "http_error"),
            str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 只有这里才需要 logger.exception：4xx 是预期内的，5xx 才是需要排查的。
        logger.exception(
            "未捕获异常",
            extra={"event": "unhandled_error", "path": request.url.path},
        )
        return _error_response(500, "internal_error", "服务内部错误，请查看服务端日志")
