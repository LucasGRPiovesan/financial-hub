"""Metering: billable_metrics and usage_events (PRO-46, docs/METERING.md).

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions.

- ``billable_metrics``: how usage is aggregated, per project. ``code`` is unique per project and,
  with ``aggregation`` and ``field``, immutable: ``fh_app`` may only archive a metric (UPDATE on
  ``status``, ``archived_at``, ``updated_at``) and a trigger refuses to un-archive it, so history
  never changes meaning. ``field`` names the event property a ``sum``, ``max`` or ``unique_count``
  reads (NULL exactly for ``count``).
- ``usage_events``: append-only. ``fh_app`` gets INSERT and SELECT only, and triggers refuse
  UPDATE, DELETE and TRUNCATE for every role. ``(project_id, transaction_id)`` is unique: the
  idempotency key of ingestion. Measures are exact: ``value`` is ``numeric(30,9)`` (sum/max) and
  ``unique_value`` is text (unique_count); there is no floating point column. The metric is tied to
  its project by a composite foreign key and the customer by a trigger (ADR-0011).

Revision ID: 0010_metering
Revises: 0009_payments
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_metering"
down_revision: str | None = "0009_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.billable_metrics (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    description text,
    aggregation text NOT NULL,
    field text,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    archived_at timestamptz,
    CONSTRAINT pk_billable_metrics PRIMARY KEY (id),
    CONSTRAINT uq_billable_metrics_project_id_code UNIQUE (project_id, code),
    CONSTRAINT fk_billable_metrics_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT ck_billable_metrics_code CHECK (code ~ '^[a-z][a-z0-9_]{0,62}$'),
    CONSTRAINT ck_billable_metrics_name CHECK (length(name) BETWEEN 1 AND 100),
    CONSTRAINT ck_billable_metrics_description CHECK (length(description) BETWEEN 1 AND 500),
    CONSTRAINT ck_billable_metrics_aggregation
        CHECK (aggregation IN ('sum', 'count', 'max', 'unique_count')),
    CONSTRAINT ck_billable_metrics_field CHECK (field ~ '^[A-Za-z_][A-Za-z0-9_]{0,63}$'),
    CONSTRAINT ck_billable_metrics_aggregation_field
        CHECK ((aggregation = 'count') = (field IS NULL)),
    CONSTRAINT ck_billable_metrics_status CHECK (status IN ('active', 'archived')),
    CONSTRAINT ck_billable_metrics_archived_at
        CHECK ((status = 'archived') = (archived_at IS NOT NULL))
);

CREATE TABLE public.usage_events (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    metric_code text NOT NULL,
    transaction_id text NOT NULL,
    occurred_at timestamptz NOT NULL,
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    value numeric(30, 9),
    unique_value text,
    ingested_at timestamptz NOT NULL,
    CONSTRAINT pk_usage_events PRIMARY KEY (id),
    CONSTRAINT uq_usage_events_project_id_transaction_id UNIQUE (project_id, transaction_id),
    CONSTRAINT fk_usage_events_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT fk_usage_events_customer_id_customer_identities
        FOREIGN KEY (customer_id) REFERENCES public.customer_identities (id),
    CONSTRAINT fk_usage_events_project_id_metric_code_billable_metrics
        FOREIGN KEY (project_id, metric_code)
        REFERENCES public.billable_metrics (project_id, code),
    CONSTRAINT ck_usage_events_transaction_id
        CHECK (transaction_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+=~-]{0,127}$'),
    CONSTRAINT ck_usage_events_properties
        CHECK (jsonb_typeof(properties) = 'object' AND octet_length(properties::text) <= 8192),
    CONSTRAINT ck_usage_events_one_measure CHECK (value IS NULL OR unique_value IS NULL),
    CONSTRAINT ck_usage_events_unique_value CHECK (length(unique_value) BETWEEN 1 AND 256)
);
-- Aggregation: one customer, one metric, a half-open time range.
CREATE INDEX ix_usage_events_aggregation
    ON public.usage_events (project_id, customer_id, metric_code, occurred_at);

-- Archiving is final: a metric never becomes active again.
CREATE FUNCTION public.billable_metrics_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF OLD.status = 'archived' AND NEW.status IS DISTINCT FROM 'archived' THEN
        RAISE EXCEPTION 'billable metric % is archived and cannot be reactivated', OLD.code
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER billable_metrics_guard
    BEFORE UPDATE ON public.billable_metrics
    FOR EACH ROW EXECUTE FUNCTION public.billable_metrics_guard();

-- A usage event's customer belongs to its project (ADR-0011).
CREATE FUNCTION public.usage_events_same_project() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF (SELECT project_id FROM public.customer_identities WHERE id = NEW.customer_id)
        IS DISTINCT FROM NEW.project_id THEN
        RAISE EXCEPTION 'usage event: customer is in another project'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER usage_events_same_project
    BEFORE INSERT ON public.usage_events
    FOR EACH ROW EXECUTE FUNCTION public.usage_events_same_project();

-- Append-only guard, for every role (the grants already withhold UPDATE/DELETE from fh_app).
CREATE FUNCTION public.usage_events_reject_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    RAISE EXCEPTION 'usage_events: % is not allowed (append-only)', TG_OP
        USING ERRCODE = 'restrict_violation';
END
$fn$;
CREATE TRIGGER usage_events_no_change
    BEFORE UPDATE OR DELETE ON public.usage_events
    FOR EACH ROW EXECUTE FUNCTION public.usage_events_reject_change();
CREATE TRIGGER usage_events_no_truncate
    BEFORE TRUNCATE ON public.usage_events
    FOR EACH STATEMENT EXECUTE FUNCTION public.usage_events_reject_change();
"""

GRANTS = """
REVOKE ALL ON public.billable_metrics, public.usage_events FROM PUBLIC;

GRANT SELECT ON public.billable_metrics, public.usage_events TO fh_app;
GRANT INSERT (id, project_id, code, name, description, aggregation, field, status, created_at,
              updated_at, archived_at)
    ON public.billable_metrics TO fh_app;
GRANT UPDATE (status, archived_at, updated_at) ON public.billable_metrics TO fh_app;
GRANT INSERT (id, project_id, customer_id, metric_code, transaction_id, occurred_at, properties,
              value, unique_value, ingested_at)
    ON public.usage_events TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP TABLE public.usage_events, public.billable_metrics")
    op.execute("DROP FUNCTION public.usage_events_reject_change()")
    op.execute("DROP FUNCTION public.usage_events_same_project()")
    op.execute("DROP FUNCTION public.billable_metrics_guard()")
