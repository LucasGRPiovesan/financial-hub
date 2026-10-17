"""SQLAlchemy Core tables of payments, owned by migrations ``0009_payments`` and
``0011_provider_webhooks``."""

from sqlalchemy import BigInteger, Column, Date, DateTime, Integer, LargeBinary, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from financial_hub.platform.db import metadata

payment_intents = Table(
    "payment_intents",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("customer_id", UUID(as_uuid=True), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("currency", Text, nullable=False),
    Column("method", Text, nullable=False),
    Column("due_date", Date, nullable=False),
    Column("external_ref", Text),
    Column("description", Text),
    Column("metadata", JSONB, nullable=False),
    Column("provider", Text, nullable=False),
    Column("provider_account_id", UUID(as_uuid=True)),
    Column("creation_status", Text, nullable=False),
    Column("attempt_id", UUID(as_uuid=True), nullable=False),
    Column("failure_code", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("invoice_id", UUID(as_uuid=True)),  # 0013_billing: the invoice it collects
)

provider_payments = Table(
    "provider_payments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("payment_intent_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("provider", Text, nullable=False),
    Column("provider_account_id", UUID(as_uuid=True)),
    Column("provider_payment_id", Text, nullable=False),
    Column("raw_status", Text, nullable=False),
    Column("capture_status", Text, nullable=False),
    Column("settlement_status", Text, nullable=False),
    Column("dispute_status", Text, nullable=False),
    Column("captured_amount", BigInteger, nullable=False),
    Column("net_amount", BigInteger),
    Column("fee_estimated", BigInteger),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

refunds = Table(
    "refunds",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("payment_intent_id", UUID(as_uuid=True), nullable=False),
    Column("provider_payment_id", UUID(as_uuid=True), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("currency", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("reason", Text),
    Column("provider_refund_id", Text),
    Column("attempt_id", UUID(as_uuid=True)),
    Column("origin", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

payment_attempts = Table(
    "payment_attempts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("payment_intent_id", UUID(as_uuid=True), nullable=False),
    Column("refund_id", UUID(as_uuid=True)),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("request_hash", LargeBinary, nullable=False),
    Column("outcome", JSONB(none_as_null=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
)

provider_webhook_events = Table(
    "provider_webhook_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("provider", Text, nullable=False),
    Column("provider_account_id", UUID(as_uuid=True), nullable=False),
    Column("provider_event_id", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("outcome", Text),
    Column("attempts", Integer, nullable=False),
    Column("last_error", Text),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("processed_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

provider_customer_provisions = Table(
    "provider_customer_provisions",
    metadata,
    Column("identity_id", UUID(as_uuid=True), primary_key=True),
    Column("provider_account_id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("attempt_id", UUID(as_uuid=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("lease_until", DateTime(timezone=True), nullable=False),
    Column("failure_code", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
