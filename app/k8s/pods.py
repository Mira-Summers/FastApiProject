"""Pod 查询与状态提取。

这里修掉了原实现的两个致命缺陷：
- `list_pods()` 调的是 `list_pod_for_all_namespaces()`，完全忽略 namespace 参数，
  等于 `/pods?namespace=prod` 会把整个集群的 Pod 都返回出去。
- `age` 直接输出原始时间戳，且完全没有重启原因。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from kubernetes.client.rest import ApiException

from app.errors import from_api_exception
from app.k8s.client import CoreV1ApiLike
from app.schemas.pods import ContainerStatus, PodInfo
from app.validation import validate_namespace, validate_pod_name

__all__ = ["build_pod_info", "get_pod", "humanize_age", "list_pods"]


def humanize_age(created: Optional[datetime], now: datetime) -> str:
    """把创建时间转成 3d4h / 12m / 45s 这样的可读时长。"""
    if created is None:
        return "unknown"
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    seconds = int((now - created).total_seconds())
    if seconds < 0:  # 节点时钟漂移时不要输出负数
        seconds = 0

    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


def _extract_state(status: Any) -> tuple[str, Optional[str]]:
    """从容器状态里取出当前处于哪个阶段，以及原因。"""
    state = getattr(status, "state", None)
    if state is None:
        return "unknown", None
    for name in ("running", "waiting", "terminated"):
        detail = getattr(state, name, None)
        if detail is not None:
            return name, getattr(detail, "reason", None)
    return "unknown", None


def build_container_status(status: Any) -> ContainerStatus:
    state_name, state_reason = _extract_state(status)

    # last_state.terminated 是 CrashLoopBackOff 排障的关键：
    # 它告诉我们容器上一次是怎么死的（OOMKilled / Error）以及退出码。
    last_reason: Optional[str] = None
    last_exit_code: Optional[int] = None
    last_state = getattr(status, "last_state", None)
    if last_state is not None and getattr(last_state, "terminated", None) is not None:
        last_reason = last_state.terminated.reason
        last_exit_code = last_state.terminated.exit_code

    started_at = None
    state = getattr(status, "state", None)
    if state is not None and getattr(state, "running", None) is not None:
        started_at = state.running.started_at

    return ContainerStatus(
        name=status.name,
        image=status.image or "",
        ready=bool(status.ready),
        restart_count=status.restart_count or 0,
        state=state_name,
        state_reason=state_reason,
        last_terminated_reason=last_reason,
        last_exit_code=last_exit_code,
        started_at=started_at,
    )


def _summarize_restart_reason(containers: list[ContainerStatus]) -> Optional[str]:
    """取重启次数最多的容器，报告它最近一次失败的原因。

    只看「有问题」的容器，健康的 Pod 返回 None，避免噪声。
    """
    failing = [
        c
        for c in containers
        if c.restart_count > 0 or c.state_reason or c.last_terminated_reason
    ]
    if not failing:
        return None

    worst = max(failing, key=lambda c: c.restart_count)
    # 优先用上次终止原因（OOMKilled / Error），它比当前的等待原因更接近根因；
    # CrashLoopBackOff 只是「反复重启」这个现象的名字。
    if worst.last_terminated_reason:
        code = f"，退出码 {worst.last_exit_code}" if worst.last_exit_code is not None else ""
        return f"{worst.name}: {worst.last_terminated_reason}{code}"
    if worst.state_reason:
        return f"{worst.name}: {worst.state_reason}"
    return f"{worst.name}: 已重启 {worst.restart_count} 次"


def build_pod_info(pod: Any, now: datetime) -> PodInfo:
    """把 SDK 的 V1Pod 转成我们自己的响应模型。纯函数，便于单测。"""
    statuses = getattr(pod.status, "container_statuses", None) or []
    containers = [build_container_status(s) for s in statuses]
    created = getattr(pod.metadata, "creation_timestamp", None)

    return PodInfo(
        name=pod.metadata.name,
        namespace=pod.metadata.namespace,
        phase=pod.status.phase or "Unknown",
        node=pod.spec.node_name,
        pod_ip=pod.status.pod_ip,
        created_at=created,
        age=humanize_age(created, now),
        ready=f"{sum(1 for c in containers if c.ready)}/{len(containers)}",
        restarts=sum(c.restart_count for c in containers),
        restart_reason=_summarize_restart_reason(containers),
        containers=containers,
        labels=dict(pod.metadata.labels or {}),
    )


def list_pods(
    core: CoreV1ApiLike,
    namespace: str,
    *,
    label_selector: Optional[str] = None,
    field_selector: Optional[str] = None,
    limit: Optional[int] = None,
    continue_token: Optional[str] = None,
    request_timeout: float,
) -> tuple[list[PodInfo], Optional[str]]:
    """列出**指定命名空间**下的 Pod。返回 (Pod 列表, 下一页游标)。"""
    namespace = validate_namespace(namespace)

    kwargs: dict[str, Any] = {"_request_timeout": request_timeout}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if field_selector:
        kwargs["field_selector"] = field_selector
    if limit is not None:
        kwargs["limit"] = limit
    if continue_token:
        kwargs["_continue"] = continue_token

    try:
        # 必须是 list_namespaced_pod。原实现用的 list_pod_for_all_namespaces()
        # 会让 namespace 参数形同虚设，越权返回全集群数据。
        result = core.list_namespaced_pod(namespace, **kwargs)
    except ApiException as exc:
        raise from_api_exception(exc, context=f"查询命名空间 {namespace} 的 Pod 失败") from exc

    now = datetime.now(timezone.utc)
    items = [build_pod_info(pod, now) for pod in result.items]

    meta = getattr(result, "metadata", None)
    # SDK 里 `continue` 是 Python 关键字，属性名可能是 continue_ 或 _continue，两种都兜住。
    next_token = getattr(meta, "continue_", None) or getattr(meta, "_continue", None)

    return items, next_token or None


def get_pod(
    core: CoreV1ApiLike,
    namespace: str,
    pod_name: str,
    *,
    request_timeout: float,
) -> PodInfo:
    namespace = validate_namespace(namespace)
    pod_name = validate_pod_name(pod_name)

    try:
        pod = core.read_namespaced_pod(
            name=pod_name, namespace=namespace, _request_timeout=request_timeout
        )
    except ApiException as exc:
        raise from_api_exception(
            exc, context=f"查询 Pod {namespace}/{pod_name} 失败"
        ) from exc

    return build_pod_info(pod, datetime.now(timezone.utc))
