"""认证：TokenReview 链路。

覆盖的重点是几条「错了不会报错、只会静默放宽」的性质：

- audience 必须显式传并且被校验——一旦丢了它，TokenReview 退化成只验签名，
  legacy 的永不过期 token 就能通过；
- 缓存 key 必须是 token 的指纹，**绝不能存明文**——这个 dict 会出现在
  内存快照、debugger、core dump 里；
- 「集群说 token 无效」可以缓存，「我们连不上集群」绝不能缓存——
  后者的后果是集群抖一下就连续拒绝所有合法用户。

认证成功的正向路径走的是**真实的** TokenReviewAuthenticator，
只有 k8s 客户端被换成假实现，所以断言的是真实逻辑。
"""

from __future__ import annotations

import json

import pytest

from app.dependencies import get_core_api_loader
from tests.fakes import FakeCoreV1Api, make_api_exception, make_token_review

PODS_URL = "/api/v1/namespaces/default/pods"
ROLLBACK_URL = "/api/v1/namespaces/default/helm/releases/web/rollback"
AUDIENCE = "k8s-ops-api"


@pytest.fixture
def client(make_client):
    """走真实认证器的客户端。

    刻意遮蔽 conftest 的同名夹具：conftest 的 `client` 会替换 `get_identity`，
    而本文件要测的正是认证链路本身，必须让真实的认证器跑起来。
    授权侧仍然是假的（`authz_api` 默认放行），焦点留在认证上。
    """
    with make_client(authenticate=False) as test_client:
        yield test_client


def token_review_requests(core_api: FakeCoreV1Api):
    return core_api.token_review_requests()


def test_missing_authorization_header_is_401(client, core_api):
    response = client.get(PODS_URL)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    # 没有凭据就不该去打扰集群。
    assert token_review_requests(core_api) == []


def test_401_carries_a_www_authenticate_challenge(client):
    """RFC 6750 要求 401 带这个头，客户端靠它判断该用哪种凭据重试。"""
    response = client.get(PODS_URL)

    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_missing_credential_never_establishes_a_cluster_connection(make_client):
    """没带凭据就不该去连集群。

    两个后果：状态码会从 401 变成 503（把排障方向带偏），
    以及未认证流量变成放大器——任何人都能用空请求逼本服务向 API Server 发起连接。
    """
    connections: list[str] = []

    with make_client(authenticate=False) as client:
        client.app.dependency_overrides[get_core_api_loader] = lambda: (
            lambda: connections.append("connected")
        )
        response = client.get(PODS_URL)

    assert response.status_code == 401
    assert connections == []


def test_presented_credential_does_reach_the_cluster(make_client, core_api):
    """上一条不能是空转的：带了凭据就必须真的去问集群。"""
    connections: list[str] = []

    def load():
        connections.append("connected")
        return core_api

    with make_client(authenticate=False) as client:
        client.app.dependency_overrides[get_core_api_loader] = lambda: load
        response = client.get(PODS_URL, headers={"Authorization": "Bearer t"})

    assert response.status_code == 200
    assert connections == ["connected"]


def test_cache_hit_does_not_establish_a_cluster_connection(make_client, core_api):
    """缓存命中连集群都不该连。

    这比「省一次往返」更值钱：API Server 抖动期间，缓存里的合法身份
    仍然能继续工作，故障不会立刻扩散到所有已认证的调用方。
    """
    connections: list[str] = []

    def load():
        connections.append("connected")
        return core_api

    with make_client(authenticate=False) as client:
        client.app.dependency_overrides[get_core_api_loader] = lambda: load
        for _ in range(3):
            client.get(PODS_URL, headers={"Authorization": "Bearer same"})

    assert connections == ["connected"]


@pytest.mark.parametrize(
    "header",
    [
        "Basic dXNlcjpwYXNz",  # 非 Bearer 方案
        "Bearer",  # 只有方案没有值
        "Bearer   ",  # 空白值
        "token abc",  # 拼错的方案
    ],
)
def test_malformed_authorization_header_is_401_not_403(client, header):
    """格式不对是「没有可用凭据」，不是「身份已知但无权」。

    报成 403 会把排障的人引向 RBAC，而实际问题只是少写了 `Bearer `。
    """
    response = client.get(PODS_URL, headers={"Authorization": header})

    assert response.status_code == 401


def test_valid_token_is_authenticated(client, core_api):
    response = client.get(PODS_URL, headers={"Authorization": "Bearer good-token"})

    assert response.status_code == 200


def test_the_configured_audience_is_sent_to_the_cluster(client, core_api):
    """`spec.audiences` 必须显式传。

    不传的话 TokenReview 只校验签名，而 legacy 的、永不过期的 token
    恰好能通过这种校验——等于悄悄放宽了信任边界。
    """
    client.get(PODS_URL, headers={"Authorization": "Bearer good-token"})

    body = token_review_requests(core_api)[0]
    assert body.spec.audiences == [AUDIENCE]


