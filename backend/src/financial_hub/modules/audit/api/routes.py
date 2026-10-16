"""``/v1/projects/{project_id}/audit-records``: read-only, append-only audit log (PRO-32).

Requires ``audit:read`` (operators only: API keys can never hold it). ``require()`` turns a
non-member into 404 (ADR-0011). Newest first; ``occurred_to`` is exclusive.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from financial_hub.modules.audit.application import (
    AuditFilters,
    AuditQueryService,
    AuditRecordView,
)
from financial_hub.modules.identity.application.api import Permission
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.shared.pagination import DEFAULT_LIMIT, MAX_LIMIT, Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}/audit-records", tags=["audit"])

CurrentScope = Annotated[ProjectScope, require(Permission.AUDIT_READ)]


def _params(
    cursor: Annotated[str | None, Query(description="Opaque cursor from `next_cursor`.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Page size.")] = DEFAULT_LIMIT,
) -> PageParams:
    return PageParams(cursor=cursor, limit=limit)


def _filters(
    action: Annotated[str | None, Query(max_length=100, description="Exact action.")] = None,
    actor_id: Annotated[str | None, Query(max_length=100)] = None,
    target_type: Annotated[str | None, Query(max_length=100)] = None,
    target_id: Annotated[str | None, Query(max_length=100)] = None,
    occurred_from: Annotated[datetime | None, Query(description="Inclusive.")] = None,
    occurred_to: Annotated[datetime | None, Query(description="Exclusive.")] = None,
) -> AuditFilters:
    return AuditFilters(
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )


class AuditRecordOut(BaseModel):
    id: str
    project_id: str | None
    actor_type: str
    actor_id: str | None
    action: str
    target_type: str
    target_id: str | None
    diff: dict[str, Any] | None
    correlation_id: str | None
    request_id: str | None
    ip: str | None
    user_agent: str | None
    occurred_at: datetime

    @classmethod
    def of(cls, view: AuditRecordView) -> AuditRecordOut:
        return cls(
            id=view.id,
            project_id=view.project_id,
            actor_type=view.actor_type,
            actor_id=view.actor_id,
            action=view.action,
            target_type=view.target_type,
            target_id=view.target_id,
            diff=view.diff,
            correlation_id=view.correlation_id,
            request_id=view.request_id,
            ip=view.ip,
            user_agent=view.user_agent,
            occurred_at=view.occurred_at,
        )


@router.get("")
async def list_audit_records(
    scope: CurrentScope,
    request: Request,
    params: Annotated[PageParams, Depends(_params)],
    filters: Annotated[AuditFilters, Depends(_filters)],
) -> Page[AuditRecordOut]:
    async with UnitOfWork(request.app.state.session_factory) as uow:
        page = await AuditQueryService(uow).list_records(scope.project_id, filters, params)
    return Page[AuditRecordOut](
        data=[AuditRecordOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )
