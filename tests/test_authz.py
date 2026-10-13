"""授权：SubjectAccessReview 与 Helm 策略引擎在路由上的实际行为。

这一层的正确性有个特点：**错了往往不会报错，只会静默放行或静默拒绝。**
所以断言必须落到具体的 SAR 报文上（verb / resource / subresource / groups），
而不是只看状态码——只看状态码的话，一个把 `subresource` 漏掉的实现
在「调用方恰好有 pods 全权限」的测试环境下会完美通过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import make_api_exception

PODS_URL = "/api/v1/namespaces/default/pods"
POD_URL = "/api/v1/namespaces/default/pods/web-abc123"
LOGS_URL = "/api/v1/namespaces/default/pods/web-abc123/logs"
HISTORY_URL = "/api/v1/namespaces/default/helm/releases/web/history"
ROLLBACK_URL = "/api/v1/namespaces/default/helm/releases/web/rollback"

ALL_BUSINESS_ROUTES = [
    ("GET", PODS_URL),
    ("GET", POD_URL),
    ("GET", LOGS_URL),
    ("GET", HISTORY_URL),
    ("POST", ROLLBACK_URL),
]


# --- 每一条业务路由都必须有守卫 -------------------------------------------


@pytest.mark.parametrize("method,url", ALL_BUSINESS_ROUTES)
def test_every_business_route_requires_authentication(make_client, method, url):
    """一条不漏地检查。

    新增路由时最容易犯的错就是忘了挂守卫，而那种路由在 code review 里
    和别的路由长得一模一样——只有把它列出来逐个打一遍才拦得住。
    """
    with make_client(authenticate=False) as client:
        response = client.request(method, url, json={"confirm": True})

    assert response.status_code == 401, f"{method} {url} 没有要求认证"


@pytest.mark.parametrize("method,url", ALL_BUSINESS_ROUTES)
def test_no_sar_is_issued_when_authentication_fails(make_client, authz_api, method, url):
    """没有身份就没有判定主体。

    先授权再认证的话，未认证的调用方可以靠 403/401 的差异探测权限信息，
    而且会白送一次集群往返。
    """
    with make_client(authenticate=False) as client:
        client.request(method, url, json={"confirm": True})

    assert authz_api.calls == []


# --- SAR 报文内容 ---------------------------------------------------------


def test_list_pods_asks_for_list_on_pods(client, authz_api):
    client.get(PODS_URL)

    attributes = authz_api.last_attributes()
    assert (attributes.verb, attributes.resource) == ("list", "pods")
    assert attributes.namespace == "default"
    # 单个对象的名字只有在 get 时才有意义，list 时必须是空的，
    # 否则会问出一个「能不能 list 这个特定名字」这种没人能答的问题。
    assert not attributes.name


def test_get_pod_asks_for_get_on_the_named_pod(client, authz_api):
    client.get(POD_URL)

    attributes = authz_api.last_attributes()
    assert (attributes.verb, attributes.resource, attributes.name) == (
        "get",
        "pods",
        "web-abc123",
    )


def test_logs_route_asks_for_the_log_subresource(client, authz_api):
    """`subresource="log"` 必须显式传——这是本次改造点名的一条。

    漏传的后果是双向的：只有 `pods/log` 权限的人会被误拒，而只有 `pods`
    权限、被明确禁止看日志的人反而能读到日志。「能看状态不能看日志」
    是很常见的合规要求，这个 bug 会直接把它破掉。
    """
    client.get(LOGS_URL)

    attributes = authz_api.last_attributes()
    assert attributes.subresource == "log"
    # 资源本身仍然是 pods，子资源是单独的维度，不是 "pods/log" 一个字符串。
    assert attributes.resource == "pods"
    assert attributes.verb == "get"
    assert attributes.name == "web-abc123"


def test_core_api_group_is_sent_as_an_empty_string(client, authz_api):
    """核心组的组名是空串而不是 "core"。

    传错或漏传会让请求落到「不指定组」的语义上，匹配不到 `apiGroups: [""]`
    的规则——表现为「明明配了权限却一直 403」。
    """
    client.get(PODS_URL)

    assert authz_api.last_attributes().group == ""


def test_both_user_and_groups_are_sent(client, authz_api):
    """只传 user 会让所有绑在组上的 RBAC 规则失效。

    而真实世界里绝大多数授权是绑组的（system:authenticated、自定义运维组）。
    把 TokenReview 给出的 groups 一并传下去，判定的结果才和
    「用户拿这个 token 直接访问 API Server」一致。
    """
    client.get(PODS_URL)

    spec = authz_api.last_spec()
    assert spec.user == "tester@example.com"
    assert spec.groups == ["ops", "system:authenticated"]


# --- 拒绝 -----------------------------------------------------------------


def test_denied_sar_becomes_403_naming_the_caller(client, authz_api):
    authz_api.allowed = False

    response = client.get(PODS_URL)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    # 报错要说清楚「谁」没有「什么」的权限，否则调用方只能去猜。
    message = response.json()["error"]["message"]
    assert "tester@example.com" in message
    assert "list pods" in message


def test_authorizer_forbidden_means_the_service_account_needs_auth_delegator(
    client, authz_api
):
    """服务账号不能创建 SAR 时的现象是「所有请求都 403」。

    这极易被误判成用户权限不够，所以必须把真正的原因写进消息里。
    """
    authz_api.error = make_api_exception(403, "Forbidden")

    response = client.get(PODS_URL)

    assert response.status_code == 403
    assert "auth-delegator" in response.json()["error"]["message"]


def test_evaluation_error_fails_closed(client, authz_api):
    """集群自己求值失败时只能拒绝。

    放行一个「权限未知」的请求，比拒绝一个「本该允许」的请求危险得多——
    前者是不可见的越权，后者是一次能被人发现的失败。
    """
    authz_api.evaluation_error = "RBAC evaluation failed"

    response = client.get(PODS_URL)

    assert response.status_code == 403


def test_sar_failure_does_not_leak_the_evaluation_error(client, authz_api):
    """求值错误里可能带着集群内部的角色名和绑定关系，只进日志不进响应。"""
    authz_api.evaluation_error = "failed to resolve role cluster-admin-secret-role"

    response = client.get(PODS_URL)

    assert "cluster-admin-secret-role" not in response.json()["error"]["message"]


def test_unreachable_cluster_during_sar_is_503(client, authz_api):
    """SAR 打不通是上游故障，不是「你没权限」。"""
    authz_api.error = ConnectionError("unreachable")

    assert client.get(PODS_URL).status_code == 503


# --- SAR 只是前置闸门 -----------------------------------------------------


def test_a_real_403_from_the_cluster_still_maps_to_403(client, core_api, authz_api):
    """SAR 通过 ≠ 操作一定成功。

    SAR 回答的是「调用方自己能不能做」，真实调用用的是**本服务 SA** 的身份。
    两者可能不一致（SA 权限更小），这时真实调用的失败必须如实反映成 403，
    绝不能因为前置闸门放行了就当成 5xx，更不能吞掉当成功。
    """
    assert authz_api.allowed is True
    core_api.error = make_api_exception(403, "Forbidden")

    response = client.get(PODS_URL)

    assert response.status_code == 403


def test_parameters_are_validated_before_the_cluster_is_asked(client, authz_api):
    """非法名字不值得换一次集群往返，而且它无论如何都会被拒。"""
    response = client.get("/api/v1/namespaces/NotAValidNamespace/pods")

    assert response.status_code == 400
    assert authz_api.calls == []


# --- Helm 走策略引擎而不是 SAR -------------------------------------------


def test_helm_routes_do_not_issue_subject_access_reviews(client, authz_api):
    """Helm 的语义单位是 release，集群 RBAC 表达不了。

    如果这里去问 SAR，问的只能是「能不能写 Deployment 之类的对象」，
    那和「能不能回滚这个 release」完全是两回事——问错了对象还不如不问，
    因为它会给人一种「已经检查过了」的错觉。
    """
    client.get(HISTORY_URL)
    client.post(ROLLBACK_URL, json={"confirm": True})

    assert authz_api.calls == []


def _policy(tmp_path: Path, body: str) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_empty_policy_denies_helm_even_for_an_authenticated_caller(make_client, helm_runner):
    """没配策略 = 没有授予任何人 Helm 权限，而不是「没限制」。

    这是 fail-closed 最要紧的一处：忘了挂 ConfigMap 的后果必须是
    「功能不可用」，绝不能是「所有人都是管理员」。
    """
    with make_client(allow_helm=False) as client:
        response = client.post(ROLLBACK_URL, json={"confirm": True})

    assert response.status_code == 403


def test_policy_scoped_to_another_namespace_does_not_grant_this_one(
    make_client, helm_runner, tmp_path
):
    policy_path = _policy(
        tmp_path,
        """