def test_token_is_sent_verbatim_and_not_parsed_locally(client, core_api):
    """本服务不解析 JWT：token 原样交给 API Server。

    自己解析意味着要自己维护签名校验、算法白名单和吊销——而吊销这件事
    只有 API Server 知道，自己实现等于绕开它。
    """
    client.get(PODS_URL, headers={"Authorization": "Bearer a.b.c"})

    assert token_review_requests(core_api)[0].spec.token == "a.b.c"


def test_rejected_token_is_401(make_client, core_api):
    core_api.token_review = make_token_review(authenticated=False, error="token expired")

    with make_client(authenticate=False) as client:
        response = client.get(PODS_URL, headers={"Authorization": "Bearer stale"})

    assert response.status_code == 401


def test_token_for_another_audience_is_rejected(make_client, core_api):
    """认证通过但 audience 不符，仍然拒绝。

    正常情况下 API Server 会在 spec.audiences 不匹配时直接返回
    authenticated=false；这个用例守的是「audience 校验被删掉之后仍然拒绝」。
    """
    core_api.token_review = make_token_review(audiences=["some-other-service"])

    with make_client(authenticate=False) as client:
        response = client.get(PODS_URL, headers={"Authorization": "Bearer wrong-aud"})

    assert response.status_code == 401


def test_authenticated_without_a_username_is_rejected(make_client, core_api):
    """没有身份就无法做任何授权判定，只能当失败处理。"""
    core_api.token_review = make_token_review(username="")

    with make_client(authenticate=False) as client:
        response = client.get(PODS_URL, headers={"Authorization": "Bearer weird"})

    assert response.status_code == 401


def test_tokenreview_forbidden_points_at_auth_delegator(make_client, core_api):
    """服务账号没有 auth-delegator 时会表现为「所有请求都 403」。

    这极易被误判成用户 RBAC 配错，所以错误消息必须直接把原因说出来。
    """
    core_api.token_review_error = make_api_exception(403, "Forbidden")

    with make_client(authenticate=False) as client:
        response = client.get(PODS_URL, headers={"Authorization": "Bearer t"})

    assert response.status_code == 403
    assert "auth-delegator" in response.json()["error"]["message"]


def test_cluster_unreachable_is_503_not_401(make_client, core_api):
    """连不上集群是「我们不知道」，绝不能报成 401。

    报成 401 会让调用方以为自己该换 token，而真正的问题在上游——
    这个区别决定了排障方向。
    """
    core_api.token_review_error = ConnectionError("api server unreachable")

    with make_client(authenticate=False) as client:
        response = client.get(PODS_URL, headers={"Authorization": "Bearer t"})

    assert response.status_code == 503


def test_upstream_failure_message_does_not_leak_internals(make_client, core_api):
    """5xx 绝不回显内部细节——异常消息里往往带着 API Server 地址。"""
    core_api.token_review_error = ConnectionError(
        "HTTPSConnectionPool(host='10.0.0.1', port=6443): Max retries exceeded"
    )

    with make_client(authenticate=False) as client:
        message = client.get(
            PODS_URL, headers={"Authorization": "Bearer t"}
        ).json()["error"]["message"]

    assert "10.0.0.1" not in message
    assert "ConnectionError" in message  # 类名可以留，便于定位


# --- 缓存 -----------------------------------------------------------------


def test_repeated_requests_hit_the_cache(client, core_api):
    """同一个 token 连续请求只应触发一次 TokenReview。

    否则每个请求都要往 API Server 打一次 TokenReview，认证本身会变成
    瓶颈，而且 API Server 抖动时全站一起 401。
    """
    for _ in range(5):
        assert client.get(PODS_URL, headers={"Authorization": "Bearer same"}).status_code == 200

    assert len(token_review_requests(core_api)) == 1


def test_different_tokens_are_cached_separately(client, core_api):
    client.get(PODS_URL, headers={"Authorization": "Bearer one"})
    client.get(PODS_URL, headers={"Authorization": "Bearer two"})

    assert len(token_review_requests(core_api)) == 2


def test_rejected_tokens_are_cached_too(client, core_api):
    """否定结果也缓存：否则一个持有无效 token 的调用方可以靠重试
    把 TokenReview 变成对 API Server 的放大攻击。"""
    core_api.token_review = make_token_review(authenticated=False)

    for _ in range(3):
        assert client.get(PODS_URL, headers={"Authorization": "Bearer bad"}).status_code == 401

    assert len(token_review_requests(core_api)) == 1


