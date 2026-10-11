"""Helm 授权策略：加载校验与求值。

策略引擎是**唯一**决定「谁能回滚什么」的地方，所以这里覆盖的重点是
「默认拒绝」在各种奇怪输入下是否依然成立——包括文件损坏、字段写错、
身份不匹配、范围不匹配，以及最容易被忽略的一类：**规则存在但方向反了**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.policy.engine import (
    REASON_ACTION_NOT_GRANTED,
    REASON_NO_MATCHING_RULE,
    REASON_NO_RULES,
    REASON_SCOPE_MISMATCH,
    PolicyEngine,
)
from app.policy.model import (
    ACTION_HELM_HISTORY,
    ACTION_HELM_ROLLBACK,
    PolicyDocument,
    PolicyError,
    load_policy,
)
from app.security.identity import Identity


def identity(
    username: str = "alice@example.com",
    groups: tuple[str, ...] = ("ops",),
    method: str = "tokenreview",
) -> Identity:
    return Identity(username=username, groups=groups, method=method)


def engine(yaml_text: str, tmp_path: Path) -> PolicyEngine:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return PolicyEngine(load_policy(str(path)))


BASIC = """
version: 1
rules:
  - subjects:
      - kind: User
        name: alice@example.com
    allow:
      - action: helm.rollback
        namespaces: [staging]
        releases: [web]
"""


# --- 加载 -----------------------------------------------------------------


def test_missing_policy_file_denies_everything_but_says_so(tmp_path):
    """指向一个不存在的文件是配置事故，必须拒绝启动而不是静默变成空策略。

    这两者的后果差得很远：直接崩掉会立刻被人发现，而静默变空策略
    表现为「所有 helm 接口都 403」，排查方向完全不同。
    """
    with pytest.raises(PolicyError, match="策略文件不存在"):
        load_policy(str(tmp_path / "nope.yaml"))


def test_no_configured_path_yields_an_empty_policy():
    """没配 POLICY_PATH 是合法状态：等同于「本服务不提供 Helm 变更能力」。"""
    document = load_policy(None)
    assert document.rules == []


def test_malformed_yaml_is_rejected(tmp_path):
    with pytest.raises(PolicyError, match="合法的 YAML"):
        load_policy(_write(tmp_path, "rules: [unclosed"))


def test_non_mapping_document_is_rejected(tmp_path):
    with pytest.raises(PolicyError, match="顶层必须是映射"):
        load_policy(_write(tmp_path, "- just\n- a\n- list\n"))


def test_empty_file_is_an_empty_policy(tmp_path):
    """空文件是明确写出来的「什么都不允许」，不该被当成错误。"""
    assert load_policy(_write(tmp_path, "")).rules == []


def test_unknown_action_is_rejected_at_load_time(tmp_path):
    """写错 action 名字必须报错，否则会得到一条永远不生效的规则却毫无提示。"""
    with pytest.raises(PolicyError, match="未知的 action"):
        load_policy(
            _write(
                tmp_path,
                """
version: 1
rules:
  - subjects: [{kind: User, name: alice@example.com}]
    allow:
      - action: helm.rollbackd
""",
            )
        )


def test_empty_scope_list_is_rejected(tmp_path):
    """空列表读起来像「没限制」，实际含义是「什么都不允许」，必须禁止。"""
    with pytest.raises(PolicyError, match="不能为空列表"):
        load_policy(
            _write(
                tmp_path,
                """
version: 1
rules:
  - subjects: [{kind: User, name: alice@example.com}]
    allow:
      - action: helm.rollback
        namespaces: []
""",
            )
        )


def test_unsupported_version_is_rejected(tmp_path):
    with pytest.raises(PolicyError, match="不支持的策略版本"):
        load_policy(_write(tmp_path, "version: 99\nrules: []\n"))


def test_rule_without_subjects_is_rejected(tmp_path):
    """没有 subjects 的规则无法判断适用于谁，留着只会变成「谁都不匹配」的死规则。"""
    with pytest.raises(PolicyError):
        load_policy(
            _write(
                tmp_path,
                """
version: 1
rules:
  - allow:
      - action: helm.rollback
