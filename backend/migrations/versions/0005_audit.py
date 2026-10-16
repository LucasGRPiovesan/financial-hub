"""audit_records: append-only audit log of privileged actions (task 1.4)

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions. The runtime role ``fh_app``
gets INSERT and SELECT only. A row trigger rejects UPDATE/DELETE and a statement trigger rejects
TRUNCATE for every role, the table owner included (mirrors the ledger guard of revision 0001).

``project_id`` is NULL for global actions (operator login/logout). ``diff`` carries non-secret
before/after values only; secrets are replaced by ``"[redacted]"`` by the application.

Revision ID: 0005_audit
Revises: 0004_api_keys_providers
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_audit"
down_revision: str | None = "0004_api_keys_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.audit_records (
    id uuid NOT NULL,
    project_id uuid,
    actor_type text NOT NULL,
    actor_id text,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text,
    diff jsonb,
    correlation_id text,
    request_id text,
    ip inet,
    user_agent text,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_audit_records PRIMARY KEY (id),
    CONSTRAINT fk_audit_records_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT ck_audit_records_actor_type CHECK (actor_type IN ('operator', 'api_key', 'system')),
    CONSTRAINT ck_audit_records_action CHECK (action ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'),
    CONSTRAINT ck_audit_records_user_agent CHECK (length(user_agent) <= 256)
);
CREATE INDEX ix_audit_records_project_id_occurred_at
    ON public.audit_records (project_id, occurred_at DESC, id DESC);
CREATE INDEX ix_audit_records_target ON public.audit_records (target_type, target_id);

CREATE FUNCTION public.audit_reject_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    RAISE EXCEPTION 'audit: % on % is not allowed (append-only)', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END
$fn$;

CREATE TRIGGER audit_records_append_only BEFORE UPDATE OR DELETE ON public.audit_records
    FOR EACH ROW EXECUTE FUNCTION public.audit_reject_change();
CREATE TRIGGER audit_records_no_truncate BEFORE TRUNCATE ON public.audit_records
    FOR EACH STATEMENT EXECUTE FUNCTION public.audit_reject_change();
"""

GRANTS = """
REVOKE ALL ON public.audit_records FROM PUBLIC;
GRANT SELECT, INSERT ON public.audit_records TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP TABLE public.audit_records")
    op.execute("DROP FUNCTION public.audit_reject_change()")
