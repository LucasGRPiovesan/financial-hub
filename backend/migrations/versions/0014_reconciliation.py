"""Reconciliation: runs, breaks and four-eyes adjustments (W8.1, ADR-0013, docs/RECONCILIATION.md).

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions.

- ``reconciliation_runs``: one per ``(project, kind, provider, account, window)``
  (``uq_reconciliation_runs_scope``: the idempotency key of a run; a failed run is retried in
  place). ``account_key`` is the provider account id as text, ``''`` when there is none (internal
  runs, the fake provider), so the unique key needs no NULL handling.
- ``reconciliation_breaks``: a discrepancy, both sides as JSON in integer minor units. At most one
  *active* (open / investigating) break per ``(project, fingerprint)``: a re-detection updates
  ``last_run_id`` instead of opening a duplicate. ``state`` moves forward only (trigger):
  ``open -> investigating -> resolved | accepted`` (``open -> resolved | accepted`` too).
- ``reconciliation_adjustments``: an R12 proposal. ``proposed_by <> decided_by`` is enforced here
  too (four-eyes); at most one ``proposed`` adjustment per break; a decided row is final (trigger).
  ``journal_entry_id`` is set only on approval, in the same transaction that posts R12.

``fh_app`` has no DELETE and may only UPDATE the state columns.

Revision ID: 0014_reconciliation
Revises: 0013_billing
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_reconciliation"
down_revision: str | None = "0013_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.reconciliation_runs (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    kind text NOT NULL,
    provider text NOT NULL,
    provider_account_id uuid,
    account_key text NOT NULL,
    window_start date NOT NULL,
    window_end date NOT NULL,
    status text NOT NULL,
    triggered_by text NOT NULL,
    summary jsonb NOT NULL,
    breaks_opened integer NOT NULL,
    error_code text,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    created_at timestamptz NOT NULL,
    CONSTRAINT pk_reconciliation_runs PRIMARY KEY (id),
    CONSTRAINT uq_reconciliation_runs_scope
        UNIQUE (project_id, kind, provider, account_key, window_start, window_end),
    CONSTRAINT uq_reconciliation_runs_project_id_id UNIQUE (project_id, id),
    CONSTRAINT fk_reconciliation_runs_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT ck_reconciliation_runs_kind CHECK (kind IN ('internal', 'transactions', 'balance')),
    CONSTRAINT ck_reconciliation_runs_status
        CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT ck_reconciliation_runs_window
        CHECK (window_end >= window_start AND window_end - window_start < 31),
    CONSTRAINT ck_reconciliation_runs_breaks_opened CHECK (breaks_opened >= 0),
    CONSTRAINT ck_reconciliation_runs_summary
        CHECK (jsonb_typeof(summary) = 'object' AND octet_length(summary::text) <= 65536)
);
CREATE INDEX ix_reconciliation_runs_project_id_created_at
    ON public.reconciliation_runs (project_id, created_at DESC, id DESC);

CREATE TABLE public.reconciliation_breaks (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    run_id uuid NOT NULL,
    last_run_id uuid NOT NULL,
    kind text NOT NULL,
    state text NOT NULL,
    fingerprint text NOT NULL,
    subject jsonb NOT NULL,
    internal jsonb,
    external jsonb,
    currency text,
    difference bigint,
    payment_intent_id uuid,
    provider_payment_id text,
    account_id uuid,
    history jsonb NOT NULL,
    resolution text,
    resolved_by text,
    resolved_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_reconciliation_breaks PRIMARY KEY (id),
    CONSTRAINT uq_reconciliation_breaks_project_id_id UNIQUE (project_id, id),
    CONSTRAINT fk_reconciliation_breaks_project_id_run_id_reconciliation_runs
        FOREIGN KEY (project_id, run_id) REFERENCES public.reconciliation_runs (project_id, id),
    CONSTRAINT fk_reconciliation_breaks_project_id_last_run_id_reconciliation_runs
        FOREIGN KEY (project_id, last_run_id)
        REFERENCES public.reconciliation_runs (project_id, id),
    CONSTRAINT ck_reconciliation_breaks_kind CHECK (kind IN (
        'ledger_drift', 'clearing_not_zero', 'missing_internal', 'missing_external',
        'amount_mismatch', 'fee_mismatch', 'status_mismatch', 'balance_drift')),
    CONSTRAINT ck_reconciliation_breaks_state
        CHECK (state IN ('open', 'investigating', 'resolved', 'accepted')),
    CONSTRAINT ck_reconciliation_breaks_fingerprint CHECK (length(fingerprint) BETWEEN 1 AND 300),
    CONSTRAINT ck_reconciliation_breaks_currency
        CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_reconciliation_breaks_closed
        CHECK ((state IN ('resolved', 'accepted')) = (resolved_at IS NOT NULL)),
    CONSTRAINT ck_reconciliation_breaks_json CHECK (
        jsonb_typeof(subject) = 'object' AND jsonb_typeof(history) = 'array'
        AND octet_length(subject::text) + octet_length(history::text)
            + coalesce(octet_length(internal::text), 0)
            + coalesce(octet_length(external::text), 0) <= 262144)
);
CREATE UNIQUE INDEX uq_reconciliation_breaks_active_fingerprint
    ON public.reconciliation_breaks (project_id, fingerprint)
    WHERE state IN ('open', 'investigating');
CREATE INDEX ix_reconciliation_breaks_project_id_created_at
    ON public.reconciliation_breaks (project_id, created_at DESC, id DESC);
CREATE INDEX ix_reconciliation_breaks_project_id_state
    ON public.reconciliation_breaks (project_id, state);

CREATE FUNCTION public.reconciliation_breaks_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id <> OLD.id OR NEW.project_id <> OLD.project_id OR NEW.run_id <> OLD.run_id
       OR NEW.kind <> OLD.kind OR NEW.fingerprint <> OLD.fingerprint
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'reconciliation break identity is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.state IN ('resolved', 'accepted') THEN
        RAISE EXCEPTION 'reconciliation break % is closed', OLD.id USING ERRCODE = '23514';
    END IF;
    IF NEW.state <> OLD.state AND NOT (
        (OLD.state = 'open' AND NEW.state IN ('investigating', 'resolved', 'accepted'))
        OR (OLD.state = 'investigating' AND NEW.state IN ('resolved', 'accepted'))
    ) THEN
        RAISE EXCEPTION 'illegal reconciliation break transition % -> %', OLD.state, NEW.state
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_reconciliation_breaks_guard
    BEFORE UPDATE ON public.reconciliation_breaks
    FOR EACH ROW EXECUTE FUNCTION public.reconciliation_breaks_guard();

CREATE TABLE public.reconciliation_adjustments (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    break_id uuid NOT NULL,
    provider_account_id uuid NOT NULL,
    currency text NOT NULL,
    amount bigint NOT NULL,
    reason text NOT NULL,
    payload_hash text NOT NULL,
    status text NOT NULL,
    proposed_by uuid NOT NULL,
    decided_by uuid,
    decision_reason text,
    journal_entry_id uuid,
    created_at timestamptz NOT NULL,
    decided_at timestamptz,
    CONSTRAINT pk_reconciliation_adjustments PRIMARY KEY (id),
    CONSTRAINT fk_reconciliation_adjustments_project_id_break_id_reconciliation_breaks
        FOREIGN KEY (project_id, break_id)
        REFERENCES public.reconciliation_breaks (project_id, id),
    CONSTRAINT fk_reconciliation_adjustments_journal_entry_id_journal_entries
        FOREIGN KEY (journal_entry_id) REFERENCES public.journal_entries (id),
    CONSTRAINT ck_reconciliation_adjustments_amount
        CHECK (amount <> 0 AND abs(amount) <= 1000000000000),
    CONSTRAINT ck_reconciliation_adjustments_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_reconciliation_adjustments_reason CHECK (length(reason) BETWEEN 1 AND 1000),
    CONSTRAINT ck_reconciliation_adjustments_payload_hash
        CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_reconciliation_adjustments_status
        CHECK (status IN ('proposed', 'approved', 'rejected')),
    CONSTRAINT ck_reconciliation_adjustments_four_eyes
        CHECK (decided_by IS NULL OR decided_by <> proposed_by),
    CONSTRAINT ck_reconciliation_adjustments_decided CHECK (
        (status = 'proposed' AND decided_by IS NULL AND decided_at IS NULL
            AND journal_entry_id IS NULL)
        OR (status = 'approved' AND decided_by IS NOT NULL AND decided_at IS NOT NULL
            AND journal_entry_id IS NOT NULL)
        OR (status = 'rejected' AND decided_by IS NOT NULL AND decided_at IS NOT NULL
            AND journal_entry_id IS NULL))
);
CREATE UNIQUE INDEX uq_reconciliation_adjustments_break_id_proposed
    ON public.reconciliation_adjustments (break_id) WHERE status = 'proposed';
CREATE INDEX ix_reconciliation_adjustments_project_id_created_at
    ON public.reconciliation_adjustments (project_id, created_at DESC, id DESC);

CREATE FUNCTION public.reconciliation_adjustments_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'proposed' THEN
        RAISE EXCEPTION 'reconciliation adjustment % is decided', OLD.id USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_reconciliation_adjustments_guard
    BEFORE UPDATE ON public.reconciliation_adjustments
    FOR EACH ROW EXECUTE FUNCTION public.reconciliation_adjustments_guard();
"""

