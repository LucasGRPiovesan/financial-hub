"""Read side of the audit log: filtered, cursor-paginated, newest first."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from financial_hub.modules.audit.infrastructure import repository
from financial_hub.modules.audit.infrastructure.repository import AuditFilters, AuditRow
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.ids import IdPrefix, format_id
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page


@dataclass(frozen=True, slots=True)
class AuditRecordView:
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


def _view(row: AuditRow) -> AuditRecordView:
    return AuditRecordView(
        id=format_id(IdPrefix.AUDIT_RECORD, row.id),
        project_id=None if row.project_id is None else format_id(IdPrefix.PROJECT, row.project_id),
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        diff=row.diff,
        correlation_id=row.correlation_id,
        request_id=row.request_id,
        ip=row.ip,
        user_agent=row.user_agent,
        occurred_at=row.occurred_at,
    )


class AuditQueryService:
    """Runs inside the caller's active :class:`UnitOfWork`."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def list_records(
        self, project_id: UUID, filters: AuditFilters, params: PageParams
    ) -> Page[AuditRecordView]:
        rows = await repository.list_records(
            self._uow.session,
            project_id,
            filters,
            cursor=params.decoded_cursor(),
            limit=params.limit + 1,
        )
        return build_page(
            rows, params.limit, _view, lambda row: Cursor(created_at=row.occurred_at, id=row.id)
        )
