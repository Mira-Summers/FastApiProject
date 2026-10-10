"""调用方身份。

Phase 2 只有审计需要「谁干的」这一信息，所以这里先只放一个纯数据类型；
真正的认证实现在 Phase 3（TokenReview）。

提前把类型定下来是为了让审计的调用签名一次成型——否则 Phase 3 要回头改一遍
所有审计调用点，而审计调用点恰恰是最不该被反复改动的地方。

**只保留 username / uid / groups。** 绝不把 token 原文、token 前缀或
`TokenReview` 的原始响应放进这个结构：它会一路流进审计文件和日志。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["ANONYMOUS", "Identity"]


@dataclass(frozen=True)
class Identity:
    """经过认证的调用方。"""

    username: str
    uid: Optional[str] = None
    groups: tuple[str, ...] = ()
    # 身份是怎么来的：tokenreview（正常）| disabled（本地开发免认证）。
    # 审计里必须带上，否则事后无法判断某次回滚到底有没有经过认证。
    method: str = "tokenreview"

    def audit_fields(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "uid": self.uid,
            "groups": list(self.groups),
            "method": self.method,
        }


# 未认证时的占位身份。生产环境下请求根本走不到业务逻辑就会 401，
# 所以它只会在「本地开发」或者 Phase 3 之前的版本里被写进审计。
ANONYMOUS = Identity(username="anonymous", method="none")
