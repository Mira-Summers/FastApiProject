"""公共测试夹具。

核心原则：测试不依赖真实集群，也不读取开发机上的真实环境变量。
`app` 夹具默认就把 k8s 客户端、helm 执行器、AuthZ 客户端替换成假实现，
所以路由测试跑的是真实的业务逻辑（app/k8s/*、app/helm/*、app/security/*），
只是 SDK 是假的。

**认证默认被绕过，但授权没有。** `get_identity` 被替换成一个固定身份，
而 `get_authz_api` 换成假客户端后，`SarAuthorizer` 与 `PolicyEngine` 仍然真实执行。
这样绝大多数既有测试不必关心认证，而授权路径依然被覆盖到；
真正要测认证链路的测试用 `make_client(authenticate=False)`。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import (
    get_authz_api,
    get_core_api,
    get_core_api_loader,
    get_helm_runner,
    get_identity,
    get_policy_engine,
)
from app.main import create_app
from app.policy.engine import PolicyEngine
from app.policy.model import (
    ACTION_HELM_HISTORY,
    ACTION_HELM_ROLLBACK,
    Grant,
    PolicyDocument,
    Rule,
    Subject,
)
from app.security.identity import Identity
from tests.fakes import FakeAuthorizationV1Api, FakeCoreV1Api, FakeHelmRunner

TEST_USERNAME = "tester@example.com"


def fixed_identity() -> Identity:
    """默认的调用方身份。授权测试断言的就是这个名字。"""
    return Identity(
        username=TEST_USERNAME,
        uid="uid-tester",
        groups=("ops", "system:authenticated"),
        method="tokenreview",
    )


def allow_all_helm_policy() -> PolicyEngine:
    """放行全部 Helm 动作的策略，用于不关心策略的测试。"""
    return PolicyEngine(
        PolicyDocument(
            version=1,
            rules=[
                Rule(
                    subjects=[Subject(kind="User", name=TEST_USERNAME)],
                    allow=[
                        Grant(action=ACTION_HELM_HISTORY),
                        Grant(action=ACTION_HELM_ROLLBACK),
                    ],
                )
            ],
        )
    )


@pytest.fixture
def settings() -> Settings:
    """显式传入空环境，避免读到开发机上真实设置的变量而让测试结果飘忽。"""
    return Settings.from_env({})


@pytest.fixture
def core_api() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
def authz_api() -> FakeAuthorizationV1Api:
    return FakeAuthorizationV1Api()


@pytest.fixture
def helm_runner() -> FakeHelmRunner:
    return FakeHelmRunner()


def _install_overrides(
    application: FastAPI,
    *,
    core_api: FakeCoreV1Api,
    helm_runner: FakeHelmRunner,
    authz_api: FakeAuthorizationV1Api,
    authenticate: bool,
    allow_helm: bool,
) -> None:
    application.dependency_overrides[get_core_api] = lambda: core_api
    # 认证路径注入的是工厂（见 get_core_api_loader），所以两个都要替换，
    # 否则走真实认证器的测试会去连一个并不存在的集群。
    application.dependency_overrides[get_core_api_loader] = lambda: (lambda: core_api)
    application.dependency_overrides[get_helm_runner] = lambda: helm_runner
    application.dependency_overrides[get_authz_api] = lambda: authz_api
    if authenticate:
        application.dependency_overrides[get_identity] = fixed_identity
    if allow_helm:
        application.dependency_overrides[get_policy_engine] = allow_all_helm_policy


@pytest.fixture
def app(
    settings: Settings,
    core_api: FakeCoreV1Api,
    helm_runner: FakeHelmRunner,
    authz_api: FakeAuthorizationV1Api,
) -> FastAPI:
    application = create_app(settings)
    _install_overrides(
        application,
        core_api=core_api,
        helm_runner=helm_runner,
        authz_api=authz_api,
        authenticate=True,
        allow_helm=True,
    )
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # 用 with 进入上下文，才能触发 lifespan。
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def raw_client(app: FastAPI) -> Iterator[TestClient]:
    """不把服务端异常重新抛出来的客户端，用于断言 500 的响应体。"""
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def make_client(
    core_api: FakeCoreV1Api,
    helm_runner: FakeHelmRunner,
    authz_api: FakeAuthorizationV1Api,
) -> Callable[..., "contextmanager[TestClient]"]:
    """用自定义环境变量构造一个隔离的 app 并进入 lifespan。

    给的是环境变量而不是 Settings 对象：这样测试断言的是「用户实际会怎么配」，
    顺带把 `Settings.from_env` 的解析逻辑也一起覆盖了。

    两个关键字开关用于关闭默认替换：
    - `authenticate=False`：不替换 `get_identity`，从而走真实的认证器
      （TokenReview 或 disabled），用来测认证链路。
    - `allow_helm=False`：不替换 `get_policy_engine`，从而走真实的策略引擎，
      用来测策略授权。
    """

    @contextmanager
    def _make(
        *,
        authenticate: bool = True,
        allow_helm: bool = True,
        authz: Optional[FakeAuthorizationV1Api] = None,
        **env: str,
    ) -> Iterator[TestClient]:
        application = create_app(Settings.from_env(env))
        _install_overrides(
            application,
            core_api=core_api,
            helm_runner=helm_runner,
            authz_api=authz if authz is not None else authz_api,
            authenticate=authenticate,
            allow_helm=allow_helm,
        )
        with TestClient(application) as test_client:
            yield test_client

    return _make
