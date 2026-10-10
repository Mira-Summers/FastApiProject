"""结构化日志。

只用标准库实现，不引入 structlog：
- request_id 通过 ContextVar 沿调用链传递，无需逐层传参
- JsonFormatter 会把 LogRecord 上的自定义 extra 字段一并输出

注意：本模块只配置 `app` 这一棵 logger 树，不去动 uvicorn 自己的 handler，
避免和 uvicorn 的日志配置互相打架。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, Dict, Optional

__all__ = [
    "configure_logging",
    "get_request_id",
    "new_request_id",
    "reset_request_id",
    "sanitize_request_id",
    "set_request_id",
]

REQUEST_ID_MAX_LEN = 64
# 只允许这些字符，防止调用方通过 X-Request-ID 注入换行来伪造日志行。
_UNSAFE_REQUEST_ID_CHARS = re.compile(r"[^A-Za-z0-9._:+-]")

_request_id: ContextVar[str] = ContextVar("request_id", default="-")

# LogRecord 自带的字段，输出时排除掉，剩下的就是调用方传的 extra。
_RESERVED_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
    "request_id",
}


def new_request_id() -> str:
    return uuid.uuid4().hex


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: Optional[str] = None) -> Token:
    return _request_id.set(value or new_request_id())


def reset_request_id(token: Token) -> None:
    _request_id.reset(token)


def sanitize_request_id(raw: Optional[str]) -> str:
    """把调用方传来的 X-Request-ID 收敛成安全字符集；不可用时退回新生成的 id。"""
    if not raw:
        return new_request_id()
    cleaned = _UNSAFE_REQUEST_ID_CHARS.sub("", raw.strip())[:REQUEST_ID_MAX_LEN]
    return cleaned or new_request_id()


def _timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, tz=timezone.utc).isoformat(timespec="milliseconds")


def _extra_fields(record: logging.LogRecord) -> Dict[str, Any]:
    return {key: value for key, value in record.__dict__.items() if key not in _RESERVED_FIELDS}


class JsonFormatter(logging.Formatter):
    """每行一条 JSON，便于日志采集器直接解析。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": _timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
        }
        payload.update(_extra_fields(record))

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # default=str 兜底，避免某个 extra 不可序列化时把日志本身打挂。
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """本地开发用的可读格式，带上 request_id 方便串起一次请求。"""

    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", None) or get_request_id()
        base = (
            f"{_timestamp(record.created)} {record.levelname:<8} "
            f"[{request_id[:8]}] {record.name}: {record.getMessage()}"
        )
        extras = _extra_fields(record)
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in extras.items())
            base = f"{base} | {rendered}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """配置 `app` logger 树。可重复调用（测试里会多次建 app）。"""
    logger = logging.getLogger("app")
    logger.setLevel(level.upper())
    # 自己处理，不再往 root 冒泡，避免和 uvicorn / pytest 的 handler 重复输出。
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    logger.addHandler(handler)
