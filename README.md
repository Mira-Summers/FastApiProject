# K8s 运维助手 API

基于 FastAPI + kubernetes Python SDK 的轻量运维接口。把「查 Pod、看日志、查/回滚 Helm release」
这几件天天要做的事收成一组 HTTP 接口，供漫游脚本、告警面板、ChatOps 机器人调用。

**它不是一个 kubectl 代理。** 调用方用什么身份、能碰哪些资源，全部由你所在集群的 RBAC 决定——
服务自己不做用户体系，也不存任何用户数据。这句话的推论贯穿全文，建议先读下面一节再动手。

---

## 一、先理解安全模型

这是本服务最容易被误解的地方，误解的后果是把生产回滚权限开给所有人。

### 1. 认证：调用方是谁

每个 `/api/v1/*` 请求必须带 `Authorization: Bearer <token>`。服务拿这个 token 去调集群的
**TokenReview** API 验证，并且**显式校验 audience**（`AUTH_AUDIENCE`，默认 `k8s-ops-api`）。

不校验 audience 的 TokenReview 只验签名，而 legacy 那种永不过期的 token 恰好能通过——
等于悄悄放宽了信任边界。所以签发 token 时**必须**带 `--audience`，两边对不上就 401。

应用本身**绝不自己解析 JWT**（不引 pyjwt）：签名验不验、是否过期、是否被吊销，全部交给
API Server 一个权威来源判断。验证结果按 token 的 **SHA-256 指纹** 缓存
`AUTH_CACHE_TTL_SECONDS` 秒（默认 30），**明文 token 不进缓存**。

> 缓存的代价是撤销延迟：token 被吊销后最长还要过这么久才开始拒绝。本服务能执行生产回滚，
> 若这个窗口不可接受，把 `AUTH_CACHE_TTL_SECONDS` 设为 `0`（每次请求打一次 TokenReview——
> 运维工具的 QPS 很低，这个代价通常付得起）。

### 2. 授权：两套系统，别混淆

这是本文档最重要的一张表：

| 操作 | 谁说了算 | 怎么自查 |
|---|---|---|
| 读 Pod、读日志 | **集群 RBAC**（通过 SubjectAccessReview） | `kubectl auth can-i list pods -n default --as=<你的用户名>` |
| Helm history / rollback | **策略文件**（`POLICY_PATH` 指向的 YAML） | 看 `configmap-policy.yaml`，`kubectl auth can-i` 查不出来 |

**为什么 Helm 要另起一套？** 因为集群 RBAC 无法表达 release 这一层语义——RBAC 的粒度是
`<资源, 动词, 命名空间>`，而「允许回滚 `web`、不允许回滚 `payment`」两个 release 都是
同一个命名空间下的 Secret，RBAC 表达不了。所以 release 级别的授权只能放在策略文件里。

**策略文件不是安全边界。** 真正的爆炸半径是本服务 ServiceAccount 的 RBAC——见下一节。

### 3. 真正的边界：本服务 SA 的权限

SAR 判定的是「**调用方**是否被允许」，而**真实调用用的是本服务 SA 的身份**。所以两个闸门是
串联的，缺一不可，也各自都能成为短板：

```
调用方 token → TokenReview（是谁） → SAR（你能不能做） → 本服务 SA 的 RBAC（我能不能做） → 真操作
```

给本服务 SA 授 `cluster-admin`，并不等于每个用户都成了管理员——但等于把 `cluster-admin`
**借给了每一个能通过 SAR 判定的人**。`deploy/rbac-*.yaml` 里授予的范围，就是这个上限。

因此 `deploy/` 里的 RBAC 清单刻意拆得很细，且 Helm 的 Secrets 权限**必须是 namespace 级的
`Role`，绝不能是 `ClusterRole`**——Secrets 的集群级读写 ≈ 交出整个集群。

---

## 二、快速开始（本地）

```powershell
# 1. 依赖。开发环境是 .venv312（Python 3.12）
.venv312\Scripts\pip install -r requirements-dev.txt

# 2. 起服务
.venv312\Scripts\uvicorn main:app --reload
```

