"""错误码映射矩阵。

原实现把所有异常都变成 500 + `str(e)`，两个后果：
- 调用方无法区分「我不该看这个」（403）和「服务坏了」（502），只能一律重试；
- 内部路径、API Server 地址被回显出去。

这里逐条钉住映射关系。`from_api_exception` 是所有 k8s 调用的唯一出口，
钉住它就等于钉住了所有接口的错误语义。
"""

from __future__ import annotations

import pytest

from app.errors import (
    Conflict,
    NotFound,
    OpsError,
    PermissionDenied,
    ServiceUnavailable,
    Unauthorized,
    UpstreamError,
    from_api_exception,
)
from tests.fakes import make_api_exception

PODS_URL = "/api/v1/namespaces/default/pods"


@pytest.mark.parametrize(
    ("upstream_status", "expected_type", "expected_status"),
    [
        (400, UpstreamError, 502),  # 400 是上游说我们请求错了，不是调用方错了
        (401, Unauthorized, 401),
        (403, PermissionDenied, 403),
        (404, NotFound, 404),
        (409, Conflict, 409),
        (422, UpstreamError, 502),
        (429, ServiceUnavailable, 503),  # 限流是「稍后重试」，不是「上游出错」
        (500, UpstreamError, 502),
        (502, UpstreamError, 502),
        (503, UpstreamError, 502),
        (418, UpstreamError, 502),
    ],
)
def test_upstream_status_maps_to_a_meaningful_domain_error(
    upstream_status, expected_type, expected_status
):
    mapped = from_api_exception(make_api_exception(upstream_status), context="测试")

    assert isinstance(mapped, expected_type)
    assert mapped.status_code == expected_status


def test_missing_status_is_treated_as_upstream_failure():
    """ApiException 的 status 可能是 None（比如连接层失败），不能因此崩掉。"""
    mapped = from_api_exception(make_api_exception(None), context="测试")

    assert mapped.status_code == 502


# --------------------------------------------------------------------------- 信息泄露


def test_5xx_never_echoes_the_upstream_reason():
    """回归护栏：5xx 带上 reason 就等于回显集群内部信息。"""
    secret = "server error at /var/run/secrets/kubernetes.io/serviceaccount/token"
    mapped = from_api_exception(make_api_exception(500, secret), context="测试")

    assert secret not in mapped.message
    assert "/var/run" not in mapped.message


def test_4xx_keeps_the_reason_because_it_helps_the_caller():
    """4xx 是调用方的问题，把原因说清楚才能自助排障。"""
    mapped = from_api_exception(make_api_exception(404, "pods \"web\" not found"), context="测试")

    assert "not found" in mapped.message


def test_context_is_always_prepended():
    mapped = from_api_exception(make_api_exception(404, "Not Found"), context="查询 Pod web-abc")

    assert mapped.message.startswith("查询 Pod web-abc")


# --------------------------------------------------------------------------- 路由层


def test_upstream_403_reaches_the_caller_as_403(client, core_api):
    """Phase 3 的 SAR 只是前置检查，真实调用的 403 仍必须如实透出。

    如果在这里被吞成 500 或 502，调用方会以为是自己写错了参数而反复重试。
    """
    core_api.error = make_api_exception(403, "Forbidden")

    response = client.get(PODS_URL)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_upstream_500_becomes_502_not_500(client, core_api):
    """502 说的是「上游坏了」，500 说的是「我坏了」——排障时指向完全不同的方向。"""
    core_api.error = make_api_exception(500, "Internal Server Error")

    response = client.get(PODS_URL)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


def test_throttling_becomes_503(client, core_api):
    core_api.error = make_api_exception(429, "Too Many Requests")

    response = client.get(PODS_URL)

    assert response.status_code == 503


# --------------------------------------------------------------------------- 统一错误体


@pytest.mark.parametrize("url", ["/api/v1/namespaces/default/nonexistent", "/nope"])
def test_unmatched_route_uses_the_same_error_envelope(client, url):
    """调用方不该为了解析错误准备两套逻辑（Starlette 默认返回 {"detail": ...}）。"""
    response = client.get(url)

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_method_not_allowed_keeps_the_allow_header(client):
    """405 收敛成统一结构，但不能丢掉 Allow——那是调用方唯一能自助纠错的线索。"""
    response = client.post(PODS_URL)

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers["allow"] == "GET"


def test_every_error_response_carries_a_request_id(client, core_api):
    core_api.error = make_api_exception(403, "Forbidden")

    for response in (client.get(PODS_URL), client.get("/nope"), client.post(PODS_URL)):
        assert response.json()["error"]["request_id"]
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_ops_error_hierarchy_is_consistent():
    """所有领域异常都必须自带 status_code 与 code，否则 handler 无法自我描述。"""
    for error_type in (
        PermissionDenied,
        NotFound,
        Conflict,
        ServiceUnavailable,
        Unauthorized,
        UpstreamError,
    ):
        error = error_type("x")
        assert isinstance(error, OpsError)
        assert isinstance(error.status_code, int)
        assert error.code
