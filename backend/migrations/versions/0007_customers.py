"""Universal customer tables: financial_customers, customer_identities, provider_customers.

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions. The runtime role ``fh_app``
gets no DELETE/TRUNCATE on any table (erasure is anonymization, docs/SECURITY.md) and UPDATE
only on the columns the application changes:

- ``financial_customers``: the universal record, one per legal person. ``document_hash`` is
  HMAC-SHA256 of the normalised CPF/CNPJ under a key derived from the API-key pepper: it lets the
  platform recognise the same person across projects without storing or exposing the number.
  The document itself is AES-256-GCM sealed (``document_key_version``/``document_nonce``/
  ``document_ciphertext``; AAD ``customer|<fcus id>``) and ``document_last4`` feeds the masked
  display. The five document columns are all set or all NULL. Anonymization nulls the hash, so
  the plain UNIQUE on ``document_hash`` is "unique among live customers" (NULLs never collide).
- ``customer_identities``: ``(project_id, external_ref)`` -> universal customer. This is what a
  project sees and owns; its name/email/phone/metadata are project-private and are wiped on
  anonymization. ``financial_customer_id`` is internal and never in an API response.
- ``provider_customers``: ``(identity, provider account)`` -> the provider's customer id. A trigger
  makes sure the identity and the account belong to the same project. Mappings are immutable.

Revision ID: 0007_customers
Revises: 0006_idempotency_rate_limits
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_customers"
down_revision: str | None = "0006_idempotency_rate_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.financial_customers (
    id uuid NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    email text,
    phone text,
    document_hash bytea,
    document_key_version integer,
    document_nonce bytea,
    document_ciphertext bytea,
    document_last4 text,
    anonymized_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_financial_customers PRIMARY KEY (id),
    CONSTRAINT uq_financial_customers_document_hash UNIQUE (document_hash),
    CONSTRAINT ck_financial_customers_kind CHECK (kind IN ('individual', 'company')),
    CONSTRAINT ck_financial_customers_name CHECK (length(name) BETWEEN 1 AND 200),
    CONSTRAINT ck_financial_customers_document CHECK (
        (document_hash IS NULL AND document_key_version IS NULL AND document_nonce IS NULL
            AND document_ciphertext IS NULL AND document_last4 IS NULL)
        OR (document_hash IS NOT NULL AND document_key_version >= 1
            AND length(document_hash) = 32 AND length(document_nonce) = 12
            AND length(document_ciphertext) > 16 AND document_last4 ~ '^[0-9]{4}$')
    ),
    CONSTRAINT ck_financial_customers_anonymized CHECK (
        anonymized_at IS NULL
        OR (name = 'anonymized' AND email IS NULL AND phone IS NULL AND document_hash IS NULL)
    )
);

CREATE TABLE public.customer_identities (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    financial_customer_id uuid NOT NULL,
    external_ref text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    email text,
    phone text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    anonymized_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_customer_identities PRIMARY KEY (id),
    CONSTRAINT uq_customer_identities_project_id UNIQUE (project_id, external_ref),
    CONSTRAINT fk_customer_identities_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT fk_customer_identities_financial_customer_id_financial_customers
        FOREIGN KEY (financial_customer_id) REFERENCES public.financial_customers (id),
    CONSTRAINT ck_customer_identities_external_ref
        CHECK (external_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:@+=~-]{0,127}$'),
    CONSTRAINT ck_customer_identities_kind CHECK (kind IN ('individual', 'company')),
    CONSTRAINT ck_customer_identities_name CHECK (length(name) BETWEEN 1 AND 200),
    CONSTRAINT ck_customer_identities_metadata
        CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 8192),
    CONSTRAINT ck_customer_identities_anonymized CHECK (
        anonymized_at IS NULL
        OR (name = 'anonymized' AND email IS NULL AND phone IS NULL AND metadata = '{}'::jsonb)
    )
);
CREATE INDEX ix_customer_identities_project_id_created_at
    ON public.customer_identities (project_id, created_at, id);
CREATE INDEX ix_customer_identities_project_id_email
    ON public.customer_identities (project_id, email) WHERE email IS NOT NULL;
CREATE INDEX ix_customer_identities_financial_customer_id
    ON public.customer_identities (financial_customer_id);

CREATE TABLE public.provider_customers (
    identity_id uuid NOT NULL,
    provider_account_id uuid NOT NULL,
    provider_customer_id text NOT NULL,
    created_at timestamptz NOT NULL,
    CONSTRAINT pk_provider_customers PRIMARY KEY (identity_id, provider_account_id),
    CONSTRAINT uq_provider_customers_provider_account_id
        UNIQUE (provider_account_id, provider_customer_id),
    CONSTRAINT fk_provider_customers_identity_id_customer_identities
        FOREIGN KEY (identity_id) REFERENCES public.customer_identities (id),
    CONSTRAINT fk_provider_customers_provider_account_id_provider_accounts
        FOREIGN KEY (provider_account_id) REFERENCES public.provider_accounts (id),
    CONSTRAINT ck_provider_customers_provider_customer_id
        CHECK (length(provider_customer_id) BETWEEN 1 AND 255)
);

-- A provider account only maps customers of its own project (ADR-0011).
CREATE FUNCTION public.provider_customers_same_project() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF (SELECT project_id FROM public.customer_identities WHERE id = NEW.identity_id)
        IS DISTINCT FROM
       (SELECT project_id FROM public.provider_accounts WHERE id = NEW.provider_account_id) THEN
        RAISE EXCEPTION 'provider customer: identity and provider account are in different projects'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER provider_customers_same_project
    BEFORE INSERT ON public.provider_customers
    FOR EACH ROW EXECUTE FUNCTION public.provider_customers_same_project();
"""

GRANTS = """
REVOKE ALL ON public.financial_customers, public.customer_identities, public.provider_customers
    FROM PUBLIC;

GRANT SELECT ON public.financial_customers, public.customer_identities,
    public.provider_customers TO fh_app;
GRANT INSERT (id, kind, name, email, phone, document_hash, document_key_version, document_nonce,
              document_ciphertext, document_last4, created_at, updated_at)
    ON public.financial_customers TO fh_app;
GRANT UPDATE (kind, name, email, phone, document_hash, document_key_version, document_nonce,
              document_ciphertext, document_last4, anonymized_at, updated_at)
    ON public.financial_customers TO fh_app;
GRANT INSERT (id, project_id, financial_customer_id, external_ref, kind, name, email, phone,
              metadata, created_at, updated_at)
    ON public.customer_identities TO fh_app;
GRANT UPDATE (financial_customer_id, kind, name, email, phone, metadata, anonymized_at, updated_at)
    ON public.customer_identities TO fh_app;
GRANT INSERT (identity_id, provider_account_id, provider_customer_id, created_at)
    ON public.provider_customers TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP TABLE public.provider_customers")
    op.execute("DROP FUNCTION public.provider_customers_same_project()")
    op.execute("DROP TABLE public.customer_identities, public.financial_customers")
