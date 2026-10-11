"""认证器的抽象与 bearer token 解析。

认证只有一件事要做：把请求里的凭据换成一个可信的 `Identity`。
换不出来就抛 `Unauthorized`。

**本服务绝不自己解析 JWT。** 解析 JWT 意味着要自己维护签名校验、算法白名单，
以及最难的那部分——密钥轮换与吊销。TokenReview 把这些全部交给 API Server：
它是唯一知道「这个 token 现在还算不算数」的一方。自己解析等于绕开吊销机制。

实现只依赖本模块，模块不反向依赖实现，装配统一放在 `app/dependencies.py`。
这样依赖图是单向的，不会出现导入环。
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, runtime_checkable

from app.errors import Unauthorized
from app.k8s.client import CoreV1ApiLike
from app.security.identity import Identity

__all__ = [
    "MISSING_CREDENTIAL_MESSAGE",
    "WWW_AUTHENTICATE",
    "Authenticator",
    "extract_bearer_token",
    "unauthorized",
]

# RFC 6750 的挑战头。必须带上，否则调用方（包括各种 SDK 和 kubectl 插件）
# 只能从状态码猜「是该换 token 还是压根没带凭据」。
# 不带 error="invalid_token" 的细节：具体原因留给响应体，避免在多处重复维护措辞。
WWW_AUTHENTICATE = "Bearer"

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": WWW_AUTHENTICATE}


@runtime_checkable
class Authenticator(Protocol):
    """把凭据换成身份。失败必须抛 `Unauthorized`。

    两个设计点都是为了「不到必要不碰集群」：

    - `requires_credential` 让依赖层在**根本不带凭据**时直接返回 401，
      不进入认证器、不建立集群连接。否则一个没带 token 的请求会先触发
      一次 API Server 连接尝试，未认证流量因此变成了对外放大的放大器。
    - `load_api` 是**工厂**而不是现成的客户端。FastAPI 会无条件解析依赖，
      直接注入客户端等于无论用不用得上都先连一次集群；交给认证器自己决定
      何时调用，才能做到「有凭据要验，才去连」。
      缓存命中时同样不需要连集群——这是缓存除了省往返之外的第二个收益。
    """

    #: 该实现是否需要调用方提供凭据。`disabled` 模式为 False。
    requires_credential: bool

    def authenticate(
        self, credential: str, *, load_api: Callable[[], CoreV1ApiLike]
    ) -> Identity: ...


def unauthorized(message: str) -> Unauthorized:
    """构造带 WWW-Authenticate 头的 401。所有认证失败都走这里，保证口径一致。"""
    return Unauthorized(message, headers=dict(_UNAUTHORIZED_HEADERS))


#: 缺少凭据时的统一说法。依赖层与认证器共用，避免两处措辞不一致。
MISSING_CREDENTIAL_MESSAGE = (
    "缺少 bearer token：请在 Authorization 头中携带集群签发的 token"
)


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """从 `Authorization` 头里取出 bearer token。

    刻意不用 FastAPI 的 `HTTPBearer`：不同版本对「格式不对」的处理不一致
    （有的抛 403，有的抛 401），而这里需要精确控制——**凭据缺失或格式不对，
    对客户端而言都是「没有可用的有效凭据」，都应该是 401**。
    403 的含义是「身份已知但不允许」，用在这里会让排障的人去查 RBAC，
    而实际问题只是少了个 `Bearer ` 前缀。
    """
    if not authorization:
        return None
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None
