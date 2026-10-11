"""events: event log/outbox, subscriptions, deliveries, inbox, schedules (ADR-0005, ADR-0004)

Objects are created as ``fh_owner`` (``SET ROLE``), like ``0001_ledger_core``. The runtime role
``fh_app`` gets only what the publisher, dispatcher and scheduler need:

- ``events``: SELECT + column-level INSERT. ``txid`` and ``recorded_at`` are not insertable, so the
  visibility horizon (``txid = pg_current_xact_id()``) cannot be forged. No UPDATE/DELETE: the log
  is append-only for the runtime (retention/partitioning is an owner job, *Planned*).
- ``event_deliveries``: SELECT, INSERT, UPDATE of the state-machine columns only (the claim uses
  ``FOR UPDATE SKIP LOCKED``, which needs UPDATE privilege).
- ``event_subscriptions``: SELECT, INSERT, UPDATE(event_types, enabled, updated_at) (managed by the
  events service; fan-out reads them ``FOR SHARE``, which needs UPDATE privilege).
- ``inbox``, ``schedule_runs``: SELECT, INSERT. ``schedules``: SELECT, INSERT, UPDATE of the
  scheduling columns.

No DELETE/TRUNCATE for ``fh_app`` anywhere.

Revision ID: 0002_events
Revises: 0001_ledger_core
Create Date: 2026-10-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_events"
down_revision: str | None = "0001_ledger_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.events (
    id uuid NOT NULL,
    type text NOT NULL,
    version integer NOT NULL,
    project_id uuid NOT NULL,
    subject text,
    occurred_at timestamptz NOT NULL,
    correlation_id uuid,
    causation_id uuid,
    traceparent text,
    data jsonb NOT NULL,
    txid xid8 NOT NULL DEFAULT pg_catalog.pg_current_xact_id(),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_events PRIMARY KEY (id),
    CONSTRAINT ck_events_type CHECK (type ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'),
    CONSTRAINT ck_events_version CHECK (version >= 1),
    CONSTRAINT ck_events_data CHECK (jsonb_typeof(data) = 'object'),
    CONSTRAINT ck_events_subject CHECK (length(subject) BETWEEN 1 AND 255),
    CONSTRAINT ck_events_traceparent CHECK (length(traceparent) <= 128)
);
-- Readers page by (txid, id) below the visibility horizon (ADR-0005), per project or globally.
CREATE INDEX ix_events_project_id_txid_id ON public.events (project_id, txid, id);
CREATE INDEX ix_events_txid_id ON public.events (txid, id);

-- Internal subscriptions may be global (project_id NULL = every project); webhook ones are always
-- project-scoped. Filters: exact type, 'prefix.*' or '*'.
CREATE TABLE public.event_subscriptions (
    id uuid NOT NULL,
    project_id uuid,
    kind text NOT NULL,
    handler text,
    endpoint_id uuid,
    event_types text[] NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_event_subscriptions PRIMARY KEY (id),
    CONSTRAINT uq_event_subscriptions_target
        UNIQUE NULLS NOT DISTINCT (project_id, handler, endpoint_id),
    CONSTRAINT ck_event_subscriptions_kind CHECK (
        (kind = 'internal' AND handler IS NOT NULL AND endpoint_id IS NULL)
        OR (kind = 'webhook' AND endpoint_id IS NOT NULL AND handler IS NULL
            AND project_id IS NOT NULL)
    ),
    CONSTRAINT ck_event_subscriptions_handler CHECK (handler ~ '^[a-z][a-z0-9_.:-]{0,127}$'),
    CONSTRAINT ck_event_subscriptions_event_types CHECK (
        cardinality(event_types) >= 1 AND array_position(event_types, NULL) IS NULL
    )
);
CREATE INDEX ix_event_subscriptions_project_id ON public.event_subscriptions (project_id)
    WHERE enabled;

CREATE TABLE public.event_deliveries (
    id uuid NOT NULL,
    event_id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    project_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL,
    lease_until timestamptz,
    lease_token uuid,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_event_deliveries PRIMARY KEY (id),
    CONSTRAINT uq_event_deliveries_event_id_subscription_id UNIQUE (event_id, subscription_id),
    CONSTRAINT fk_event_deliveries_event_id_events
        FOREIGN KEY (event_id) REFERENCES public.events (id),
    CONSTRAINT fk_event_deliveries_subscription_id_event_subscriptions
        FOREIGN KEY (subscription_id) REFERENCES public.event_subscriptions (id),
    CONSTRAINT ck_event_deliveries_status
        CHECK (status IN ('pending', 'leased', 'succeeded', 'retry_scheduled', 'dead')),
    CONSTRAINT ck_event_deliveries_lease CHECK (
        (status = 'leased') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)
        AND (lease_token IS NULL) = (lease_until IS NULL)
    ),
    CONSTRAINT ck_event_deliveries_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_event_deliveries_last_error CHECK (length(last_error) <= 2000)
);
-- Claim paths of the dispatcher: due work and expired leases.
CREATE INDEX ix_event_deliveries_due ON public.event_deliveries (next_attempt_at)
    WHERE status IN ('pending', 'retry_scheduled');
CREATE INDEX ix_event_deliveries_lease_until ON public.event_deliveries (lease_until)
    WHERE status = 'leased';
CREATE INDEX ix_event_deliveries_subscription_id ON public.event_deliveries (subscription_id);
CREATE INDEX ix_event_deliveries_status_project_id ON public.event_deliveries (status, project_id);

-- ADR-0004: one effect per (handler, event), written in the handler's transaction.
CREATE TABLE public.inbox (
    handler text NOT NULL,
    event_id uuid NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_inbox PRIMARY KEY (handler, event_id),
    CONSTRAINT fk_inbox_event_id_events FOREIGN KEY (event_id) REFERENCES public.events (id)
);

CREATE TABLE public.schedules (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    name text NOT NULL,
    event_type text NOT NULL,
    interval_seconds integer NOT NULL,
    next_due_at timestamptz NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_schedules PRIMARY KEY (id),
    CONSTRAINT uq_schedules_project_id_name UNIQUE (project_id, name),
    CONSTRAINT ck_schedules_name CHECK (name ~ '^[a-z][a-z0-9_.:-]{0,127}$'),
    CONSTRAINT ck_schedules_interval_seconds CHECK (interval_seconds > 0)
);
CREATE INDEX ix_schedules_next_due_at ON public.schedules (next_due_at) WHERE enabled;

CREATE TABLE public.schedule_runs (
    schedule_id uuid NOT NULL,
    due_at timestamptz NOT NULL,
    event_id uuid NOT NULL,
    emitted_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_schedule_runs PRIMARY KEY (schedule_id, due_at),
    CONSTRAINT fk_schedule_runs_schedule_id_schedules
        FOREIGN KEY (schedule_id) REFERENCES public.schedules (id),
    CONSTRAINT fk_schedule_runs_event_id_events
        FOREIGN KEY (event_id) REFERENCES public.events (id)
);
"""

