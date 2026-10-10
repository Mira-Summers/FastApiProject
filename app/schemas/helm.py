"""Helm 相关的请求与响应模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class HelmRevision(BaseModel):
    revision: int
    updated: Optional[str] = None
    status: str
    chart: Optional[str] = None
    app_version: Optional[str] = None
    description: Optional[str] = None


class HelmHistoryResponse(BaseModel):
    release: str
    namespace: str
    revisions: list[HelmRevision]


class RollbackRequest(BaseModel):
    # 不传则回滚到上一个版本
    revision: Optional[int] = Field(default=None, ge=1, description="目标版本号")
    # 回滚是不可逆的生产变更，强制调用方显式确认，避免脚本误触
    confirm: bool = Field(default=False, description="必须显式传 true 才执行")


class RollbackResponse(BaseModel):
    """回滚成功时的响应。

    失败不在这里表达——它会抛出对应的领域异常，由统一的错误响应结构返回
    （例如 404 release 不存在、502 helm 报错、504 超时）。
    """

    release: str
    namespace: str
    to_revision: Optional[int] = None
    message: str
    output: Optional[str] = None
