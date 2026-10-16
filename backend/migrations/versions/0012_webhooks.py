"""Outbound webhooks: webhook_endpoints and webhook_delivery_attempts (W6.3, INTEGRATIONS s3).

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions.

- ``webhook_endpoints``: one per project receiver. Each row owns exactly one ``event_subscriptions``
  row of kind ``webhook`` (``subscription_id``), so publishing fans out to it like to any internal
  handler. The signing secret is AES-256-GCM sealed (``secret_*``); during a rotation the previous
  secret stays sealed in ``previous_*`` until ``previous_expires_at`` (all four or none). ``fh_app``
  has no DELETE: endpoints are disabled, never removed (their deliveries stay inspectable).
- ``webhook_delivery_attempts``: append-only log of each HTTP attempt of a webhook delivery
  (status code, latency, truncated error). ``fh_app`` may only INSERT and SELECT.

Revision ID: 0012_webhooks
Revises: 0011_provider_webhooks
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_webhooks"
down_revision: str | None = "0011_provider_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.webhook_endpoints (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    url text NOT NULL,
    description text,
    event_types text[] NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    secret_key_version integer NOT NULL,
    secret_nonce bytea NOT NULL,
    secret_ciphertext bytea NOT NULL,
    previous_key_version integer,
    previous_nonce bytea,
    previous_ciphertext bytea,
    previous_expires_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    disabled_at timestamptz,
    CONSTRAINT pk_webhook_endpoints PRIMARY KEY (id),
    CONSTRAINT uq_webhook_endpoints_subscription_id UNIQUE (subscription_id),
    CONSTRAINT fk_webhook_endpoints_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT fk_webhook_endpoints_subscription_id_event_subscriptions
        FOREIGN KEY (subscription_id) REFERENCES public.event_subscriptions (id),
    CONSTRAINT ck_webhook_endpoints_url
        CHECK (length(url) BETWEEN 1 AND 2048 AND url ~ '^https?://'),
    CONSTRAINT ck_webhook_endpoints_description CHECK (length(description) BETWEEN 1 AND 500),
    CONSTRAINT ck_webhook_endpoints_event_types
        CHECK (cardinality(event_types) BETWEEN 1 AND 50),
    CONSTRAINT ck_webhook_endpoints_previous_secret CHECK (
        (previous_key_version IS NULL AND previous_nonce IS NULL
         AND previous_ciphertext IS NULL AND previous_expires_at IS NULL)
        OR (previous_key_version IS NOT NULL AND previous_nonce IS NOT NULL
            AND previous_ciphertext IS NOT NULL AND previous_expires_at IS NOT NULL)
    ),
    CONSTRAINT ck_webhook_endpoints_disabled_at CHECK (enabled = (disabled_at IS NULL))
);

CREATE INDEX ix_webhook_endpoints_project_id_created_at
    ON public.webhook_endpoints (project_id, created_at DESC, id DESC);

CREATE TABLE public.webhook_delivery_attempts (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    endpoint_id uuid NOT NULL,
    delivery_id uuid NOT NULL,
    attempt integer NOT NULL,
    outcome text NOT NULL,
    status_code integer,
    latency_ms integer NOT NULL,
    error text,
    attempted_at timestamptz NOT NULL,
    CONSTRAINT pk_webhook_delivery_attempts PRIMARY KEY (id),
    CONSTRAINT fk_webhook_delivery_attempts_endpoint_id_webhook_endpoints
        FOREIGN KEY (endpoint_id) REFERENCES public.webhook_endpoints (id),
    CONSTRAINT fk_webhook_delivery_attempts_delivery_id_event_deliveries
        FOREIGN KEY (delivery_id) REFERENCES public.event_deliveries (id),
    CONSTRAINT ck_webhook_delivery_attempts_attempt CHECK (attempt >= 1),
    CONSTRAINT ck_webhook_delivery_attempts_outcome
        CHECK (outcome IN ('delivered', 'failed', 'refused')),
    CONSTRAINT ck_webhook_delivery_attempts_status_code CHECK (status_code BETWEEN 100 AND 599),
    CONSTRAINT ck_webhook_delivery_attempts_latency_ms CHECK (latency_ms >= 0),
    CONSTRAINT ck_webhook_delivery_attempts_error CHECK (length(error) <= 2000)
);

CREATE INDEX ix_webhook_delivery_attempts_delivery_id
    ON public.webhook_delivery_attempts (delivery_id, attempted_at);
CREATE INDEX ix_webhook_delivery_attempts_endpoint_id_attempted_at
    ON public.webhook_delivery_attempts (endpoint_id, attempted_at DESC, id DESC);
"""

GRANTS = """
REVOKE ALL ON public.webhook_endpoints, public.webhook_delivery_attempts FROM PUBLIC;

GRANT SELECT ON public.webhook_endpoints, public.webhook_delivery_attempts TO fh_app;
GRANT INSERT (id, project_id, subscription_id, url, description, event_types, enabled,
              secret_key_version, secret_nonce, secret_ciphertext, created_at, updated_at)
    ON public.webhook_endpoints TO fh_app;
GRANT UPDATE (url, description, event_types, enabled, secret_key_version, secret_nonce,
              secret_ciphertext, previous_key_version, previous_nonce, previous_ciphertext,
              previous_expires_at, updated_at, disabled_at)
    ON public.webhook_endpoints TO fh_app;
GRANT INSERT (id, project_id, endpoint_id, delivery_id, attempt, outcome, status_code, latency_ms,
              error, attempted_at)
    ON public.webhook_delivery_attempts TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP TABLE public.webhook_delivery_attempts, public.webhook_endpoints")
