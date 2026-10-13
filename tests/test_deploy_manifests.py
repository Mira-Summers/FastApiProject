"""部署清单的自检。

deploy/ 里的错误有个共同特点：**单测看不见，症状也不指向原因**。

最典型的一条是就绪探针少一个权限：应用代码完全正确、测试全绿，
但 `/health/ready` 永远 503 → Pod 永远不 Ready → `rollout status` 一直挂着，
看起来像集群有问题或者镜像有问题。写这个文件就是因为真的漏了一条
（`ping()` 需要 `namespaces` 的 list 权限，而 `system:authenticated` 默认没有）。

所以这里检查的不是 YAML 语法，而是**清单与代码之间的一致性**：
权限够不够、探针指的路径对不对、有没有把生产环境的认证关掉。
真正能不能 apply 到集群上，仍然只能在集群里验证。
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"

SERVICE_ACCOUNT = ("k8s-ops-api", "k8s-ops-api")

# --- 代码里的集群调用 → 它需要的 RBAC 权限 ---------------------------------
#
# 键是 app/ 里实际出现的方法名（接收者只能是 core 或 api），值是
# (apiGroup, resource, verb)。新增集群调用时，test_every_cluster_call_is_mapped
# 会先失败，提醒你把权限补进这里、再补进 deploy/。
CALL_PERMISSIONS: dict[str, tuple[str, str, str]] = {
    "list_namespaced_pod": ("", "pods", "list"),
    "read_namespaced_pod": ("", "pods", "get"),
    "read_namespaced_pod_log": ("", "pods/log", "get"),
    # 就绪探针（app/k8s/client.py 的 ping）用 limit=1 的 namespace 列表探活。
    "list_namespace": ("", "namespaces", "list"),
    # 这两个由 system:auth-delegator 提供，deploy/ 里没有对应的 Role 定义，
    # 见 _granted_permissions 里的特判。
    "create_token_review": ("authentication.k8s.io", "tokenreviews", "create"),
    "create_subject_access_review": (
        "authorization.k8s.io",
        "subjectaccessreviews",
        "create",
    ),
}

DELEGATOR_GRANTS = frozenset(
    {
        ("authentication.k8s.io", "tokenreviews", "create"),
        ("authorization.k8s.io", "subjectaccessreviews", "create"),
    }
)

DELEGATOR_ROLE = "system:auth-delegator"

# Settings.from_env 会读取的全部环境变量。
# 刻意在测试里重写一遍而不是从 app/config.py 里推导：这是一份**独立**的陈述，
# 从源码里推导的话，"配置项改了名而清单没改" 这种错就一起被推导没了。
KNOWN_ENV_KEYS = frozenset(
    {
        "APP_NAME",
        "APP_VERSION",
        "APP_ENV",
        "LOG_LEVEL",
        "LOG_JSON",
        "K8S_REQUEST_TIMEOUT_SECONDS",
        "K8S_READY_CACHE_SECONDS",
        "K8S_FOLLOW_MAX_SECONDS",
        "HELM_BINARY",
        "HELM_TIMEOUT_SECONDS",
        "HELM_INHERIT_KUBECONFIG",
        "AUTH_MODE",
        "AUTH_AUDIENCE",
        "AUTH_CACHE_TTL_SECONDS",
        "AUTH_ALLOW_INSECURE",
        "AUTH_DEV_USERNAME",
        "AUTH_DEV_GROUPS",
        "POLICY_PATH",
        "AUDIT_LOG_PATH",
    }
)

_CALL_PREFIXES = ("list_", "read_", "create_", "get_", "patch_", "delete_", "replace_")
_CALL_RECEIVERS = frozenset({"core", "api"})


def deploy_documents() -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted(DEPLOY.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if document:
                documents.append(document)
    assert documents, "deploy/ 里没有清单"
    return documents


def documents_of_kind(*kinds: str) -> list[dict[str, Any]]:
    wanted = set(kinds)
    return [d for d in deploy_documents() if d.get("kind") in wanted]


@pytest.fixture(scope="module")
def config_map() -> dict[str, str]:
    data = documents_of_kind("ConfigMap")
    config = next(d for d in data if d["metadata"]["name"] == "k8s-ops-api-config")
    return config["data"]


@pytest.fixture(scope="module")
def container() -> dict[str, Any]:
    deployment = documents_of_kind("Deployment")[0]
    return deployment["spec"]["template"]["spec"]["containers"][0]


@pytest.fixture(scope="module")
def pod_spec() -> dict[str, Any]:
    deployment = documents_of_kind("Deployment")[0]
    return deployment["spec"]["template"]["spec"]


# --- 权限一致性 ------------------------------------------------------------


def _cluster_calls() -> set[str]:
    """从 app/ 的 AST 里找出所有「对集群客户端的调用」的方法名。"""
    found: set[str] = set()
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            receiver = node.func.value
            if not isinstance(receiver, ast.Name) or receiver.id not in _CALL_RECEIVERS:
                continue
            if node.func.attr.startswith(_CALL_PREFIXES):
                found.add(node.func.attr)
    return found


def _granted_permissions() -> set[tuple[str, str, str]]:
    """deploy/ 里 Role / ClusterRole 实际授予的 (group, resource, verb)。"""
    granted: set[tuple[str, str, str]] = set()
    for document in documents_of_kind("Role", "ClusterRole"):
        for rule in document.get("rules") or []:
            for group in rule.get("apiGroups") or []:
                for resource in rule.get("resources") or []:
                    for verb in rule.get("verbs") or []:
                        granted.add((group, resource, verb))
    return granted


def _delegator_is_bound() -> bool:
    for document in documents_of_kind("ClusterRoleBinding"):
        if document["roleRef"]["name"] != DELEGATOR_ROLE:
            continue
        subjects = document.get("subjects") or []
        if any(
            (s.get("name"), s.get("namespace")) == SERVICE_ACCOUNT
            and s.get("kind") == "ServiceAccount"
            for s in subjects
        ):
            return True
    return False


def test_every_cluster_call_is_mapped() -> None:
    """代码里出现新的集群调用时，先在这里失败，而不是等到集群里才发现。"""
    unmapped = sorted(_cluster_calls() - set(CALL_PERMISSIONS))
    assert not unmapped, (
        f"这些集群调用没有登记所需权限：{unmapped}。"
        "请把它们需要的 (apiGroup, resource, verb) 加进 CALL_PERMISSIONS，"
        "并确认 deploy/ 的 RBAC 里有对应授权。"
    )


def test_deploy_grants_every_permission_the_code_needs() -> None:
    """清单里的 RBAC 必须覆盖代码实际发起的每一次集群调用。

    漏一条的后果取决于漏的是哪条：漏 pods/log 是日志接口 403；
    漏 namespaces 则是就绪探针永远失败、Deployment 永远起不来。
    """
    granted = _granted_permissions()
    if _delegator_is_bound():
        granted |= DELEGATOR_GRANTS

    required = {CALL_PERMISSIONS[call] for call in _cluster_calls()}
    missing = sorted(required - granted)
    assert not missing, f"deploy/ 的 RBAC 缺少这些权限：{missing}"


def test_readiness_permission_is_granted() -> None:
    """单独守着就绪探针那一条 —— 它是唯一"漏了不会报 403、只会永远不 Ready"的权限。"""
    assert ("", "namespaces", "list") in _granted_permissions(), (
        "就绪探针用 list_namespace 探活（app/k8s/client.py 的 ping），"
        "缺了 namespaces 的 list 权限，Pod 永远不会 Ready。"
    )


def test_auth_delegator_is_bound_to_the_service_account() -> None:
    """没有 system:auth-delegator，每一个请求都会 403（TokenReview 建不出来）。"""
    assert _delegator_is_bound(), f"没有把 {DELEGATOR_ROLE} 绑到 {SERVICE_ACCOUNT}"


# --- 危险配置 --------------------------------------------------------------


def test_production_manifest_never_disables_auth(config_map: dict[str, str]) -> None:
    """免认证模式绝不能出现在部署清单里。

    `Settings._validate_insecure_auth` 有一道锁是"检测到在 Pod 内就拒绝启动"，
    但那道锁保护不了这一条：把它写进 ConfigMap 之后，
    下次有人为了排障改掉 APP_ENV，认证就真的关了，而且没有任何提示。
    """
    assert config_map.get("AUTH_MODE") == "tokenreview"
    assert "AUTH_ALLOW_INSECURE" not in config_map


def test_config_map_only_uses_env_keys_that_settings_reads(
    config_map: dict[str, str],
) -> None:
    """拼错的环境变量名会被 Settings 静默忽略，表现为"配置没生效"。"""
    unknown = sorted(set(config_map) - KNOWN_ENV_KEYS)
    assert not unknown, (
        f"这些环境变量 Settings 不会读取：{unknown}。"
        "要么是拼错了，要么是新加的配置项还没加进 app/config.py。"
    )


def test_helm_secrets_are_granted_namespace_scoped_only() -> None:
    """Secrets 的集群级读写 ≈ 交出整个集群（SA token 本身就是 Secret）。

    带 helm 的 SA 尤其要注意：它能拿到 release Secret，也就意味着
    它一旦被滥用，能读到目标 namespace 里的所有 Secret。
    """
    for document in documents_of_kind("ClusterRole"):
        for rule in document.get("rules") or []:
            assert "secrets" not in (rule.get("resources") or []), (
                f"ClusterRole {document['metadata']['name']} 授予了 Secrets 权限；"
                "helm 的 release Secret 权限必须是 namespace 级的 Role。"
            )


def test_helm_role_covers_every_namespace_the_policy_allows() -> None:
    """策略放行了、RBAC 没放行，是"授权通过但操作失败"的错位。"""
    policy_map = next(
        d
        for d in documents_of_kind("ConfigMap")
        if d["metadata"]["name"] == "k8s-ops-api-policy"
    )
    policy = yaml.safe_load(policy_map["data"]["policy.yaml"])

    allowed_namespaces = {
        namespace
        for rule in policy.get("rules", [])
        for grant in rule.get("allow", [])
        for namespace in grant.get("namespaces", ["*"])
    }
    role_namespaces = {
        d["metadata"]["namespace"]
        for d in documents_of_kind("Role")
        if any("secrets" in (r.get("resources") or []) for r in d.get("rules") or [])
    }

    if "*" in allowed_namespaces:
        pytest.skip("策略用通配符放行了所有 namespace，无法逐一比对 Role")

    missing = sorted(allowed_namespaces - role_namespaces)
    assert not missing, (
        f"策略允许在这些 namespace 回滚，但 deploy/ 里没有对应的 helm Role：{missing}"
    )


# --- 探针与生命周期 --------------------------------------------------------


def test_liveness_probe_never_touches_the_cluster(container: dict[str, Any]) -> None:
    """存活探针指向 /health/ready 是"上游抖动被放大成雪崩"的经典写法。"""
    assert container["livenessProbe"]["httpGet"]["path"] == "/health/live"
    assert container["startupProbe"]["httpGet"]["path"] == "/health/live"
    assert container["readinessProbe"]["httpGet"]["path"] == "/health/ready"


def test_readiness_timeout_covers_the_api_server_timeout(
    container: dict[str, Any], config_map: dict[str, str]
) -> None:
    """探针超时必须 ≥ 应用的 API 调用超时。

    否则 API Server 只是慢一点就会被判成不健康，Pod 在 Endpoints 里反复摘挂。
    """
    api_timeout = int(config_map["K8S_REQUEST_TIMEOUT_SECONDS"])
    probe_timeout = int(container["readinessProbe"]["timeoutSeconds"])
    assert probe_timeout >= api_timeout, (
        f"就绪探针超时 {probe_timeout}s 小于 K8S_REQUEST_TIMEOUT_SECONDS={api_timeout}s"
    )


def test_termination_grace_covers_the_helm_timeout(
    pod_spec: dict[str, Any], config_map: dict[str, str]
) -> None:
    """一次回滚最长占用 HELM_TIMEOUT_SECONDS。

    grace 期不够长的话，kubelet 会在回滚执行到一半时 SIGKILL：
    release 停在 pending-rollback，而审计里只有一条 attempted。
    """
    helm_timeout = int(config_map["HELM_TIMEOUT_SECONDS"])
    grace = int(pod_spec["terminationGracePeriodSeconds"])
    assert grace > helm_timeout, (
        f"terminationGracePeriodSeconds={grace}s 不足以覆盖 helm 超时 {helm_timeout}s"
    )


def test_read_only_rootfs_still_has_writable_mounts(container: dict[str, Any]) -> None:
    """根文件系统只读时，helm 和 Python 需要的可写目录必须挂出来。

    漏了的症状是 helm 报写不进缓存、或者 tempfile 失败 —— 都是运行期才暴露。
    """
    if not container.get("securityContext", {}).get("readOnlyRootFilesystem"):
        pytest.skip("没有开启 readOnlyRootFilesystem")

    mounted = {mount["mountPath"] for mount in container.get("volumeMounts") or []}
    # /home/opsapi 对应 Dockerfile 里钉死的 HELM_CACHE_HOME / HELM_CONFIG_HOME /
    # HELM_DATA_HOME —— helm 3 按 XDG 规范把状态放在 $HOME 下。
    assert "/home/opsapi" in mounted, "helm 的 XDG 目录没有可写挂载点"
    assert "/tmp" in mounted, "只读根文件系统下 /tmp 也必须挂出来"


# --- kustomize -------------------------------------------------------------


def test_egress_policy_is_not_enabled_by_default() -> None:
    """默认拒绝出向的策略必须先改 CIDR，不能默默跟着 kustomize 一起 apply。"""
    kustomization = documents_of_kind("Kustomization")[0]
    resources = kustomization.get("resources") or []
    assert "networkpolicy-egress.yaml" not in resources
    assert "networkpolicy.yaml" in resources


def test_kustomization_lists_every_manifest_except_the_opt_in_one() -> None:
    """清单文件不能悄悄漏掉：加了文件却忘了加进 kustomization，等于没部署。"""
    kustomization = documents_of_kind("Kustomization")[0]
    listed = set(kustomization.get("resources") or [])
    on_disk = {path.name for path in DEPLOY.glob("*.yaml")}
    expected = on_disk - {"kustomization.yaml", "networkpolicy-egress.yaml"}

    assert listed >= expected, f"这些清单没有纳入 kustomization：{sorted(expected - listed)}"
