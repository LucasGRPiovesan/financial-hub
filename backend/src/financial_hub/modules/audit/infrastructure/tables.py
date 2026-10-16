"""SQLAlchemy Core table of the audit log (owned by migration ``0005_audit``)."""

from sqlalchemy import Column, DateTime, ForeignKey, Table, Text, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

from financial_hub.platform.db import metadata

audit_records = Table(
    "audit_records",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id")),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text),
    Column("action", Text, nullable=False),
    Column("target_type", Text, nullable=False),
    Column("target_id", Text),
    Column("diff", JSONB),
    Column("correlation_id", Text),
    Column("request_id", Text),
    Column("ip", INET),
    Column("user_agent", Text),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
