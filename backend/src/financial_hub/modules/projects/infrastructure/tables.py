"""SQLAlchemy Core table of projects (owned by migration ``0003_identity``)."""

from sqlalchemy import CheckConstraint, Column, DateTime, Table, Text, func
from sqlalchemy.dialects.postgresql import UUID

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
