"""SQLAlchemy Core tables of integrations, owned by migration ``0015_integrations``."""

from sqlalchemy import Boolean, Column, DateTime, Integer, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from financial_hub.platform.db import metadata

integration_manifests = Table(
    "integration_manifests",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("content", JSONB, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("validated_at", DateTime(timezone=True)),
    Column("approved_by", Text),
    Column("approved_at", DateTime(timezone=True)),
    Column("four_eyes", Boolean),
    Column("approval_base_version", Integer),
    Column("approval_diff", JSONB),
    Column("rejected_by", Text),
    Column("rejected_at", DateTime(timezone=True)),
    Column("rejection_reason", Text),
    Column("activated_at", DateTime(timezone=True)),
    Column("superseded_at", DateTime(timezone=True)),
)

integration_manifest_activations = Table(
    "integration_manifest_activations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("version", Integer, nullable=False),
    Column("previous_version", Integer),
    Column("activated_by", Text, nullable=False),
    Column("activated_at", DateTime(timezone=True), nullable=False),
    Column("materialized", JSONB, nullable=False),
)

integration_webhook_endpoints = Table(
    "integration_webhook_endpoints",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("endpoint_id", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("event_types", ARRAY(Text), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

business_events = Table(
    "business_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("payload_hash", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("manifest_version", Integer, nullable=False),
    Column("outcome", JSONB, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
)
