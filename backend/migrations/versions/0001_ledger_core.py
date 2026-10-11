"""ledger core: accounts, journal entries, postings, integrity triggers, ledger_apply, DB roles

ADR-0003 (revised) and FINANCIAL_MODEL s2. Runs as the migration (owner) login, which must be a
superuser or hold CREATEROLE: it creates two cluster roles idempotently.

- ``fh_owner`` (NOLOGIN) owns every ledger object, so the SECURITY DEFINER functions run as a
  non-superuser that can touch only what it owns.
- ``fh_app`` (LOGIN, runtime) gets SELECT/INSERT on the ledger tables, UPDATE only on
  ``journal_entries.status`` and EXECUTE on ``ledger_apply``/``ledger_lock_accounts``. It has no
  UPDATE/DELETE elsewhere, owns nothing (cannot ALTER/DISABLE TRIGGER), cannot create objects.
  Its password comes from ``FH_APP_DB_PASSWORD`` (never hard-coded); unset leaves it unchanged.

Balance columns change only inside ``ledger_apply``; ``applied_status`` records which status the
balances reflect, so applying twice is a no-op and the deferred check proves every committed entry
is applied exactly once.

Revision ID: 0001_ledger_core
Revises: 0000_baseline
Create Date: 2026-10-10
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_ledger_core"
down_revision: str | None = "0000_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_PASSWORD_ENV = "FH_APP_DB_PASSWORD"  # noqa: S105 - name of the env var, not a secret

ROLES = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fh_owner') THEN
        CREATE ROLE fh_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fh_app') THEN
        CREATE ROLE fh_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
    -- The migration login creates objects as fh_owner (SET ROLE below).
    EXECUTE pg_catalog.format('GRANT fh_owner TO %I', current_user);
    EXECUTE pg_catalog.format('GRANT CONNECT ON DATABASE %I TO fh_app', current_database());
    -- No temporary objects for anyone but superusers: fh_app gets no DDL at all.
    EXECUTE pg_catalog.format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO fh_owner;
GRANT USAGE ON SCHEMA public TO fh_app;
"""

ALTER_APP_LOGIN = """
DO $$
BEGIN
    EXECUTE 'ALTER ROLE fh_app WITH LOGIN PASSWORD '
        || pg_catalog.quote_literal(pg_catalog.current_setting('fh.app_password'));
END
$$;
"""

