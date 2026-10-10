"""Prometheus 指标定义与记录入口。

三个容易踩坑、必须显式处理的设计点：

1. **route 标签取路由模板，不是真实路径。**
   `/api/v1/namespaces/default/pods/web-7d9f8b6c4d-x2k9p` 会把 Pod 名当成标签值。
   Pod 一重建标签值就变，基数无上限增长，足以把 Prometheus 打爆。
   所以标签取 `scope["route"].path`，形如 `/api/v1/namespaces/{namespace}/pods/{pod_name}`。

2. **没匹配到任何路由的请求统一记成 `__unmatched__`。**
   否则一个扫描器随便打几千个随机路径，就能造出几千条时间序列。
   这是一条特别容易被忽略的基数放大路径——它不需要任何权限。

3. **指标定义在模块级。**
   `create_app()` 在测试里会被反复调用，指标重复注册会抛
   `ValueError: Duplicated timeseries`。定义在模块级就只注册一次。
   代价是测试里只能断言增量，不能断言绝对值——本来也应该这样断言。

另外：默认 registry 是**进程内**的。uvicorn 多 worker 时每个 worker 各自计数，
`/metrics` 只会返回被 scrape 到的那个 worker 的数字。本服务应当单 worker 运行
（运维工具 QPS 很低，单 worker 足够），这一点在 Phase 5 的 Deployment 里会明确写死。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from prometheus_client import Counter, Gauge, Histogram

from app.errors import BadRequest
from app.validation import validate_namespace

__all__ = [
    "HELM_ROLLBACKS_TOTAL",
    "HTTP_REQUEST_DURATION",
    "HTTP_REQUESTS_IN_PROGRESS",
    "HTTP_REQUESTS_TOTAL",
    "INVALID_LABEL_VALUE",
    "UNMATCHED_ROUTE",
    "is_metered_route",
    "is_scrape",
    "observe_request",
    "record_rollback",
    "route_template",
    "track_in_progress",
]

# 标签值兜底。所有「无法收敛成有限集合」的输入都必须落到固定值上，
# 否则标签基数就由调用方决定了。
UNMATCHED_ROUTE = "__unmatched__"
INVALID_LABEL_VALUE = "__invalid__"

METRICS_PATH = "/metrics"
_PROBE_ROUTES = frozenset({METRICS_PATH, "/health/live", "/health/ready"})

HTTP_REQUESTS_TOTAL = Counter(
    "k8s_ops_http_requests_total",
    "按路由、方法、状态码统计的 HTTP 请求数",
    ["method", "route", "status"],
)

# 桶要覆盖到慢操作：回滚的 helm 超时上限是 120 秒，默认桶最大只到 10 秒，
# 那样所有回滚都会掉进 +Inf，histogram_quantile 直接失去意义。
HTTP_REQUEST_DURATION = Histogram(
    "k8s_ops_http_request_duration_seconds",
    "HTTP 请求耗时",
    ["method", "route"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

# 在途请求数。这个指标看着普通，但对本服务特别有用：路由是同步函数，
# 跑在 anyio 的有限线程池里，一旦 API Server 挂起，worker 会被逐个占满，
# 表现为「进程活着、所有请求都超时」。这个 gauge 就是那个故障的先行指标。
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "k8s_ops_http_requests_in_progress",
    "当前正在处理中的 HTTP 请求数",
    ["method"],
)

HELM_ROLLBACKS_TOTAL = Counter(
    "k8s_ops_helm_rollbacks_total",
    "Helm 回滚执行结果统计",
    ["namespace", "result"],
)


def route_template(scope: Mapping[str, Any]) -> str:
    """从 ASGI scope 里取路由模板。

    FastAPI 的 `APIRoute.matches()` 会把 `scope["route"]` 写进 scope，且 Starlette 的
    Router 是就地更新同一个 dict（`scope.update(child_scope)`），所以中间件在
    `call_next` 之后能读到它。404 等未匹配的情况读不到，返回固定兜底值。
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED_ROUTE


def is_metered_route(route: str) -> bool:
    """探针与抓取端点不计入 HTTP 指标。

    kubelet 每几秒探一次、Prometheus 每 15 秒抓一次，这些流量会彻底淹没真实业务请求，
    让 `rate(k8s_ops_http_requests_total)` 变成一条毫无信息量的直线。
    这些端点的可用性本来就由 K8s 和 Prometheus 自己在监控。
    """
    return route not in _PROBE_ROUTES


def is_scrape(path: str) -> bool:
    """判断这是不是 Prometheus 的抓取请求本身。

    抓取请求必须排除在「在途请求数」之外：否则每次 scrape 都会把自己算进去，
    这个 gauge 永远不会读到 0，运维看图的第一反应会是「是不是卡住了」。
    这里比较的是固定字面量，和标签基数无关。
    """
    return path == METRICS_PATH


def _safe_namespace(value: str) -> str:
    """把命名空间收敛成可安全用作标签的值。

    这条防线不是洁癖：回滚接口在**校验之前**就要记账（否则失败的尝试会丢记录），
    此时 namespace 完全由调用方控制，直接当标签用等于把基数交给了攻击者。
    """
    try:
        return validate_namespace(value)
    except BadRequest:
        return INVALID_LABEL_VALUE


def observe_request(
    *, method: str, route: str, status_code: int, duration_seconds: float
) -> None:
    HTTP_REQUESTS_TOTAL.labels(
        method=method, route=route, status=str(status_code)
    ).inc()
    HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(duration_seconds)


@contextmanager
def track_in_progress(method: str) -> Iterator[None]:
    gauge = HTTP_REQUESTS_IN_PROGRESS.labels(method=method)
    gauge.inc()
    try:
        yield
    finally:
        # 必须走 finally：请求抛异常时漏掉 dec 会让这个 gauge 只增不减，
        # 反而变成一条误导性的告警曲线。
        gauge.dec()


def record_rollback(namespace: str, result: str) -> None:
    HELM_ROLLBACKS_TOTAL.labels(namespace=_safe_namespace(namespace), result=result).inc()
