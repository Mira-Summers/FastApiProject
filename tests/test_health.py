"""探针语义。

这组测试守的是一条运维红线：**liveness 绝不依赖集群**。
把这条搞错的后果不是「服务挂了」，而是「集群一抖，kubelet 把所有健康副本一起杀掉」，
上游故障被放大成自己的故障。
"""

from __future__ import annotations

from tests.fakes import make_api_exception


def test_root_still_serves_service_info(client):
    body = client.get("/").json()

    assert body["name"] == "k8s-ops-api"
    assert body["env"] == "development"


# --------------------------------------------------------------------------- liveness


def test_liveness_returns_ok_without_touching_the_cluster(client, core_api):
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert core_api.calls == [], "liveness 探针碰了 k8s API，集群抖动时会引发重启雪崩"


def test_liveness_stays_healthy_while_the_cluster_is_broken(client, core_api):
    """回归护栏：这里必须仍然是 200。返回非 2xx 就等于让 kubelet 杀 Pod。"""
    core_api.error = RuntimeError("集群完全不可达")

    assert client.get("/health/live").status_code == 200
    assert client.get("/").status_code == 200


# --------------------------------------------------------------------------- readiness


def test_readiness_probes_the_api_server_cheaply(client, core_api):
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert core_api.call_names() == ["list_namespace"]
    _, kwargs = core_api.calls[0]
    # limit=1：足够验证「网络可达 + 认证通过 + 授权通过」，又几乎不产生负载。
    assert kwargs["limit"] == 1
    # 探活本身必须有超时，否则 API Server 挂起会把请求线程一起占住。
    assert kwargs["_request_timeout"] > 0


def test_readiness_reports_503_when_the_api_server_errors(client, core_api):
    core_api.error = make_api_exception(500, "Internal Server Error")

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_readiness_reports_503_when_our_service_account_is_forbidden(client, core_api):
    """本服务账号没权限 = 我们服务不了请求。这是我们的问题，所以是 503 而不是 403。

    调用方自己的授权（Phase 3 的 SubjectAccessReview）是另一条独立的链路。
    """
    core_api.error = make_api_exception(403, "Forbidden")

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert "权限" in response.json()["error"]["message"]


def test_readiness_does_not_leak_connection_details(app, raw_client):
    """连接层异常不是 ApiException，SDK 会把 urllib3 的原始异常直接抛出来。"""

    class UnreachableApi:
        def list_namespace(self, **kwargs):
            raise OSError("Connection refused: 10.96.0.1:443")

    from app.dependencies import get_core_api

    app.dependency_overrides[get_core_api] = lambda: UnreachableApi()

    response = raw_client.get("/health/ready")

    assert response.status_code == 503
    assert "10.96.0.1" not in response.text
    assert "Connection refused" not in response.text


# --------------------------------------------------------------------------- 缓存


def test_readiness_result_is_cached_to_spare_the_api_server(client, core_api):
    """kubelet 每几秒探一次，几百个 Pod 就是每秒几十次请求。"""
    client.get("/health/ready")
    client.get("/health/ready")
    client.get("/health/ready")

    assert len(core_api.calls) == 1


def test_readiness_failure_is_cached_too(client, core_api):
    """反直觉但关键：API Server 挂掉时探针会更密集地重试，

    不缓存失败结果的话，探针自己就成了压垮它的最后一根稻草。
    """
    core_api.error = make_api_exception(500, "Internal Server Error")

    client.get("/health/ready")
    client.get("/health/ready")
    client.get("/health/ready")

    assert len(core_api.calls) == 1


def test_readiness_cache_can_be_disabled(make_client, core_api):
    """K8S_READY_CACHE_SECONDS=0 表示每次都真探，排障时用得上。"""
    with make_client(K8S_READY_CACHE_SECONDS="0") as client:
        client.get("/health/ready")
        client.get("/health/ready")

    assert len(core_api.calls) == 2
