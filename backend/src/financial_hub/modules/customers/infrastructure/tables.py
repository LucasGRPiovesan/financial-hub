"""SQLAlchemy Core tables of customers, owned by migration ``0007_customers``."""

from sqlalchemy import Column, DateTime, Integer, LargeBinary, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from financial_hub.platform.db import metadata

financial_customers = Table(
    "financial_customers",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("email", Text),
    Column("phone", Text),
    Column("document_hash", LargeBinary),
    Column("document_key_version", Integer),
    Column("document_nonce", LargeBinary),
    Column("document_ciphertext", LargeBinary),
    Column("document_last4", Text),
    Column("anonymized_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

customer_identities = Table(
    "customer_identities",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("financial_customer_id", UUID(as_uuid=True), nullable=False),
    Column("external_ref", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("email", Text),
    Column("phone", Text),
    Column("metadata", JSONB, nullable=False),
    Column("anonymized_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

provider_customers = Table(
    "provider_customers",
    metadata,
    Column("identity_id", UUID(as_uuid=True), primary_key=True),
    Column("provider_account_id", UUID(as_uuid=True), primary_key=True),
    Column("provider_customer_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
