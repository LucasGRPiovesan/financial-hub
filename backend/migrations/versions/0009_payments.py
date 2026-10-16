"""Payments: payment_intents, provider_payments, refunds, payment_attempts (PRO-40, ADR-0012).

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions. ``fh_app`` gets no
DELETE/TRUNCATE and UPDATE only on the columns the application changes:

- ``payment_intents``: what a product asked to collect. ``amount`` is integer minor units (never a
  float), ``customer_id`` is the project's customer identity. ``provider`` is the provider *name*;
  ``provider_account_id`` is NULL exactly when ``provider = 'fake'`` (the scriptable development
  provider has no account). ``creation_status`` + ``attempt_id`` implement the external-call
  protocol (ADR-0012 s3): ``attempt_id`` is the fencing token of the current outbound attempt and
  the outcome is recorded with a compare-and-set on ``(id, attempt_id, creation_status)``.
- ``provider_payments``: one payment at a provider, with the orthogonal state fields (ADR-0012 s1).
  ``(provider, provider_account_id, provider_payment_id)`` is unique with NULLS NOT DISTINCT, so the
  fake provider's ids are unique too. A settled or disputed payment must be captured.
- ``refunds``: full or partial refunds of a provider payment (``rf_`` id = provider external
  reference). ``origin`` says whether the hub requested it or the provider reported it.
- ``payment_attempts``: every outbound create/refund call (fencing token = ``id``), with the SHA-256
  of the canonical request and the outcome (provider ids and codes only, never PII).

Triggers keep every row inside one project: the intent's customer and provider account belong to
its project, and provider payments, refunds and attempts match their intent.

No ledger postings here: they come with the settlement ledger (PRO-42).

Revision ID: 0009_payments
Revises: 0008_member_management
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_payments"
down_revision: str | None = "0008_member_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.payment_intents (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    amount bigint NOT NULL,
    currency text NOT NULL,
    method text NOT NULL,
    due_date date NOT NULL,
    external_ref text,
    description text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider text NOT NULL,
    provider_account_id uuid,
    creation_status text NOT NULL,
    attempt_id uuid NOT NULL,
    failure_code text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_payment_intents PRIMARY KEY (id),
    CONSTRAINT uq_payment_intents_project_id_external_ref UNIQUE (project_id, external_ref),
    CONSTRAINT fk_payment_intents_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT fk_payment_intents_customer_id_customer_identities
        FOREIGN KEY (customer_id) REFERENCES public.customer_identities (id),
    CONSTRAINT fk_payment_intents_provider_account_id_provider_accounts
        FOREIGN KEY (provider_account_id) REFERENCES public.provider_accounts (id),
    CONSTRAINT ck_payment_intents_amount CHECK (amount > 0),
    CONSTRAINT ck_payment_intents_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_payment_intents_method CHECK (method IN ('pix', 'boleto', 'card')),
    CONSTRAINT ck_payment_intents_external_ref
        CHECK (external_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:@+=~-]{0,127}$'),
    CONSTRAINT ck_payment_intents_description CHECK (length(description) BETWEEN 1 AND 500),
    CONSTRAINT ck_payment_intents_metadata
        CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 8192),
    CONSTRAINT ck_payment_intents_provider
        CHECK (provider IN ('fake', 'asaas', 'stripe', 'mercadopago')),
    CONSTRAINT ck_payment_intents_provider_account_id
        CHECK ((provider = 'fake') = (provider_account_id IS NULL)),
    CONSTRAINT ck_payment_intents_creation_status
        CHECK (creation_status IN ('create_requested', 'created', 'rejected', 'unknown')),
    CONSTRAINT ck_payment_intents_failure_code CHECK (length(failure_code) BETWEEN 1 AND 100)
);
CREATE INDEX ix_payment_intents_project_id_created_at
    ON public.payment_intents (project_id, created_at, id);
CREATE INDEX ix_payment_intents_customer_id ON public.payment_intents (customer_id);
CREATE INDEX ix_payment_intents_unresolved ON public.payment_intents (updated_at)
    WHERE creation_status IN ('create_requested', 'unknown');

CREATE TABLE public.provider_payments (
    id uuid NOT NULL,
    payment_intent_id uuid NOT NULL,
    project_id uuid NOT NULL,
    provider text NOT NULL,
    provider_account_id uuid,
    provider_payment_id text NOT NULL,
    raw_status text NOT NULL,
    capture_status text NOT NULL,
    settlement_status text NOT NULL,
    dispute_status text NOT NULL,
    captured_amount bigint NOT NULL DEFAULT 0,
    net_amount bigint,
    fee_estimated bigint,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_provider_payments PRIMARY KEY (id),
    -- One provider payment per intent: the backstop of the fenced write (never a duplicate).
    CONSTRAINT uq_provider_payments_payment_intent_id UNIQUE (payment_intent_id),
    CONSTRAINT uq_provider_payments_provider_payment_id
        UNIQUE NULLS NOT DISTINCT (provider, provider_account_id, provider_payment_id),
    CONSTRAINT fk_provider_payments_payment_intent_id_payment_intents
        FOREIGN KEY (payment_intent_id) REFERENCES public.payment_intents (id),
    CONSTRAINT fk_provider_payments_provider_account_id_provider_accounts
        FOREIGN KEY (provider_account_id) REFERENCES public.provider_accounts (id),
    CONSTRAINT ck_provider_payments_provider_account_id
        CHECK ((provider = 'fake') = (provider_account_id IS NULL)),
    CONSTRAINT ck_provider_payments_provider_payment_id
        CHECK (length(provider_payment_id) BETWEEN 1 AND 255),
    CONSTRAINT ck_provider_payments_raw_status CHECK (length(raw_status) BETWEEN 1 AND 100),
    CONSTRAINT ck_provider_payments_capture_status
        CHECK (capture_status IN ('pending', 'captured', 'failed', 'canceled')),
    CONSTRAINT ck_provider_payments_settlement_status
        CHECK (settlement_status IN ('none', 'settled')),
    CONSTRAINT ck_provider_payments_dispute_status
        CHECK (dispute_status IN ('none', 'open', 'won', 'lost')),
    CONSTRAINT ck_provider_payments_settled_needs_capture
        CHECK (settlement_status = 'none' OR capture_status = 'captured'),
    CONSTRAINT ck_provider_payments_dispute_needs_capture
        CHECK (dispute_status = 'none' OR capture_status = 'captured'),
    CONSTRAINT ck_provider_payments_amounts CHECK (
        captured_amount >= 0
        AND (net_amount IS NULL OR net_amount BETWEEN 0 AND captured_amount)
        AND (capture_status = 'captured') = (captured_amount > 0)
    )
);

CREATE TABLE public.refunds (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    payment_intent_id uuid NOT NULL,
    provider_payment_id uuid NOT NULL,
    amount bigint NOT NULL,
    currency text NOT NULL,
    status text NOT NULL,
    reason text,
    provider_refund_id text,
    attempt_id uuid,
    origin text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_refunds PRIMARY KEY (id),
    CONSTRAINT uq_refunds_provider_payment_id_provider_refund_id
        UNIQUE (provider_payment_id, provider_refund_id),
    CONSTRAINT fk_refunds_payment_intent_id_payment_intents
        FOREIGN KEY (payment_intent_id) REFERENCES public.payment_intents (id),
    CONSTRAINT fk_refunds_provider_payment_id_provider_payments
        FOREIGN KEY (provider_payment_id) REFERENCES public.provider_payments (id),
    CONSTRAINT ck_refunds_amount CHECK (amount > 0),
    CONSTRAINT ck_refunds_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_refunds_status CHECK (
        status IN ('requested', 'submitted', 'unknown', 'done', 'denied', 'cancelled')
    ),
    CONSTRAINT ck_refunds_reason CHECK (length(reason) BETWEEN 1 AND 500),
    CONSTRAINT ck_refunds_provider_refund_id CHECK (length(provider_refund_id) BETWEEN 1 AND 255),
    CONSTRAINT ck_refunds_origin CHECK (
        (origin = 'hub' AND attempt_id IS NOT NULL)
        OR (origin = 'provider' AND attempt_id IS NULL AND status = 'done')
    )
);
CREATE INDEX ix_refunds_payment_intent_id ON public.refunds (payment_intent_id, created_at, id);
CREATE INDEX ix_refunds_unresolved ON public.refunds (updated_at)
    WHERE status IN ('requested', 'unknown');

CREATE TABLE public.payment_attempts (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    payment_intent_id uuid NOT NULL,
    refund_id uuid,
    kind text NOT NULL,
    status text NOT NULL,
    request_hash bytea NOT NULL,
    outcome jsonb,
    created_at timestamptz NOT NULL,
    completed_at timestamptz,
    CONSTRAINT pk_payment_attempts PRIMARY KEY (id),
    CONSTRAINT fk_payment_attempts_payment_intent_id_payment_intents
        FOREIGN KEY (payment_intent_id) REFERENCES public.payment_intents (id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_payment_attempts_refund_id_refunds
        FOREIGN KEY (refund_id) REFERENCES public.refunds (id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_payment_attempts_kind CHECK (
        (kind = 'create_payment' AND refund_id IS NULL)
        OR (kind = 'refund' AND refund_id IS NOT NULL)
    ),
    CONSTRAINT ck_payment_attempts_status CHECK (
        status IN ('requested', 'succeeded', 'rejected', 'unknown', 'absent', 'superseded')
    ),
    CONSTRAINT ck_payment_attempts_request_hash CHECK (length(request_hash) = 32),
    CONSTRAINT ck_payment_attempts_outcome
        CHECK (outcome IS NULL OR jsonb_typeof(outcome) = 'object')
);
CREATE INDEX ix_payment_attempts_payment_intent_id
    ON public.payment_attempts (payment_intent_id, created_at);

-- The current attempt (fencing token) of an intent / refund is one of its attempts.
ALTER TABLE public.payment_intents
    ADD CONSTRAINT fk_payment_intents_attempt_id_payment_attempts
    FOREIGN KEY (attempt_id) REFERENCES public.payment_attempts (id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE public.refunds
    ADD CONSTRAINT fk_refunds_attempt_id_payment_attempts
    FOREIGN KEY (attempt_id) REFERENCES public.payment_attempts (id) DEFERRABLE INITIALLY DEFERRED;

-- An intent's customer and provider account belong to its project (ADR-0011), and the account
-- is of the intent's provider.
CREATE FUNCTION public.payment_intents_same_project() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
    account record;
BEGIN
    IF (SELECT project_id FROM public.customer_identities WHERE id = NEW.customer_id)
        IS DISTINCT FROM NEW.project_id THEN
        RAISE EXCEPTION 'payment intent: customer is in another project'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.provider_account_id IS NOT NULL THEN
        SELECT project_id, provider INTO account
            FROM public.provider_accounts WHERE id = NEW.provider_account_id;
        IF account.project_id IS DISTINCT FROM NEW.project_id
            OR account.provider IS DISTINCT FROM NEW.provider THEN
            RAISE EXCEPTION 'payment intent: provider account is in another project or provider'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER payment_intents_same_project
    BEFORE INSERT ON public.payment_intents
    FOR EACH ROW EXECUTE FUNCTION public.payment_intents_same_project();

-- Provider payments, refunds and attempts belong to their intent's project (and provider).
CREATE FUNCTION public.payments_match_intent() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
    intent record;
BEGIN
    SELECT project_id, provider, provider_account_id INTO intent
        FROM public.payment_intents WHERE id = NEW.payment_intent_id;
    IF intent.project_id IS DISTINCT FROM NEW.project_id THEN
        RAISE EXCEPTION '%: payment intent is in another project', TG_TABLE_NAME
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- Nested IFs: plpgsql does not short-circuit, and NEW has only its own table's fields.
    IF TG_TABLE_NAME = 'provider_payments' THEN
        IF intent.provider IS DISTINCT FROM NEW.provider
            OR intent.provider_account_id IS DISTINCT FROM NEW.provider_account_id THEN
            RAISE EXCEPTION 'provider payment: provider account differs from the intent''s'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF TG_TABLE_NAME = 'refunds' THEN
        IF (SELECT payment_intent_id FROM public.provider_payments
                WHERE id = NEW.provider_payment_id) IS DISTINCT FROM NEW.payment_intent_id THEN
            RAISE EXCEPTION 'refund: provider payment belongs to another intent'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER provider_payments_match_intent
    BEFORE INSERT ON public.provider_payments
    FOR EACH ROW EXECUTE FUNCTION public.payments_match_intent();
CREATE TRIGGER refunds_match_intent
    BEFORE INSERT ON public.refunds
    FOR EACH ROW EXECUTE FUNCTION public.payments_match_intent();
CREATE TRIGGER payment_attempts_match_intent
    BEFORE INSERT ON public.payment_attempts
    FOR EACH ROW EXECUTE FUNCTION public.payments_match_intent();
"""

