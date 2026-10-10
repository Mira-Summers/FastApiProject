"""应用配置。

设计约束：
- 只从环境变量读取，**不做任何 IO、网络或 kubeconfig 加载**。原 k8s_client.py 在
  import 期调用 load_config()，导致没有集群的机器上连 /health 都起不来。
- 配置非法时启动期直接抛 ConfigError 拒绝启动，而不是等到请求进来才发现。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Mapping, Optional

__all__ = ["ConfigError", "Settings", "get_settings"]


class ConfigError(RuntimeError):
    """配置非法。启动期抛出，直接拒绝启动。"""


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})

VALID_ENVS = frozenset({"development", "production", "test"})
VALID_AUTH_MODES = frozenset({"tokenreview", "disabled"})
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _read_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(f"{key} 需要布尔值（true/false），收到 {raw!r}")


def _read_int(
    env: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{key} 需要整数，收到 {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} 不能小于 {minimum}，收到 {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{key} 不能大于 {maximum}，收到 {value}")
    return value


def _read_str(env: Mapping[str, str], key: str, default: str) -> str:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        raise ConfigError(f"{key} 不能为空字符串")
    return value


def _read_optional_bool(env: Mapping[str, str], key: str) -> Optional[bool]:
    return None if env.get(key) is None else _read_bool(env, key, default=False)


@dataclass(frozen=True)
class Settings:
    """全部运行期配置。frozen 是为了让它能安全地被缓存和跨线程共享。"""

    app_name: str = "k8s-ops-api"
    app_version: str = "2.0.0"
    app_env: str = "development"

    log_level: str = "INFO"
    # None 表示按 app_env 自动决定：production 输出 JSON，其余输出人类可读格式。
    log_json: Optional[bool] = None

    # --- Kubernetes ---
    # 单次 API 调用的超时。没有它的话 API Server 卡住会把请求一起拖死。
    k8s_request_timeout: float = 10.0
    # readiness 探活的缓存时间，避免每次 kubelet 探测都打一次 API Server。
    k8s_ready_cache_seconds: float = 5.0
    # 日志 follow 的最长持续时间。一个活跃的 follow 会占用一个线程池 worker。
    k8s_follow_max_seconds: int = 300

    # --- Helm ---
    helm_binary: str = "helm"
    helm_timeout_seconds: int = 120

    # --- 认证 ---
    # tokenreview: 调集群 TokenReview 校验调用方身份（推荐，生产必须）
    # disabled:    仅本地开发用，需同时满足 APP_ENV=development 与 AUTH_ALLOW_INSECURE=true
    auth_mode: str = "tokenreview"
    # TokenReview 的 audience。不校验 aud 会接受 legacy 的永不过期 token，等于放宽信任边界。
    auth_audience: str = "k8s-ops-api"
    # 身份校验结果的缓存时间，用于减少 API Server 往返。代价是撤销延迟最多这么久。
    auth_cache_ttl_seconds: int = 30
    auth_allow_insecure: bool = False
    # disabled 模式下的合成身份。
    auth_dev_username: str = "dev@local"
    auth_dev_groups: tuple[str, ...] = field(default_factory=lambda: ("system:authenticated",))

    # --- 审计 ---
    # 为空则审计事件只写日志；设置后额外追加写入该 JSONL 文件。
    audit_log_path: Optional[str] = None

    def __post_init__(self) -> None:
        resolved_json = self._resolved_log_json()
        # frozen dataclass 用 object.__setattr__ 做一次性的规范化。
        object.__setattr__(self, "log_json", resolved_json)

    def _resolved_log_json(self) -> bool:
        if self.log_json is not None:
            return self.log_json
        return self.app_env == "production"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def auth_disabled(self) -> bool:
        return self.auth_mode == "disabled"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        """从环境变量构造配置。传入 env 可在测试里完全隔离真实环境。"""
        source: Mapping[str, str] = os.environ if env is None else env

        settings = cls(
            app_name=_read_str(source, "APP_NAME", cls.app_name),
            app_version=_read_str(source, "APP_VERSION", cls.app_version),
            app_env=_read_str(source, "APP_ENV", cls.app_env).lower(),
            log_level=_read_str(source, "LOG_LEVEL", cls.log_level).upper(),
            log_json=_read_optional_bool(source, "LOG_JSON"),
            k8s_request_timeout=float(
                _read_int(source, "K8S_REQUEST_TIMEOUT_SECONDS", 10, minimum=1, maximum=600)
            ),
            k8s_ready_cache_seconds=float(
                _read_int(source, "K8S_READY_CACHE_SECONDS", 5, minimum=0, maximum=300)
            ),
            k8s_follow_max_seconds=_read_int(
                source, "K8S_FOLLOW_MAX_SECONDS", 300, minimum=1, maximum=3600
            ),
            helm_binary=_read_str(source, "HELM_BINARY", cls.helm_binary),
            helm_timeout_seconds=_read_int(
                source, "HELM_TIMEOUT_SECONDS", 120, minimum=1, maximum=3600
            ),
            auth_mode=_read_str(source, "AUTH_MODE", cls.auth_mode).lower(),
            auth_audience=_read_str(source, "AUTH_AUDIENCE", cls.auth_audience),
            auth_cache_ttl_seconds=_read_int(
                source, "AUTH_CACHE_TTL_SECONDS", 30, minimum=0, maximum=3600
            ),
            auth_allow_insecure=_read_bool(source, "AUTH_ALLOW_INSECURE", False),
            auth_dev_username=_read_str(source, "AUTH_DEV_USERNAME", cls.auth_dev_username),
            audit_log_path=source.get("AUDIT_LOG_PATH") or None,
        )
        settings.validate(env=source)
        return settings

    def validate(self, env: Optional[Mapping[str, str]] = None) -> None:
        """启动期自检。任何一条不满足都直接拒绝启动。"""
        source: Mapping[str, str] = os.environ if env is None else env

        if self.app_env not in VALID_ENVS:
            raise ConfigError(f"APP_ENV 必须是 {sorted(VALID_ENVS)} 之一，收到 {self.app_env!r}")
        if self.auth_mode not in VALID_AUTH_MODES:
            raise ConfigError(
                f"AUTH_MODE 必须是 {sorted(VALID_AUTH_MODES)} 之一，收到 {self.auth_mode!r}"
            )
        if self.log_level not in VALID_LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL 必须是 {sorted(VALID_LOG_LEVELS)} 之一，收到 {self.log_level!r}"
            )

        if self.auth_disabled:
            self._validate_insecure_auth(source)

    def _validate_insecure_auth(self, env: Mapping[str, str]) -> None:
        """关掉认证是很危险的操作，所以设两道锁，缺一不可。"""
        if self.app_env != "development":
            raise ConfigError(
                "AUTH_MODE=disabled 只允许在 APP_ENV=development 下使用，"
                f"当前 APP_ENV={self.app_env!r}。关掉认证意味着任何能访问本端口的人"
                "都能回滚 Helm release。"
            )
        if not self.auth_allow_insecure:
            raise ConfigError(
                "AUTH_MODE=disabled 需要显式设置 AUTH_ALLOW_INSECURE=true 作为确认。"
            )
        # 第三道锁：跑在 Pod 里就绝不接受免认证，防止配置被误带进集群。
        if env.get("KUBERNETES_SERVICE_HOST"):
            raise ConfigError(
                "检测到运行在 Kubernetes Pod 内（KUBERNETES_SERVICE_HOST 已设置），"
                "拒绝以 AUTH_MODE=disabled 启动。"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级缓存的配置。测试里直接构造 Settings 并覆盖依赖，不走这个缓存。"""
    return Settings.from_env()
