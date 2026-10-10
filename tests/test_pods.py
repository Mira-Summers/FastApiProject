"""Pod 接口与状态提取测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.k8s.pods import build_pod_info, humanize_age
from tests.fakes import make_api_exception, make_container_status, make_pod

PODS_URL = "/api/v1/namespaces/{namespace}/pods"


# --------------------------------------------------------------------------- 越权回归


def test_list_pods_uses_the_requested_namespace(client, core_api):
    """回归护栏：原实现调的是 list_pod_for_all_namespaces()，namespace 参数形同虚设，
    等于 `/pods?namespace=prod` 会把整个集群的 Pod 都返回出去。"""
    core_api.pods = [make_pod(namespace="prod")]

    response = client.get(PODS_URL.format(namespace="prod"))

    assert response.status_code == 200
    method, kwargs = core_api.calls[0]
    assert method == "list_namespaced_pod"
    assert kwargs["namespace"] == "prod"


def test_list_pods_passes_filters_and_pagination(client, core_api):
    core_api.pods = [make_pod()]
    core_api.continue_token = "next-page"

    response = client.get(
        PODS_URL.format(namespace="default"),
        params={"label_selector": "app=web", "field_selector": "status.phase=Running",
                "limit": 5, "continue": "cursor-1"},
    )

    assert response.status_code == 200
    _, kwargs = core_api.calls[0]
    assert kwargs["label_selector"] == "app=web"
    assert kwargs["field_selector"] == "status.phase=Running"
    assert kwargs["limit"] == 5
    assert kwargs["_continue"] == "cursor-1"
    assert response.json()["continue_token"] == "next-page"


def test_list_pods_returns_an_envelope(client, core_api):
    core_api.pods = [make_pod(name="a"), make_pod(name="b")]

    body = client.get(PODS_URL.format(namespace="default")).json()

    assert body["namespace"] == "default"
    assert body["count"] == 2
    assert [item["name"] for item in body["items"]] == ["a", "b"]


def test_get_pod_detail(client, core_api):
    core_api.pods = [make_pod(name="web-abc123")]

    response = client.get(f"{PODS_URL.format(namespace='default')}/web-abc123")

    assert response.status_code == 200
    assert response.json()["name"] == "web-abc123"


# --------------------------------------------------------------------------- 错误码语义


def test_missing_pod_maps_to_404(client, core_api):
    core_api.error = make_api_exception(404, "Not Found")

    response = client.get(f"{PODS_URL.format(namespace='default')}/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_forbidden_maps_to_403_not_500(client, core_api):
    """原实现把所有异常一律转成 500，权限不足本应是 403。"""
    core_api.error = make_api_exception(403, "Forbidden")

    response = client.get(PODS_URL.format(namespace="default"))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_upstream_server_error_maps_to_502(client, core_api):
    core_api.error = make_api_exception(500, "Internal Server Error")

    response = client.get(PODS_URL.format(namespace="default"))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


@pytest.mark.parametrize("namespace", ["Default", "with_underscore", "-leading", "a" * 64])
def test_invalid_namespace_is_rejected_before_calling_cluster(client, core_api, namespace):
    response = client.get(PODS_URL.format(namespace=namespace))

    assert response.status_code == 400
    assert core_api.calls == []


# --------------------------------------------------------------------------- 状态提取


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (3660, "1h1m"),
        (86400, "1d"),
        (90000, "1d1h"),
    ],
)
def test_humanize_age(seconds, expected):
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert humanize_age(now - timedelta(seconds=seconds), now) == expected


def test_humanize_age_handles_missing_and_clock_skew():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)

    assert humanize_age(None, now) == "unknown"
    # 节点时钟比我们快时不能输出负数
    assert humanize_age(now + timedelta(seconds=30), now) == "0s"


def test_age_in_response_is_humanized_not_a_raw_timestamp(client, core_api):
    core_api.pods = [make_pod(age_seconds=7200)]

    item = client.get(PODS_URL.format(namespace="default")).json()["items"][0]

    assert item["age"] == "2h"


def test_restart_reason_prefers_last_terminated_over_waiting_state():
    """CrashLoopBackOff 只是现象，OOMKilled 才是根因，要优先报后者。"""
    pod = make_pod(
        containers=[
            make_container_status(
                name="app",
                restart_count=7,
                state="waiting",
                state_reason="CrashLoopBackOff",
                last_reason="OOMKilled",
                last_exit_code=137,
            )
        ]
    )

    info = build_pod_info(pod, datetime.now(timezone.utc))

    assert info.restarts == 7
    assert info.restart_reason == "app: OOMKilled，退出码 137"


def test_restart_reason_falls_back_to_waiting_state():
    pod = make_pod(
        containers=[
            make_container_status(
                name="app", restart_count=0, state="waiting", state_reason="ImagePullBackOff"
            )
        ]
    )

    info = build_pod_info(pod, datetime.now(timezone.utc))

    assert info.restart_reason == "app: ImagePullBackOff"


def test_healthy_pod_has_no_restart_reason():
    pod = make_pod(containers=[make_container_status()])

    info = build_pod_info(pod, datetime.now(timezone.utc))

    assert info.restart_reason is None
    assert info.ready == "1/1"


def test_ready_reports_partial_availability():
    pod = make_pod(
        containers=[
            make_container_status(name="app", ready=True),
            make_container_status(name="sidecar", ready=False),
        ]
    )

    info = build_pod_info(pod, datetime.now(timezone.utc))

    assert info.ready == "1/2"
