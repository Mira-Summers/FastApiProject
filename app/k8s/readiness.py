"""就绪探活。

readiness 与 liveness 的区别是 K8s 上最容易配错的地方，这里按下面的语义处理：

- `/health/live`  **绝不**触碰 API Server。碰了的话，集群一抖动 kubelet 就会
  把本来完全健康的 Pod 反复杀掉重启，把一个上游故障放大成一场雪崩。
- `/health/ready` 必须真打一次 API Server，但要做好缓存：kubelet 每几秒探一次，
  几百个 Pod 就是每秒几十次请求，白送的 API Server 压力。

**失败结果同样进缓存。** 这一点反直觉但很关键：API Server 挂掉时探针反而会更密集地
重试，如果不缓存失败，探针本身就会成为压垮它的最后一根稻草。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.errors import OpsError
from app.k8s.client import CoreV1ApiLike, ping

__all__ = ["ReadinessProbe", "ReadinessResult"]


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    detail: Optional[str] = None


class ReadinessProbe:
    """带 TTL 缓存的 API Server 探活。

    `ttl_seconds=0` 表示每次都真探（测试与排障用）。
    """

    def __init__(self, ttl_seconds: float = 5.0) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._lock = threading.Lock()
        self._cached: Optional[ReadinessResult] = None
        self._expires_at = 0.0

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def check(self, core: CoreV1ApiLike, *, request_timeout: float) -> ReadinessResult:
        if self._fresh() is not None:
            return self._cached  # type: ignore[return-value]

        # 单飞（single-flight）：缓存刚过期时如果有 N 个探针同时进来，
        # 只让第一个真的去打 API Server，其余的拿锁后直接复用它的结果。
        with self._lock:
            # 双重检查：等锁期间可能已经被别的线程填好了。
            if self._fresh() is not None:
                return self._cached  # type: ignore[return-value]
            result = self._probe(core, request_timeout=request_timeout)
            self._cached = result
            self._expires_at = time.monotonic() + self._ttl
            return result

    def _fresh(self) -> Optional[ReadinessResult]:
        if self._ttl <= 0 or self._cached is None:
            return None
        return self._cached if time.monotonic() < self._expires_at else None

    @staticmethod
    def _probe(core: CoreV1ApiLike, *, request_timeout: float) -> ReadinessResult:
        try:
            ping(core, request_timeout=request_timeout)
        except OpsError as exc:
            return ReadinessResult(ready=False, detail=exc.message)
        return ReadinessResult(ready=True)
