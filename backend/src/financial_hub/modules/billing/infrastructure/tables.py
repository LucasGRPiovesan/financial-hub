"""SQLAlchemy Core tables of plans, subscriptions and invoices (owned by migration 0013_billing)."""

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, Integer, Numeric, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from financial_hub.platform.db import metadata

plans = Table(
    "plans",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("billing_interval", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

plan_prices = Table(
    "plan_prices",
    metadata,
    Column("plan_id", UUID(as_uuid=True), primary_key=True),
    Column("price_id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("position", Integer, nullable=False),
)

subscriptions = Table(
    "subscriptions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("customer_id", UUID(as_uuid=True), nullable=False),
    Column("plan_id", UUID(as_uuid=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("anchor_day", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("trial_end", DateTime(timezone=True)),
    Column("current_period_start", DateTime(timezone=True), nullable=False),
    Column("current_period_end", DateTime(timezone=True), nullable=False),
    Column("cancel_at_period_end", Boolean, nullable=False),
    Column("canceled_at", DateTime(timezone=True)),
    Column("ended_at", DateTime(timezone=True)),
    Column("payment_method", Text, nullable=False),
    Column("days_until_due", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

invoices = Table(
    "invoices",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("subscription_id", UUID(as_uuid=True), nullable=False),
    Column("customer_id", UUID(as_uuid=True), nullable=False),
    Column("currency", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("period_start", DateTime(timezone=True), nullable=False),
    Column("period_end", DateTime(timezone=True), nullable=False),
    Column("total", BigInteger, nullable=False),
    Column("credits_applied", BigInteger, nullable=False),
    Column("amount_due", BigInteger, nullable=False),
    Column("payment_intent_id", UUID(as_uuid=True)),
    Column("due_date", Date),
    Column("finalized_at", DateTime(timezone=True)),
    Column("paid_at", DateTime(timezone=True)),
    Column("voided_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

invoice_lines = Table(
    "invoice_lines",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("invoice_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("position", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("price_id", UUID(as_uuid=True), nullable=False),
    Column("price_code", Text, nullable=False),
    Column("price_version_id", UUID(as_uuid=True), nullable=False),
    Column("price_version", Integer, nullable=False),
    Column("model", Text, nullable=False),
    Column("metric_code", Text),
    Column("quantity", Numeric(38, 18, asdecimal=True)),
    Column("amount", BigInteger, nullable=False),
    Column("rounding", Text, nullable=False),
    Column("details", JSONB, nullable=False),
)
