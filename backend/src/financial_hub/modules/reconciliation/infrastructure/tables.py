"""SQLAlchemy Core tables of reconciliation, owned by migration ``0014_reconciliation``."""

from sqlalchemy import BigInteger, Column, Date, DateTime, Integer, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from financial_hub.platform.db import metadata

reconciliation_runs = Table(
    "reconciliation_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("provider", Text, nullable=False),  # '' for internal runs
    Column("provider_account_id", UUID(as_uuid=True)),
    Column("account_key", Text, nullable=False),  # provider_account_id as text, '' when none
    Column("window_start", Date, nullable=False),
    Column("window_end", Date, nullable=False),
    Column("status", Text, nullable=False),
    Column("triggered_by", Text, nullable=False),
    Column("summary", JSONB, nullable=False),
    Column("breaks_opened", Integer, nullable=False),
    Column("error_code", Text),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

reconciliation_breaks = Table(
    "reconciliation_breaks",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("last_run_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("subject", JSONB, nullable=False),
    Column("internal", JSONB(none_as_null=True)),
    Column("external", JSONB(none_as_null=True)),
    Column("currency", Text),
    Column("difference", BigInteger),
    Column("payment_intent_id", UUID(as_uuid=True)),
    Column("provider_payment_id", Text),
    Column("account_id", UUID(as_uuid=True)),
    Column("history", JSONB, nullable=False),
    Column("resolution", Text),
    Column("resolved_by", Text),
    Column("resolved_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

reconciliation_adjustments = Table(
    "reconciliation_adjustments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("break_id", UUID(as_uuid=True), nullable=False),
    Column("provider_account_id", UUID(as_uuid=True), nullable=False),
    Column("currency", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("reason", Text, nullable=False),
    Column("payload_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("proposed_by", UUID(as_uuid=True), nullable=False),
    Column("decided_by", UUID(as_uuid=True)),
    Column("decision_reason", Text),
    Column("journal_entry_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True)),
)