打开 http://127.0.0.1:8000/docs 看交互式文档，或直接跟着
[test_main.http](test_main.http) 里的请求逐条试（PyCharm 里可以直接点运行）。

本地没有集群时的预期表现，这几点正好证明了 fail-closed 设计：

| 请求 | 结果 | 原因 |
|---|---|---|
| `GET /health/live` | `200 {"status":"ok"}` | 存活探针刻意不碰 k8s API |
| `GET /health/ready` | `503` | 真打一次 API Server，连不上就不就绪 |
| `GET /` | `200` | 服务元信息 |
| `/api/v1/*` | `401` | 拿不到集群来验证 token |
| helm 接口 | `403` | 没配 `POLICY_PATH` = 空策略 = 拒绝一切 |

### 要连真集群（kind）

本服务用的是集群内认证，本地跑要么有 kubeconfig、要么在 Pod 里。本地连 kind 时需要让
helm 子进程继承 kubeconfig（默认**不继承**，见配置项表里 `HELM_INHERIT_KUBECONFIG` 的说明）：

```powershell
$env:HELM_INHERIT_KUBECONFIG = "true"
.venv312\Scripts\uvicorn main:app --reload
```

---

## 三、接口

所有 `/api/v1/*` 接口都要 `Authorization` 头。`/health/*`、`/`、`/metrics` 免认证。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health/live` | 存活探针，**绝不触碰 k8s API** |
| GET | `/health/ready` | 就绪探针，真打一次 API Server（结果缓存） |
| GET | `/` | 服务元信息（名称/版本/环境） |
| GET | `/metrics` | Prometheus 指标（不在 OpenAPI 文档里） |
| GET | `/api/v1/namespaces/{ns}/pods` | 列 Pod。`label_selector`、`field_selector`、`limit`(1–1000)、`continue` |
| GET | `/api/v1/namespaces/{ns}/pods/{pod}` | 单个 Pod 详情（含重启原因、人类可读 age） |
| GET | `/api/v1/namespaces/{ns}/pods/{pod}/logs` | 读日志。`container`、`tail_lines`(默认 200，1–10000)、`previous`、`since_seconds`、`timestamps` |
| GET | `/api/v1/namespaces/{ns}/helm/releases/{rel}/history` | release 版本历史（策略授权） |
| POST | `/api/v1/namespaces/{ns}/helm/releases/{rel}/rollback` | 回滚（策略授权 + `confirm: true`） |

关于日志接口的几个细节：

- `previous=true` 才能看到 **CrashLoopBackOff 容器的上一轮日志**——排查 CrashLoop 时这几乎
  是唯一有用的东西，默认的当前容器日志往往只剩几行启动失败输出。
- `pods/log` 在 RBAC 里是**独立子资源**，可以单独授予。「能看 Pod 状态但看不了日志」是常见且
  合理的配置，因为日志里经常夹着凭据（环境变量、连接串、启动参数）。所以本服务的 SA 权限也
  拆成了两个 ClusterRole。

关于回滚接口：

- 请求体必须显式含 `confirm: true`，不传直接 400。这道确认是防脚本和误操作的。
- 不传 `revision` 时回滚到上一个版本。
- 参数校验（不合法的值在进业务逻辑之前就被拒）：namespace 必须是 DNS-1123 label（≤63 字符）、
  release name 必须是 DNS-1123 label（≤53 字符）、pod name 必须是 DNS-1123 subdomain（≤253 字符）。

---

## 四、错误响应

所有错误收敛成同一种结构，`request_id` 与日志、响应头 `X-Request-ID` 一致，方便串联排障：

```json
{
  "error": {
    "code": "forbidden",
    "message": "没有权限在该命名空间读取日志",
    "detail": null,
    "request_id": "0f3c…"
  }
}
```

| HTTP | code | 什么情况 |
|---|---|---|
| 400 | `bad_request` | 参数语义不合法（如回滚没传 `confirm`） |
| 401 | `unauthorized` | 没带 token / token 无效 / audience 不匹配 |
| 403 | `forbidden` | SAR 判定不通过；或策略文件未授权 |
| 404 | `not_found` | Pod / release 不存在 |
| 405 | `method_not_allowed` | 方法不对 |
| 409 | `conflict` | 状态冲突 |
| 415 | `unsupported_media_type` | Content-Type 不对 |
| 422 | `validation_error` | Pydantic 参数校验失败（路径/查询参数格式） |
| 500 | `internal_error` | 服务内部错误，**不回显任何内部细节** |
| 502 | `upstream_error` | API Server 返回了错误 |
| 503 | `service_unavailable` | 集群不可达 / 未就绪 |
| 504 | `upstream_timeout` | 调 API Server 或 helm 超时 |

**500 响应绝不回显 `str(e)`、异常类名、API Server 地址。** 这些信息在服务端日志里，
不在给调用方的响应里。

---

## 五、可观测性

### 日志

stdlib `logging` + 自写 JSON Formatter，`APP_ENV=production` 时默认输出 JSON
（也可用 `LOG_JSON` 显式控制）。每条请求日志带 `request_id`、方法、路径、状态码、耗时。
调用方传来的 `X-Request-ID` 会被复用（**字符集先做收敛**，否则可以用它注入换行伪造日志行）。

### 审计

`helm.rollback` 这类变更操作会写审计事件（`event=audit`），记录操作者身份、目标、参数、结果。

默认**只走 stdout**，不设 `AUDIT_LOG_PATH`：emptyDir 上的 `audit.jsonl` 会随 Pod 重启一起
消失，对事后追责没有价值，反而让人以为有持久记录。**若你的集群没有日志采集器，务必挂 PVC
并设置 `AUDIT_LOG_PATH`**——这一步不要省。

### 指标

| 指标 | 类型 | 标签 |
|---|---|---|
| `k8s_ops_http_requests_total` | Counter | `method`, `route`, `status` |
| `k8s_ops_http_request_duration_seconds` | Histogram | `method`, `route` |
| `k8s_ops_http_requests_in_progress` | Gauge | `method` |
| `k8s_ops_helm_rollbacks_total` | Counter | `namespace`, `result` |

几个刻意为之的地方：

- **直方图分桶到 120 秒**。默认桶最大只到 10 秒，而回滚的 helm 超时上限是 120 秒——
  那样所有回滚都会掉进 `+Inf`，`histogram_quantile` 直接失去意义。
- `route` 用**路由模板**（`/api/v1/namespaces/{namespace}/pods`）而不是真实路径，否则每个
  namespace 都会产生一条新时间序列，基数爆炸。未匹配的 404 会归到 `__unmatched__` 哨兵值。
- **`requests_in_progress` 是本服务最有用的先行指标**。路由是同步函数，跑在 anyio 的有限
  线程池里；一旦 API Server 挂起，worker 会被逐个占满，表现为「进程活着、所有请求都超时」。
  这个 gauge 就是那个故障的前兆。
- 探针和抓取请求不计入指标（否则 `in_progress` 永远读到 1）。

---

## 六、配置项

在 `deploy/configmap-config.yaml` 里改，改完需要滚动重启：

```bash
kubectl -n k8s-ops-api rollout restart deploy/k8s-ops-api
```

ConfigMap 的更新**不会**自动传进已经跑着的进程——这是刻意的，让「配置何时变更」在
Deployment 的 revision 历史里留下痕迹。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `APP_NAME` / `APP_VERSION` | `k8s-ops-api` / `2.0.0` | 服务元信息 |
| `APP_ENV` | `development` | `development` / `production` |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `LOG_JSON` | 随 `APP_ENV` | 未设置时 production 为 JSON |
| `K8S_REQUEST_TIMEOUT_SECONDS` | `10` | 单次 API 调用超时（1–600） |
| `K8S_READY_CACHE_SECONDS` | `5` | 就绪探活结果缓存（0–300） |
| `K8S_FOLLOW_MAX_SECONDS` | `300` | 日志 follow 最长时长 |
| `HELM_BINARY` | `helm` | helm 可执行文件 |
| `HELM_TIMEOUT_SECONDS` | `120` | 单次 helm 调用超时 |
| `HELM_INHERIT_KUBECONFIG` | `false` | 是否把父进程的 `KUBECONFIG` 传给 helm 子进程 |
| `AUTH_MODE` | `tokenreview` | `tokenreview` / `disabled`（后者仅本地） |
| `AUTH_AUDIENCE` | `k8s-ops-api` | TokenReview 校验的 audience |
| `AUTH_CACHE_TTL_SECONDS` | `30` | 身份缓存时长（即最大撤销延迟） |
| `AUTH_ALLOW_INSECURE` | `false` | `disabled` 模式的第二道锁 |
| `AUTH_DEV_USERNAME` / `AUTH_DEV_GROUPS` | `dev@local` / `system:authenticated` | `disabled` 模式的合成身份 |
| `POLICY_PATH` | 空 | Helm 策略文件路径。**空 = 拒绝一切 Helm 操作** |
| `AUDIT_LOG_PATH` | 空 | 审计 JSONL 落盘路径。空 = 只走 stdout |

> `HELM_INHERIT_KUBECONFIG` 默认 `false` 的原因：`KUBECONFIG` 会把 helm 指到**另一个**集群，
> 导致「读 Pod 用的集群」和「回滚打的集群」不是同一个。只在本地开发指向 kind 时才打开。

### 免认证模式（`AUTH_MODE=disabled`）

多重条件同时成立才允许启动，任一不满足直接拒绝启动：

1. `APP_ENV=development`
2. 显式设置 `AUTH_ALLOW_INSECURE=true`
3. **不在 Pod 内**（`KUBERNETES_SERVICE_HOST` 不存在）

第 3 条专门用来防止它被误带进集群。启动时还会打一条刺眼的 WARNING。

即便如此，**这个模式绝不能出现在部署清单里**。`tests/test_deploy_manifests.py` 会检查
ConfigMap 中不含这两个变量，且 `AUTH_MODE` 必须是 `tokenreview`。

---

## 七、集群调用 → RBAC 对照表

本服务对集群的全部调用就在下面这六条，不多不少：

| 代码位置 | 集群调用 | 需要的权限 | 由谁提供 |
|---|---|---|---|
| [app/k8s/client.py](app/k8s/client.py) `ping()` | `list_namespace(limit=1)` | `namespaces` 的 `list` | `k8s-ops-api:readiness` |
| [app/k8s/pods.py](app/k8s/pods.py) | `list_namespaced_pod` | `pods` 的 `list` | `k8s-ops-api:pods-reader` |
| [app/k8s/pods.py](app/k8s/pods.py) | `read_namespaced_pod` | `pods` 的 `get` | `k8s-ops-api:pods-reader` |
| [app/k8s/logs.py](app/k8s/logs.py) | `read_namespaced_pod_log` | `pods/log` 的 `get` | `k8s-ops-api:pods-logs-reader` |
| [app/security/tokenreview.py](app/security/tokenreview.py) | `create_token_review` | `tokenreviews` 的 `create` | `system:auth-delegator` |
| [app/security/authorizer.py](app/security/authorizer.py) | `create_subject_access_review` | `subjectaccessreviews` 的 `create` | `system:auth-delegator` |

**⚠ 第一个是这里最容易踩的坑。** 就绪探针用 `list_namespace(limit=1)` 做探活——它一次同时验证
「网络可达 + 认证通过 + 授权通过」，而且 `limit=1` 几乎不产生负载。代价是探针因此依赖
`namespaces` 的 `list` 权限，而这是**集群级资源**，`system:authenticated` 默认没有。

漏掉这条的后果不是某个接口报 403，而是：

```
/health/ready 永远 503 → Pod 永远不 Ready → Deployment 永远没有可用副本 → rollout status 一直挂着
```

现象看起来像「集群连不上」或「镜像有问题」，而不像「RBAC 少了一条」。

`tests/test_deploy_manifests.py` 会 AST 扫 `app/` 找出所有集群调用，与 `deploy/` 里实际授予的
权限逐一比对。**新增集群调用时它会先失败**，提醒你补权限。

想从「全集群」收窄到「某几个 namespace」？不用改 ClusterRole——`RoleBinding` 可以引用
`ClusterRole`（反过来不行），在目标 namespace 里建 RoleBinding 指向它即可，之后可以删掉
`deploy/rbac-pods.yaml` 里的 ClusterRoleBinding。具体命令见该文件末尾的注释。

---

## 八、部署

### 前置

1. 改镜像 tag：[deploy/kustomization.yaml](deploy/kustomization.yaml) 里 `images.newTag`
   （注意它和 ConfigMap 里的 `APP_VERSION` 是两件事：前者是镜像，后者是 `/` 接口自报的版本，
   升级时两个都要改）。
2. 改策略：[deploy/configmap-policy.yaml](deploy/configmap-policy.yaml)。**这是决定谁能回滚的
   文件，务必按自己的实际情况写。**
3. 为每个需要操作 Helm 的 namespace 建 Role/RoleBinding。`deploy/rbac-helm.yaml` 里给的是
   `default` 命名空间的样例——注意它的 namespace 是**目标 release 的**命名空间，
   不是本服务自己的。

### 部署

```bash
kubectl apply -k deploy/
kubectl -n k8s-ops-api rollout status deploy/k8s-ops-api
```

`deploy/kustomization.yaml` 里刻意**不写** `namespace:` 字段，所有对象的 namespace 都是显式
写死的。原因是个经典坑：kustomize 的 namespace 变换**不会**改写 RoleBinding 的
`subjects[].namespace`，一旦用它换命名空间，就会出现「主体还在老命名空间」这种只改了一半的
状态，而报错信息完全指不到这里。

### 拿 token

```bash
# 给某个 SA 签一个本服务能认的 token（注意 --audience 必须与 AUTH_AUDIENCE 一致）
kubectl create token ops-api -n k8s-ops-api --audience=k8s-ops-api

curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8000/api/v1/namespaces/default/pods
```

### 网络策略

[deploy/networkpolicy.yaml](deploy/networkpolicy.yaml) 是入向限制，默认启用——只允许
`monitoring` 和 `k8s-ops-api-client` 两个命名空间访问。

> NetworkPolicy 工作在 L3/L4，**无法按 HTTP 路径过滤**，所以没办法把 `/metrics` 和业务接口
> 在同一个端口上分开。`/metrics` 不要求 token（Prometheus 未必带），隔离靠的就是这条策略。
> 另外 NetworkPolicy 需要 CNI 支持，不支持的 CNI 会静默忽略它——验证方法见该文件注释。

[deploy/networkpolicy-egress.yaml](deploy/networkpolicy-egress.yaml) 是出向限制，**默认不在
kustomization 里，需要手动启用**。原因是它要求填本集群 API Server 的地址段，填错会让应用
全部 503 且现象像代码 bug。启用步骤和验证命令写在文件头部，**第 4 步的验证不能省**——
策略语法正确 ≠ 流量通。

---

## 九、排障

按症状查：

| 症状 | 大概率原因 |
|---|---|
| `rollout status` 一直卡住，Pod 永远 `0/1 Ready` | 缺 `namespaces` 的 `list` 权限（见第七节） |
| 每个请求都 403 | `system:auth-delegator` 没绑到本服务的 SA |
| Pod / 日志正常，但所有 helm 接口 403 | 没挂 `POLICY_PATH`（空策略 = 拒绝一切），启动日志里有 `event=empty_policy` 的警告 |
| 回滚报 `cannot get release` | 目标 namespace 下没有 helm 的 `Role`/`RoleBinding`（release 状态存在该 namespace 的 Secret 里） |
| 401 | token 过期、audience 不匹配、或 `AUTH_AUDIENCE` 与签发时不一致 |
| 503 且 `/health/ready` 也 503 | API Server 不可达。若刚启用出向 NetworkPolicy，先怀疑是它拦过头了 |
| 进程活着但所有请求超时 | API Server 挂起把 anyio 线程池占满了，看 `k8s_ops_http_requests_in_progress` |
| 日志接口 403 但 Pod 接口正常 | 本服务的 SA 只有 `pods` 没有 `pods/log`（这是可以刻意配置的） |

启动日志里 `event=startup` 那条会带上 `auth_mode`、`policy_path`、`policy_rules` 数量——
排障时先看它。

---

## 十、测试

```powershell
.venv312\Scripts\pytest
```

259 个测试，不需要集群、不需要 helm（helm 相关全部用 fake）。分层：

| 文件 | 覆盖 |
|---|---|
| `test_auth` / `test_authz` / `test_policy` | 认证、SAR 授权、策略引擎 |
| `test_pods` / `test_logs` / `test_helm` | 三个业务域（含参数校验、上游错误映射） |
| `test_errors` / `test_error_mapping` | 异常层次 → HTTP 状态码 |
| `test_metrics` / `test_audit` / `test_config` | 指标、审计、配置加载与启动自检 |
| `test_health` / `test_validation` / `test_smoke` | 探针、参数校验、应用装配 |
| `test_deploy_manifests` | `deploy/` 与代码的一致性（见第七节） |

### 只能在真实环境做的验证

以下三件事没法在本地单测里覆盖，**部署前请在集群上过一遍**：

```bash
# 1. 镜像能构建（本地无 Docker 守护进程）
docker build -t k8s-ops-api:2.0.0 .

