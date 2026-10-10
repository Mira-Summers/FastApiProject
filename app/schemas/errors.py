"""统一错误响应模型。

所有失败响应都是同一个形状，便于调用方与告警系统统一处理：
    {"error": {"code": ..., "message": ..., "detail": ..., "request_id": ...}}
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="稳定的机器可读错误码，如 not_found / forbidden")
    message: str = Field(description="面向人的说明")
    detail: Optional[Any] = Field(default=None, description="补充信息，参数校验失败时为字段列表")
    request_id: str = Field(description="用于在服务端日志中定位这一次请求")


class ErrorResponse(BaseModel):
    error: ErrorDetail


# 供路由声明 OpenAPI 响应时复用
COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "请求不合法"},
    401: {"model": ErrorResponse, "description": "未提供或无法验证身份"},
    403: {"model": ErrorResponse, "description": "身份合法但无权执行该操作"},
    404: {"model": ErrorResponse, "description": "目标不存在"},
    422: {"model": ErrorResponse, "description": "参数校验失败"},
    502: {"model": ErrorResponse, "description": "上游集群返回错误"},
    503: {"model": ErrorResponse, "description": "依赖的组件不可用"},
    504: {"model": ErrorResponse, "description": "上游超时"},
}
