"""Prometheus 指标。

测试一律断言**增量**而不是绝对值：指标是模块级单例，整个测试会话共享，
用绝对值会随执行顺序变化而飘。

另外这里有两类「基数护栏」测试——它们守的不是功能，而是「别把 Prometheus 打爆」。
"""

from __future__ import annotations

import pytest
from prometheus_client.parser import text_string_to_metric_families

from app.dependencies import get_core_api
from app.helm.runner import HelmResult
from app.metrics import (
    HELM_ROLLBACKS_TOTAL,
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS_TOTAL,
    INVALID_LABEL_VALUE,
    UNMATCHED_ROUTE,
    record_rollback,
)
from tests.fakes import make_api_exception

PODS_ROUTE = "/api/v1/namespaces/{namespace}/pods"
POD_ROUTE = "/api/v1/namespaces/{namespace}/pods/{pod_name}"
HELM_ROUTE = "/api/v1/namespaces/{namespace}/helm/releases/{release_name}/rollback"


def samples(client) -> list[tuple[str, dict[str, str], float]]:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return [
        (sample.name, dict(sample.labels), sample.value)
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    ]


def counter(client, name: str, **labels: str) -> float:
    """取某个标签组合下的计数值，不存在则为 0。"""
    for sample_name, sample_labels, value in samples(client):
        if sample_name == name and sample_labels == {k: str(v) for k, v in labels.items()}:
            return value
    return 0.0


def test_metrics_endpoint_is_reachable_without_a_token(client):
    """Prometheus 抓取时不带 token，这个端点必须保持免认证。"""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "k8s_ops_http_requests_total" in response.text


def test_content_type_declares_charset_exactly_once(client):
    """回归护栏：Starlette 会给 text/* 无条件追加 charset，

    而 prometheus_client 的常量里本来就有一个，两者叠加会产出重复参数
    （`...charset=utf-8; charset=utf-8`）。Go 的 mime.ParseMediaType 会因此报错，
    抓取端解析不了这个响应头。
    """
    content_type = client.get("/metrics").headers["content-type"]

    assert content_type.count("charset=") == 1
    # 格式版本必须保留，它是 Prometheus 判断用哪种解析器的依据。
    assert "version=0.0.4" in content_type
    assert content_type.startswith("text/plain")


def test_request_is_counted_under_the_route_template(client):
    """回归护栏：曾经用真实路径做标签，Pod 名会把标签基数撑爆。"""
    before = counter(
        client, "k8s_ops_http_requests_total", method="GET", route=PODS_ROUTE, status="200"
    )

    client.get("/api/v1/namespaces/default/pods")

    after = counter(
        client, "k8s_ops_http_requests_total", method="GET", route=PODS_ROUTE, status="200"
    )
    assert after - before == 1


def test_pod_name_never_appears_as_a_label_value(client):
    """Pod 会不断重建，一旦 Pod 名进了标签就等于时间序列无上限增长。"""
    client.get("/api/v1/namespaces/default/pods/web-7d9f8b6c4d-x2k9p")

    text = client.get("/metrics").text

    assert "web-7d9f8b6c4d-x2k9p" not in text
    assert POD_ROUTE in text


def test_unmatched_paths_collapse_into_one_series(client):
    """扫描器不需要任何权限就能造出无数条时间序列，这条护栏必须守住。"""
    before = counter(client, "k8s_ops_http_requests_total", method="GET", route=UNMATCHED_ROUTE, status="404")

    client.get("/totally/random/path/one")
    client.get("/another/random/path/two")

    after = counter(client, "k8s_ops_http_requests_total", method="GET", route=UNMATCHED_ROUTE, status="404")

    assert after - before == 2
    text = client.get("/metrics").text
    assert "/totally/random/path/one" not in text
    assert "/another/random/path/two" not in text


def test_error_status_is_recorded_on_the_route_template(client, core_api):
    """上游 403 必须记成 403 而不是 200，否则错误率告警形同虚设。"""
    core_api.error = make_api_exception(403, "Forbidden")
    before = counter(
        client, "k8s_ops_http_requests_total", method="GET", route=PODS_ROUTE, status="403"
    )

    response = client.get("/api/v1/namespaces/default/pods")

    assert response.status_code == 403
    assert (
        counter(client, "k8s_ops_http_requests_total", method="GET", route=PODS_ROUTE, status="403")
        - before
        == 1
    )


def test_duration_histogram_is_observed(client):
    before = counter(client, "k8s_ops_http_request_duration_seconds_count", method="GET", route=PODS_ROUTE)

    client.get("/api/v1/namespaces/default/pods")

    after = counter(client, "k8s_ops_http_request_duration_seconds_count", method="GET", route=PODS_ROUTE)
    assert after - before == 1