def test_upstream_failures_are_never_cached(make_client, core_api):
    """集群不可达不能被缓存成「token 无效」。

    缓存了的话，集群抖一下就会连续拒绝所有合法用户，而且抖动结束后
    还要再等一个 TTL 才能恢复——故障被自己放大了。
    """
    core_api.token_review_error = ConnectionError("unreachable")

    with make_client(authenticate=False) as client:
        for _ in range(3):
            assert client.get(PODS_URL, headers={"Authorization": "Bearer t"}).status_code == 503

    assert len(token_review_requests(core_api)) == 3


def test_cache_ttl_of_zero_disables_caching(make_client, core_api):
    """把 TTL 设为 0 是「每次请求都问集群」，用于需要即时吊销的场景。"""
    with make_client(authenticate=False, AUTH_CACHE_TTL_SECONDS="0") as client:
        for _ in range(2):
            client.get(PODS_URL, headers={"Authorization": "Bearer t"})

    assert len(token_review_requests(core_api)) == 2


def test_cache_never_holds_the_plaintext_token(make_client):
    """缓存 key 必须是 token 的指纹。

    token 一旦进了这个 dict，就会出现在内存快照、debugger 展开的视图和
    core dump 里——等于给自己造了一个「所有活跃凭据」的文件。
    """
    secret = "super-secret-token-value"

    with make_client(authenticate=False) as client:
        client.get(PODS_URL, headers={"Authorization": f"Bearer {secret}"})
        cache = client.app.state.authenticator._cache

    assert cache, "缓存应该是非空的"
    assert secret not in "".join(cache)
    # key 是 64 位十六进制（SHA-256），长度本身就说明它不是一个 token。
    assert all(len(key) == 64 for key in cache)


def test_cache_is_bounded(make_client):
    """缓存必须有上限，否则持有大量不同无效 token 的调用方可以撑爆内存。"""
    with make_client(authenticate=False) as client:
        for index in range(1200):
            client.get(PODS_URL, headers={"Authorization": f"Bearer token-{index}"})

        assert client.app.state.authenticator.cache_size() <= 1024


# --- disabled 模式 --------------------------------------------------------


def test_disabled_mode_needs_no_token(make_client):
    """免认证模式下不带任何凭据也应当被放行。

    conftest 的 make_client 默认不接管授权，所以这里显式放行 Helm 策略，
    否则会因为空策略而在授权环节被拒——那是另一个话题。
    """
    with make_client(
        authenticate=False,
        APP_ENV="development",
        AUTH_MODE="disabled",
        AUTH_ALLOW_INSECURE="true",
    ) as client:
        assert client.get(PODS_URL).status_code == 200


def test_disabled_mode_still_reports_a_synthetic_identity(make_client, helm_runner, tmp_path):
    """审计里必须能看出「这次操作没有经过认证」，否则事后无法区分责任。

    这条用例顺带把「免认证身份 → 真实策略引擎 → 审计」串成一条链：
    策略按 `system:authenticated` 组授权，而合成身份恰好带有这个组，
    说明免认证模式并没有把授权层一起短路掉（那是另一种更危险的失败）。
    """
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        """
version: 1
rules:
  - subjects: [{kind: Group, name: system:authenticated}]
    allow: [{action: helm.rollback}]
""",
        encoding="utf-8",
    )
    audit_path = tmp_path / "audit.jsonl"

    with make_client(
        authenticate=False,
        # 关掉策略替换，让真实的策略引擎参与判定。
        allow_helm=False,
        POLICY_PATH=str(policy_path),
        AUDIT_LOG_PATH=str(audit_path),
        APP_ENV="development",
        AUTH_MODE="disabled",
        AUTH_ALLOW_INSECURE="true",
    ) as client:
        response = client.post(ROLLBACK_URL, json={"confirm": True})

    assert response.status_code == 200
    actor = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])["actor"]
    assert actor["method"] == "disabled"
    assert actor["username"] == "dev@local"


def test_disabled_mode_ignores_presented_tokens(make_client, core_api):
    """开了免认证就不再校验 token——半开半关的认证最容易被误以为开着。"""
    with make_client(
        authenticate=False,
        APP_ENV="development",
        AUTH_MODE="disabled",
        AUTH_ALLOW_INSECURE="true",
    ) as client:
        response = client.get(PODS_URL, headers={"Authorization": "Bearer garbage"})

    assert response.status_code == 200
    assert token_review_requests(core_api) == []


def test_metrics_endpoint_never_requires_a_token(client):
    """Prometheus 抓取时不会带凭据，要求 token 等于让监控瞎掉。"""
    assert client.get("/metrics").status_code == 200


@pytest.mark.parametrize("path", ["/health/live", "/health/ready", "/"])
def test_operational_endpoints_stay_reachable_without_a_token(client, path):
    """探针和管理端点不能被认证挡住：kubelet 不会带 token 来探活。

    `/health/ready` 在这里会是 503（假客户端探活失败），这不影响结论——
    关键是没有变成 401。
    """
    assert client.get(path).status_code != 401
