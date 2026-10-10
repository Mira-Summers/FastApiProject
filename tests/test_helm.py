"""Helm 接口与命令构造测试。"""

from __future__ import annotations

import json

import pytest

from app.errors import UpstreamTimeout
from app.helm.runner import HelmResult, build_clean_env

HELM_URL = "/api/v1/namespaces/{namespace}/helm/releases/{release}"


def _rollback(client, release: str = "web", namespace: str = "default", **payload):
    body = {"confirm": True, **payload}
    return client.post(f"{HELM_URL.format(namespace=namespace, release=release)}/rollback", json=body)


# --------------------------------------------------------------------------- 二次确认


def test_rollback_without_confirm_is_rejected(client, helm_runner):
    """回滚是不可逆的生产变更，不接受「忘了传确认」的请求。"""
    response = client.post(
        f"{HELM_URL.format(namespace='default', release='web')}/rollback", json={"revision": 3}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert helm_runner.commands == []


def test_rollback_with_explicit_false_confirm_is_rejected(client, helm_runner):
    response = client.post(
        f"{HELM_URL.format(namespace='default', release='web')}/rollback",
        json={"revision": 3, "confirm": False},
    )

    assert response.status_code == 400
    assert helm_runner.commands == []


# --------------------------------------------------------------------------- 参数注入


@pytest.mark.parametrize(
    "release",
    # 注意不含 `/`：路由的路径参数本身不能跨段，带了斜杠会先撞上 404 而不是这里要验的校验拒绝。
    ["--kubeconfig=evil", "-n", "--namespace=evil", "--kube-context=prod"],
)
def test_release_name_cannot_inject_helm_flags(client, helm_runner, release):
    """回归护栏：原实现不校验 release_name，`--kubeconfig=/tmp/x` 会被 cobra 当成 flag
    解析掉，等于可以劫持 helm 使用的 kubeconfig / 切换目标集群。"""
    response = _rollback(client, release=release)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert helm_runner.commands == [], "被拒绝的请求绝不能真的执行 helm"


@pytest.mark.parametrize("namespace", ["Default", "-bad", "has space", "a" * 64])
def test_invalid_namespace_in_path_is_rejected(client, helm_runner, namespace):
    response = _rollback(client, namespace=namespace)

    assert response.status_code == 400
    assert helm_runner.commands == []


def test_rollback_argv_separates_flags_from_positional_args(client, helm_runner):
    helm_runner._result = HelmResult(0, "Rollback was a success!", "")

    response = _rollback(client, release="web", revision=3)

    assert response.status_code == 200
    # `--` 之后一律按位置参数处理，是正则校验之外的第二道防线
    assert helm_runner.commands[0] == ["rollback", "-n", "default", "--", "web", "3"]


def test_rollback_without_revision_targets_previous(client, helm_runner):
    helm_runner._result = HelmResult(0, "ok", "")

    response = _rollback(client, release="web")

    assert response.status_code == 200
    assert helm_runner.commands[0] == ["rollback", "-n", "default", "--", "web"]
    assert response.json()["to_revision"] is None


# --------------------------------------------------------------------------- 失败语义


def test_rollback_failure_maps_to_502_with_helm_stderr(client, helm_runner):
    helm_runner._result = HelmResult(1, "", "Error: cannot patch \"web\" with kind Deployment")

    response = _rollback(client, release="web", revision=1)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"
    assert "cannot patch" in response.json()["error"]["message"]


def test_missing_release_maps_to_404_not_502(client, helm_runner):
    helm_runner._result = HelmResult(1, "", "Error: release: not found")

    response = _rollback(client, release="web", revision=1)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_rollback_timeout_warns_about_pending_rollback(client, helm_runner):
    """超时被杀时 release 可能停在 pending-rollback，必须如实说明而不是谎报成功。"""
    helm_runner.error = UpstreamTimeout("helm 执行超过 120 秒被终止")

    response = _rollback(client, release="web", revision=1)

    assert response.status_code == 504
    message = response.json()["error"]["message"]
    assert "pending-rollback" in message


def test_helm_not_installed_maps_to_503(client, helm_runner):
    from app.errors import ServiceUnavailable

    helm_runner.error = ServiceUnavailable("找不到 helm 可执行文件")

    response = _rollback(client, release="web", revision=1)

    assert response.status_code == 503


# --------------------------------------------------------------------------- 历史查询


def test_history_parses_helm_json_output(client, helm_runner):
    helm_runner._result = HelmResult(
        0,
        json.dumps(
            [
                {
                    "revision": 2,
                    "updated": "2026-01-02 03:04:05.000000 +0000 UTC",
                    "status": "deployed",
                    "chart": "web-1.2.0",
                    "app_version": "1.2.0",
                    "description": "Upgrade complete",
                },
                {"revision": 1, "status": "superseded", "chart": "web-1.1.0"},
            ]
        ),
        "",
    )

    response = client.get(f"{HELM_URL.format(namespace='default', release='web')}/history")

    assert response.status_code == 200
    revisions = response.json()["revisions"]
    assert [r["revision"] for r in revisions] == [2, 1]
    assert revisions[0]["status"] == "deployed"
    assert revisions[0]["app_version"] == "1.2.0"


def test_history_argv_is_safe(client, helm_runner):
    helm_runner._result = HelmResult(0, "[]", "")

    client.get(f"{HELM_URL.format(namespace='default', release='web')}/history")

    assert helm_runner.commands[0] == ["history", "-n", "default", "-o", "json", "--", "web"]


def test_history_missing_release_maps_to_404(client, helm_runner):
    helm_runner._result = HelmResult(1, "", "Error: release: not found")

    response = client.get(f"{HELM_URL.format(namespace='default', release='web')}/history")

    assert response.status_code == 404


def test_history_rejects_unparseable_output(client, helm_runner):
    helm_runner._result = HelmResult(0, "not json at all", "")

    response = client.get(f"{HELM_URL.format(namespace='default', release='web')}/history")

    assert response.status_code == 502


# --------------------------------------------------------------------------- 子进程环境


def test_clean_env_drops_kubeconfig_by_default():
    """KUBECONFIG 会把 helm 指到另一个集群，导致读 Pod 和回滚打的不是同一个集群。"""
    env = build_clean_env(
        {
            "PATH": "/usr/bin",
            "KUBECONFIG": "/home/ops/other-cluster.yaml",
            "AWS_SECRET_ACCESS_KEY": "should-not-leak",
        }
    )

    assert env["PATH"] == "/usr/bin"
    assert "KUBECONFIG" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_clean_env_keeps_incluster_discovery_vars():
    """集群内运行时 client-go 靠这些变量发现 API Server，剔掉 helm 就连不上了。"""
    env = build_clean_env(
        {
            "KUBERNETES_SERVICE_HOST": "10.96.0.1",
            "KUBERNETES_SERVICE_PORT": "443",
        }
    )

    assert env["KUBERNETES_SERVICE_HOST"] == "10.96.0.1"
    assert env["KUBERNETES_SERVICE_PORT"] == "443"


def test_clean_env_keeps_helm_prefixed_vars():
    env = build_clean_env({"HELM_CACHE_HOME": "/tmp/helm-cache", "OTHER": "x"})

    assert env["HELM_CACHE_HOME"] == "/tmp/helm-cache"
    assert "OTHER" not in env


def test_clean_env_can_inherit_kubeconfig_when_explicitly_enabled():
    env = build_clean_env({"KUBECONFIG": "/home/ops/kind.yaml"}, inherit_kubeconfig=True)

    assert env["KUBECONFIG"] == "/home/ops/kind.yaml"
