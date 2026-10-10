"""审计日志。

审计和应用日志是两件事，不能混在一起：

- 应用日志服务于排障，可以采样、可以轮转掉、可以丢；
- 审计日志要回答的是「**谁**在**什么时候**对**哪个 release** 做了**什么**，结果如何」。
  它必须**先写意图、再做动作**——否则进程在动作执行到一半时被杀，
  这次不可逆的生产变更就没有任何记录，事后完全说不清楚。

所以回滚会写两条：`attempted` 和 `succeeded`/`failed`。

落盘的取舍：每条 append 后立刻 close（而不是常驻文件句柄）。
写入量极低（只有回滚会触发），而重新打开能天然接受外部轮转，
不用自己实现 logrotate 交互——运维工具没必要在这里追求吞吐。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.logging_setup import get_request_id
from app.security.identity import ANONYMOUS, Identity

logger = logging.getLogger("app.audit")

__all__ = ["AUDIT_OUTCOME_ATTEMPTED", "AUDIT_OUTCOME_FAILED", "AUDIT_OUTCOME_SUCCEEDED", "AuditEvent", "AuditLogger"]

AUDIT_OUTCOME_ATTEMPTED = "attempted"
AUDIT_OUTCOME_SUCCEEDED = "succeeded"
AUDIT_OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class AuditEvent:
    """一次受审计的操作。字段名就是审计文件的字段名，改这里等于改对外格式。"""

    action: str
    outcome: str
    resource: Mapping[str, Any]
    actor: Optional[Identity] = None
    detail: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "request_id": get_request_id(),
            "action": self.action,
            "outcome": self.outcome,
            "resource": dict(self.resource),
            "actor": (self.actor or ANONYMOUS).audit_fields(),
            "detail": self.detail,
        }


class AuditError(RuntimeError):
    """审计日志不可用。启动期抛出，直接拒绝启动。"""


class AuditLogger:
    """把审计事件同时写到结构化日志和（可选的）JSONL 文件。

    `path` 为空时只写日志。生产环境应当设置 `AUDIT_LOG_PATH`，
    让审计记录落到独立于应用日志的文件里，便于单独采集与保留。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or None
        self._lock = threading.Lock()
        if self._path:
            self._verify_writable(self._path)

    @property
    def path(self) -> Optional[str]:
        return self._path

    @staticmethod
    def _verify_writable(path: str) -> None:
        """启动期就验证可写性。

        审计是「静默失败」最危险的地方：如果等到第一次回滚才发现写不进去，
        你以为有记录，其实没有。宁可起不来。
        """
        try:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise AuditError(
                f"审计日志路径不可写：{path}（{exc.strerror or exc}）。"
                "审计不可用时不启动服务，避免回滚操作没有记录。"
            ) from exc

    def record(self, event: AuditEvent) -> None:
        """记录一条审计事件。本方法**不会抛异常**。"""
        payload = event.to_payload()

        # 结构化日志这一行是主记录：`event=audit` 让采集器能单独过滤出来。
        logger.info("审计事件", extra={"event": "audit", **payload})

        if self._path:
            self._append(payload)

    def _append(self, payload: Mapping[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            # 落盘失败必须刺眼，但不能让请求失败：动作可能已经真的执行了，
            # 对调用方谎报失败反而更糟，而且日志里已经有同样的事实。
            logger.exception(
                "审计日志落盘失败", extra={"event": "audit_write_failed", "path": self._path}
            )
