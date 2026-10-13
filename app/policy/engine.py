"""策略求值。纯函数，不碰集群、不碰网络，因此可以穷举测试。

**fail-closed 是这一层的唯一设计目标**：任何「没想清楚」的分支都必须落到拒绝。
具体地——没有匹配的规则 → 拒绝；规则存在但没有对应 action 的 allow → 拒绝；
策略文件缺失 → 拒绝；Engine 内部出任何意外 → 拒绝（由调用方兜底）。

一条容易踩的坑：**subject 匹配是「或」关系，不是「与」。**
调用方同时属于多个 group，只要身份本身或任意一个 group 命中规则，
这条规则就适用于他。如果写成「身份和所有 group 都要命中」，
那么任何 group 成员都会被自己的其它 group 拖累，规则永远不生效——
而 fail-closed 会把这个 bug 表现成「配了规则但一直被拒」，很难查。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from app.policy.model import (
    WILDCARD,
    Grant,
    PolicyDocument,
    Rule,
)
from app.security.identity import Identity

__all__ = ["Decision", "PolicyEngine"]

# 拒绝原因。写成常量而不是就地拼字符串，是为了让调用方和测试能稳定引用，
# 也避免把「谁没有权限」这类信息拼进给外面看的消息里时口径不一致。
REASON_NO_RULES = "策略为空：未为任何主体配置 Helm 操作权限"
REASON_NO_MATCHING_RULE = "当前身份没有任何适用的授权规则"
REASON_ACTION_NOT_GRANTED = "适用规则中未授予该操作"
REASON_SCOPE_MISMATCH = "授权范围不包含目标命名空间或 release"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

    @property
    def denied(self) -> bool:
        return not self.allowed


@dataclass(frozen=True)
class _Request:
    action: str
    namespace: str
    release: Optional[str]


class PolicyEngine:
    """对 Helm 动作做授权判定。"""

    def __init__(self, document: PolicyDocument) -> None:
        self._document = document

    @property
    def document(self) -> PolicyDocument:
        return self._document

    def evaluate(
        self,
        identity: Identity,
        *,
        action: str,
        namespace: str,
        release: Optional[str] = None,
    ) -> Decision:
        request = _Request(action=action, namespace=namespace, release=release)

        if not self._document.rules:
            return Decision(allowed=False, reason=REASON_NO_RULES)

        applicable = [rule for rule in self._document.rules if _matches(rule, identity)]
        if not applicable:
            return Decision(allowed=False, reason=REASON_NO_MATCHING_RULE)

        # 只要有一条适用规则授予了该动作，就放行。多条规则是累加关系：
        # 运维给一个身份加权限时应该只需要「再加一条」，而不是去改已有的那条。
        has_action = False
        for rule in applicable:
            for grant in rule.allow:
                if grant.action != action:
                    continue
                has_action = True
                if _in_scope(grant, request):
                    return Decision(allowed=True, reason="")

        # 区分「压根没给这个动作」和「给了但范围不对」，因为这两者的处置方式不同：
        # 前者要找管理员加权限，后者多半是自己写错了 namespace 或 release 名。
        reason = REASON_SCOPE_MISMATCH if has_action else REASON_ACTION_NOT_GRANTED
        return Decision(allowed=False, reason=reason)


def _matches(rule: Rule, identity: Identity) -> bool:
    """身份本身或任意一个 group 命中即视为适用。"""
    return any(_subject_matches(subject.kind, subject.name, identity) for subject in rule.subjects)


def _subject_matches(kind: str, name: str, identity: Identity) -> bool:
    if kind == "User":
        return identity.username == name
    if kind == "Group":
        return name in identity.groups
    # 理论上不可达：Subject.kind 是 Literal["User", "Group"]，加载期已校验。
    # 但这里是授权路径，多写一行保守分支的代价远小于猜错的代价。
    return False


def _in_scope(grant: Grant, request: _Request) -> bool:
    if not _value_in(grant.namespaces, request.namespace):
        return False
    # release 为 None（例如只按 namespace 判定的动作）时，只校验 namespace。
    if request.release is None:
        return True
    return _value_in(grant.releases, request.release)


def _value_in(allowed: Iterable[str], value: str) -> bool:
    for candidate in allowed:
        if candidate == WILDCARD or candidate == value:
            return True
    return False
