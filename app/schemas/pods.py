"""Pod 相关的响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ContainerStatus(BaseModel):
    """单个容器的状态。运维排障时最关心的是 state_reason 与上次退出原因。"""

    name: str
    image: str
    ready: bool
    restart_count: int
    # running / waiting / terminated / unknown
    state: str
    # 容器当前处于等待或终止状态的原因，例如 CrashLoopBackOff、ImagePullBackOff、OOMKilled
    state_reason: Optional[str] = None
    # 上一次终止的原因与退出码——CrashLoopBackOff 排障的关键信息
    last_terminated_reason: Optional[str] = None
    last_exit_code: Optional[int] = None
    started_at: Optional[datetime] = None


class PodInfo(BaseModel):
    name: str
    namespace: str
    # Pod 的 phase：Pending / Running / Succeeded / Failed / Unknown
    phase: str
    node: Optional[str] = None
    pod_ip: Optional[str] = None
    created_at: Optional[datetime] = None
    # 人类可读的存活时长（如 3d4h），原实现返回的是原始时间戳
    age: str
    # 就绪容器数 / 总容器数，例如 "1/2"
    ready: str
    restarts: int
    # 汇总出来的最近一次失败原因，取重启次数最多的容器
    restart_reason: Optional[str] = None
    containers: list[ContainerStatus] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class PodListResponse(BaseModel):
    namespace: str
    count: int
    items: list[PodInfo]
    # 集群侧还有更多结果时返回，用于翻页；为 null 表示已取完
    continue_token: Optional[str] = None
