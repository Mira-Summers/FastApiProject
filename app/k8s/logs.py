"""Pod 日志读取。

修掉了原实现的致命缺陷：`log = core_v1.read_namespaced_pod_log(...)` 之后写的却是
`return logs.split("\\n")`——变量名对不上，这个接口从来没有成功返回过。

同时补上了运维真正需要的参数：container / previous / since_seconds / timestamps。
其中 `previous` 尤其重要：CrashLoopBackOff 的容器当前是起不来的状态，
不看上一个容器的日志就等于看不到任何有用信息。
"""

from __future__ import annotations

from typing import Any, Optional

from kubernetes.client.rest import ApiException

from app.errors import from_api_exception
from app.k8s.client import CoreV1ApiLike
from app.validation import validate_namespace, validate_pod_name

__all__ = ["read_pod_logs"]


def read_pod_logs(
    core: CoreV1ApiLike,
    namespace: str,
    pod_name: str,
    *,
    container: Optional[str] = None,
    tail_lines: Optional[int] = None,
    previous: bool = False,
    since_seconds: Optional[int] = None,
    timestamps: bool = False,
    request_timeout: float,
) -> list[str]:
    """读取 Pod 日志，按行返回。"""
    namespace = validate_namespace(namespace)
    pod_name = validate_pod_name(pod_name)

    kwargs: dict[str, Any] = {"_request_timeout": request_timeout}
    if container:
        kwargs["container"] = container
    if tail_lines is not None:
        kwargs["tail_lines"] = tail_lines
    if previous:
        kwargs["previous"] = True
    if since_seconds is not None:
        kwargs["since_seconds"] = since_seconds
    if timestamps:
        kwargs["timestamps"] = True

    try:
        log = core.read_namespaced_pod_log(name=pod_name, namespace=namespace, **kwargs)
    except ApiException as exc:
        raise from_api_exception(
            exc, context=f"读取 Pod {namespace}/{pod_name} 的日志失败"
        ) from exc

    if not log:
        return []
    # splitlines() 比 split("\n") 更稳：日志里出现 \r\n 时不会留下尾随的 \r。
    return log.splitlines()