TABLES = """
CREATE TABLE public.ledger_accounts (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    code text NOT NULL,
    currency char(3) NOT NULL,
    type text NOT NULL,
    normal_balance text NOT NULL GENERATED ALWAYS AS (
        CASE WHEN type IN ('asset', 'expense') THEN 'debit' ELSE 'credit' END
    ) STORED,
    no_overdraft boolean NOT NULL DEFAULT false,
    customer_id uuid,
    payee_id uuid,
    provider_account_id uuid,
    posted_dr bigint NOT NULL DEFAULT 0,
    posted_cr bigint NOT NULL DEFAULT 0,
    pending_dr bigint NOT NULL DEFAULT 0,
    pending_cr bigint NOT NULL DEFAULT 0,
    version bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_ledger_accounts PRIMARY KEY (id),
    CONSTRAINT uq_ledger_accounts_project_code_currency UNIQUE (project_id, code, currency),
    CONSTRAINT uq_ledger_accounts_id_project_currency UNIQUE (id, project_id, currency),
    CONSTRAINT ck_ledger_accounts_type
        CHECK (type IN ('asset', 'liability', 'income', 'expense', 'equity')),
    CONSTRAINT ck_ledger_accounts_code CHECK (code ~ '^[[:graph:]]{1,200}$'),
    CONSTRAINT ck_ledger_accounts_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_ledger_accounts_balances_non_negative CHECK (
        posted_dr >= 0 AND posted_cr >= 0 AND pending_dr >= 0 AND pending_cr >= 0
    ),
    CONSTRAINT ck_ledger_accounts_version CHECK (version >= 0)
);

CREATE TABLE public.journal_entries (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    status text NOT NULL,
    applied_status text,
    idempotency_key text NOT NULL,
    payload_hash text NOT NULL,
    recipe text NOT NULL,
    reverses_entry_id uuid,
    correlation_id uuid,
    causation_id uuid,
    effective_at timestamptz NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_journal_entries PRIMARY KEY (id),
    CONSTRAINT uq_journal_entries_project_idempotency_key UNIQUE (project_id, idempotency_key),
    CONSTRAINT uq_journal_entries_id_project UNIQUE (id, project_id),
    CONSTRAINT fk_journal_entries_reverses_entry_id_journal_entries
        FOREIGN KEY (reverses_entry_id, project_id)
        REFERENCES public.journal_entries (id, project_id),
    CONSTRAINT ck_journal_entries_status CHECK (status IN ('pending', 'posted', 'archived')),
    CONSTRAINT ck_journal_entries_applied_status
        CHECK (applied_status IN ('pending', 'posted', 'archived')),
    CONSTRAINT ck_journal_entries_idempotency_key
        CHECK (length(idempotency_key) BETWEEN 1 AND 255),
    CONSTRAINT ck_journal_entries_payload_hash CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_journal_entries_recipe CHECK (length(recipe) BETWEEN 1 AND 64),
    CONSTRAINT ck_journal_entries_metadata CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT ck_journal_entries_not_self_reversal CHECK (reverses_entry_id <> id)
);
-- An entry is reversed at most once.
CREATE UNIQUE INDEX uq_journal_entries_reverses_entry_id
    ON public.journal_entries (reverses_entry_id) WHERE reverses_entry_id IS NOT NULL;

CREATE TABLE public.ledger_postings (
    id uuid NOT NULL,
    entry_id uuid NOT NULL,
    project_id uuid NOT NULL,
    account_id uuid NOT NULL,
    currency char(3) NOT NULL,
    direction text NOT NULL,
    amount bigint NOT NULL,
    CONSTRAINT pk_ledger_postings PRIMARY KEY (id),
    CONSTRAINT fk_ledger_postings_entry_id_journal_entries
        FOREIGN KEY (entry_id, project_id) REFERENCES public.journal_entries (id, project_id),
    CONSTRAINT fk_ledger_postings_account_id_ledger_accounts
        FOREIGN KEY (account_id, project_id, currency)
        REFERENCES public.ledger_accounts (id, project_id, currency),
    CONSTRAINT ck_ledger_postings_direction CHECK (direction IN ('debit', 'credit')),
    CONSTRAINT ck_ledger_postings_amount_positive CHECK (amount > 0)
);
CREATE INDEX ix_ledger_postings_entry_id ON public.ledger_postings (entry_id);
CREATE INDEX ix_ledger_postings_account_id ON public.ledger_postings (account_id);
"""

