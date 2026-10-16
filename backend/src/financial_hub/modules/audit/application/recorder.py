"""Write audit records inside the caller's transaction.

``record`` never commits: the audit row commits or rolls back together with the action it
describes, so there is no audited action without a record and no record of an action that did not
happen.
"""

import json
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.audit.application.context import AuditContext
from financial_hub.modules.audit.domain import USER_AGENT_MAX_LENGTH, AuditTarget, redact
from financial_hub.modules.audit.infrastructure import repository
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid


def _json_default(value: object) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return str(value)  # UUID, Decimal, ... : text keeps the diff readable and serialisable


def _normalise(diff: Mapping[str, Any]) -> dict[str, Any]:
    """Redacted and JSON-safe copy of ``diff``."""
    return json.loads(json.dumps(redact(diff), default=_json_default))


class AuditRecorder:
    @staticmethod
    async def record(
        session: AsyncSession,
        *,
        actor: AuditContext,
        action: str,
        target: AuditTarget,
        diff: Mapping[str, Any] | None = None,
        project_id: UUID | None = None,
    ) -> str:
        """INSERT one record in ``session``'s transaction; returns its ``aud_`` id.

        ``actor`` is the full context (who + request facts). ``project_id`` is ``None`` for
        global actions such as operator login.
        """
        record_id = new_uuid()
        user_agent = actor.user_agent[:USER_AGENT_MAX_LENGTH] if actor.user_agent else None
        await repository.insert_record(
            session,
            {
                "id": record_id,
                "project_id": project_id,
                "actor_type": actor.actor.type.value,
                "actor_id": actor.actor.id,
                "action": action,
                "target_type": target.type,
                "target_id": target.id,
                "diff": None if diff is None else _normalise(diff),
                "correlation_id": actor.correlation_id,
                "request_id": actor.request_id,
                "ip": actor.ip,
                "user_agent": user_agent,
            },
        )
        return format_id(IdPrefix.AUDIT_RECORD, record_id)