GRANTS = """
REVOKE ALL ON public.events, public.event_subscriptions, public.event_deliveries, public.inbox,
    public.schedules, public.schedule_runs FROM PUBLIC;

GRANT SELECT ON public.events, public.event_subscriptions, public.event_deliveries, public.inbox,
    public.schedules, public.schedule_runs TO fh_app;
GRANT INSERT (id, type, version, project_id, subject, occurred_at, correlation_id, causation_id,
              traceparent, data)
    ON public.events TO fh_app;
GRANT INSERT (id, project_id, kind, handler, endpoint_id, event_types, enabled)
    ON public.event_subscriptions TO fh_app;
GRANT UPDATE (event_types, enabled, updated_at) ON public.event_subscriptions TO fh_app;
GRANT INSERT (id, event_id, subscription_id, project_id, next_attempt_at)
    ON public.event_deliveries TO fh_app;
GRANT UPDATE (status, attempts, next_attempt_at, lease_until, lease_token, last_error, updated_at)
    ON public.event_deliveries TO fh_app;
GRANT INSERT (handler, event_id) ON public.inbox TO fh_app;
GRANT INSERT (id, project_id, name, event_type, interval_seconds, next_due_at, enabled)
    ON public.schedules TO fh_app;
GRANT UPDATE (event_type, interval_seconds, next_due_at, enabled, updated_at)
    ON public.schedules TO fh_app;
GRANT INSERT (schedule_id, due_at, event_id) ON public.schedule_runs TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute(
        "DROP TABLE public.schedule_runs, public.schedules, public.inbox, "
        "public.event_deliveries, public.event_subscriptions, public.events"
    )