FUNCTIONS = """
-- Append-only guard (UPDATE/DELETE/TRUNCATE on postings, DELETE/TRUNCATE on entries/accounts).
CREATE FUNCTION public.ledger_reject_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    RAISE EXCEPTION 'ledger: % on % is not allowed (append-only)', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END
$fn$;

CREATE FUNCTION public.ledger_accounts_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.posted_dr <> 0 OR NEW.posted_cr <> 0 OR NEW.pending_dr <> 0
           OR NEW.pending_cr <> 0 OR NEW.version <> 0 THEN
            RAISE EXCEPTION 'ledger: accounts start with zero balances'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.project_id, NEW.code, NEW.currency, NEW.type, NEW.no_overdraft,
        NEW.customer_id, NEW.payee_id, NEW.provider_account_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.code, OLD.currency, OLD.type, OLD.no_overdraft,
        OLD.customer_id, OLD.payee_id, OLD.provider_account_id, OLD.created_at) THEN
        RAISE EXCEPTION 'ledger: account % identity is immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.posted_dr, NEW.posted_cr, NEW.pending_dr, NEW.pending_cr, NEW.version)
       IS DISTINCT FROM (OLD.posted_dr, OLD.posted_cr, OLD.pending_dr, OLD.pending_cr, OLD.version)
       AND pg_catalog.current_setting('fh.ledger_apply', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'ledger: balances of account % change only through ledger_apply', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION public.journal_entries_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.applied_status IS NOT NULL THEN
            RAISE EXCEPTION 'ledger: applied_status is set only by ledger_apply'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.status NOT IN ('pending', 'posted') THEN
            RAISE EXCEPTION 'ledger: a new entry is pending or posted, got %', NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.project_id, NEW.idempotency_key, NEW.payload_hash, NEW.recipe,
        NEW.reverses_entry_id, NEW.correlation_id, NEW.causation_id, NEW.effective_at,
        NEW.metadata, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.idempotency_key, OLD.payload_hash, OLD.recipe,
        OLD.reverses_entry_id, OLD.correlation_id, OLD.causation_id, OLD.effective_at,
        OLD.metadata, OLD.created_at) THEN
        RAISE EXCEPTION 'ledger: journal entry % is immutable except its status', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'pending' AND NEW.status IN ('posted', 'archived')) THEN
        RAISE EXCEPTION 'ledger: journal entry % cannot go from % to %',
            OLD.id, OLD.status, NEW.status USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.applied_status IS DISTINCT FROM OLD.applied_status
       AND (NEW.applied_status IS DISTINCT FROM NEW.status
            OR pg_catalog.current_setting('fh.ledger_apply', true) IS DISTINCT FROM 'on') THEN
        RAISE EXCEPTION 'ledger: applied_status of % is set only by ledger_apply', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;

-- Postings can only join an entry whose balances were not applied yet (same transaction).
CREATE FUNCTION public.ledger_postings_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
    v_applied text;
BEGIN
    SELECT applied_status INTO v_applied FROM public.journal_entries WHERE id = NEW.entry_id;
    IF FOUND AND v_applied IS NOT NULL THEN
        RAISE EXCEPTION 'ledger: entry % is already applied; postings are immutable',
            NEW.entry_id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;

-- Invariants of one entry, checked at commit (DEFERRABLE INITIALLY DEFERRED triggers below).
CREATE FUNCTION public.ledger_assert_entry(p_entry_id uuid) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
    v_project uuid;
    v_status text;
    v_applied text;
    v_count bigint;
BEGIN
    SELECT project_id, status, applied_status INTO v_project, v_status, v_applied
        FROM public.journal_entries WHERE id = p_entry_id;
    IF NOT FOUND THEN
        RETURN;  -- the composite foreign keys reject orphan postings
    END IF;
    IF v_applied IS DISTINCT FROM v_status THEN
        RAISE EXCEPTION 'ledger: entry % balances not applied (status %, applied %)',
            p_entry_id, v_status, v_applied USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*) INTO v_count FROM public.ledger_postings WHERE entry_id = p_entry_id;
    IF v_count < 2 THEN
        RAISE EXCEPTION 'ledger: entry % has % posting(s), needs at least 2', p_entry_id, v_count
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.ledger_postings WHERE entry_id = p_entry_id GROUP BY currency
        HAVING sum(CASE WHEN direction = 'debit' THEN amount ELSE 0 END)
            <> sum(CASE WHEN direction = 'credit' THEN amount ELSE 0 END)
    ) THEN
        RAISE EXCEPTION 'ledger: entry % is not balanced per currency', p_entry_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.ledger_postings p
        JOIN public.ledger_accounts a ON a.id = p.account_id
        WHERE p.entry_id = p_entry_id
          AND (p.project_id <> v_project OR a.project_id <> v_project OR a.currency <> p.currency)
    ) THEN
        RAISE EXCEPTION 'ledger: entry % mixes projects or currencies', p_entry_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.ledger_accounts a
        WHERE a.id IN (SELECT account_id FROM public.ledger_postings WHERE entry_id = p_entry_id)
          AND a.no_overdraft
          AND CASE a.normal_balance
                  WHEN 'credit' THEN a.posted_cr - a.posted_dr - a.pending_dr
                  ELSE a.posted_dr - a.posted_cr - a.pending_cr
              END < 0
    ) THEN
        RAISE EXCEPTION 'ledger: entry % overdraws a no_overdraft account', p_entry_id
            USING ERRCODE = 'check_violation';
    END IF;
END
$fn$;

CREATE FUNCTION public.ledger_check_entry() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
    v_entry uuid;
BEGIN
    IF TG_TABLE_NAME = 'ledger_postings' THEN
        v_entry := NEW.entry_id;
    ELSE
        v_entry := NEW.id;
    END IF;
    PERFORM public.ledger_assert_entry(v_entry);
    RETURN NULL;
END
$fn$;

-- Locks accounts in id order (the only lock order in the ledger, ADR-0003 s5). Never mutates.
CREATE FUNCTION public.ledger_lock_accounts(p_account_ids uuid[])
RETURNS SETOF public.ledger_accounts
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $fn$
    SELECT * FROM public.ledger_accounts
    WHERE id = ANY (p_account_ids)
    ORDER BY id
    FOR UPDATE;
$fn$;

-- The only writer of balance columns and applied_status. Idempotent per status.
CREATE FUNCTION public.ledger_apply(p_entry_id uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $fn$
DECLARE
    v_status text;
    v_applied text;
    v_pending integer;
    v_posted integer;
    v_count integer;
BEGIN
    SELECT status, applied_status INTO v_status, v_applied
        FROM public.journal_entries WHERE id = p_entry_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger_apply: entry % not found', p_entry_id
            USING ERRCODE = 'no_data_found';
    END IF;
    IF v_applied IS NOT DISTINCT FROM v_status THEN
        RETURN 0;
    ELSIF v_applied IS NULL AND v_status = 'pending' THEN
        v_pending := 1; v_posted := 0;
    ELSIF v_applied IS NULL AND v_status = 'posted' THEN
        v_pending := 0; v_posted := 1;
    ELSIF v_applied = 'pending' AND v_status = 'posted' THEN
        v_pending := -1; v_posted := 1;
    ELSIF v_applied = 'pending' AND v_status = 'archived' THEN
        v_pending := -1; v_posted := 0;
    ELSE
        RAISE EXCEPTION 'ledger_apply: invalid transition % -> % for entry %',
            v_applied, v_status, p_entry_id USING ERRCODE = 'restrict_violation';
    END IF;

    PERFORM 1 FROM public.ledger_accounts
        WHERE id IN (SELECT account_id FROM public.ledger_postings WHERE entry_id = p_entry_id)
        ORDER BY id FOR UPDATE;

    PERFORM pg_catalog.set_config('fh.ledger_apply', 'on', true);
    UPDATE public.ledger_accounts a
    SET posted_dr = a.posted_dr + v_posted * d.dr,
        posted_cr = a.posted_cr + v_posted * d.cr,
        pending_dr = a.pending_dr + v_pending * d.dr,
        pending_cr = a.pending_cr + v_pending * d.cr,
        version = a.version + 1
    FROM (
        SELECT account_id,
               coalesce(sum(amount) FILTER (WHERE direction = 'debit'), 0) AS dr,
               coalesce(sum(amount) FILTER (WHERE direction = 'credit'), 0) AS cr
        FROM public.ledger_postings WHERE entry_id = p_entry_id GROUP BY account_id
    ) d
    WHERE a.id = d.account_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    UPDATE public.journal_entries SET applied_status = v_status WHERE id = p_entry_id;
    PERFORM pg_catalog.set_config('fh.ledger_apply', 'off', true);
    RETURN v_count;
END
$fn$;
"""

