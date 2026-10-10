"""应用装配层面的冒烟测试与回归护栏。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.logging_setup import REQUEST_ID_MAX_LEN, sanitize_request_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_app_imports_without_cluster(tmp_path):
    """回归护栏：import 期绝不能加载 kubeconfig。

    原 k8s_client.py 在 import 期调用 load_config()，导致没有集群的机器上进程直接
    起不来。这里构造一个必然失败的 kubeconfig 环境再导入 app.main——只要有人在
    import 期去连集群，这个子进程就会崩，测试随之失败。
    """
    env = dict(os.environ)
    env["KUBECONFIG"] = str(tmp_path / "does-not-exist")
    env.pop("KUBERNETES_SERVICE_HOST", None)
    env.pop("KUBERNETES_PORT", None)

    result = subprocess.run(
        [sys.executable, "-c", "import app.main; print(app.main.app.title)"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"import app.main 失败：\n{result.stderr}"
    assert "K8s" in result.stdout


def test_liveness_does_not_require_cluster(client: TestClient):
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_service_info_returns_metadata(client: TestClient):
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "k8s-ops-api"
    assert body["env"] == "development"
    assert body["docs"] == "/docs"


def test_response_carries_request_id(client: TestClient):
    response = client.get("/health/live")

    assert response.headers["X-Request-ID"]


def test_client_supplied_request_id_is_echoed(client: TestClient):
    response = client.get("/health/live", headers={"X-Request-ID": "trace-abc-123"})

    assert response.headers["X-Request-ID"] == "trace-abc-123"


def test_request_id_is_sanitized_against_log_injection():
    """调用方不能用 X-Request-ID 注入换行来伪造日志行。"""
    assert sanitize_request_id("abc\r\ndef") == "abcdef"
    assert sanitize_request_id("a b\tc") == "abc"
    assert sanitize_request_id("keep.me:ok+1-2_3") == "keep.me:ok+1-2_3"


def test_request_id_falls_back_when_empty_or_oversized():
    assert sanitize_request_id(None)
    assert sanitize_request_id("")
    # 全是非法字符时不能退化成空字符串
    assert sanitize_request_id("!!!")
    assert len(sanitize_request_id("x" * 500)) == REQUEST_ID_MAX_LEN
