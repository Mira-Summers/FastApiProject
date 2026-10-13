"""基于集群 TokenReview 的认证。

每个请求都要调一次 API Server 的话，认证本身会成为瓶颈和单点，
所以带一个短 TTL 缓存。缓存设计上有三个必须守住的点：

1. **缓存 key 用 token 的 SHA-256，绝不存明文。**
   这个 dict 会被 dump 到内存快照、被 debugger 展开、被 core dump 落到磁盘。
   存明文 token 等于给自己造了一个「能拿到所有活跃凭据」的文件。
   SHA-256 在这里不需要加盐：它不是防爆破的密码哈希，只是把不可预测的
   长字符串压缩成固定长度的索引键，而且必须确定性——同一个 token 每次都要
   算出同一个 key。加盐反而要额外存盐，没有意义。

2. **TTL 就是吊销延迟。**
   ServiceAccount token 被删除、SA 被停用之后，本服务最多还会认它
   `AUTH_CACHE_TTL_SECONDS` 秒。这个值不能配大——默认 30 秒是刻意的。

3. **只缓存 TokenReview 返回了确定结论的结果。**
   API Server 超时、连不上、本服务 SA 没有 auth-delegator 权限——这些是
   「我们不知道」，不是「token 无效」。把它当否定结果缓存下来，
   等于集群抖一下就连续 30 秒拒绝所有合法用户，而且抖动结束后还要再等一轮。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from kubernetes.client import V1TokenReview, V1TokenReviewSpec
from kubernetes.client.rest import ApiException

from app.config import Settings
from app.errors import PermissionDenied, ServiceUnavailable, from_api_exception
from app.k8s.client import CoreV1ApiLike
from app.security.authenticator import MISSING_CREDENTIAL_MESSAGE, unauthorized
from app.security.identity import Identity

logger = logging.getLogger("app.security")

__all__ = ["TokenReviewAuthenticator"]

# 缓存条目上限。没有上限的话，一个能构造大量不同 token 的调用方
# （哪怕是无效 token）就能把进程内存顶上去。超出后按插入顺序淘汰最老的一条。
DEFAULT_MAX_CACHE_ENTRIES = 1024

_REASON_NOT_AUTHENTICATED = "token 未通过集群认证（无效、已过期或已被吊销）"


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    # None 表示「集群明确判定这个 token 不是有效身份」。
    # 与 expires_at 一起构成一次可缓存的否定结论。
    identity: Optional[Identity]


class TokenReviewAuthenticator:
    """调用 `POST /apis/authentication.k8s.io/v1/tokenreviews` 校验调用方。"""

    requires_credential = True

    def __init__(
        self,
        settings: Settings,
        *,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
    ) -> None:
        self._audience = settings.auth_audience
        self._ttl = float(settings.auth_cache_ttl_seconds)
        self._request_timeout = settings.k8s_request_timeout
        self._max_entries = max(1, max_cache_entries)
        self._lock = threading.Lock()
        self._cache: dict[str, _CacheEntry] = {}

    # --- 对外接口 ---------------------------------------------------------

    def authenticate(
        self, credential: str, *, load_api: Callable[[], CoreV1ApiLike]
    ) -> Identity:
        token = (credential or "").strip()
        if not token:
            # 正常情况下依赖层已经拦掉了，这里是「直接调用认证器」时的兜底。
            raise unauthorized(MISSING_CREDENTIAL_MESSAGE)

        cache_key = _fingerprint(token)
        entry = self._lookup(cache_key)
        if entry is not None:
            # 缓存命中连集群都不用连——这比省一次往返更值钱：
            # API Server 抖动期间，缓存里的合法身份仍然能继续工作。
            if entry.identity is None:
                raise unauthorized(_REASON_NOT_AUTHENTICATED)
            return entry.identity

        # 只有真的要问集群时，才把客户端建出来。
        identity = self._review(token, load_api())  # 上游故障在这里抛 503/403
        self._store(cache_key, identity)

        if identity is None:
            raise unauthorized(_REASON_NOT_AUTHENTICATED)
        return identity

    def cache_size(self) -> int:
        """供测试与运维诊断使用；不暴露任何 token 信息。"""
        with self._lock:
            return len(self._cache)

    # --- 缓存 -------------------------------------------------------------

    def _lookup(self, key: str) -> Optional[_CacheEntry]:
        if self._ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                # 顺手清理：过期的条目留着只会占内存，而读到过期条目是最危险的情况。
                del self._cache[key]
                return None
            return entry

    def _store(self, key: str, identity: Optional[Identity]) -> None:
        if self._ttl <= 0:
            return
        now = time.monotonic()
        entry = _CacheEntry(expires_at=now + self._ttl, identity=identity)
        with self._lock:
            self._purge_expired_locked(now)
            if len(self._cache) >= self._max_entries and key not in self._cache:
                # dict 保持插入顺序，弹出的第一个就是最老的。
                self._cache.pop(next(iter(self._cache)), None)
            self._cache[key] = entry

    def _purge_expired_locked(self, now: float) -> None:
        expired = [key for key, item in self._cache.items() if item.expires_at <= now]
        for key in expired:
            del self._cache[key]

    # --- TokenReview ------------------------------------------------------

    def _review(self, token: str, api: CoreV1ApiLike) -> Optional[Identity]:
        """返回值：`Identity` 表示认证通过，`None` 表示集群明确判定 token 无效。

        「无法判断」的情况一律抛异常而不是返回 None——见模块文档第 3 点。
        """
        body = V1TokenReview(
            spec=V1TokenReviewSpec(token=token, audiences=[self._audience])
        )
        try:
            review = api.create_token_review(body=body, _request_timeout=self._request_timeout)
        except ApiException as exc:
            if exc.status == 403:
                # 这是部署配置问题，不是调用方的问题，必须说清楚否则极难排查：
                # 现象是「所有请求都 403」，很容易被误判成用户 RBAC 配错了。
                raise PermissionDenied(
                    "本服务账号无权创建 TokenReview，无法校验调用方身份。"
                    "请为服务账号绑定 system:auth-delegator 权限。"
                ) from exc
            if exc.status == 401:
                raise ServiceUnavailable(
                    "集群拒绝了本服务的身份，无法校验调用方身份"
                ) from exc
            raise from_api_exception(exc, context="调用 TokenReview 失败") from exc
        except Exception as exc:  # noqa: BLE001 —— 见下方说明
            # 连接层失败（不可达 / DNS / TLS / 超时）SDK 直接抛 urllib3 原始异常。
            # 只带异常类名，不回显 message，避免泄露 API Server 地址等内部信息。
            raise ServiceUnavailable(
                f"无法连接 API Server 校验身份（{type(exc).__name__}）"
            ) from exc

        return self._interpret(review)

    def _interpret(self, review: object) -> Optional[Identity]:
        status = getattr(review, "status", None)
        if status is None:
            logger.warning(
                "TokenReview 响应中没有 status", extra={"event": "tokenreview_malformed"}
            )
            return None

        if not getattr(status, "authenticated", False):
            # status.error 说明失败原因，对排障有用，但它来自集群、
            # 且可能回显 token 片段，因此只写日志不返回给调用方。
            logger.info(
                "TokenReview 判定 token 未通过认证",
                extra={
                    "event": "token_invalid",
                    "reason": getattr(status, "error", None) or "unknown",
                },
            )
            return None

        user = getattr(status, "user", None)
        username = getattr(user, "username", None)
        if not username:
            # 认证通过但没有身份，无法做任何授权判定，只能当失败处理。
            logger.warning(
                "TokenReview 认证通过但未返回 username",
                extra={"event": "tokenreview_malformed"},
            )
            return None

        if not self._audience_matches(status):
            logger.warning(
                "TokenReview 返回的 audience 不包含本服务",
                extra={
                    "event": "token_audience_mismatch",
                    "expected": self._audience,
                    "actual": list(getattr(status, "audiences", None) or []),
                },
            )
            return None

        return Identity(
            username=username,
            uid=getattr(user, "uid", None),
            groups=tuple(getattr(user, "groups", None) or ()),
            method="tokenreview",
        )

    def _audience_matches(self, status: object) -> bool:
        """显式校验 audience。

        请求里已经带了 `spec.audiences`，正常情况下 API Server 会在
        audience 不匹配时直接返回 `authenticated: false`。这里再查一遍是因为
        `spec.audiences` 一旦被清空，TokenReview 就退化成「只验签名」，
        而 legacy 的、永不过期的 token 恰好能通过这种校验。
        这条防线的存在意味着：将来有人误删了 spec 里的 audience，
        结果仍然是「拒绝」，而不是「静默放宽信任边界」。
        """
        audiences = getattr(status, "audiences", None) or []
        return self._audience in audiences


def _fingerprint(token: str) -> str:
    """token 的 SHA-256，用作缓存 key。绝不把明文 token 存进任何结构。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