TRIGGERS = """
CREATE TRIGGER ledger_accounts_guard BEFORE INSERT OR UPDATE ON public.ledger_accounts
    FOR EACH ROW EXECUTE FUNCTION public.ledger_accounts_guard();
CREATE TRIGGER ledger_accounts_no_delete BEFORE DELETE ON public.ledger_accounts
    FOR EACH ROW EXECUTE FUNCTION public.ledger_reject_change();
CREATE TRIGGER ledger_accounts_no_truncate BEFORE TRUNCATE ON public.ledger_accounts
    FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_reject_change();

CREATE TRIGGER journal_entries_guard BEFORE INSERT OR UPDATE ON public.journal_entries
    FOR EACH ROW EXECUTE FUNCTION public.journal_entries_guard();
CREATE TRIGGER journal_entries_no_delete BEFORE DELETE ON public.journal_entries
    FOR EACH ROW EXECUTE FUNCTION public.ledger_reject_change();
CREATE TRIGGER journal_entries_no_truncate BEFORE TRUNCATE ON public.journal_entries
    FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_reject_change();
CREATE CONSTRAINT TRIGGER journal_entries_check AFTER INSERT OR UPDATE ON public.journal_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.ledger_check_entry();

CREATE TRIGGER ledger_postings_guard BEFORE INSERT ON public.ledger_postings
    FOR EACH ROW EXECUTE FUNCTION public.ledger_postings_guard();
CREATE TRIGGER ledger_postings_append_only BEFORE UPDATE OR DELETE ON public.ledger_postings
    FOR EACH ROW EXECUTE FUNCTION public.ledger_reject_change();
CREATE TRIGGER ledger_postings_no_truncate BEFORE TRUNCATE ON public.ledger_postings
    FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_reject_change();
CREATE CONSTRAINT TRIGGER ledger_postings_check AFTER INSERT ON public.ledger_postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.ledger_check_entry();
"""