GRANTS = """
REVOKE ALL ON public.payment_intents, public.provider_payments, public.refunds,
    public.payment_attempts FROM PUBLIC;

GRANT SELECT ON public.payment_intents, public.provider_payments, public.refunds,
    public.payment_attempts TO fh_app;
GRANT INSERT (id, project_id, customer_id, amount, currency, method, due_date, external_ref,
              description, metadata, provider, provider_account_id, creation_status, attempt_id,
              failure_code, created_at, updated_at)
    ON public.payment_intents TO fh_app;
GRANT UPDATE (creation_status, attempt_id, failure_code, updated_at)
    ON public.payment_intents TO fh_app;
GRANT INSERT (id, payment_intent_id, project_id, provider, provider_account_id,
              provider_payment_id, raw_status, capture_status, settlement_status, dispute_status,
              captured_amount, net_amount, fee_estimated, created_at, updated_at)
    ON public.provider_payments TO fh_app;
GRANT UPDATE (raw_status, capture_status, settlement_status, dispute_status, captured_amount,
              net_amount, fee_estimated, updated_at)
    ON public.provider_payments TO fh_app;
GRANT INSERT (id, project_id, payment_intent_id, provider_payment_id, amount, currency, status,
              reason, provider_refund_id, attempt_id, origin, created_at, updated_at)
    ON public.refunds TO fh_app;
GRANT UPDATE (status, provider_refund_id, attempt_id, updated_at) ON public.refunds TO fh_app;
GRANT INSERT (id, project_id, payment_intent_id, refund_id, kind, status, request_hash, outcome,
              created_at, completed_at)
    ON public.payment_attempts TO fh_app;
GRANT UPDATE (status, outcome, completed_at) ON public.payment_attempts TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute(
        "DROP TABLE public.payment_attempts, public.refunds, public.provider_payments, "
        "public.payment_intents"
    )
    op.execute("DROP FUNCTION public.payments_match_intent()")
    op.execute("DROP FUNCTION public.payment_intents_same_project()")
