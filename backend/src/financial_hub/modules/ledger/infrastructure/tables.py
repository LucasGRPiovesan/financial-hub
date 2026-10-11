"""SQLAlchemy Core tables of the ledger (owned by migration ``0001_ledger_core``).

Triggers, the deferred integrity check, ``ledger_apply`` and ``ledger_lock_accounts`` live only in
the migration; these definitions mirror the columns and keys for queries (and autogenerate).
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB, UUID

from financial_hub.platform.db import metadata

ledger_accounts = Table(
    "ledger_accounts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("code", Text, nullable=False),
    Column("currency", CHAR(3), nullable=False),
    Column("type", Text, nullable=False),
    Column(
        "normal_balance",
        Text,
        Computed("CASE WHEN type IN ('asset', 'expense') THEN 'debit' ELSE 'credit' END"),
        nullable=False,
    ),
    Column("no_overdraft", Boolean, nullable=False, server_default=text("false")),
    Column("customer_id", UUID(as_uuid=True)),
    Column("payee_id", UUID(as_uuid=True)),
    Column("provider_account_id", UUID(as_uuid=True)),
    Column("posted_dr", BigInteger, nullable=False, server_default=text("0")),
    Column("posted_cr", BigInteger, nullable=False, server_default=text("0")),
    Column("pending_dr", BigInteger, nullable=False, server_default=text("0")),
    Column("pending_cr", BigInteger, nullable=False, server_default=text("0")),
    Column("version", BigInteger, nullable=False, server_default=text("0")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "project_id", "code", "currency", name="uq_ledger_accounts_project_code_currency"
    ),
    UniqueConstraint("id", "project_id", "currency", name="uq_ledger_accounts_id_project_currency"),
    CheckConstraint("type IN ('asset', 'liability', 'income', 'expense', 'equity')", name="type"),
)

journal_entries = Table(
    "journal_entries",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("applied_status", Text),
    Column("idempotency_key", Text, nullable=False),
    Column("payload_hash", Text, nullable=False),
    Column("recipe", Text, nullable=False),
    Column("reverses_entry_id", UUID(as_uuid=True)),
    Column("correlation_id", UUID(as_uuid=True)),
    Column("causation_id", UUID(as_uuid=True)),
    Column("effective_at", DateTime(timezone=True), nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "project_id", "idempotency_key", name="uq_journal_entries_project_idempotency_key"
    ),
    UniqueConstraint("id", "project_id", name="uq_journal_entries_id_project"),
    ForeignKeyConstraint(
        ["reverses_entry_id", "project_id"],
        ["journal_entries.id", "journal_entries.project_id"],
        name="fk_journal_entries_reverses_entry_id_journal_entries",
    ),
    CheckConstraint("status IN ('pending', 'posted', 'archived')", name="status"),
)

ledger_postings = Table(
    "ledger_postings",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("entry_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("account_id", UUID(as_uuid=True), nullable=False),
    Column("currency", CHAR(3), nullable=False),
    Column("direction", Text, nullable=False),
    Column("amount", BigInteger, nullable=False),
    ForeignKeyConstraint(
        ["entry_id", "project_id"],
        ["journal_entries.id", "journal_entries.project_id"],
        name="fk_ledger_postings_entry_id_journal_entries",
    ),
    ForeignKeyConstraint(
        ["account_id", "project_id", "currency"],
        ["ledger_accounts.id", "ledger_accounts.project_id", "ledger_accounts.currency"],
        name="fk_ledger_postings_account_id_ledger_accounts",
    ),
    CheckConstraint("direction IN ('debit', 'credit')", name="direction"),
    CheckConstraint("amount > 0", name="amount_positive"),
    Index("ix_ledger_postings_entry_id", "entry_id"),
    Index("ix_ledger_postings_account_id", "account_id"),
)