GRANTS = """
REVOKE ALL ON public.reconciliation_runs, public.reconciliation_breaks,
    public.reconciliation_adjustments FROM PUBLIC;

GRANT SELECT ON public.reconciliation_runs, public.reconciliation_breaks,
    public.reconciliation_adjustments TO fh_app;
GRANT INSERT (id, project_id, kind, provider, provider_account_id, account_key, window_start,
              window_end, status, triggered_by, summary, breaks_opened, error_code, started_at,
              completed_at, created_at)
    ON public.reconciliation_runs TO fh_app;
GRANT UPDATE (status, triggered_by, summary, breaks_opened, error_code, started_at, completed_at)
    ON public.reconciliation_runs TO fh_app;
GRANT INSERT (id, project_id, run_id, last_run_id, kind, state, fingerprint, subject, internal,
              external, currency, difference, payment_intent_id, provider_payment_id, account_id,
              history, resolution, resolved_by, resolved_at, created_at, updated_at)
    ON public.reconciliation_breaks TO fh_app;
GRANT UPDATE (last_run_id, state, internal, external, difference, history, resolution,
              resolved_by, resolved_at, updated_at)
    ON public.reconciliation_breaks TO fh_app;
GRANT INSERT (id, project_id, break_id, provider_account_id, currency, amount, reason,
              payload_hash, status, proposed_by, created_at)
    ON public.reconciliation_adjustments TO fh_app;
GRANT UPDATE (status, decided_by, decision_reason, journal_entry_id, decided_at)
    ON public.reconciliation_adjustments TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute(
        "DROP TABLE public.reconciliation_adjustments, public.reconciliation_breaks, "
        "public.reconciliation_runs"
    )
    op.execute("DROP FUNCTION public.reconciliation_adjustments_guard()")
    op.execute("DROP FUNCTION public.reconciliation_breaks_guard()")
