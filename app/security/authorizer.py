"""资源级授权：把判定委托给集群 RBAC。

**为什么是 SubjectAccessReview 而不是自己写一张权限表。**
自己维护「谁能看哪些 namespace」意味着要重新实现一遍 RBAC：角色继承、
聚合规则、组绑定、默认权限……而且会和集群里已有的角色定义长期漂移。
SAR 让集群用它自己的规则回答「这个身份能不能对这个资源做这个动作」，
答案天然和 `kubectl auth can-i` 一致，运维不需要学第二套模型。

**但 SAR 有一个致命的语义陷阱，必须写清楚：**

    SAR 回答的是「调用方**自己**能不能做这件事」，
    而真实调用用的是**本服务 ServiceAccount** 的身份。

所以 SAR 通过 ≠ 操作一定成功，SAR 只是**前置闸门**。真实调用返回的 403
必须原样映射成 403（见 `app/errors.py` 的 `from_api_exception`），
绝不能因为 SAR 放行了就把真实调用的失败当成 5xx 或干脆吞掉。
反过来说，这也意味着本服务的 SA 权限就是爆炸半径的上限：
即使 SAR 说某个用户有权限，SA 自己没权限的话操作照样失败。
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, runtime_checkable

from kubernetes.client import (
    V1ResourceAttributes,
    V1SubjectAccessReview,
    V1SubjectAccessReviewSpec,
)
from kubernetes.client.rest import ApiException

from app.config import Settings
from app.errors import PermissionDenied, ServiceUnavailable, from_api_exception
from app.k8s.client import AuthorizationV1ApiLike
from app.security.identity import Identity

logger = logging.getLogger("app.security")

__all__ = ["Authorizer", "SarAuthorizer", "dev_hint"]

# 核心 API 组（Pod、日志等）的组名是空字符串，不是 "core"。
# 必须显式传，否则请求会落到「不指定组」的语义上，匹配不到 apiGroups: [""] 的规则。
CORE_API_GROUP = ""


@runtime_checkable
class Authorizer(Protocol):
    def authorize(
        self,
        identity: Identity,
        *,
        verb: str,
        resource: str,
        api: AuthorizationV1ApiLike,
        namespace: Optional[str] = None,
        subresource: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None: ...


class SarAuthorizer:
    """用 SubjectAccessReview 判定资源权限。不通过则抛 `PermissionDenied`。"""

    def __init__(self, settings: Settings) -> None:
        self._request_timeout = settings.k8s_request_timeout

    def authorize(
        self,
        identity: Identity,
        *,
        verb: str,
        resource: str,
        api: AuthorizationV1ApiLike,
        namespace: Optional[str] = None,
        subresource: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        spec = V1SubjectAccessReviewSpec(
            # user 和 groups 都要传。RBAC 里绝大多数授权是绑到组上的
            # （system:authenticated、自定义的运维组），只传 user 会让这些
            # 授权全部落空，表现为「明明配了权限却一直 403」。
            user=identity.username,
            groups=list(identity.groups) or None,
            resource_attributes=V1ResourceAttributes(
                group=CORE_API_GROUP,
                namespace=namespace,
                verb=verb,
                resource=resource,
                # subresource 必须显式传：漏了它，判定的就是 pods 而不是 pods/log，
                # 而权限模型里 pods/log 是可以单独授予（也很可能只授了它）的。
                # 漏传的后果是「读日志」被按「读 Pod 对象」来判定，两者权限不同，
                # 会既产生误拒也产生越权放行。
                subresource=subresource,
                name=name,
            ),
        )

        try:
            review = api.create_subject_access_review(
                body=V1SubjectAccessReview(spec=spec), _request_timeout=self._request_timeout
            )
        except ApiException as exc:
            if exc.status == 403:
                raise PermissionDenied(
                    "本服务账号无权创建 SubjectAccessReview，无法判定调用方权限。"
                    "请为服务账号绑定 system:auth-delegator 权限。"
                ) from exc
            if exc.status == 401:
                raise ServiceUnavailable("集群拒绝了本服务的身份，无法判定调用方权限") from exc
            raise from_api_exception(exc, context="调用 SubjectAccessReview 失败") from exc
        except Exception as exc:  # noqa: BLE001 —— 连接层失败，同 TokenReview 的处理
            raise ServiceUnavailable(
                f"无法连接 API Server 判定权限（{type(exc).__name__}）"
            ) from exc

        self._enforce(
            review,
            identity,
            verb=verb,
            resource=resource,
            subresource=subresource,
            namespace=namespace,
            name=name,
        )

    def _enforce(
        self,
        review: object,
        identity: Identity,
        *,
        verb: str,
        resource: str,
        subresource: Optional[str],
        namespace: Optional[str],
        name: Optional[str],
    ) -> None:
        status = getattr(review, "status", None)
        allowed = bool(getattr(status, "allowed", False))
        evaluation_error = getattr(status, "evaluationError", None)
        target = _describe(
            verb=verb,
            resource=resource,
            subresource=subresource,
            namespace=namespace,
            name=name,
        )

        # 求值失败先于 allowed 判定：集群说「我没算出来」的时候，
        # 无论它同时还说了什么，答案都只能是拒绝。
        if evaluation_error:
            # 这是运维侧的配置问题，但对外仍然只能是拒绝——放行一个
            # 「权限未知」的请求比拒绝一个「本该允许」的请求危险得多。
            logger.error(
                "SubjectAccessReview 求值失败",
                extra={
                    "event": "authz_evaluation_error",
                    "username": identity.username,
                    "target": target,
                    "evaluation_error": evaluation_error,
                },
            )
            # evaluation_error 可能带着集群内部的角色名与绑定关系，只进日志。
            raise PermissionDenied(f"拒绝访问：{target}（集群 RBAC 求值失败，请联系集群管理员）")

        if allowed:
            return

        logger.info(
            "授权被拒绝",
            extra={
                "event": "authz_denied",
                "username": identity.username,
                "groups": list(identity.groups),
                "target": target,
            },
        )
        raise PermissionDenied(
            f"身份 {identity.username} 没有 {target} 的权限"
            + dev_hint(identity)
        )


def dev_hint(identity: Identity) -> str:
    """免认证模式下额外提示「认证关了但授权没关」。

    这是本地开发最容易卡住的地方：以为 AUTH_MODE=disabled 就能畅通无阻，
    结果每个请求都 403。与其让人去翻代码，不如把话直接写在错误里。
    """
    if identity.method != "disabled":
        return ""
    return (
        f"（当前 AUTH_MODE=disabled，认证已关闭但授权仍然生效；"
        f"本地调试请给 {identity.username} 绑定相应 Role）"
    )


def _describe(
    *,
    verb: str,
    resource: str,
    subresource: Optional[str],
    namespace: Optional[str],
    name: Optional[str],
) -> str:
    """拼出人能读的目标描述，用于错误消息与日志。"""
    target = f"{resource}/{subresource}" if subresource else resource
    if name:
        target = f"{target} {name}"
    if namespace:
        target = f"{target}（namespace={namespace}）"
    return f"{verb} {target}"
