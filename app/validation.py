"""Kubernetes 资源名的合法性校验。

有两个作用：
1. 给出比 API Server 更早、更清楚的报错。
2. **安全**：这些值会被拼进 helm 的命令行。原实现不做任何校验，传
   `release_name="--kubeconfig=/tmp/x"` 会被 cobra 当成 flag 解析掉，
   等于可以劫持 helm 使用的 kubeconfig / context。
   下面的正则强制首字符必须是字母或数字，从根本上排除了以 `-` 开头的值。
"""

from __future__ import annotations

import re

from app.errors import BadRequest

__all__ = [
    "validate_namespace",
    "validate_pod_name",
    "validate_release_name",
]

# DNS-1123 label：小写字母、数字、连字符，首尾必须是字母或数字
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# DNS-1123 subdomain：在 label 的基础上允许点号分段
_DNS_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")

_NAMESPACE_MAX_LEN = 63
_RELEASE_MAX_LEN = 53  # Helm 自身的限制
_POD_NAME_MAX_LEN = 253


def _check(value: str, *, field: str, pattern: re.Pattern[str], max_len: int) -> str:
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{field} 不能为空")
    if len(value) > max_len:
        raise BadRequest(f"{field} 长度不能超过 {max_len}，当前 {len(value)}")
    if not pattern.match(value):
        raise BadRequest(
            f"{field} 不合法：只允许小写字母、数字和连字符，且首尾必须是字母或数字（收到 {value!r}）"
        )
    return value


def validate_namespace(namespace: str) -> str:
    """命名空间名，DNS-1123 label。"""
    return _check(namespace, field="namespace", pattern=_DNS_LABEL, max_len=_NAMESPACE_MAX_LEN)


def validate_release_name(release_name: str) -> str:
    """Helm release 名。比命名空间更严格（最长 53），同样是 DNS-1123 label。"""
    return _check(
        release_name, field="release_name", pattern=_DNS_LABEL, max_len=_RELEASE_MAX_LEN
    )


def validate_pod_name(pod_name: str) -> str:
    """Pod 名，DNS-1123 subdomain。"""
    return _check(pod_name, field="pod_name", pattern=_DNS_SUBDOMAIN, max_len=_POD_NAME_MAX_LEN)
