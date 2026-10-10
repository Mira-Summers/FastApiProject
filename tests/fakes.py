"""测试用的假实现。

目的是让路由测试既能覆盖真实的业务逻辑（app/k8s/*、app/helm/* 全部真实执行），
又完全不依赖真实集群或 helm 二进制。假对象采用 duck typing，形状对齐 SDK。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from kubernetes.client.rest import ApiException

from app.helm.runner import HelmResult

__all__ = [
    "FakeCoreV1Api",
    "FakeHelmRunner",
    "make_api_exception",
    "make_container_status",
    "make_pod",
    "make_state",
]


def make_api_exception(status: int, reason: str = "Error") -> ApiException:
    return ApiException(status=status, reason=reason)


def make_state(state: str, reason: Optional[str] = None) -> SimpleNamespace:
    """构造 V1ContainerState 形状的对象，只会有一个分支非 None。"""
    return SimpleNamespace(
        running=SimpleNamespace(started_at=None) if state == "running" else None,
        waiting=SimpleNamespace(reason=reason) if state == "waiting" else None,
        terminated=(
            SimpleNamespace(reason=reason, exit_code=137)
            if state == "terminated"
            else None
        ),
    )


def make_container_status(
    name: str = "app",
    *,
    image: str = "nginx:1.25",
    ready: bool = True,
    restart_count: int = 0,
    state: str = "running",
    state_reason: Optional[str] = None,
    last_reason: Optional[str] = None,
    last_exit_code: Optional[int] = None,
) -> SimpleNamespace:
    if last_reason is None:
        last_state = SimpleNamespace(terminated=None)
    else:
        last_state = SimpleNamespace(
            terminated=SimpleNamespace(reason=last_reason, exit_code=last_exit_code)
        )
    return SimpleNamespace(
        name=name,
        image=image,
        ready=ready,
        restart_count=restart_count,
        state=make_state(state, state_reason),
        last_state=last_state,
    )


def make_pod(
    name: str = "web-abc123",
    namespace: str = "default",
    *,
    phase: str = "Running",
    node: str = "node-1",
    pod_ip: str = "10.0.0.1",
    age_seconds: int = 3600,
    containers: Optional[list[SimpleNamespace]] = None,
    labels: Optional[dict[str, str]] = None,
) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=namespace,
            labels=labels if labels is not None else {"app": "web"},
            creation_timestamp=now - timedelta(seconds=age_seconds),
        ),
        spec=SimpleNamespace(node_name=node),
        status=SimpleNamespace(
            phase=phase,
            pod_ip=pod_ip,
            container_statuses=(
                containers if containers is not None else [make_container_status()]
            ),
        ),
    )


class FakeCoreV1Api:
    """替代 CoreV1Api。会记录每次调用的参数，便于断言路由是否如实透传。"""

    def __init__(
        self,
        *,
        pods: Optional[list[Any]] = None,
        log_text: str = "",
        continue_token: Optional[str] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.pods = pods if pods is not None else []
        self.log_text = log_text
        self.continue_token = continue_token
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        if self.error is not None:
            raise self.error

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def list_namespaced_pod(self, namespace: str, **kwargs: Any) -> Any:
        self._record("list_namespaced_pod", {"namespace": namespace, **kwargs})
        return SimpleNamespace(
            items=self.pods,
            metadata=SimpleNamespace(continue_=self.continue_token),
        )

    def read_namespaced_pod(self, name: str, namespace: str, **kwargs: Any) -> Any:
        self._record("read_namespaced_pod", {"name": name, "namespace": namespace, **kwargs})
        if not self.pods:
            raise make_api_exception(404, "Not Found")
        return self.pods[0]

    def read_namespaced_pod_log(self, name: str, namespace: str, **kwargs: Any) -> str:
        self._record(
            "read_namespaced_pod_log", {"name": name, "namespace": namespace, **kwargs}
        )
        return self.log_text

    def list_namespace(self, **kwargs: Any) -> Any:
        self._record("list_namespace", kwargs)
        return SimpleNamespace(items=[])


class FakeHelmRunner:
    """替代 HelmRunner，不需要机器上真的装了 helm。"""

    def __init__(
        self,
        *,
        result: Optional[HelmResult] = None,
        results: Optional[list[HelmResult]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self._result = result if result is not None else HelmResult(0, "", "")
        self._queue = list(results) if results else None
        self.error = error
        self.commands: list[list[str]] = []

    def run(self, args: Any, *, timeout: Optional[float] = None) -> HelmResult:
        self.commands.append(list(args))
        if self.error is not None:
            raise self.error
        if self._queue:
            return self._queue.pop(0)
        return self._result
