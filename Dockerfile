# K8s 运维助手 —— 运行镜像
#
# 对照旧版修掉的问题：
#   1. `CMD["uvicorn",...]` 缺 CMD 后的空格 —— 这行是语法错误，镜像根本构建不出来。
#   2. `curl -fsSL .../get-helm-3 | bash` 装的是「构建当时的最新 helm」：同一个
#      Dockerfile 今天和明天构建出的镜像内容不同，且全程没有任何校验。
#      改为固定版本 + 官方 SHA256 校验（见 HELM_SHA256_*）。
#   3. 基线 3.11 与开发环境 .venv312（3.12）错配 —— 「本地能跑、容器里挂」多半来自这里。
#   4. `COPY . .` 把整个工作目录塞进镜像，只靠 .dockerignore 兜底；现在显式列出要拷贝的路径。
#
# ── 基镜像：默认用 tag，建议改成 digest ────────────────────────────────
# tag 是可变的，`python:3.12-slim-bookworm` 今天和下周指向的可能是两个不同的层。
# 要真正可复现，先查真实摘要再替换（需要构建机能访问 Docker Hub）：
#
#     docker buildx imagetools inspect python:3.12-slim-bookworm   # 看 Digest 一行
#     docker build --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<真实摘要> .
#
# 这里刻意没有写死一个摘要：本仓库的开发环境访问不到 Docker Hub，凭空填一个没有
# 核对过的摘要，比不填更危险 —— 要么构建失败，要么你相信自己钉住了一个并不存在的版本。

ARG PYTHON_IMAGE=python:3.12-slim-bookworm


# ── 构建阶段 ──────────────────────────────────────────────────────────
# 只产出两样东西：helm 二进制和装好的依赖树。
# 不需要编译器：运行时依赖全部提供 manylinux wheel（已用
# `pip download --only-binary=:all: --platform manylinux2014_x86_64` 逐个确认过），
# 所以旧版那个装 gcc 的 builder 阶段可以直接删掉。
FROM ${PYTHON_IMAGE} AS builder

# BuildKit 会自动注入 TARGETARCH（amd64 / arm64）。用旧构建器时它是空的，
# 下面的 case 会立刻报错退出 —— 这是有意的：装错架构的 helm 要到运行期
# 执行回滚时才暴露，而且错误信息毫无指向性。
ARG TARGETARCH

ARG HELM_VERSION=v3.22.0
# 取自官方发布渠道：https://get.helm.sh/helm-<版本>-linux-<架构>.tar.gz.sha256sum
# 升级 helm 时这几个值必须一起改，改漏了构建会因校验失败而中止 —— 正是想要的效果。
ARG HELM_SHA256_AMD64=1e4ab49e429626cf6c6958d914248b78c9730803c2751b87627e171dc800e7bb
ARG HELM_SHA256_ARM64=f14e804dfee240f55525b667488fe9adca349e63e00c9af634c0beb1421ac310

RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) helm_arch=amd64; helm_sha256="${HELM_SHA256_AMD64}" ;; \
        arm64) helm_arch=arm64; helm_sha256="${HELM_SHA256_ARM64}" ;; \
        *) \
            echo "不支持的 TARGETARCH='${TARGETARCH}'。" >&2; \
            echo "请用 BuildKit 构建（Docker 23+ 默认开启）；若必须用旧构建器，" >&2; \
            echo "请显式指定架构：--build-arg TARGETARCH=amd64（或 arm64）。" >&2; \
            exit 1 \
            ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL -o /tmp/helm.tar.gz \
        "https://get.helm.sh/helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz"; \
    echo "${helm_sha256}  /tmp/helm.tar.gz" | sha256sum -c -; \
    mkdir -p /out; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    install -m 0755 "/tmp/linux-${helm_arch}/helm" /out/helm; \
    rm -rf /tmp/helm.tar.gz "/tmp/linux-${helm_arch}" /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── 运行阶段 ──────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE}

# PYTHONUNBUFFERED 是必需的：日志就是审计通道（见 app/audit.py），
# 缓冲会让审计记录延迟若干秒才出现在采集器里。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /out/helm /usr/local/bin/helm

# uid/gid 固定为 1000，与 deploy/deployment.yaml 里的 runAsUser/runAsGroup/fsGroup 对齐。
# 不固定的话，k8s 用别的 uid 覆盖时，/home 下的 helm 状态目录会变成不可写。
RUN set -eux; \
    groupadd --gid 1000 opsapi; \
    useradd --uid 1000 --gid 1000 --create-home --home-dir /home/opsapi opsapi

# helm 3 按 XDG 规范把状态放在 $HOME 下。容器里 HOME 未必被设置（k8s 用 runAsUser
# 覆盖 uid 时尤其如此），显式钉住这三个变量既保证目录可写，也让行为可预期。
# 注意 app/helm/runner.py 的子进程环境白名单会放行全部 HELM_* 变量，所以这里设的值
# 确实会传给 helm —— 这是「容器里 helm 写不进缓存」这类问题的排查起点。
ENV HOME=/home/opsapi \
    HELM_CACHE_HOME=/home/opsapi/.cache/helm \
    HELM_CONFIG_HOME=/home/opsapi/.config/helm \
    HELM_DATA_HOME=/home/opsapi/.local/share/helm

RUN set -eux; \
    mkdir -p "${HELM_CACHE_HOME}" "${HELM_CONFIG_HOME}" "${HELM_DATA_HOME}"; \
    chown -R opsapi:opsapi /home/opsapi

# 显式列出拷贝内容而不是 `COPY . .`：镜像里放了什么一眼可见，
# 不会因为 .dockerignore 漏了一项就把测试、IDE 配置或 .venv 打进镜像。
COPY --chown=opsapi:opsapi app ./app
COPY --chown=opsapi:opsapi main.py ./

USER opsapi

EXPOSE 8000

# 探 /health/live 而不是 /health/ready：存活判断不能依赖集群可达性，
# 否则 API Server 抖一下 kubelet 就会杀掉本来健康的 Pod，把上游故障放大成雪崩。
# 这条 HEALTHCHECK 服务于 docker run / compose；k8s 里用的是 Deployment 上的 probes。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"]

# --workers 1 是硬要求：prometheus_client 的默认 registry 是进程内的，
#   多 worker 会让每个进程各记一份计数，/metrics 上看到的是被 N 等分的曲线。
# --no-access-log：应用自己已经打了一条结构化访问日志（含 request_id、耗时、status），
#   uvicorn 的访问日志只会再打一条格式不同的重复记录。
#   注意那条日志里的 client 字段来自 request.client.host，会被 uvicorn 的 --proxy-headers
#   影响：默认只信任来自 127.0.0.1 的 X-Forwarded-For。前面挂了 Ingress 又想看到真实客户端
#   IP 时，设 FORWARDED_ALLOW_IPS —— 别图省事设成 *，那等于让调用方自己声明来源 IP。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
