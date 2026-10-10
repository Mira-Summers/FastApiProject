"""Kubernetes 客户端的构建与探活。

关键约束：**配置加载必须懒执行**。
原实现在模块 import 期就调用 load_config()，导致没有集群的机器上进程直接起不来，
连 /health 都活不了。这里改成第一次真正要访问集群时才加载。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

from app.config import Settings
from app.errors import ServiceUnavailable, from_api_exception

logger = logging.getLogger("app.k8s")

__all__ = ["CoreV1ApiLike", "build_core_api", "ping"]

_lock = threading.Lock()
_cache: dict[Settings, Any] = {}


class CoreV1ApiLike(Protocol):
    """我们实际用到的 CoreV1Api 子集。

    抽成 Protocol 是为了测试能注入同形状的假实现，从而完全不依赖真实集群。
    """

    def list_namespaced_pod(self, namespace: str, **kwargs: Any) -> Any: ...

    def read_namespaced_pod(self, name: str, namespace: str, **kwargs: Any) -> Any: ...

    def read_namespaced_pod_log(self, name: str, namespace: str, **kwargs: Any) -> str: ...

    def list_namespace(self, **kwargs: Any) -> Any: ...


def _load_default_configuration() -> None:
    """按「集群内配置 → 本地 kubeconfig」的顺序加载。

    走 SDK 自带的加载器很重要：in-cluster 加载器会给 Configuration 装上
    refresh_api_key_hook，每分钟左右检查并重读 ServiceAccount token，
    所以长跑的 Pod 不会因为 token 轮换而 401。
    **不要手写 Authorization 头**，那会绕过这个刷新机制。
    """
    try:
        config.load_incluster_config()
        logger.info(
            "已加载集群内配置", extra={"event": "kubeconfig_loaded", "source": "incluster"}
        )
        return
    except ConfigException:
        pass

    try:
        config.load_kube_config()
    except Exception as exc:
        raise ServiceUnavailable(
            "无法加载 Kubernetes 配置：既不在集群内运行，也找不到可用的 kubeconfig"
        ) from exc
    logger.info("已加载本地 kubeconfig", extra={"event": "kubeconfig_loaded", "source": "kubeconfig"})


def build_core_api(settings: Settings) -> CoreV1ApiLike:
    """构建（并缓存）CoreV1Api。首次调用时才真正去加载集群配置。"""
    cached = _cache.get(settings)
    if cached is not None:
        return cached

    with _lock:
        cached = _cache.get(settings)
        if cached is None:
            _load_default_configuration()
            # 刻意不传 configuration：让 ApiClient 走 Configuration.get_default_copy()，
            # 这样 in-cluster 的 token 刷新钩子才会被保留（deepcopy 对函数是原子操作）。
            cached = client.CoreV1Api()
            _cache[settings] = cached
            logger.info("已构建 CoreV1Api 客户端", extra={"event": "core_api_ready"})
    return cached


def ping(core: CoreV1ApiLike, *, request_timeout: float) -> None:
    """一次廉价的 API Server 探活。**仅供 readiness 探针使用。**

    用 limit=1 的 namespace 列表：足以验证「网络可达 + 认证通过 + 授权通过」，
    又几乎不产生负载。失败时抛出领域异常，由调用方决定如何反映到就绪状态。
    """
    try:
        core.list_namespace(limit=1, _request_timeout=request_timeout)
    except ApiException as exc:
        raise from_api_exception(exc, context="探活 API Server 失败") from exc
    except Exception as exc:  # noqa: BLE001 —— 见下方说明，这里刻意兜底
        # 连接层面的失败（API Server 不可达、DNS 解析不了、TLS 握手失败、读超时）
        # SDK 不会包装成 ApiException，而是直接把 urllib3 的原始异常抛出来。
        # 而「API Server 不可达」恰恰是 readiness 最需要如实报告的情况，
        # 让它变成 500 + 一整篇堆栈既不对也没用。
        # 只带异常类名、不带 message，避免把内部地址或路径回显出去。
        raise ServiceUnavailable(f"无法连接 API Server（{type(exc).__name__}）") from exc
