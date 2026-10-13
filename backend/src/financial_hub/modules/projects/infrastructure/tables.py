"""SQLAlchemy Core tables of projects, API keys and provider accounts.

``projects`` is owned by migration ``0003_identity``; ``api_keys`` and ``provider_accounts`` by
``0004_api_keys_providers``.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, UUID

from financial_hub.platform.db import metadata

projects = Table(
    "projects",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("slug", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('active', 'archived')", name="status"),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("environment", Text, nullable=False),
    Column("prefix", Text, nullable=False, unique=True),
    # HMAC-SHA256(pepper, secret): the secret itself is never stored.
    Column("secret_hash", BYTEA, nullable=False),
    Column("scopes", ARRAY(Text), nullable=False),
    Column("created_by", UUID(as_uuid=True), ForeignKey("operators.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
)

provider_accounts = Table(
    "provider_accounts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False),
    Column("provider", Text, nullable=False),
    Column("environment", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=False),
    # AES-256-GCM envelope: data key version, random 96-bit nonce, ciphertext || tag.
    Column("key_version", Integer, nullable=False),
    Column("nonce", BYTEA, nullable=False),
    Column("ciphertext", BYTEA, nullable=False),
    Column("credentials_hint", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