""",
            )
        )


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


# --- 求值：默认拒绝 -------------------------------------------------------


def test_empty_policy_denies_everything():
    decision = PolicyEngine(load_policy(None)).evaluate(
        identity(), action=ACTION_HELM_ROLLBACK, namespace="staging", release="web"
    )
    assert decision.denied
    assert decision.reason == REASON_NO_RULES


def test_unknown_identity_is_denied(tmp_path):
    policy = engine(BASIC, tmp_path)
    decision = policy.evaluate(
        identity("mallory@example.com", groups=()), action=ACTION_HELM_ROLLBACK,
        namespace="staging", release="web",
    )
    assert decision.denied
    assert decision.reason == REASON_NO_MATCHING_RULE


def test_granted_action_outside_its_scope_is_denied(tmp_path):
    """策略允许的是 staging/web，就不能拿去回滚 prod。"""
    policy = engine(BASIC, tmp_path)
    decision = policy.evaluate(
        identity(), action=ACTION_HELM_ROLLBACK, namespace="prod", release="web"
    )
    assert decision.denied
    assert decision.reason == REASON_SCOPE_MISMATCH


def test_granted_scope_but_wrong_action_is_denied(tmp_path):
    policy = engine(BASIC, tmp_path)
    decision = policy.evaluate(
        identity(), action=ACTION_HELM_HISTORY, namespace="staging", release="web"
    )
    assert decision.denied
    # 「没给这个动作」和「给了但范围不对」要能区分开：
    # 前者要找管理员加权限，后者多半是自己把 namespace 或 release 写错了。
    assert decision.reason == REASON_ACTION_NOT_GRANTED


# --- 求值：放行 -----------------------------------------------------------


def test_exact_scope_is_allowed(tmp_path):
    decision = engine(BASIC, tmp_path).evaluate(
        identity(), action=ACTION_HELM_ROLLBACK, namespace="staging", release="web"
    )
    assert decision.allowed


def test_wildcard_scope_is_allowed(tmp_path):
    policy = engine(
        """
version: 1
rules:
  - subjects: [{kind: Group, name: ops}]
    allow:
      - action: helm.rollback
        namespaces: ["*"]
        releases: ["*"]
""",
        tmp_path,
    )
    assert policy.evaluate(
        identity(groups=("ops",)), action=ACTION_HELM_ROLLBACK,
        namespace="anything", release="whatever",
    ).allowed


def test_subject_matching_is_or_not_and(tmp_path):
    """身份本身**或**任意一个 group 命中即可。

    如果写成「身份和所有 group 都要命中」，那么任何多组用户都会被自己的
    其它 group 拖累，规则永远不生效——而 fail-closed 会把这种 bug 表现成
    「配了规则却一直被拒」，非常难查。
    """
    policy = engine(
        """
version: 1
rules:
  - subjects: [{kind: User, name: alice@example.com}]
    allow: [{action: helm.history}]
""",
        tmp_path,
    )
    # 这个身份属于若干与规则无关的组，但 User 主体命中，仍然应当放行。
    decision = policy.evaluate(
        identity(groups=("developers", "system:authenticated")),
        action=ACTION_HELM_HISTORY, namespace="staging", release="web",
    )
    assert decision.allowed


def test_rules_are_additive_across_documents(tmp_path):
    """多条规则是累加关系：加权限只需再写一条，而不是回去改已有的那条。"""
    policy = engine(
        """
version: 1
rules:
  - subjects: [{kind: User, name: alice@example.com}]
    allow:
      - action: helm.history
        namespaces: [staging]
  - subjects: [{kind: User, name: alice@example.com}]
    allow:
      - action: helm.rollback
        namespaces: [prod]
        releases: [api]
""",
        tmp_path,
    )
    assert policy.evaluate(
        identity(), action=ACTION_HELM_HISTORY, namespace="staging", release="web"
    ).allowed
    assert policy.evaluate(
        identity(), action=ACTION_HELM_ROLLBACK, namespace="prod", release="api"
    ).allowed


def test_history_without_a_release_only_checks_the_namespace(tmp_path):
    """按 namespace 判定的动作不该被 release 维度卡住。"""
    policy = PolicyDocument.model_validate(
        {
            "version": 1,
            "rules": [
                {
                    "subjects": [{"kind": "User", "name": "alice@example.com"}],
                    "allow": [
                        {"action": "helm.history", "namespaces": ["staging"], "releases": ["web"]}
                    ],
                }
            ],
        }
    )
    assert PolicyEngine(policy).evaluate(
        identity(), action=ACTION_HELM_HISTORY, namespace="staging", release=None
    ).allowed


def test_group_grant_applies_to_a_disabled_dev_identity(tmp_path):
    """`AUTH_MODE=disabled` 下合成身份的 groups 也要能参与策略匹配。

    否则本地开发时策略引擎永远拒绝，等于把这个模式变成不可用——
    而它的存在意义正是让本地调试不必每次签 token。
    """
    policy = engine(
        """
version: 1
rules:
  - subjects: [{kind: Group, name: system:authenticated}]
    allow: [{action: helm.rollback}]
""",
        tmp_path,
    )
    dev = Identity(username="dev@local", groups=("system:authenticated",), method="disabled")
    assert policy.evaluate(
        dev, action=ACTION_HELM_ROLLBACK, namespace="default", release="web"
    ).allowed
