"""SQLAlchemy Core tables of metering, owned by migration ``0010_metering``."""

from sqlalchemy import Column, DateTime, Numeric, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from financial_hub.platform.db import metadata

billable_metrics = Table(
    "billable_metrics",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text),
    Column("aggregation", Text, nullable=False),
    Column("field", Text),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("archived_at", DateTime(timezone=True)),
)

usage_events = Table(
    "usage_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("customer_id", UUID(as_uuid=True), nullable=False),
    Column("metric_code", Text, nullable=False),
    Column("transaction_id", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("properties", JSONB, nullable=False),
    Column("value", Numeric(30, 9, asdecimal=True)),
    Column("unique_value", Text),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
)
