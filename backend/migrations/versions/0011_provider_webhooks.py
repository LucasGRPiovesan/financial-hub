"""Inbound provider webhooks and customer provisioning (W4.4, docs/INTEGRATIONS.md s2).

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions. ``fh_app`` gets no
DELETE/TRUNCATE and UPDATE only on the columns the application changes:

- ``provider_webhook_events``: the raw inbox of provider notifications. ``(provider_account_id,
  provider_event_id)`` is unique (the provider's event id, Asaas ``evt_...``, is the dedup key), so
  a redelivery inserts nothing and does no work. ``body`` is the request body exactly as received
  (bounded by the route's size limit). Processing never trusts it for amounts: it only names the
  payment to refetch. ``status``: ``received`` (stored, not processed yet), ``processed``,
  ``ignored`` (a documented event with no effect), ``parked`` (unknown/unsupported, kept for
  review), ``failed`` (retried by the sweeper until ``attempts`` reaches its limit).
- ``provider_customer_provisions``: the lease of the customer-provisioning call (the same
  external-call protocol as charges, ADR-0012 s3). One row per ``(identity, provider account)``;
  ``attempt_id`` is the fencing token and ``lease_until`` keeps a second caller from sending a
  concurrent create while a call (or its unknown outcome) may still land at the provider.

Triggers keep both tables inside the provider account's project.

Revision ID: 0011_provider_webhooks
Revises: 0010_metering
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_provider_webhooks"
down_revision: str | None = "0010_metering"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.provider_webhook_events (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    provider text NOT NULL,
    provider_account_id uuid NOT NULL,
    provider_event_id text NOT NULL,
    event_type text NOT NULL,
    body text NOT NULL,
    status text NOT NULL DEFAULT 'received',
    outcome text,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    received_at timestamptz NOT NULL,
    processed_at timestamptz,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_provider_webhook_events PRIMARY KEY (id),
    CONSTRAINT uq_provider_webhook_events_provider_event_id
        UNIQUE (provider_account_id, provider_event_id),
    CONSTRAINT fk_provider_webhook_events_provider_account_id_provider_accounts
        FOREIGN KEY (provider_account_id) REFERENCES public.provider_accounts (id),
    CONSTRAINT ck_provider_webhook_events_provider CHECK (length(provider) BETWEEN 1 AND 50),
    CONSTRAINT ck_provider_webhook_events_provider_event_id
        CHECK (length(provider_event_id) BETWEEN 1 AND 200),
    CONSTRAINT ck_provider_webhook_events_event_type
        CHECK (length(event_type) BETWEEN 1 AND 100),
    CONSTRAINT ck_provider_webhook_events_body CHECK (octet_length(body) <= 1048576),
    CONSTRAINT ck_provider_webhook_events_status
        CHECK (status IN ('received', 'processed', 'ignored', 'parked', 'failed')),
    CONSTRAINT ck_provider_webhook_events_outcome CHECK (length(outcome) <= 100),
    CONSTRAINT ck_provider_webhook_events_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_provider_webhook_events_last_error CHECK (length(last_error) <= 200),
    CONSTRAINT ck_provider_webhook_events_processed_at
        CHECK ((processed_at IS NULL) = (status IN ('received', 'failed')))
);
-- The sweeper's retry path: rows not processed yet, oldest first.
CREATE INDEX ix_provider_webhook_events_pending ON public.provider_webhook_events (received_at)
    WHERE status IN ('received', 'failed');
CREATE INDEX ix_provider_webhook_events_project_id
    ON public.provider_webhook_events (project_id, received_at);

CREATE TABLE public.provider_customer_provisions (
    identity_id uuid NOT NULL,
    provider_account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    status text NOT NULL,
    lease_until timestamptz NOT NULL,
    failure_code text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_provider_customer_provisions PRIMARY KEY (identity_id, provider_account_id),
    CONSTRAINT fk_provider_customer_provisions_identity_id_customer_identities
        FOREIGN KEY (identity_id) REFERENCES public.customer_identities (id),
    CONSTRAINT fk_provider_customer_provisions_provider_account_id_provider_accounts
        FOREIGN KEY (provider_account_id) REFERENCES public.provider_accounts (id),
    CONSTRAINT ck_provider_customer_provisions_status
        CHECK (status IN ('requested', 'unknown', 'created', 'rejected')),
    CONSTRAINT ck_provider_customer_provisions_failure_code CHECK (length(failure_code) <= 100)
);

-- Both rows live inside the provider account's project (ADR-0011).
CREATE FUNCTION public.provider_inbound_same_project() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF (SELECT project_id FROM public.provider_accounts WHERE id = NEW.provider_account_id)
        IS DISTINCT FROM NEW.project_id THEN
        RAISE EXCEPTION 'provider account % is not in project %',
            NEW.provider_account_id, NEW.project_id
            USING ERRCODE = 'check_violation';
    END IF;
    -- Nested, not AND: PL/pgSQL may evaluate NEW.identity_id on the webhook table, which has none.
    IF TG_TABLE_NAME = 'provider_customer_provisions' THEN
        IF (SELECT project_id FROM public.customer_identities WHERE id = NEW.identity_id)
            IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'customer identity % is not in project %',
                NEW.identity_id, NEW.project_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER provider_webhook_events_same_project
BEFORE INSERT ON public.provider_webhook_events
FOR EACH ROW EXECUTE FUNCTION public.provider_inbound_same_project();

CREATE TRIGGER provider_customer_provisions_same_project
BEFORE INSERT ON public.provider_customer_provisions
FOR EACH ROW EXECUTE FUNCTION public.provider_inbound_same_project();
"""

GRANTS = """
REVOKE ALL ON public.provider_webhook_events, public.provider_customer_provisions FROM PUBLIC;
REVOKE ALL ON FUNCTION public.provider_inbound_same_project() FROM PUBLIC;

GRANT SELECT ON public.provider_webhook_events, public.provider_customer_provisions TO fh_app;
GRANT INSERT (id, project_id, provider, provider_account_id, provider_event_id, event_type, body,
              status, received_at, updated_at)
    ON public.provider_webhook_events TO fh_app;
GRANT UPDATE (status, outcome, attempts, last_error, processed_at, updated_at)
    ON public.provider_webhook_events TO fh_app;
GRANT INSERT (identity_id, provider_account_id, project_id, attempt_id, status, lease_until,
              failure_code, created_at, updated_at)
    ON public.provider_customer_provisions TO fh_app;
GRANT UPDATE (attempt_id, status, lease_until, failure_code, updated_at)
    ON public.provider_customer_provisions TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP TABLE public.provider_customer_provisions, public.provider_webhook_events")
    op.execute("DROP FUNCTION public.provider_inbound_same_project()")
