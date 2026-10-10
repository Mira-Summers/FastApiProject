"""依赖装配的唯一入口。

所有外部副作用（配置、k8s 客户端、helm 执行器）都从这里注入。测试通过
`app.dependency_overrides` 替换这几个 provider，就能完全不依赖真实集群。
"""

from __future__ import annotations

from fastapi import Depends, Request

from app.audit import AuditLogger
from app.config import Settings
from app.helm.runner import HelmRunner
from app.k8s.client import CoreV1ApiLike, build_core_api
from app.k8s.readiness import ReadinessProbe

__all__ = [
    "get_audit_logger",
    "get_core_api",
    "get_helm_runner",
    "get_readiness_probe",
    "get_settings",
]


def get_settings(request: Request) -> Settings:
    """取应用实例上的配置。

    刻意不直接调用 `app.config.get_settings()`：那个是带 lru_cache 的进程级环境读取，
    会让 `create_app(settings)` 传入的配置失效，测试也就无法隔离真实环境变量。
    """
    return request.app.state.settings


def get_core_api(settings: Settings = Depends(get_settings)) -> CoreV1ApiLike:
    """首次调用时才真正去加载集群配置（懒加载）。"""
    return build_core_api(settings)


def get_helm_runner(settings: Settings = Depends(get_settings)) -> HelmRunner:
    return HelmRunner(settings)


def get_audit_logger(request: Request) -> AuditLogger:
    """审计器挂在 app 上而不是每次构造：它持有文件锁，必须是进程内唯一实例。"""
    return request.app.state.audit


def get_readiness_probe(request: Request) -> ReadinessProbe:
    """探活缓存挂在 app 上，测试里每个 app 一份，互不串味。"""
    return request.app.state.readiness
