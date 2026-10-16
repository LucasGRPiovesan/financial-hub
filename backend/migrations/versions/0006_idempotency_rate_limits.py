"""idempotency_records and rate_limit_buckets: Idempotency-Key replay and token buckets (PRO-33)

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions. Both tables are operational
state, not records: the runtime role ``fh_app`` gets SELECT, INSERT, UPDATE and DELETE (expired
keys and idle buckets are purged by the cleanup job).

- ``idempotency_records`` (ADR-0004): one row per ``(project_id, principal_id, key)``; global
  routes use the nil project id. ``in_progress`` rows carry no response; ``completed`` rows carry
  the response status, the allowlisted headers and the body (NULL when it was not stored: too
  large, or a secret shown once). ``request_hash`` is a SHA-256 digest.
- ``rate_limit_buckets``: one row per bucket; ``tokens`` is exact ``numeric`` (never a float),
  refilled by elapsed time and decremented in one ``INSERT ... ON CONFLICT DO UPDATE``.

Revision ID: 0006_idempotency_rate_limits
Revises: 0005_audit
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_idempotency_rate_limits"
down_revision: str | None = "0005_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.idempotency_records (
    project_id uuid NOT NULL,
    principal_id text NOT NULL,
    key text NOT NULL,
    method text NOT NULL,
    path text NOT NULL,
    request_hash bytea NOT NULL,
    status text NOT NULL,
    response_status smallint,
    response_headers jsonb,
    response_body bytea,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CONSTRAINT pk_idempotency_records PRIMARY KEY (project_id, principal_id, key),
    CONSTRAINT ck_idempotency_records_key CHECK (key ~ '^[!-~]{1,255}$'),
    CONSTRAINT ck_idempotency_records_principal_id CHECK (length(principal_id) BETWEEN 1 AND 100),
    CONSTRAINT ck_idempotency_records_request_hash CHECK (length(request_hash) = 32),
    CONSTRAINT ck_idempotency_records_status CHECK (status IN ('in_progress', 'completed')),
    CONSTRAINT ck_idempotency_records_response CHECK (
        (status = 'in_progress' AND response_status IS NULL AND response_headers IS NULL
            AND response_body IS NULL)
        OR (status = 'completed' AND response_status BETWEEN 100 AND 599)
    ),
    CONSTRAINT ck_idempotency_records_expiry CHECK (expires_at > created_at)
);
CREATE INDEX ix_idempotency_records_expires_at ON public.idempotency_records (expires_at);

CREATE TABLE public.rate_limit_buckets (
    bucket_key text NOT NULL,
    tokens numeric(18, 6) NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_rate_limit_buckets PRIMARY KEY (bucket_key),
    CONSTRAINT ck_rate_limit_buckets_bucket_key CHECK (length(bucket_key) BETWEEN 1 AND 200),
    CONSTRAINT ck_rate_limit_buckets_tokens CHECK (tokens >= 0)
);
CREATE INDEX ix_rate_limit_buckets_updated_at ON public.rate_limit_buckets (updated_at);
"""

GRANTS = """
REVOKE ALL ON public.idempotency_records, public.rate_limit_buckets FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.idempotency_records, public.rate_limit_buckets TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP TABLE public.rate_limit_buckets, public.idempotency_records")
