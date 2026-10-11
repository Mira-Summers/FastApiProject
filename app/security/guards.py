"""路由级授权守卫。

写成「一个路由动作对应一个守卫函数」，而不是让每个路由自己在函数体里调
`authorize(...)`，理由是三种失败模式（没认证、参数非法、没权限）的**执行顺序**
非常容易被写错，而它的正确顺序是：

    认证 → 参数校验 → 授权

- **认证必须最先**。否则未认证的调用方能通过观察 400 和 403 的差异来探测
  「这个 namespace 名格式对不对」之类的信息，而且会白送一次集群往返。
- **参数校验必须早于授权**。把 `../../etc` 这种名字塞进 SubjectAccessReview
  是在浪费集群资源，而它无论如何都会被拒绝——不如用 400 挡在自己这里。
- **授权必须早于真正访问集群**。这就是 SAR 作为前置闸门的全部意义。

用一个函数把这三步固定下来，路由文件里就只剩下业务逻辑，不会再出现
「新加的路由忘了加守卫」这种事——因为守卫是以 `Depends(...)` 的形式
写在路由签名里的，漏了它，处理函数就拿不到 `Identity` 参数。
"""

from __future__ import annotations

from fastapi import Depends

from app.dependencies import (
    get_authz_api,
    get_authorizer,
    get_identity,
    get_policy_engine,
)
from app.errors import PermissionDenied
from app.k8s.client import AuthorizationV1ApiLike
from app.policy.engine import PolicyEngine
from app.policy.model import ACTION_HELM_HISTORY, ACTION_HELM_ROLLBACK
from app.security.authorizer import Authorizer, dev_hint
from app.security.identity import Identity
from app.validation import validate_namespace, validate_pod_name, validate_release_name

__all__ = [
    "require_helm_history",
    "require_helm_rollback",
    "require_pod_get",
    "require_pod_list",
    "require_pod_logs",
]


# --- Pod 类：委托集群 RBAC -------------------------------------------------


def require_pod_list(
    namespace: str,
    identity: Identity = Depends(get_identity),
    authorizer: Authorizer = Depends(get_authorizer),
    api: AuthorizationV1ApiLike = Depends(get_authz_api),
) -> Identity:
    """列 Pod 需要 `list pods`。"""
    validate_namespace(namespace)
    authorizer.authorize(
        identity, verb="list", resource="pods", api=api, namespace=namespace
    )
    return identity


def require_pod_get(
    namespace: str,
    pod_name: str,
    identity: Identity = Depends(get_identity),
    authorizer: Authorizer = Depends(get_authorizer),
    api: AuthorizationV1ApiLike = Depends(get_authz_api),
) -> Identity:
    """看 Pod 详情需要 `get pods`。"""
    validate_namespace(namespace)
    validate_pod_name(pod_name)
    authorizer.authorize(
        identity, verb="get", resource="pods", api=api, namespace=namespace, name=pod_name
    )
    return identity


def require_pod_logs(
    namespace: str,
    pod_name: str,
    identity: Identity = Depends(get_identity),
    authorizer: Authorizer = Depends(get_authorizer),
    api: AuthorizationV1ApiLike = Depends(get_authz_api),
) -> Identity:
    """读日志需要 `get pods/log`。

    `pods/log` 是**独立的子资源**，在 RBAC 里可以单独授予。运维场景里
    「能看 Pod 状态但不能看日志」是很常见的需求（日志里常有凭据、PII）。
    所以这里必须把 `subresource="log"` 显式传下去——漏了它，判定会退化成
    「能不能读 Pod 对象」，既可能误拒（只有 log 权限的人被挡）也可能越权放行
    （有 pods 读权限但被明确禁掉 log 的人反而看到了日志）。
    """
    validate_namespace(namespace)
    validate_pod_name(pod_name)
    authorizer.authorize(
        identity,
        verb="get",
        resource="pods",
        api=api,
        namespace=namespace,
        subresource="log",
        name=pod_name,
    )
    return identity


# --- Helm 类：走策略引擎 ---------------------------------------------------

# 为什么 Helm 不用 SAR：SAR 判定的是「能不能对某个 K8s 资源做某个动作」，
# 而 Helm 的语义单位是 release。回滚 release 在集群里表现为对一批
# Deployment/Secret 的写入，SAR 无法表达「只允许回滚 staging 的 web」——
# 它连 release 这个概念都不存在。所以 Helm 必须走策略引擎。
#
# 代价要说清楚：**策略文件不是安全边界，服务账号的 RBAC 才是。**
# 策略只决定「我们愿不愿意替调用方执行」，真正能不能改动集群资源，
# 仍然取决于本服务 ServiceAccount 的权限。


def require_helm_history(
    namespace: str,
    release_name: str,
    identity: Identity = Depends(get_identity),
    policy: PolicyEngine = Depends(get_policy_engine),
) -> Identity:
    validate_namespace(namespace)
    validate_release_name(release_name)
    _enforce_policy(
        policy,
        identity,
        action=ACTION_HELM_HISTORY,
        namespace=namespace,
        release=release_name,
    )
    return identity


def require_helm_rollback(
    namespace: str,
    release_name: str,
    identity: Identity = Depends(get_identity),
    policy: PolicyEngine = Depends(get_policy_engine),
) -> Identity:
    validate_namespace(namespace)
    validate_release_name(release_name)
    _enforce_policy(
        policy,
        identity,
        action=ACTION_HELM_ROLLBACK,
        namespace=namespace,
        release=release_name,
    )
    return identity


def _enforce_policy(
    policy: PolicyEngine,
    identity: Identity,
    *,
    action: str,
    namespace: str,
    release: str,
) -> None:
    decision = policy.evaluate(
        identity, action=action, namespace=namespace, release=release
    )
    if decision.allowed:
        return
    raise PermissionDenied(
        f"身份 {identity.username} 无权对 {namespace}/{release} 执行 {action}："
        f"{decision.reason}"
        + dev_hint(identity)
    )
