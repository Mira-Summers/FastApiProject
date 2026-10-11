"""依赖装配的唯一入口。

所有外部副作用（配置、k8s 客户端、helm 执行器、认证器、策略引擎）都从这里注入。
测试通过 `app.dependency_overrides` 替换这几个 provider，就能完全不依赖真实集群。

**这里也是「谁能碰集群」的唯一决定点。** 认证器与策略引擎都在应用启动时构造并挂在
`app.state` 上，而不是每次请求重建：
- 认证器持有 token 指纹缓存，必须进程内唯一，否则缓存形同虚设；
- 策略文件只在启动时读取一次（见 `app/config.py` 的 `policy_path`），
  每次请求重读文件会把「策略」变成一个可以被热改的运行时状态。

注意构造它们**不会**碰集群：集群客户端是在第一次真正需要时才由 `get_core_api` /
`get_authz_api` 构建的。这是本服务能在没有集群的机器上跑起来的前提。
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, Request

from app.audit import AuditLogger
from app.config import Settings
from app.helm.runner import HelmRunner
from app.k8s.client import (
    AuthorizationV1ApiLike,
    CoreV1ApiLike,
    build_authz_api,
    build_core_api,
)
from app.k8s.readiness import ReadinessProbe
from app.policy.engine import PolicyEngine
from app.security.authenticator import (
    MISSING_CREDENTIAL_MESSAGE,
    Authenticator,
    extract_bearer_token,
    unauthorized,
)
from app.security.authorizer import Authorizer
from app.security.disabled import DisabledAuthenticator
from app.security.identity import Identity
from app.security.tokenreview import TokenReviewAuthenticator

__all__ = [
    "get_audit_logger",
    "get_authenticator",
    "get_authz_api",
    "get_authorizer",
    "get_core_api",
    "get_core_api_loader",
    "get_helm_runner",
    "get_identity",
    "get_policy_engine",
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


def get_authz_api(settings: Settings = Depends(get_settings)) -> AuthorizationV1ApiLike:
    """SubjectAccessReview 用的客户端。同样懒加载。"""
    return build_authz_api(settings)


def get_helm_runner(settings: Settings = Depends(get_settings)) -> HelmRunner:
    return HelmRunner(settings)


def get_audit_logger(request: Request) -> AuditLogger:
    """审计器挂在 app 上而不是每次构造：它持有文件锁，必须是进程内唯一实例。"""
    return request.app.state.audit


def get_readiness_probe(request: Request) -> ReadinessProbe:
    """探活缓存挂在 app 上，测试里每个 app 一份，互不串味。"""
    return request.app.state.readiness


def get_authenticator(request: Request) -> Authenticator:
    return request.app.state.authenticator


def get_authorizer(request: Request) -> Authorizer:
    return request.app.state.authorizer


def get_policy_engine(request: Request) -> PolicyEngine:
    return request.app.state.policy


def get_core_api_loader(
    settings: Settings = Depends(get_settings),
) -> Callable[[], CoreV1ApiLike]:
    """返回一个**尚未建立连接**的客户端工厂。

    认证路径必须注入工厂而不是现成的客户端：FastAPI 会无条件解析依赖，
    直接注入 `get_core_api` 的写法会让一个**连 token 都没带**的请求也先去
    连一次集群。后果有两个，都不好受：

    - 状态码错了。本该是 401「请带上凭据」，实际变成 503「连不上集群」，
      把排障方向整个带偏；
    - 未认证流量变成放大器。任何人都能用不带凭据的请求逼本服务向
      API Server 发起连接。

    交给认证器自己决定何时调用，才能做到「有凭据要验，才去连」。
    """
    return lambda: build_core_api(settings)


def get_identity(
    request: Request,
    authenticator: Authenticator = Depends(get_authenticator),
    load_core: Callable[[], CoreV1ApiLike] = Depends(get_core_api_loader),
) -> Identity:
    """从 `Authorization: Bearer <token>` 解析出调用方身份。

    这是所有业务路由的强制前置：没有它，路由上的授权守卫就没有主体可判。

    这里刻意在调用认证器**之前**先把「没带凭据」判掉：
    这一步完全是本地的，不碰集群、不查缓存，所以一个没带 token 的请求
    会以最快的速度、且不产生任何集群副作用地拿到 401。
    """
    credential = extract_credential(request)
    if not credential and authenticator.requires_credential:
        raise unauthorized(MISSING_CREDENTIAL_MESSAGE)
    return authenticator.authenticate(credential, load_api=load_core)


def extract_credential(request: Request) -> str:
    """取出 bearer token，取不到返回空串。

    返回空串而不是在这里抛 401，是为了把「没有凭据」和「凭据无效」的判定
    都收敛到认证器的语义里——`AUTH_MODE=disabled` 时压根不需要凭据，
    在依赖层无条件拦掉会让免认证模式失效。
    """
    return extract_bearer_token(request.headers.get("authorization")) or ""


def build_authenticator(settings: Settings) -> Authenticator:
    """按 `AUTH_MODE` 选择认证实现。

    刻意写成显式分支而不是注册表：只有两个实现，而且 `disabled` 的启用条件
    （三道锁）已经由 `Settings` 在启动期校验过了。这里多一层抽象，
    只会让「怎么就把认证关掉了」变得更难追。
    """
    if settings.auth_disabled:
        return DisabledAuthenticator(settings)
    return TokenReviewAuthenticator(settings)
