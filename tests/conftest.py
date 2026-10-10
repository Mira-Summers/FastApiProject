"""公共测试夹具。

核心原则：测试不依赖真实 k8s 集群，也不读取开发机上的真实环境变量。
"""

from __future__ import annotations

from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """显式传入空环境，避免读到开发机上真实设置的变量而让测试结果飘忽。"""
    return Settings.from_env({})


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # 用 with 进入上下文，才能触发 lifespan（启动/关闭日志与后续的客户端初始化）。
    with TestClient(app) as test_client:
        yield test_client