def test_histogram_buckets_cover_slow_rollbacks(client):
    """默认桶最大 10 秒，而 helm 回滚上限是 120 秒——那样所有回滚都会掉进 +Inf。"""
    bucket_bounds = {
        labels["le"]
        for name, labels, _ in samples(client)
        if name == "k8s_ops_http_request_duration_seconds_bucket"
    }

    assert "120.0" in bucket_bounds
    assert "+Inf" in bucket_bounds


def test_in_progress_gauge_returns_to_zero(client):
    client.get("/api/v1/namespaces/default/pods")

    for name, labels, value in samples(client):
        if name == "k8s_ops_http_requests_in_progress":
            assert value == 0.0, f"{labels} 在请求结束后没有归零"


def test_unhandled_exception_is_counted_as_500(app, raw_client):
    """call_next 抛异常时也必须记一笔，否则最严重的故障反而不出现在指标里。"""

    class ExplodingApi:
        def list_namespaced_pod(self, namespace, **kwargs):
            raise RuntimeError("boom")

    app.dependency_overrides[get_core_api] = lambda: ExplodingApi()
    before = counter(
        raw_client, "k8s_ops_http_requests_total", method="GET", route=PODS_ROUTE, status="500"
    )

    assert raw_client.get("/api/v1/namespaces/default/pods").status_code == 500

    assert (
        counter(
            raw_client, "k8s_ops_http_requests_total", method="GET", route=PODS_ROUTE, status="500"
        )
        - before
        == 1
    )


@pytest.mark.parametrize("path", ["/metrics", "/health/live", "/health/ready"])
def test_probe_and_scrape_traffic_is_not_metered(client, path):
    """kubelet 与 Prometheus 的固定频率流量会淹没真实业务请求。"""
    client.get(path)

    text = client.get("/metrics").text

    assert f'route="{path}"' not in text


# --------------------------------------------------------------------------- 业务指标


def test_successful_rollback_is_counted(client, helm_runner):
    helm_runner._result = HelmResult(0, "Rollback was a success!", "")
    before = counter(client, "k8s_ops_helm_rollbacks_total", namespace="default", result="succeeded")

    client.post(
        "/api/v1/namespaces/default/helm/releases/web/rollback", json={"confirm": True}
    )

    assert (
        counter(client, "k8s_ops_helm_rollbacks_total", namespace="default", result="succeeded")
        - before
        == 1
    )


def test_failed_rollback_is_counted_as_failed(client, helm_runner):
    helm_runner._result = HelmResult(1, "", "Error: cannot patch \"web\"")
    before = counter(client, "k8s_ops_helm_rollbacks_total", namespace="default", result="failed")

    response = client.post(
        "/api/v1/namespaces/default/helm/releases/web/rollback", json={"confirm": True}
    )

    assert response.status_code == 502
    assert (
        counter(client, "k8s_ops_helm_rollbacks_total", namespace="default", result="failed")
        - before
        == 1
    )


def test_invalid_namespace_cannot_expand_the_label_cardinality(client, helm_runner):
    """调用方完全控制 namespace 时，它绝不能成为标签值。

    Phase 3 之后非法 namespace 在守卫处就被 400 挡掉，压根走不到记账那一步，
    所以这里断言的是更强的一条性质：**不产生任何新的时间序列**。
    调用方不该能用请求路径来制造指标基数——这是不需要任何权限就能触发的放大路径。
    """
    response = client.post(
        "/api/v1/namespaces/NotAValidNamespace/helm/releases/web/rollback",
        json={"confirm": True},
    )

    assert response.status_code == 400
    assert "NotAValidNamespace" not in client.get("/metrics").text


def test_record_rollback_neutralises_unvalidated_namespaces():
    """`record_rollback` 自己也要兜底，不能假设调用方一定校验过。

    这是一个模块级入口，将来多一个调用点就可能多一处漏校验。
    把「标签值必须收敛成有限集合」这条性质放在指标模块内部，
    就不必依赖每个调用点都记得先校验。
    """
    before = (
        HELM_ROLLBACKS_TOTAL.labels(namespace=INVALID_LABEL_VALUE, result="failed")._value.get()
    )
    record_rollback("../../etc/passwd", "failed")

    assert (
        HELM_ROLLBACKS_TOTAL.labels(namespace=INVALID_LABEL_VALUE, result="failed")._value.get()
        - before
        == 1
    )


def test_all_metric_names_are_prefixed() -> None:
    """没有前缀的指标名很容易和集群里其他 exporter 撞车。"""
    for metric in (HTTP_REQUESTS_TOTAL, HTTP_REQUEST_DURATION, HELM_ROLLBACKS_TOTAL):
        assert metric._name.startswith("k8s_ops_")
