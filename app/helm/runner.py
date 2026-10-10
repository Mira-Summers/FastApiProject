"""helm CLI 的安全封装。

原实现直接用 subprocess 拼参数，缺少下面这些防护：

1. **参数注入**：`release_name` 若以 `-` 开头会被 cobra 当成 flag 解析。
   传 `--kubeconfig=/tmp/evil` 就能劫持 helm 使用的 kubeconfig，
   传 `--kube-context=其他集群` 就能让回滚打到别的集群去。
   → 由 app/validation.py 的正则从根上排除，位置参数前再加 `--` 作双保险。
2. **环境污染**：直接继承父进程环境，`KUBECONFIG` 会把 helm 指到别的集群，
   导致「读 Pod 用的集群」和「回滚打的集群」不是同一个。
   → 子进程环境用白名单重建，但保留 in-cluster 发现所需的变量。
3. **无超时**：helm 卡住会一直挂着。→ 强制超时。
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from app.config import Settings
from app.errors import ServiceUnavailable, UpstreamTimeout

logger = logging.getLogger("app.helm")

__all__ = ["HelmResult", "HelmRunner", "build_clean_env"]


@dataclass(frozen=True)
class HelmResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# 白名单而非黑名单：只保留确实需要的，其余一律不传给子进程。
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        # Windows 上 subprocess 需要这些
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        # 集群内运行时 client-go 靠这两个变量发现 API Server，必须保留
        "KUBERNETES_SERVICE_HOST",
        "KUBERNETES_SERVICE_PORT",
        "KUBERNETES_SERVICE_PORT_HTTPS",
    }
)


def build_clean_env(
    base: Mapping[str, str], *, inherit_kubeconfig: bool = False
) -> dict[str, str]:
    """重建子进程环境：白名单内的变量 + 全部 HELM_* 变量。

    `KUBECONFIG` 默认被剔除——它是最容易让 helm 打到错误集群的变量。
    确实需要沿用外部 kubeconfig 的场景（本地连 kind），用配置显式打开。
    """
    env = {key: value for key, value in base.items() if key in _ENV_ALLOWLIST}
    env.update({key: value for key, value in base.items() if key.startswith("HELM_")})

    if inherit_kubeconfig and "KUBECONFIG" in base:
        env["KUBECONFIG"] = base["KUBECONFIG"]

    return env


class HelmRunner:
    """执行 helm 子命令。测试里可以用假实现替换，从而不需要真的装 helm。"""

    def __init__(
        self,
        settings: Settings,
        *,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._binary = settings.helm_binary
        self._default_timeout = settings.helm_timeout_seconds
        self._env = build_clean_env(
            env if env is not None else os.environ,
            inherit_kubeconfig=settings.helm_inherit_kubeconfig,
        )

    @property
    def binary(self) -> str:
        return self._binary

    def run(self, args: Sequence[str], *, timeout: Optional[float] = None) -> HelmResult:
        effective_timeout = timeout or self._default_timeout
        command = [self._binary, *args]

        # 只记录参数，不记录环境变量（可能含凭据）。
        logger.info(
            "执行 helm 命令",
            extra={"event": "helm_exec", "helm_args": list(args), "timeout": effective_timeout},
        )

        try:
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env=self._env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ServiceUnavailable(
                f"找不到 helm 可执行文件（{self._binary}），请确认容器内已安装 helm"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise UpstreamTimeout(
                f"helm 执行超过 {effective_timeout} 秒被终止"
            ) from exc

        return HelmResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
