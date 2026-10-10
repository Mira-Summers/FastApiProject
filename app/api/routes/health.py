"""健康检查与服务元信息。

探针语义严格区分（这是 K8s 上最容易配错的地方）：

- `/health/live`  存活：只反映进程本身，**绝不触碰 k8s API**。否则 API Server 抖动或
  服务账号被 RBAC 拒绝时，kubelet 会误杀本来健康的 Pod，把上游故障放大成雪崩。
- `/health/ready` 就绪：真打一次廉价的 API Server 探活，失败即从 Endpoints 摘除。
  带短超时 + 结果缓存（含失败结果），避免把探针本身变成 API Server 的压力源。

两个探针不能共用一个实现——这正是「liveness probe 配了业务依赖」这类事故的来源。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import Settings
from app.dependencies import get_core_api, get_readiness_probe, get_settings
from app.errors import ServiceUnavailable
from app.k8s.client import CoreV1ApiLike
from app.k8s.readiness import ReadinessProbe

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="存活探针")
def liveness() -> dict[str, str]:
    """不依赖任何外部组件，进程能响应就是存活。

    这里**不能**加 `Depends(get_core_api)`：那会让 /health/live 依赖集群配置，
    等于把上面写的语义又破坏掉。
    """
    return {"status": "ok"}


@router.get("/health/ready", summary="就绪探针")
def readiness(
    probe: ReadinessProbe = Depends(get_readiness_probe),
    core: CoreV1ApiLike = Depends(get_core_api),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    result = probe.check(core, request_timeout=settings.k8s_request_timeout)
    if not result.ready:
        # 失败返回 503 而不是 500：这是「暂时不可用」，不是「服务内部出错」，
        # 语义不同，看图排障时的判断也不同。
        raise ServiceUnavailable(result.detail or "服务未就绪：无法连接集群")
    return {"status": "ready"}


@router.get("/", summary="服务元信息")
def service_info(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
        "docs": "/docs",
    }