version: 1
rules:
  - subjects: [{kind: User, name: tester@example.com}]
    allow:
      - action: helm.rollback
        namespaces: [staging]
        releases: [web]
""",
    )

    with make_client(allow_helm=False, POLICY_PATH=policy_path) as client:
        staging = client.post(
            "/api/v1/namespaces/staging/helm/releases/web/rollback", json={"confirm": True}
        )
        prod = client.post(
            "/api/v1/namespaces/prod/helm/releases/web/rollback", json={"confirm": True}
        )

    assert staging.status_code == 200
    assert prod.status_code == 403


def test_denied_policy_names_the_action_and_the_scope(make_client, helm_runner, tmp_path):
    policy_path = _policy(
        tmp_path,
        """
version: 1
rules:
  - subjects: [{kind: User, name: tester@example.com}]
    allow: [{action: helm.history}]
""",
    )

    with make_client(allow_helm=False, POLICY_PATH=policy_path) as client:
        response = client.post(ROLLBACK_URL, json={"confirm": True})

    message = response.json()["error"]["message"]
    assert "helm.rollback" in message
    assert "default/web" in message


def test_denied_helm_request_is_not_executed(make_client, helm_runner):
    """授权失败必须在执行前拦住，所以 helm 命令一次都不该被调用。"""
    with make_client(allow_helm=False) as client:
        client.post(ROLLBACK_URL, json={"confirm": True})

    assert helm_runner.commands == []


def test_broken_policy_file_refuses_to_start(make_client):
    """策略加载失败绝不能静默降级。

    一个「策略没加载成功但仍然起来了」的服务，会以一个没人知道的权限状态
    对外提供服务——这比直接崩掉危险得多。
    """
    from app.policy.model import PolicyError

    with pytest.raises(PolicyError):
        with make_client(allow_helm=False, POLICY_PATH="/nonexistent/policy.yaml"):
            pass
