"""Public API of audit for other modules (ARCHITECTURE s3, import-linter contract 6).

Other modules record privileged actions through these exports only, passing their own session so
the audit row shares the action's transaction.
"""

from financial_hub.modules.audit.application.context import AuditContext, request_audit_context
from financial_hub.modules.audit.application.queries import AuditQueryService, AuditRecordView
from financial_hub.modules.audit.application.recorder import AuditRecorder
from financial_hub.modules.audit.domain import (
    REDACTED,
    Actor,
    ActorType,
    AuditTarget,
    changed_fields,
)
from financial_hub.modules.audit.infrastructure.repository import AuditFilters

__all__ = [
    "REDACTED",
    "Actor",
    "ActorType",
    "AuditContext",
    "AuditFilters",
    "AuditQueryService",
    "AuditRecordView",
    "AuditRecorder",
    "AuditTarget",
    "changed_fields",
    "request_audit_context",
]
