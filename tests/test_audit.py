"""审计日志。

重点不是「有没有写」，而是三条性质：
- 顺序：**先写意图再动手**，进程中途被杀也要留下痕迹；
- 内容：字段是封闭集合，不夹带 token / header 之类的敏感信息；
- 韧性：审计写入失败不能让已经执行的变更对调用方谎报失败。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from app.audit import (
    AUDIT_OUTCOME_ATTEMPTED,
    AuditError,
    AuditEvent,
    AuditLogger,
)
from app.config import Settings
from app.helm.runner import HelmResult
from app.main import create_app

ROLLBACK_URL = "/api/v1/namespaces/default/helm/releases/web/rollback"

# 审计记录的字段是一份对外格式约定，多一个少一个都应当是有意识的选择。
EXPECTED_FIELDS = {"ts", "request_id", "action", "outcome", "resource", "actor", "detail"}


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_app_logs(name: str) -> Iterator[list[logging.LogRecord]]:
    """直接挂 handler 抓日志。

    不能用 pytest 的 caplog：`configure_logging` 刻意把 `app` 这棵 logger 树的
    `propagate` 设成了 False，记录不会冒泡到 root，caplog 的 handler 什么也收不到。
    """
    handler = _CaptureHandler()
    target = logging.getLogger(name)
    previous_level = target.level
    target.setLevel(logging.DEBUG)
    target.addHandler(handler)
    try:
        yield handler.records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


def test_rollback_writes_intent_before_result(make_client, helm_runner, audit_path):
    """顺序是硬要求：先 attempted 再 succeeded，不能反。"""
    helm_runner._result = HelmResult(0, "Rollback was a success!", "")

    with make_client(AUDIT_LOG_PATH=str(audit_path)) as client:
        response = client.post(ROLLBACK_URL, json={"confirm": True, "revision": 3})

    assert response.status_code == 200
    events = read_events(audit_path)
    assert [e["outcome"] for e in events] == ["attempted", "succeeded"]
    assert all(e["action"] == "helm.rollback" for e in events)
    assert events[0]["resource"] == {
        "namespace": "default",
        "release": "web",
        "requested_revision": 3,
    }
    # 成功那条要把它实际回滚到的版本也记下来，否则事后对不上账。
    assert events[1]["resource"]["to_revision"] == 3


def test_failed_rollback_records_the_reason(make_client, helm_runner, audit_path):
    """失败的尝试同样要留档——只记成功的审计等于没有审计。"""
    helm_runner._result = HelmResult(1, "", "Error: cannot patch \"web\"")

    with make_client(AUDIT_LOG_PATH=str(audit_path)) as client:
        response = client.post(ROLLBACK_URL, json={"confirm": True})

    assert response.status_code == 502
    events = read_events(audit_path)
    assert [e["outcome"] for e in events] == ["attempted", "failed"]
    assert "cannot patch" in events[1]["detail"]


def test_rejected_rollback_is_not_audited_as_an_action(make_client, audit_path):
    """缺 confirm 的请求压根不是一次回滚动作，不该在审计里制造噪音。"""
    with make_client(AUDIT_LOG_PATH=str(audit_path)) as client:
        response = client.post(ROLLBACK_URL, json={"revision": 3})

    assert response.status_code == 400
    assert read_events(audit_path) == []


def test_audit_record_has_a_closed_set_of_fields(make_client, helm_runner, audit_path):
    """护栏：防止有人往审计里塞 token、header 或整个 request 对象。"""
    helm_runner._result = HelmResult(0, "ok", "")

    with make_client(AUDIT_LOG_PATH=str(audit_path)) as client:
        client.post(ROLLBACK_URL, json={"confirm": True})

    for event in read_events(audit_path):
        assert set(event) == EXPECTED_FIELDS
        assert set(event["actor"]) == {"username", "uid", "groups", "method"}


def test_actor_is_the_authenticated_identity(make_client, helm_runner, audit_path):
    """审计里的 actor 必须是**通过认证的那张身份**，而不是匿名的占位值。

    这条链路的每一环都可能断掉：认证器解析出了身份、守卫把它返回了、
    路由把它传给了审计事件。任何一环漏掉，审计就会退化成「有人回滚了」
    这种毫无用处的记录。
    """
    from tests.conftest import TEST_USERNAME

    helm_runner._result = HelmResult(0, "ok", "")

    with make_client(AUDIT_LOG_PATH=str(audit_path)) as client:
        client.post(ROLLBACK_URL, json={"confirm": True})

    actor = read_events(audit_path)[0]["actor"]
    assert actor["username"] == TEST_USERNAME
    assert actor["method"] == "tokenreview"
    # 字段是封闭集合：审计里不允许出现任何计划外的键，否则很容易在某次改动中
    # 「顺手」把凭据或原始 TokenReview 响应塞进来，而且不会有任何东西报警。
    assert set(actor) == {"username", "uid", "groups", "method"}


def test_audit_request_id_matches_the_response_header(make_client, helm_runner, audit_path):
    """审计必须能和访问日志、错误响应串成一条线，否则排障时三份记录对不上。"""
    helm_runner._result = HelmResult(0, "ok", "")

    with make_client(AUDIT_LOG_PATH=str(audit_path)) as client:
        response = client.post(ROLLBACK_URL, json={"confirm": True})

    request_id = response.headers["X-Request-ID"]
    assert all(e["request_id"] == request_id for e in read_events(audit_path))


def test_unwritable_audit_path_refuses_to_start(tmp_path):
    """审计静默失败最危险：你以为有记录，其实没有。所以宁可起不来。"""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")

    with pytest.raises(AuditError):
        AuditLogger(str(blocker / "audit.jsonl"))


def test_create_app_refuses_to_start_with_an_unwritable_audit_path(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")

    with pytest.raises(AuditError):
        create_app(Settings.from_env({"AUDIT_LOG_PATH": str(blocker / "audit.jsonl")}))


def test_audit_write_failure_does_not_break_the_rollback(make_client, helm_runner, audit_path):
    """回滚可能已经真的执行了。因为审计落盘失败就报错，等于对调用方撒谎。"""
    helm_runner._result = HelmResult(0, "Rollback was a success!", "")

    with make_client(AUDIT_LOG_PATH=str(audit_path)) as client:
        # 启动后把文件换成目录，模拟磁盘满 / 权限被改 / 被外部轮转掉。
        audit_path.unlink()
        audit_path.mkdir()

        response = client.post(ROLLBACK_URL, json={"confirm": True})

    assert response.status_code == 200


def test_newline_in_release_name_never_reaches_the_audit_file(make_client, audit_path):
    """带换行的 release 名在守卫处就被拒掉，压根进不了审计。

    「JSONL 里的换行必须被转义」这条性质由下面的
    `test_newline_in_a_resource_value_cannot_forge_an_audit_line` 直接覆盖；
    这里断言的是更前面的一道防线：非法名字不会变成审计记录里的一个字段，
    因此也就不存在「先落库再清理」的机会。
    """
    with make_client(AUDIT_LOG_PATH=str(audit_path)) as client:
        response = client.post(
            "/api/v1/namespaces/default/helm/releases/web%0Afake/rollback",
            json={"confirm": True},
        )

    assert response.status_code == 400
    assert read_events(audit_path) == []


def test_newline_in_a_resource_value_cannot_forge_an_audit_line(audit_path):
    """JSON 编码本身就是防注入手段：换行会被转义，不会伪造出额外一行。

    只要 resource 里有可能来自外部的值（release 名、Pod 名、调用方传入的
    任意字符串），这个性质就必须成立——审计文件一旦能被伪造行，
    「谁回滚了什么」就不再可信。
    """
    logger = AuditLogger(str(audit_path))
    logger.record(
        AuditEvent(
            action="helm.rollback",
            outcome=AUDIT_OUTCOME_ATTEMPTED,
            resource={"release": "web\nfake"},
        )
    )

    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["resource"]["release"] == "web\nfake"


def test_audit_goes_to_the_structured_log_when_no_path_is_configured():
    """没配 AUDIT_LOG_PATH 时审计不能凭空消失，至少要进结构化日志。

    日志这一行才是主记录（`event=audit` 让采集器能单独过滤），文件是补充。
    """
    audit = AuditLogger(None)

    with capture_app_logs("app.audit") as records:
        audit.record(
            AuditEvent(action="helm.rollback", outcome=AUDIT_OUTCOME_ATTEMPTED, resource={"a": 1})
        )

    assert len(records) == 1
    record = records[0]
    assert record.event == "audit"
    # extra 里的字段必须真的挂在 LogRecord 上，JsonFormatter 才取得到。
    assert record.action == "helm.rollback"
    assert record.actor["username"] == "anonymous"
    assert record.request_id


def test_audit_file_is_created_at_startup(make_client, audit_path):
    """启动就把文件建出来，运维才好确认「审计确实在写」。"""
    with make_client(AUDIT_LOG_PATH=str(audit_path)):
        assert audit_path.exists()
