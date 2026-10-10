"""Pod 日志的响应模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class LogResponse(BaseModel):
    pod_name: str
    namespace: str
    # 未指定时表示使用 Pod 的默认（第一个）容器
    container: Optional[str] = None
    # 是否读取上一个已终止容器的日志——CrashLoopBackOff 场景必看
    previous: bool = False
    tail_lines: Optional[int] = None
    line_count: int
    lines: list[str]
