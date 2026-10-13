"""本地开发用的免认证逃生开关。

**这个类存在的唯一理由是本地调试时不想每次都去签 token。**
它不做任何校验，任何请求都会拿到同一个合成身份。

启用它的三道锁全部在 `Settings._validate_insecure_auth` 里，
这里不重复实现——两处校验迟早会不一致，而不一致的那种情况一定是最宽松的那处生效：

1. `APP_ENV=development`
2. `AUTH_ALLOW_INSECURE=true`（显式确认，防止随手写个 AUTH_MODE=disabled 就上线）
3. 环境中不存在 `KUBERNETES_SERVICE_HOST`（跑在 Pod 里就绝不接受）

第 3 条最关键：它挡住了「本地验证时打开的开关被一起打进镜像」这条路径。
真正跑在集群里的副本一定会被这条拒绝启动。
"""

from __future__ import annotations

import logging
from typing import Callable

from app.config import Settings
from app.k8s.client import CoreV1ApiLike
from app.security.identity import Identity

logger = logging.getLogger("app.security")

__all__ = ["DisabledAuthenticator"]


class DisabledAuthenticator:
    """不校验任何凭据，固定返回一个开发身份。"""

    requires_credential = False

    def __init__(self, settings: Settings) -> None:
        self._username = settings.auth_dev_username
        self._groups = tuple(settings.auth_dev_groups)
        logger.warning(
            "认证已被关闭，所有请求都会被当作固定开发身份",
            extra={
                "event": "auth_disabled",
                "username": self._username,
                "groups": list(self._groups),
            },
        )

    def authenticate(
        self, credential: str, *, load_api: Callable[[], CoreV1ApiLike]
    ) -> Identity:
        # credential 与 load_api 都被刻意忽略：这个模式下的语义就是「不校验」，
        # 所以连集群都不会连一下。
        # 若哪天想改成「带了 token 就顺带校验一下」，那是个更危险的中间态——
        # 半开半关的认证最容易让人误以为它是开着的。要么全关（本地），要么全开（生产）。
        return Identity(
            username=self._username,
            uid=None,
            groups=self._groups,
            method="disabled",
        )
