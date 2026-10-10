"""Helm 业务操作：查询历史与执行回滚。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.errors import NotFound, UpstreamError, UpstreamTimeout
from app.helm.runner import HelmResult, HelmRunner
from app.schemas.helm import HelmRevision
from app.validation import validate_namespace, validate_release_name

logger = logging.getLogger("app.helm")

__all__ = ["RollbackOutcome", "get_history", "rollback"]

_MAX_OUTPUT_CHARS = 2000

# helm 在 release 不存在时报的是 "release: not found"，语义上应该是 404 而不是 502。
_NOT_FOUND_MARKERS = ("not found", "no revisions", "not deployed")


@dataclass(frozen=True)
class RollbackOutcome:
    release: str
    namespace: str
    to_revision: Optional[int]
    message: str
    output: Optional[str]


def _truncate(text: str) -> str:
    """helm 的输出可能很长，截断后再放进响应，避免响应体失控。"""
    text = (text or "").strip()
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + "...(输出已截断)"


def _failure_detail(result: HelmResult) -> str:
    return _truncate(result.stderr) or _truncate(result.stdout) or f"helm 退出码 {result.returncode}"


def _raise_for_failure(result: HelmResult, *, action: str, release: str, namespace: str) -> None:
    detail = _failure_detail(result)
    if any(marker in detail.lower() for marker in _NOT_FOUND_MARKERS):
        raise NotFound(f"命名空间 {namespace} 下不存在 release {release}")
    raise UpstreamError(f"{action}失败：{detail}")


def _parse_revision(item: dict[str, Any]) -> HelmRevision:
    # helm 3 的 JSON 输出用小写下划线，但不同版本对 appVersion 的写法不完全一致，
    # 两种都兜住。
    return HelmRevision(
        revision=int(item.get("revision", 0)),
        updated=item.get("updated"),
        status=item.get("status") or "unknown",
        chart=item.get("chart"),
        app_version=item.get("app_version") or item.get("appVersion"),
        description=item.get("description"),
    )


def get_history(runner: HelmRunner, release_name: str, namespace: str) -> list[HelmRevision]:
    release_name = validate_release_name(release_name)
    namespace = validate_namespace(namespace)

    result = runner.run(["history", "-n", namespace, "-o", "json", "--", release_name])
    if not result.ok:
        _raise_for_failure(
            result, action=f"查询 {namespace}/{release_name} 的历史", release=release_name, namespace=namespace
        )

    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise UpstreamError("helm history 的输出无法解析为 JSON") from exc

    if not isinstance(raw, list):
        raise UpstreamError("helm history 的输出格式不符合预期")

    return [_parse_revision(item) for item in raw if isinstance(item, dict)]


def rollback(
    runner: HelmRunner,
    release_name: str,
    namespace: str,
    revision: Optional[int] = None,
) -> RollbackOutcome:
    release_name = validate_release_name(release_name)
    namespace = validate_namespace(namespace)

    # `--` 之后一律按位置参数处理，作为正则校验之外的第二道防线。
    args: list[str] = ["rollback", "-n", namespace, "--", release_name]
    if revision is not None:
        args.append(str(revision))

    try:
        result = runner.run(args)
    except UpstreamTimeout as exc:
        # 超时被杀时 helm 可能已经把 release 标成 pending-rollback 却还没写回新版本。
        # 如实说明，既不能谎报成功，也不能暗示可以无脑重试。
        raise UpstreamTimeout(
            f"回滚 {namespace}/{release_name} 超时，helm 进程已被终止。"
            "release 可能停留在 pending-rollback 状态，"
            "请先用 history 确认当前版本，再决定是否重试。"
        ) from exc

    if not result.ok:
        _raise_for_failure(
            result, action=f"回滚 {namespace}/{release_name}", release=release_name, namespace=namespace
        )

    return RollbackOutcome(
        release=release_name,
        namespace=namespace,
        to_revision=revision,
        message="回滚成功",
        output=_truncate(result.stdout) or None,
    )
