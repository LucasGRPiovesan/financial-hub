"""SQLAlchemy Core tables of the price catalog, owned by migration ``0013_billing``."""

from sqlalchemy import Column, DateTime, Integer, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from financial_hub.platform.db import metadata

prices = Table(
    "prices",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("metric_code", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

price_versions = Table(
    "price_versions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("price_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("version", Integer, nullable=False),
    Column("model", JSONB, nullable=False),
    Column("effective_from", DateTime(timezone=True), nullable=False),
    Column("effective_to", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
