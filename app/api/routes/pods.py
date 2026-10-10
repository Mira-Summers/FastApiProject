"""Pod 查询接口。

路由采用 K8s 风格：命名空间作为路径的一部分，作用域一眼可见，
不会再出现「namespace 靠 query 传、写错就落到 default」的问题。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.config import Settings
from app.dependencies import get_core_api, get_settings
from app.k8s.client import CoreV1ApiLike
from app.k8s.pods import get_pod as fetch_pod
from app.k8s.pods import list_pods as fetch_pods
from app.schemas.errors import COMMON_ERROR_RESPONSES
from app.schemas.pods import PodInfo, PodListResponse

router = APIRouter(
    prefix="/api/v1/namespaces/{namespace}/pods",
    tags=["pods"],
    responses=COMMON_ERROR_RESPONSES,
)


@router.get("", response_model=PodListResponse, summary="列出命名空间下的 Pod")
def list_pods(
    namespace: str,
    label_selector: Optional[str] = Query(
        None, description="标签选择器，例如 app=web,tier!=cache"
    ),
    field_selector: Optional[str] = Query(
        None, description="字段选择器，例如 status.phase=Running"
    ),
    limit: Optional[int] = Query(None, ge=1, le=1000, description="单页最大返回条数"),
    continue_token: Optional[str] = Query(
        None, alias="continue", description="翻页游标，用上一次响应里的 continue_token"
    ),
    core: CoreV1ApiLike = Depends(get_core_api),
    settings: Settings = Depends(get_settings),
) -> PodListResponse:
    items, next_token = fetch_pods(
        core,
        namespace,
        label_selector=label_selector,
        field_selector=field_selector,
        limit=limit,
        continue_token=continue_token,
        request_timeout=settings.k8s_request_timeout,
    )
    return PodListResponse(
        namespace=namespace,
        count=len(items),
        items=items,
        continue_token=next_token,
    )


@router.get("/{pod_name}", response_model=PodInfo, summary="查询单个 Pod 的详情")
def get_pod(
    namespace: str,
    pod_name: str,
    core: CoreV1ApiLike = Depends(get_core_api),
    settings: Settings = Depends(get_settings),
) -> PodInfo:
    return fetch_pod(
        core, namespace, pod_name, request_timeout=settings.k8s_request_timeout
    )
