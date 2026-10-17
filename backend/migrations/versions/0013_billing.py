"""Billing: price catalog, plans, subscriptions and invoices (W5.3, ADR-0007, docs/DOMAIN.md).

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions. ``btree_gist`` is created
before the role switch (by the migration login): the non-overlap constraint of price versions
compares a ``uuid`` with ``=`` inside a GiST index.

- ``prices``: a price ``code`` per project, in one currency. ``metric_code`` pins the billable
  metric a usage price rates (NULL for fixed prices). Immutable: ``fh_app`` has no UPDATE.
- ``price_versions``: append-only versions of a price. ``model`` (JSONB, validated by the pricing
  domain) never changes; ``fh_app`` may only set ``effective_to`` of the open-ended version, once
  (a trigger refuses any other change). ``ex_price_versions_no_overlap`` keeps the windows
  ``[effective_from, effective_to)`` of one price disjoint, in the database too.
- ``plans`` / ``plan_prices``: a plan is a set of prices billed monthly in one currency.
- ``subscriptions``: one customer on one plan; at most one not-canceled subscription per
  ``(project, customer, plan)``. A trigger keeps the customer in the subscription's project and
  the status moving forward (trialing -> active -> canceled).
- ``invoices``: one per ``(subscription, period_start)`` (``uq_invoices_subscription_period``: the
  idempotency key of the period close). Amounts are integer minor units and immutable
  (``amount_due = total - credits_applied``); ``fh_app`` may only move the status columns and a
  trigger enforces ``draft -> finalized -> paid | void | uncollectible``.
- ``invoice_lines``: append-only rated lines, each pinning its price version, metric and rounding.
- ``payment_intents.invoice_id``: the invoice an intent collects (set only by billing, never from
  the HTTP API). Payments skips R1 for these intents: billing books it at finalize.

Revision ID: 0013_billing
Revises: 0012_webhooks
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_billing"
down_revision: str | None = "0012_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.prices (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    currency text NOT NULL,
    metric_code text,
    created_at timestamptz NOT NULL,
    CONSTRAINT pk_prices PRIMARY KEY (id),
    CONSTRAINT uq_prices_project_id_code UNIQUE (project_id, code),
    CONSTRAINT uq_prices_project_id_id UNIQUE (project_id, id),
    CONSTRAINT fk_prices_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT fk_prices_project_id_metric_code_billable_metrics
        FOREIGN KEY (project_id, metric_code)
        REFERENCES public.billable_metrics (project_id, code),
    CONSTRAINT ck_prices_code CHECK (code ~ '^[a-z0-9][a-z0-9_.-]{0,99}$'),
    CONSTRAINT ck_prices_name CHECK (length(name) BETWEEN 1 AND 200),
    CONSTRAINT ck_prices_currency CHECK (currency ~ '^[A-Z]{3}$')
);

CREATE TABLE public.price_versions (
    id uuid NOT NULL,
    price_id uuid NOT NULL,
    project_id uuid NOT NULL,
    version integer NOT NULL,
    model jsonb NOT NULL,
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    created_at timestamptz NOT NULL,
    CONSTRAINT pk_price_versions PRIMARY KEY (id),
    CONSTRAINT uq_price_versions_price_id_version UNIQUE (price_id, version),
    CONSTRAINT fk_price_versions_project_id_price_id_prices
        FOREIGN KEY (project_id, price_id) REFERENCES public.prices (project_id, id),
    CONSTRAINT ck_price_versions_version CHECK (version >= 1),
    CONSTRAINT ck_price_versions_model
        CHECK (jsonb_typeof(model) = 'object' AND octet_length(model::text) <= 16384),
    CONSTRAINT ck_price_versions_window
        CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT ex_price_versions_no_overlap EXCLUDE USING gist (
        price_id WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
);

CREATE TABLE public.plans (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    currency text NOT NULL,
    billing_interval text NOT NULL DEFAULT 'month',
    created_at timestamptz NOT NULL,
    CONSTRAINT pk_plans PRIMARY KEY (id),
    CONSTRAINT uq_plans_project_id_code UNIQUE (project_id, code),
    CONSTRAINT uq_plans_project_id_id UNIQUE (project_id, id),
    CONSTRAINT fk_plans_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT ck_plans_code CHECK (code ~ '^[a-z0-9][a-z0-9_.-]{0,99}$'),
    CONSTRAINT ck_plans_name CHECK (length(name) BETWEEN 1 AND 200),
    CONSTRAINT ck_plans_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_plans_billing_interval CHECK (billing_interval = 'month')
);

CREATE TABLE public.plan_prices (
    plan_id uuid NOT NULL,
    price_id uuid NOT NULL,
    project_id uuid NOT NULL,
    position integer NOT NULL,
    CONSTRAINT pk_plan_prices PRIMARY KEY (plan_id, price_id),
    CONSTRAINT uq_plan_prices_plan_id_position UNIQUE (plan_id, position),
    CONSTRAINT fk_plan_prices_project_id_plan_id_plans
        FOREIGN KEY (project_id, plan_id) REFERENCES public.plans (project_id, id),
    CONSTRAINT fk_plan_prices_project_id_price_id_prices
        FOREIGN KEY (project_id, price_id) REFERENCES public.prices (project_id, id),
    CONSTRAINT ck_plan_prices_position CHECK (position >= 0)
);

CREATE TABLE public.subscriptions (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    status text NOT NULL,
    anchor_day integer NOT NULL,
    started_at timestamptz NOT NULL,
    trial_end timestamptz,
    current_period_start timestamptz NOT NULL,
    current_period_end timestamptz NOT NULL,
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    canceled_at timestamptz,
    ended_at timestamptz,
    payment_method text NOT NULL,
    days_until_due integer NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_subscriptions PRIMARY KEY (id),
    CONSTRAINT uq_subscriptions_project_id_id UNIQUE (project_id, id),
    CONSTRAINT fk_subscriptions_project_id_plan_id_plans
        FOREIGN KEY (project_id, plan_id) REFERENCES public.plans (project_id, id),
    CONSTRAINT fk_subscriptions_customer_id_customer_identities
        FOREIGN KEY (customer_id) REFERENCES public.customer_identities (id),
    CONSTRAINT ck_subscriptions_status CHECK (status IN ('trialing', 'active', 'canceled')),
    CONSTRAINT ck_subscriptions_anchor_day CHECK (anchor_day BETWEEN 1 AND 31),
    CONSTRAINT ck_subscriptions_period CHECK (current_period_end > current_period_start),
    CONSTRAINT ck_subscriptions_trial CHECK (trial_end IS NULL OR trial_end > started_at),
    CONSTRAINT ck_subscriptions_payment_method CHECK (payment_method IN ('pix', 'boleto', 'card')),
    CONSTRAINT ck_subscriptions_days_until_due CHECK (days_until_due BETWEEN 0 AND 60),
    CONSTRAINT ck_subscriptions_ended CHECK ((status = 'canceled') = (ended_at IS NOT NULL))
);
-- One live subscription per customer and plan; canceled ones stay as history.
CREATE UNIQUE INDEX uq_subscriptions_live
    ON public.subscriptions (project_id, customer_id, plan_id) WHERE status <> 'canceled';
-- The period-close job: live subscriptions whose period has ended.
CREATE INDEX ix_subscriptions_due
    ON public.subscriptions (current_period_end) WHERE status <> 'canceled';

CREATE TABLE public.invoices (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    subscription_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    currency text NOT NULL,
    status text NOT NULL DEFAULT 'draft',
    period_start timestamptz NOT NULL,
    period_end timestamptz NOT NULL,
    total bigint NOT NULL,
    credits_applied bigint NOT NULL DEFAULT 0,
    amount_due bigint NOT NULL,
    payment_intent_id uuid,
    due_date date,
    finalized_at timestamptz,
    paid_at timestamptz,
    voided_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_invoices PRIMARY KEY (id),
    CONSTRAINT uq_invoices_project_id_id UNIQUE (project_id, id),
    CONSTRAINT uq_invoices_subscription_period UNIQUE (subscription_id, period_start),
    CONSTRAINT uq_invoices_payment_intent_id UNIQUE (payment_intent_id),
    CONSTRAINT fk_invoices_project_id_subscription_id_subscriptions
        FOREIGN KEY (project_id, subscription_id)
        REFERENCES public.subscriptions (project_id, id),
    CONSTRAINT fk_invoices_customer_id_customer_identities
        FOREIGN KEY (customer_id) REFERENCES public.customer_identities (id),
    CONSTRAINT fk_invoices_payment_intent_id_payment_intents
        FOREIGN KEY (payment_intent_id) REFERENCES public.payment_intents (id),
    CONSTRAINT ck_invoices_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_invoices_status
        CHECK (status IN ('draft', 'finalized', 'paid', 'void', 'uncollectible')),
    CONSTRAINT ck_invoices_period CHECK (period_end > period_start),
    CONSTRAINT ck_invoices_amounts CHECK (
        total >= 0 AND credits_applied >= 0 AND credits_applied <= total
        AND amount_due = total - credits_applied
    ),
    CONSTRAINT ck_invoices_finalized_at
        CHECK ((status = 'draft') = (finalized_at IS NULL) OR status = 'void'),
    CONSTRAINT ck_invoices_paid_at CHECK ((status = 'paid') = (paid_at IS NOT NULL)),
    CONSTRAINT ck_invoices_voided_at CHECK ((status = 'void') = (voided_at IS NOT NULL))
);
CREATE INDEX ix_invoices_project_id_created_at
    ON public.invoices (project_id, created_at DESC, id DESC);

CREATE TABLE public.invoice_lines (
    id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    project_id uuid NOT NULL,
    position integer NOT NULL,
    kind text NOT NULL,
    price_id uuid NOT NULL,
    price_code text NOT NULL,
    price_version_id uuid NOT NULL,
    price_version integer NOT NULL,
    model text NOT NULL,
    metric_code text,
    quantity numeric(38, 18),
    amount bigint NOT NULL,
    rounding text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT pk_invoice_lines PRIMARY KEY (id),
    CONSTRAINT uq_invoice_lines_invoice_id_position UNIQUE (invoice_id, position),
    CONSTRAINT fk_invoice_lines_project_id_invoice_id_invoices
        FOREIGN KEY (project_id, invoice_id) REFERENCES public.invoices (project_id, id),
    CONSTRAINT fk_invoice_lines_project_id_price_id_prices
        FOREIGN KEY (project_id, price_id) REFERENCES public.prices (project_id, id),
    CONSTRAINT fk_invoice_lines_price_version_id_price_versions
        FOREIGN KEY (price_version_id) REFERENCES public.price_versions (id),
    CONSTRAINT ck_invoice_lines_kind CHECK (kind IN ('fixed', 'usage')),
    CONSTRAINT ck_invoice_lines_usage_metric CHECK ((kind = 'usage') = (metric_code IS NOT NULL)),
    CONSTRAINT ck_invoice_lines_amount CHECK (amount >= 0),
    CONSTRAINT ck_invoice_lines_details
        CHECK (jsonb_typeof(details) = 'object' AND octet_length(details::text) <= 16384)
);

ALTER TABLE public.payment_intents ADD COLUMN invoice_id uuid;
ALTER TABLE public.payment_intents ADD CONSTRAINT fk_payment_intents_invoice_id_invoices
    FOREIGN KEY (invoice_id) REFERENCES public.invoices (id);
CREATE UNIQUE INDEX uq_payment_intents_invoice_id
    ON public.payment_intents (invoice_id) WHERE invoice_id IS NOT NULL;

-- Only the open-ended version may be closed, once: history never changes meaning.
CREATE FUNCTION public.price_versions_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF OLD.effective_to IS NOT NULL OR NEW.effective_to IS NULL
        OR NEW.model IS DISTINCT FROM OLD.model
        OR NEW.effective_from IS DISTINCT FROM OLD.effective_from THEN
        RAISE EXCEPTION 'price version % of price % is immutable', OLD.version, OLD.price_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER price_versions_guard
    BEFORE UPDATE ON public.price_versions
    FOR EACH ROW EXECUTE FUNCTION public.price_versions_guard();

-- A subscription's customer belongs to its project (ADR-0011); its status only moves forward.
CREATE FUNCTION public.subscriptions_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF (SELECT project_id FROM public.customer_identities WHERE id = NEW.customer_id)
            IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'subscription: customer is in another project'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'canceled' AND NEW.status IS DISTINCT FROM 'canceled'
        OR OLD.status = 'active' AND NEW.status = 'trialing' THEN
        RAISE EXCEPTION 'subscription %: % -> % is not allowed', OLD.id, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER subscriptions_guard
    BEFORE INSERT OR UPDATE ON public.subscriptions
    FOR EACH ROW EXECUTE FUNCTION public.subscriptions_guard();

-- draft -> finalized -> paid | void | uncollectible; draft -> void; paid and void are final.
CREATE FUNCTION public.invoices_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF (SELECT project_id FROM public.customer_identities WHERE id = NEW.customer_id)
            IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'invoice: customer is in another project'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'draft' AND NEW.status IN ('finalized', 'void'))
        OR (OLD.status = 'finalized' AND NEW.status IN ('paid', 'void', 'uncollectible'))
        OR (OLD.status = 'uncollectible' AND NEW.status IN ('paid', 'void'))
    ) THEN
        RAISE EXCEPTION 'invoice %: % -> % is not allowed', OLD.id, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.payment_intent_id IS NOT NULL
        AND NEW.payment_intent_id IS DISTINCT FROM OLD.payment_intent_id THEN
        RAISE EXCEPTION 'invoice %: the payment intent cannot change', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER invoices_guard
    BEFORE INSERT OR UPDATE ON public.invoices
    FOR EACH ROW EXECUTE FUNCTION public.invoices_guard();
"""

