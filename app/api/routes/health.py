"""健康检查与服务元信息。

探针语义严格区分（这是 K8s 上最容易配错的地方）：
- /health/live  存活：只反映进程本身，**绝不触碰 k8s API**。否则 API Server 抖动或
  服务账号被 RBAC 拒绝时，kubelet 会误杀本来健康的 Pod。
- /health/ready 就绪：需要一次廉价的 API Server 探活，失败即从 Endpoints 摘除。
  在 Phase 2 实现（届时会加上短超时与结果缓存，避免每次探测都打 API Server）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="存活探针")
def liveness() -> dict[str, str]:
    """不依赖任何外部组件，进程能响应就是存活。"""
    return {"status": "ok"}


@router.get("/", summary="服务元信息")
def service_info(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
        "docs": "/docs",
    }
