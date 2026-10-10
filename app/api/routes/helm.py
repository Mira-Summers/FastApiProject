"""Helm release 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.audit import (
    AUDIT_OUTCOME_ATTEMPTED,
    AUDIT_OUTCOME_FAILED,
    AUDIT_OUTCOME_SUCCEEDED,
    AuditEvent,
    AuditLogger,
)
from app.dependencies import get_audit_logger, get_helm_runner
from app.errors import BadRequest, OpsError
from app.helm.runner import HelmRunner
from app.helm.service import get_history as fetch_history
from app.helm.service import rollback as perform_rollback
from app.metrics import record_rollback
from app.schemas.errors import COMMON_ERROR_RESPONSES
from app.schemas.helm import (
    HelmHistoryResponse,
    RollbackRequest,
    RollbackResponse,
)

router = APIRouter(
    prefix="/api/v1/namespaces/{namespace}/helm/releases",
    tags=["helm"],
    responses=COMMON_ERROR_RESPONSES,
)


@router.get(
    "/{release_name}/history", response_model=HelmHistoryResponse, summary="查询 release 的版本历史"
)
def get_history(
    namespace: str,
    release_name: str,
    runner: HelmRunner = Depends(get_helm_runner),
) -> HelmHistoryResponse:
    revisions = fetch_history(runner, release_name, namespace)
    return HelmHistoryResponse(release=release_name, namespace=namespace, revisions=revisions)


@router.post(
    "/{release_name}/rollback",
    response_model=RollbackResponse,
    summary="回滚 release 到指定版本",
    description=(
        "回滚是不可逆的生产变更：请求体必须显式包含 `confirm: true`。\n\n"
        "不传 `revision` 时回滚到上一个版本。"
    ),
)
def rollback(
    namespace: str,
    release_name: str,
    payload: RollbackRequest,
    runner: HelmRunner = Depends(get_helm_runner),
    audit: AuditLogger = Depends(get_audit_logger),
) -> RollbackResponse:
    # 二次确认：防止脚本或误操作直接把生产回滚掉。
    if not payload.confirm:
        raise BadRequest("回滚是不可逆的生产变更，需要在请求体中显式传入 confirm=true")

    resource = {
        "namespace": namespace,
        "release": release_name,
        "requested_revision": payload.revision,
    }
    # 先写意图，再动手。回滚一旦开始就无法撤销，必须在执行之前留下
    # 「谁要对什么做什么」的记录——进程如果在执行中被杀，这条就是唯一的痕迹。
    audit.record(
        AuditEvent(action="helm.rollback", outcome=AUDIT_OUTCOME_ATTEMPTED, resource=resource)
    )

    try:
        outcome = perform_rollback(runner, release_name, namespace, payload.revision)
    except OpsError as exc:
        record_rollback(namespace, AUDIT_OUTCOME_FAILED)
        audit.record(
            AuditEvent(
                action="helm.rollback",
                outcome=AUDIT_OUTCOME_FAILED,
                resource=resource,
                detail=exc.message,
            )
        )
        raise

    record_rollback(namespace, AUDIT_OUTCOME_SUCCEEDED)
    audit.record(
        AuditEvent(
            action="helm.rollback",
            outcome=AUDIT_OUTCOME_SUCCEEDED,
            resource={**resource, "to_revision": outcome.to_revision},
        )
    )
    return RollbackResponse(
        release=outcome.release,
        namespace=outcome.namespace,
        to_revision=outcome.to_revision,
        message=outcome.message,
        output=outcome.output,
    )
