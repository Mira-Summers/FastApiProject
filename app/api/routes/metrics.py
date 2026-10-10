"""Prometheus 抓取端点。

**这个端点刻意不做认证**，而且以后也不要加：Prometheus 抓取时不会带 token，
给它加认证的结果是抓取静默失败——不会报错，只会一直显示旧数据，非常难排查。

正确的收敛方式是网络层（Phase 5 的 NetworkPolicy 只允许监控 namespace 访问本端口），
而不是在这一层加鉴权。这条约束是刻意的，不是遗漏。
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["metrics"])


def _content_type() -> str:
    """去掉 `charset` 参数。

    `CONTENT_TYPE_LATEST` 本身已经带了 `charset=utf-8`，而 Starlette 0.27 会给任何
    `text/*` 的 media_type **无条件**再追加一次 charset，结果是
    `text/plain; version=0.0.4; charset=utf-8; charset=utf-8`——重复参数。
    Go 的 `mime.ParseMediaType` 会因为重复参数名直接报错，抓取端解析不了这个头。
    所以这里先摘掉，让 Starlette 补上唯一的一个。

    `version=0.0.4` 仍然取自库常量，不写死，避免将来格式版本升级时对不上。
    """
    parts = [
        part.strip()
        for part in CONTENT_TYPE_LATEST.split(";")
        if not part.strip().lower().startswith("charset=")
    ]
    return "; ".join(parts)


@router.get(
    "/metrics",
    summary="Prometheus 指标",
    # 指标端点不该出现在给调用方看的 OpenAPI 文档里，它不是业务接口。
    include_in_schema=False,
)
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=_content_type())
