"""统一错误响应与信息泄露防护。"""

from __future__ import annotations

from app.dependencies import get_core_api
from tests.fakes import make_api_exception


class ExplodingApi:
    """模拟抛出未预期异常的集群客户端。"""

    def __init__(self, message: str) -> None:
        self._message = message

    def list_namespaced_pod(self, namespace, **kwargs):
        raise RuntimeError(self._message)


def test_error_envelope_has_a_stable_shape(client, core_api):
    core_api.error = make_api_exception(404, "Not Found")

    body = client.get("/api/v1/namespaces/default/pods").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "detail", "request_id"}
    assert body["error"]["request_id"]


def test_error_request_id_matches_the_response_header(client, core_api):
    """错误体里的 request_id 必须能对上日志，否则排障时无法把两边串起来。"""
    core_api.error = make_api_exception(403, "Forbidden")

    response = client.get("/api/v1/namespaces/default/pods")

    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_unexpected_error_does_not_leak_internals(app, raw_client):
    """回归护栏：原实现把所有异常都 `str(e)` 回给调用方，等于泄露内部路径。"""
    secret = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    app.dependency_overrides[get_core_api] = lambda: ExplodingApi(secret)

    response = raw_client.get("/api/v1/namespaces/default/pods")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert secret not in response.text
    assert "RuntimeError" not in response.text


def test_validation_error_lists_offending_fields(client):
    response = client.get("/api/v1/namespaces/default/pods", params={"limit": 0})

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert any("limit" in field["field"] for field in body["detail"])


def test_unknown_route_returns_404(client):
    assert client.get("/api/v1/namespaces/default/nonexistent").status_code == 404
