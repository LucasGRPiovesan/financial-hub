"""SQLAlchemy Core tables of the event log (owned by migration ``0002_events``).

These mirror columns and keys for metadata/autogenerate; the queries live in ``repository`` as
plain SQL because they rely on Postgres features (``xid8``, ``SKIP LOCKED``, snapshot functions).
"""

from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.types import UserDefinedType

from financial_hub.platform.db import metadata


class Xid8(UserDefinedType[Any]):
    """Postgres ``xid8`` (64-bit transaction id). Read it as ``txid::text`` and convert."""

    cache_ok = True

    def get_col_spec(self, **_kw: Any) -> str:
        return "xid8"


events = Table(
    "events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("type", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("subject", Text),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("correlation_id", UUID(as_uuid=True)),
    Column("causation_id", UUID(as_uuid=True)),
    Column("traceparent", Text),
    Column("data", JSONB, nullable=False),
    Column("txid", Xid8(), nullable=False, server_default=text("pg_catalog.pg_current_xact_id()")),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

event_subscriptions = Table(
    "event_subscriptions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True)),
    Column("kind", Text, nullable=False),
    Column("handler", Text),
    Column("endpoint_id", UUID(as_uuid=True)),
    Column("event_types", ARRAY(Text), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "project_id",
        "handler",
        "endpoint_id",
        name="uq_event_subscriptions_target",
        postgresql_nulls_not_distinct=True,
    ),
)

event_deliveries = Table(
    "event_deliveries",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("event_id", UUID(as_uuid=True), ForeignKey("events.id"), nullable=False),
    Column(
        "subscription_id",
        UUID(as_uuid=True),
        ForeignKey("event_subscriptions.id"),
        nullable=False,
    ),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("lease_until", DateTime(timezone=True)),
    Column("lease_token", UUID(as_uuid=True)),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "event_id", "subscription_id", name="uq_event_deliveries_event_id_subscription_id"
    ),
)

inbox = Table(
    "inbox",
    metadata,
    Column("handler", Text, primary_key=True),
    Column("event_id", UUID(as_uuid=True), ForeignKey("events.id"), primary_key=True),
    Column("processed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

schedules = Table(
    "schedules",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("interval_seconds", Integer, nullable=False),
    Column("next_due_at", DateTime(timezone=True), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("project_id", "name", name="uq_schedules_project_id_name"),
)

schedule_runs = Table(
    "schedule_runs",
    metadata,
    Column("schedule_id", UUID(as_uuid=True), ForeignKey("schedules.id"), primary_key=True),
    Column("due_at", DateTime(timezone=True), primary_key=True),
    Column("event_id", UUID(as_uuid=True), ForeignKey("events.id"), nullable=False),
    Column("emitted_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
