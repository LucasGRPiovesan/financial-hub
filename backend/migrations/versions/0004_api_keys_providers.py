"""api_keys and provider_accounts: project API keys and encrypted provider credentials (task 1.2)

Objects are created as ``fh_owner`` (``SET ROLE``), like the previous revisions. The runtime role
``fh_app`` gets no DELETE/TRUNCATE on either table (keys are revoked, accounts disabled):

- ``api_keys``: SELECT, INSERT, UPDATE(last_used_at, revoked_at). Only the HMAC-SHA256 digest of the
  secret is stored (``secret_hash``); ``prefix`` is the public lookup id. Name, scopes, project,
  expiry and creator are immutable for the runtime, and a trigger makes revocation irreversible
  (``revoked_at`` can never be cleared or moved).
- ``provider_accounts``: SELECT, INSERT, UPDATE(name, status, key_version, nonce, ciphertext,
  credentials_hint, updated_at). ``provider``, ``environment`` and ``project_id`` are immutable
  (they are part of the AES-GCM associated data, so changing them would orphan the ciphertext).
  ``nonce`` is 12 bytes; ``ciphertext`` includes the 16-byte GCM tag.

Revision ID: 0004_api_keys_providers
Revises: 0003_identity
Create Date: 2026-10-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_api_keys_providers"
down_revision: str | None = "0003_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.api_keys (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    name text NOT NULL,
    environment text NOT NULL,
    prefix text NOT NULL,
    secret_hash bytea NOT NULL,
    scopes text[] NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL,
    last_used_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz,
    CONSTRAINT pk_api_keys PRIMARY KEY (id),
    CONSTRAINT uq_api_keys_prefix UNIQUE (prefix),
    CONSTRAINT fk_api_keys_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT fk_api_keys_created_by_operators
        FOREIGN KEY (created_by) REFERENCES public.operators (id),
    CONSTRAINT ck_api_keys_name CHECK (length(name) BETWEEN 1 AND 100 AND name = btrim(name)),
    CONSTRAINT ck_api_keys_environment CHECK (environment IN ('test', 'live')),
    CONSTRAINT ck_api_keys_prefix CHECK (prefix ~ '^[a-z0-9]{8}$'),
    CONSTRAINT ck_api_keys_secret_hash CHECK (length(secret_hash) = 32),
    CONSTRAINT ck_api_keys_scopes CHECK (cardinality(scopes) >= 1),
    CONSTRAINT ck_api_keys_expiry CHECK (expires_at IS NULL OR expires_at > created_at)
);
CREATE INDEX ix_api_keys_project_id_created_at ON public.api_keys (project_id, created_at, id);

CREATE TABLE public.provider_accounts (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    provider text NOT NULL,
    environment text NOT NULL,
    name text NOT NULL,
    status text NOT NULL,
    key_version integer NOT NULL,
    nonce bytea NOT NULL,
    ciphertext bytea NOT NULL,
    credentials_hint text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_provider_accounts PRIMARY KEY (id),
    CONSTRAINT uq_provider_accounts_project_id
        UNIQUE (project_id, provider, environment, name),
    CONSTRAINT fk_provider_accounts_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT ck_provider_accounts_provider
        CHECK (provider IN ('asaas', 'stripe', 'mercadopago')),
    CONSTRAINT ck_provider_accounts_environment CHECK (environment IN ('sandbox', 'production')),
    CONSTRAINT ck_provider_accounts_name
        CHECK (length(name) BETWEEN 1 AND 100 AND name = btrim(name)),
    CONSTRAINT ck_provider_accounts_status CHECK (status IN ('active', 'disabled')),
    CONSTRAINT ck_provider_accounts_key_version CHECK (key_version >= 1),
    CONSTRAINT ck_provider_accounts_nonce CHECK (length(nonce) = 12),
    CONSTRAINT ck_provider_accounts_ciphertext CHECK (length(ciphertext) > 16),
    CONSTRAINT ck_provider_accounts_credentials_hint CHECK (length(credentials_hint) <= 4)
);
CREATE INDEX ix_provider_accounts_project_id_created_at
    ON public.provider_accounts (project_id, created_at, id);

CREATE FUNCTION public.api_keys_revocation_is_final() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
        RAISE EXCEPTION 'api key % is revoked: revocation is final', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER api_keys_revocation_is_final
    BEFORE UPDATE ON public.api_keys
    FOR EACH ROW EXECUTE FUNCTION public.api_keys_revocation_is_final();
"""

GRANTS = """
REVOKE ALL ON public.api_keys, public.provider_accounts FROM PUBLIC;

GRANT SELECT ON public.api_keys, public.provider_accounts TO fh_app;
GRANT INSERT (id, project_id, name, environment, prefix, secret_hash, scopes, created_by,
              created_at, expires_at)
    ON public.api_keys TO fh_app;
GRANT UPDATE (last_used_at, revoked_at) ON public.api_keys TO fh_app;
GRANT INSERT (id, project_id, provider, environment, name, status, key_version, nonce,
              ciphertext, credentials_hint, created_at, updated_at)
    ON public.provider_accounts TO fh_app;
GRANT UPDATE (name, status, key_version, nonce, ciphertext, credentials_hint, updated_at)
    ON public.provider_accounts TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP TABLE public.provider_accounts, public.api_keys")
    op.execute("DROP FUNCTION public.api_keys_revocation_is_final()")