GRANTS = """
REVOKE ALL ON public.ledger_accounts, public.journal_entries, public.ledger_postings FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.ledger_reject_change(), public.ledger_accounts_guard(),
    public.journal_entries_guard(), public.ledger_postings_guard(),
    public.ledger_assert_entry(uuid), public.ledger_check_entry(),
    public.ledger_lock_accounts(uuid[]), public.ledger_apply(uuid)
FROM PUBLIC;

GRANT SELECT ON public.ledger_accounts, public.journal_entries, public.ledger_postings TO fh_app;
GRANT INSERT (id, project_id, code, currency, type, no_overdraft,
              customer_id, payee_id, provider_account_id)
    ON public.ledger_accounts TO fh_app;
GRANT INSERT (id, project_id, status, idempotency_key, payload_hash, recipe, reverses_entry_id,
              correlation_id, causation_id, effective_at, metadata)
    ON public.journal_entries TO fh_app;
GRANT UPDATE (status) ON public.journal_entries TO fh_app;
GRANT INSERT ON public.ledger_postings TO fh_app;
-- Trigger functions run as the invoking role (fh_app); the deferred check calls this one.
GRANT EXECUTE ON FUNCTION public.ledger_assert_entry(uuid) TO fh_app;
GRANT EXECUTE ON FUNCTION public.ledger_lock_accounts(uuid[]), public.ledger_apply(uuid) TO fh_app;
"""


def upgrade() -> None:
    op.execute(ROLES)
    password = os.environ.get(APP_PASSWORD_ENV)
    if password:
        # Quoted server-side (quote_literal) from a transaction-local setting: no SQL injection,
        # and the value never appears in the statement text of the ALTER ROLE.
        op.execute(
            sa.text("SELECT pg_catalog.set_config('fh.app_password', :password, true)").bindparams(
                password=password
            )
        )
        op.execute(ALTER_APP_LOGIN)
        op.execute("SELECT pg_catalog.set_config('fh.app_password', '', true)")
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(FUNCTIONS)
    op.execute(TRIGGERS)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    # ledger_lock_accounts returns the ledger_accounts row type: drop it before the tables.
    op.execute("DROP FUNCTION public.ledger_lock_accounts(uuid[])")
    op.execute("DROP TABLE public.ledger_postings, public.journal_entries, public.ledger_accounts")
    op.execute(
        """
        DROP FUNCTION public.ledger_apply(uuid),
            public.ledger_check_entry(), public.ledger_assert_entry(uuid),
            public.ledger_postings_guard(), public.journal_entries_guard(),
            public.ledger_accounts_guard(), public.ledger_reject_change();
        DO $$
        BEGIN
            -- Leaves this database without any privilege of the roles, then drops them.
            EXECUTE pg_catalog.format('GRANT TEMPORARY ON DATABASE %I TO PUBLIC',
                current_database());
            EXECUTE pg_catalog.format('REVOKE ALL ON DATABASE %I FROM fh_app', current_database());
        END
        $$;
        REVOKE ALL ON SCHEMA public FROM fh_app, fh_owner;
        DROP OWNED BY fh_app, fh_owner;
        DROP ROLE fh_app;
        DROP ROLE fh_owner;
        """
    )