GRANTS = """
REVOKE ALL ON public.prices, public.price_versions, public.plans, public.plan_prices,
    public.subscriptions, public.invoices, public.invoice_lines FROM PUBLIC;

GRANT SELECT ON public.prices, public.price_versions, public.plans, public.plan_prices,
    public.subscriptions, public.invoices, public.invoice_lines TO fh_app;
GRANT INSERT (id, project_id, code, name, currency, metric_code, created_at)
    ON public.prices TO fh_app;
GRANT INSERT (id, price_id, project_id, version, model, effective_from, effective_to, created_at)
    ON public.price_versions TO fh_app;
GRANT UPDATE (effective_to) ON public.price_versions TO fh_app;
GRANT INSERT (id, project_id, code, name, currency, billing_interval, created_at)
    ON public.plans TO fh_app;
GRANT INSERT (plan_id, price_id, project_id, position) ON public.plan_prices TO fh_app;
GRANT INSERT (id, project_id, customer_id, plan_id, status, anchor_day, started_at, trial_end,
              current_period_start, current_period_end, cancel_at_period_end, canceled_at,
              ended_at, payment_method, days_until_due, created_at, updated_at)
    ON public.subscriptions TO fh_app;
GRANT UPDATE (status, current_period_start, current_period_end, cancel_at_period_end,
              canceled_at, ended_at, updated_at)
    ON public.subscriptions TO fh_app;
GRANT INSERT (id, project_id, subscription_id, customer_id, currency, status, period_start,
              period_end, total, credits_applied, amount_due, created_at, updated_at)
    ON public.invoices TO fh_app;
GRANT UPDATE (status, payment_intent_id, due_date, finalized_at, paid_at, voided_at, updated_at)
    ON public.invoices TO fh_app;
GRANT INSERT (id, invoice_id, project_id, position, kind, price_id, price_code, price_version_id,
              price_version, model, metric_code, quantity, amount, rounding, details)
    ON public.invoice_lines TO fh_app;
GRANT INSERT (invoice_id) ON public.payment_intents TO fh_app;
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP INDEX public.uq_payment_intents_invoice_id")
    op.execute("ALTER TABLE public.payment_intents DROP COLUMN invoice_id")
    op.execute(
        "DROP TABLE public.invoice_lines, public.invoices, public.subscriptions, "
        "public.plan_prices, public.plans, public.price_versions, public.prices"
    )
    op.execute("DROP FUNCTION public.invoices_guard()")
    op.execute("DROP FUNCTION public.subscriptions_guard()")
    op.execute("DROP FUNCTION public.price_versions_guard()")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