# 2. 清单能通过服务端校验
kubectl apply -k deploy/ --dry-run=server

# 3. 真链路的正向与负向
kubectl create token ops-api -n k8s-ops-api --audience=k8s-ops-api   # → 200
curl -H "Authorization: Bearer <错 audience 的 token>" …             # → 401
curl -H "Authorization: Bearer <无 SAR 权限的 token>" …              # → 403
```

---

## 十一、目录结构

```
main.py                      # 薄 shim: from app.main import app（兼容 PyCharm 入口）
app/
├─ main.py                   # create_app() 工厂 + 中间件 + 路由挂载
├─ config.py                 # Settings（stdlib os.getenv）+ 启动期自检
├─ errors.py                 # 领域异常层次 → HTTP 状态码
├─ audit.py                  # 审计事件
├─ metrics.py                # Prometheus 指标
├─ logging_setup.py          # JSON Formatter + request_id ContextVar
├─ dependencies.py           # 唯一的装配点：所有 Depends provider
├─ security/                 # identity / tokenreview / disabled / authorizer(SAR)
├─ policy/                   # model（加载校验）+ engine（fail-closed 求值）
├─ k8s/                      # client（懒加载+超时）/ pods / logs / readiness
├─ helm/                     # runner（安全 subprocess）/ service
├─ schemas/                  # Pydantic v2 模型，按域拆
└─ api/routes/               # health / pods / logs / helm / metrics
deploy/                      # namespace / sa / rbac-* / configmap-* / deployment / service / networkpolicy / kustomization
tests/                       # 分层测试 + fakes
```

**硬性约束（改动时请保持）：`import app.main` 不得有任何 k8s 连接副作用。**
这既是能在没有集群的机器上跑测试的前提，也是原 `k8s_client.py` 在 import 期调用
`load_config()` 导致进程直接崩掉的教训。所有 k8s 客户端一律懒加载。

---

## 十二、几条不能动的约束

改动这个服务时，下面这些不是风格偏好，是安全属性：

- **绝不手写 `Authorization` 头**，也绝不手写 in-cluster 客户端。SDK 的 `load_incluster_config()`
  自带 `refresh_api_key_hook`，长跑时 token 轮换不会 401——手写就没了。
- **绝不自己解析 JWT**。签名、过期、吊销全部交给 TokenReview。
- **明文 token 绝不进缓存**，缓存 key 用 SHA-256 指纹。
- **SAR 通过不等于操作成功**。SAR 只是预检门，真实调用的 403 必须如实映射为 403。
- **liveness 探针绝不调用 k8s API**。否则 API Server 抖动时 kubelet 会误杀健康的 Pod，
  把上游故障放大成雪崩。
- **5xx 响应绝不回显内部细节**（`str(e)`、异常类名、API Server 地址）。
- **Helm 的 Secrets 权限必须是 namespace 级 `Role`**，绝不能是 `ClusterRole`。
- **单 worker**：`prometheus_client` 默认 registry 是进程内的，多 worker 会漏计数。镜像和
  Deployment 都固定在 1。
- **绝不虚构基础镜像 digest 或 helm 校验和**。Dockerfile 里的 SHA256 必须是从真实来源核验过的
  值；核验不了就宁可不填。
