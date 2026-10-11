"""SQLAlchemy Core tables of identity (owned by migration ``0003_identity``).

They mirror columns and keys for metadata; the queries live in ``repository`` as plain SQL.
``projects`` belongs to the projects module and is declared there.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import INET, UUID

from financial_hub.platform.db import metadata

operators = Table(
    "operators",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("email", Text, nullable=False, unique=True),
    Column("display_name", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="active"),
    Column("failed_login_count", Integer, nullable=False, server_default="0"),
    Column("locked_until", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("password_changed_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('active', 'disabled')", name="status"),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", LargeBinary, primary_key=True),
    Column("operator_id", UUID(as_uuid=True), ForeignKey("operators.id"), nullable=False),
    Column("csrf_token_hash", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("idle_expires_at", DateTime(timezone=True), nullable=False),
    Column("absolute_expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("user_agent", Text),
    Column("ip", INET),
)

project_memberships = Table(
    "project_memberships",
    metadata,
    Column("operator_id", UUID(as_uuid=True), ForeignKey("operators.id"), nullable=False),
    # FK to projects(id) exists in the database; not mirrored to keep modules' metadata apart.
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("role", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("operator_id", "project_id"),
    CheckConstraint("role IN ('owner', 'admin', 'finance', 'viewer')", name="role"),
)

login_throttle = Table(
    "login_throttle",
    metadata,
    Column("scope", Text, nullable=False),
    Column("key_hash", LargeBinary, nullable=False),
    Column("window_started_at", DateTime(timezone=True), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("blocked_until", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("scope", "key_hash"),
)
