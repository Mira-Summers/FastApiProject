"""Pod 日志接口。

顺带修掉了原实现的路径错误：日志接口原本挂在 `/pods/{pod_name}/pods` 上。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.config import Settings
from app.dependencies import get_core_api, get_settings
from app.k8s.client import CoreV1ApiLike
from app.k8s.logs import read_pod_logs
from app.schemas.errors import COMMON_ERROR_RESPONSES
from app.schemas.logs import LogResponse
from app.security.guards import require_pod_logs
from app.security.identity import Identity

router = APIRouter(
    prefix="/api/v1/namespaces/{namespace}/pods",
    tags=["logs"],
    responses=COMMON_ERROR_RESPONSES,
)


@router.get(
    "/{pod_name}/logs",
    response_model=LogResponse,
    summary="读取 Pod 日志",
    description=(
        "需要调用方具备该 namespace 的 `get pods/log` 权限。\n\n"
        "`pods/log` 是独立子资源，在 RBAC 里可以单独授予——"
        "「能看 Pod 状态但不能看日志」是常见且合理的配置。"
    ),
)
def get_pod_logs(
    namespace: str,
    pod_name: str,
    _identity: Identity = Depends(require_pod_logs),
    container: Optional[str] = Query(None, description="容器名；不传则用 Pod 的默认容器"),
    tail_lines: Optional[int] = Query(
        200, ge=1, le=10000, description="只返回末尾这么多行"
    ),
    previous: bool = Query(
        False,
        description="读取上一个已终止容器的日志。CrashLoopBackOff 排障必用——"
        "当前容器起不来时，只有上一个容器的日志有信息",
    ),
    since_seconds: Optional[int] = Query(
        None, ge=1, description="只返回最近这么多秒内的日志"
    ),
    timestamps: bool = Query(False, description="每行前加上时间戳"),
    core: CoreV1ApiLike = Depends(get_core_api),
    settings: Settings = Depends(get_settings),
) -> LogResponse:
    lines = read_pod_logs(
        core,
        namespace,
        pod_name,
        container=container,
        tail_lines=tail_lines,
        previous=previous,
        since_seconds=since_seconds,
        timestamps=timestamps,
        request_timeout=settings.k8s_request_timeout,
    )
    return LogResponse(
        pod_name=pod_name,
        namespace=namespace,
        container=container,
        previous=previous,
        tail_lines=tail_lines,
        line_count=len(lines),
        lines=lines,
    )
