"""Pod 日志接口测试。"""

from __future__ import annotations

from tests.fakes import make_api_exception

LOGS_URL = "/api/v1/namespaces/{namespace}/pods/{pod}/logs"


def test_logs_are_returned_as_lines(client, core_api):
    """回归护栏：原实现写的是 `log = read_namespaced_pod_log(...)` 却 `return logs.split()`,
    变量名对不上，这个接口从来没有成功返回过。"""
    core_api.log_text = "starting\nlistening on :8080\nready"

    response = client.get(LOGS_URL.format(namespace="default", pod="web-abc"))

    assert response.status_code == 200
    body = response.json()
    assert body["lines"] == ["starting", "listening on :8080", "ready"]
    assert body["line_count"] == 3
    assert body["pod_name"] == "web-abc"


def test_crlf_logs_do_not_keep_carriage_returns(client, core_api):
    core_api.log_text = "line1\r\nline2\r\n"

    body = client.get(LOGS_URL.format(namespace="default", pod="web-abc")).json()

    assert body["lines"] == ["line1", "line2"]


def test_empty_log_returns_empty_list(client, core_api):
    core_api.log_text = ""

    body = client.get(LOGS_URL.format(namespace="default", pod="web-abc")).json()

    assert body["lines"] == []
    assert body["line_count"] == 0


def test_previous_and_container_are_forwarded_to_the_cluster(client, core_api):
    """CrashLoopBackOff 的容器当前起不来，必须能读上一个容器的日志。"""
    core_api.log_text = "panic: nil pointer"

    response = client.get(
        LOGS_URL.format(namespace="default", pod="web-abc"),
        params={"previous": True, "container": "app", "tail_lines": 50, "since_seconds": 600,
                "timestamps": True},
    )

    assert response.status_code == 200
    _, kwargs = core_api.calls[0]
    assert kwargs["previous"] is True
    assert kwargs["container"] == "app"
    assert kwargs["tail_lines"] == 50
    assert kwargs["since_seconds"] == 600
    assert kwargs["timestamps"] is True
    assert response.json()["previous"] is True


def test_previous_defaults_to_false(client, core_api):
    client.get(LOGS_URL.format(namespace="default", pod="web-abc"))

    _, kwargs = core_api.calls[0]
    assert "previous" not in kwargs


def test_missing_pod_logs_map_to_404(client, core_api):
    core_api.error = make_api_exception(404, "Not Found")

    response = client.get(LOGS_URL.format(namespace="default", pod="nope"))

    assert response.status_code == 404


def test_tail_lines_out_of_range_is_422(client, core_api):
    response = client.get(
        LOGS_URL.format(namespace="default", pod="web-abc"), params={"tail_lines": 0}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert core_api.calls == []


def test_old_broken_log_path_no_longer_exists(client):
    """旧的 `/pods/{pod_name}/pods` 路径已按计划移除（破坏性变更）。"""
    response = client.get("/pods/web-abc/pods")

    assert response.status_code == 404
