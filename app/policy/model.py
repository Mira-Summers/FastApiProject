"""策略文件的数据模型与加载校验。

几个刻意的选择：

1. **只支持精确匹配和 `"*"`，不支持通配符/正则。**
   `web-*` 这种模式看起来方便，但它让「这条规则到底授权了什么」变得需要推演。
   授权规则必须能被人一眼读明白——尤其是回滚这种不可逆操作。
2. **未知的 action 在加载期就报错。**
   写错成 `helm.rollbak` 的话，fail-closed 会让它「安全地不生效」，
   但这等于配了一条永远不生效的规则却没有任何提示。宁可启动失败。
3. **用 `yaml.safe_load`，不用 `yaml.load`。**
   策略文件可能来自 ConfigMap，而 `yaml.load` 能构造任意 Python 对象。
"""

from __future__ import annotations

from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

__all__ = [
    "ACTION_HELM_HISTORY",
    "ACTION_HELM_ROLLBACK",
    "KNOWN_ACTIONS",
    "Grant",
    "PolicyDocument",
    "PolicyError",
    "Rule",
    "Subject",
    "load_policy",
]

ACTION_HELM_HISTORY = "helm.history"
ACTION_HELM_ROLLBACK = "helm.rollback"

KNOWN_ACTIONS = frozenset({ACTION_HELM_HISTORY, ACTION_HELM_ROLLBACK})

WILDCARD = "*"

SUPPORTED_VERSIONS = frozenset({1})


class PolicyError(RuntimeError):
    """策略文件不存在、无法解析或内容非法。启动期抛出，拒绝启动。"""


class Subject(BaseModel):
    """策略的作用对象。对应 K8s 里 TokenReview 返回的身份。"""

    model_config = {"frozen": True}

    kind: Literal["User", "Group"]
    name: str = Field(min_length=1)


class Grant(BaseModel):
    """一条授权：允许在哪些 namespace 的哪些 release 上执行某个动作。"""

    model_config = {"frozen": True}

    action: str
    namespaces: list[str] = Field(default_factory=lambda: [WILDCARD])
    releases: list[str] = Field(default_factory=lambda: [WILDCARD])

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in KNOWN_ACTIONS:
            raise ValueError(
                f"未知的 action {value!r}，可用值：{sorted(KNOWN_ACTIONS)}"
            )
        return value

    @field_validator("namespaces", "releases")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            # 空列表是危险的：读的人会以为「没限制」，实际含义是「什么都不允许」。
            # 禁止写出没有意义的规则，要什么都不给就别写这条。
            raise ValueError("不能为空列表；要匹配全部请显式写 ['*']")
        return value


class Rule(BaseModel):
    model_config = {"frozen": True}

    subjects: list[Subject] = Field(min_length=1)
    allow: list[Grant] = Field(default_factory=list)


class PolicyDocument(BaseModel):
    model_config = {"frozen": True}

    version: int
    rules: list[Rule] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value not in SUPPORTED_VERSIONS:
            raise PolicyError(
                f"不支持的策略版本 {value}，本服务只认识 {sorted(SUPPORTED_VERSIONS)}"
            )
        return value


def load_policy(path: Optional[str]) -> PolicyDocument:
    """从文件加载策略。`path` 为 None 时返回**空策略**（拒绝一切）。

    空策略是 fail-closed 的：没配置策略时所有 Helm 变更操作都被拒绝。
    这样「忘了挂 ConfigMap」的后果是「操作被拒绝」，而不是「所有人都是管理员」。
    """
    if not path:
        return PolicyDocument(version=1, rules=[])

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise PolicyError(
            f"策略文件不存在：{path}。如果确实不想启用任何 Helm 操作，"
            "请显式留空 POLICY_PATH 而不是指向一个不存在的文件。"
        ) from exc
    except OSError as exc:
        raise PolicyError(f"策略文件无法读取：{path}（{exc.strerror or exc}）") from exc
    except yaml.YAMLError as exc:
        raise PolicyError(f"策略文件不是合法的 YAML：{path}（{exc}）") from exc

    if raw is None:
        # 空文件 = 空策略，语义明确，放行到下面的校验。
        raw = {"version": 1, "rules": []}

    if not isinstance(raw, dict):
        raise PolicyError(f"策略文件的顶层必须是映射（mapping），实际是 {type(raw).__name__}")

    try:
        return PolicyDocument.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"策略文件内容非法：{path}\n{exc}") from exc
