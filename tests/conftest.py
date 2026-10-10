"""公共测试夹具。

核心原则：测试不依赖真实集群，也不读取开发机上的真实环境变量。
`app` 夹具默认就把 k8s 客户端和 helm 执行器替换成假实现，
所以路由测试跑的是真实的业务逻辑（app/k8s/*、app/helm/*），只是 SDK 是假的。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_core_api, get_helm_runner
from app.main import create_app
from tests.fakes import FakeCoreV1Api, FakeHelmRunner


@pytest.fixture
def settings() -> Settings:
    """显式传入空环境，避免读到开发机上真实设置的变量而让测试结果飘忽。"""
    return Settings.from_env({})


@pytest.fixture
def core_api() -> FakeCoreV1Api:
    return FakeCoreV1Api()


@pytest.fixture
def helm_runner() -> FakeHelmRunner:
    return FakeHelmRunner()


@pytest.fixture
def app(
    settings: Settings, core_api: FakeCoreV1Api, helm_runner: FakeHelmRunner
) -> FastAPI:
    application = create_app(settings)
    application.dependency_overrides[get_core_api] = lambda: core_api
    application.dependency_overrides[get_helm_runner] = lambda: helm_runner
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
    core_api: FakeCoreV1Api, helm_runner: FakeHelmRunner
) -> Callable[..., "contextmanager[TestClient]"]:
    """用自定义环境变量构造一个隔离的 app 并进入 lifespan。

    给的是环境变量而不是 Settings 对象：这样测试断言的是「用户实际会怎么配」，
    顺带把 `Settings.from_env` 的解析逻辑也一起覆盖了。
    """

    @contextmanager
    def _make(**env: str) -> Iterator[TestClient]:
        application = create_app(Settings.from_env(env))
        application.dependency_overrides[get_core_api] = lambda: core_api
        application.dependency_overrides[get_helm_runner] = lambda: helm_runner
        with TestClient(application) as test_client:
            yield test_client

    return _make
