"""Integrations: manifest versions, activations, materialized endpoints, business events (W6.1,
ADR-0008, docs/INTEGRATIONS.md s1).

Created as ``fh_owner`` (``SET ROLE``), like the previous revisions.

- ``integration_manifests``: one row per ``(project, version)``. ``content`` is the manifest as
  submitted and ``content_hash`` its canonical SHA-256; both are mutable only while the row is a
  ``draft`` (trigger), so an approval always refers to exactly the reviewed content. The status
  moves only along ``draft -> validated -> approved -> active <-> superseded`` (``rejected`` from
  any pre-active state); at most one ``active`` version per project (partial unique index).
  ``four_eyes`` records whether the approver differs from the author (CHECK); approval columns
  are written once.
- ``integration_manifest_activations``: append-only history of activations and rollbacks, with
  what each one materialized (metric codes, endpoint ids; never secrets).
- ``integration_webhook_endpoints``: the endpoints a manifest activation created, keyed by
  ``(project, fingerprint)`` of url + event types, so re-activating creates no duplicates.
- ``business_events``: append-only; unique ``(project, name, idempotency_key)`` makes ingestion
  idempotent. ``payload`` is stored with the manifest's personal-data pointers redacted.

``fh_app`` has no DELETE; it may UPDATE only the lifecycle columns of ``integration_manifests``.

Revision ID: 0015_integrations
Revises: 0014_reconciliation
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_integrations"
down_revision: str | None = "0014_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.integration_manifests (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    version integer NOT NULL,
    status text NOT NULL,
    content jsonb NOT NULL,
    content_hash text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    validated_at timestamptz,
    approved_by text,
    approved_at timestamptz,
    four_eyes boolean,
    approval_base_version integer,
    approval_diff jsonb,
    rejected_by text,
    rejected_at timestamptz,
    rejection_reason text,
    activated_at timestamptz,
    superseded_at timestamptz,
    CONSTRAINT pk_integration_manifests PRIMARY KEY (id),
    CONSTRAINT uq_integration_manifests_project_id_version UNIQUE (project_id, version),
    CONSTRAINT fk_integration_manifests_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT ck_integration_manifests_version CHECK (version >= 1),
    CONSTRAINT ck_integration_manifests_status CHECK (status IN (
        'draft', 'validated', 'approved', 'active', 'superseded', 'rejected')),
    CONSTRAINT ck_integration_manifests_content CHECK (
        jsonb_typeof(content) = 'object' AND octet_length(content::text) <= 524288),
    CONSTRAINT ck_integration_manifests_content_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_integration_manifests_approval CHECK (
        (approved_by IS NULL) = (approved_at IS NULL)
        AND (approved_by IS NULL) = (four_eyes IS NULL)
        AND (four_eyes IS NULL OR four_eyes = (approved_by <> created_by))
        AND (status NOT IN ('approved', 'active', 'superseded') OR approved_by IS NOT NULL)),
    CONSTRAINT ck_integration_manifests_validated CHECK (
        status IN ('draft', 'rejected') OR validated_at IS NOT NULL),
    CONSTRAINT ck_integration_manifests_rejected CHECK (
        (status = 'rejected') = (rejected_at IS NOT NULL)
        AND (rejected_by IS NULL) = (rejected_at IS NULL)
        AND (rejection_reason IS NULL OR length(rejection_reason) BETWEEN 1 AND 1000)),
    CONSTRAINT ck_integration_manifests_approval_diff CHECK (
        approval_diff IS NULL OR (jsonb_typeof(approval_diff) = 'object'
            AND octet_length(approval_diff::text) <= 1048576))
);
CREATE UNIQUE INDEX uq_integration_manifests_project_id_active
    ON public.integration_manifests (project_id) WHERE status = 'active';

CREATE FUNCTION public.integration_manifests_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id <> OLD.id OR NEW.project_id <> OLD.project_id OR NEW.version <> OLD.version
       OR NEW.created_by <> OLD.created_by OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'manifest version identity is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.status <> 'draft' AND (NEW.content IS DISTINCT FROM OLD.content
                                  OR NEW.content_hash <> OLD.content_hash) THEN
        RAISE EXCEPTION 'manifest version % is immutable once validated', OLD.version
            USING ERRCODE = '23514';
    END IF;
    IF OLD.approved_by IS NOT NULL AND (
        NEW.approved_by IS DISTINCT FROM OLD.approved_by
        OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
        OR NEW.four_eyes IS DISTINCT FROM OLD.four_eyes
        OR NEW.approval_base_version IS DISTINCT FROM OLD.approval_base_version
        OR NEW.approval_diff IS DISTINCT FROM OLD.approval_diff) THEN
        RAISE EXCEPTION 'manifest version % approval is final', OLD.version
            USING ERRCODE = '23514';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'draft' AND NEW.status IN ('validated', 'rejected'))
        OR (OLD.status = 'validated' AND NEW.status IN ('approved', 'rejected'))
        OR (OLD.status = 'approved' AND NEW.status IN ('active', 'rejected'))
        OR (OLD.status = 'active' AND NEW.status = 'superseded')
        OR (OLD.status = 'superseded' AND NEW.status = 'active')
    ) THEN
        RAISE EXCEPTION 'illegal manifest transition % -> %', OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status = 'rejected' THEN
        RAISE EXCEPTION 'manifest version % is rejected', OLD.version USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_integration_manifests_guard
    BEFORE UPDATE ON public.integration_manifests
    FOR EACH ROW EXECUTE FUNCTION public.integration_manifests_guard();

CREATE TABLE public.integration_manifest_activations (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    version integer NOT NULL,
    previous_version integer,
    activated_by text NOT NULL,
    activated_at timestamptz NOT NULL,
    materialized jsonb NOT NULL,
    CONSTRAINT pk_integration_manifest_activations PRIMARY KEY (id),
    CONSTRAINT fk_integration_manifest_activations_project_id_version_integration_manifests
        FOREIGN KEY (project_id, version)
        REFERENCES public.integration_manifests (project_id, version),
    CONSTRAINT ck_integration_manifest_activations_materialized CHECK (
        jsonb_typeof(materialized) = 'object' AND octet_length(materialized::text) <= 65536)
);
CREATE INDEX ix_integration_manifest_activations_project_id_activated_at
    ON public.integration_manifest_activations (project_id, activated_at DESC, id DESC);

CREATE TABLE public.integration_webhook_endpoints (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    fingerprint text NOT NULL,
    endpoint_id text NOT NULL,
    url text NOT NULL,
    event_types text[] NOT NULL,
    version integer NOT NULL,
    created_at timestamptz NOT NULL,
    CONSTRAINT pk_integration_webhook_endpoints PRIMARY KEY (id),
    CONSTRAINT uq_integration_webhook_endpoints_project_id_fingerprint
        UNIQUE (project_id, fingerprint),
    CONSTRAINT fk_integration_webhook_endpoints_project_id_version_integration_manifests
        FOREIGN KEY (project_id, version)
        REFERENCES public.integration_manifests (project_id, version),
    CONSTRAINT ck_integration_webhook_endpoints_fingerprint
        CHECK (fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TABLE public.business_events (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    name text NOT NULL,
    idempotency_key text NOT NULL,
    payload_hash text NOT NULL,
    payload jsonb NOT NULL,
    manifest_version integer NOT NULL,
    outcome jsonb NOT NULL,
    received_at timestamptz NOT NULL,
    CONSTRAINT pk_business_events PRIMARY KEY (id),
    CONSTRAINT uq_business_events_project_id_name_idempotency_key
        UNIQUE (project_id, name, idempotency_key),
    CONSTRAINT fk_business_events_project_id_manifest_version_integration_manifests
        FOREIGN KEY (project_id, manifest_version)
        REFERENCES public.integration_manifests (project_id, version),
    CONSTRAINT ck_business_events_name CHECK (length(name) BETWEEN 1 AND 100),
    CONSTRAINT ck_business_events_idempotency_key
        CHECK (idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+=~-]{0,63}$'),
    CONSTRAINT ck_business_events_payload_hash CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_business_events_json CHECK (
        jsonb_typeof(payload) = 'object' AND jsonb_typeof(outcome) = 'object'
        AND octet_length(payload::text) + octet_length(outcome::text) <= 262144)
);
CREATE INDEX ix_business_events_project_id_received_at
    ON public.business_events (project_id, received_at DESC, id DESC);
"""

GRANTS = """
REVOKE ALL ON public.integration_manifests, public.integration_manifest_activations,
    public.integration_webhook_endpoints, public.business_events FROM PUBLIC;

GRANT SELECT ON public.integration_manifests, public.integration_manifest_activations,
    public.integration_webhook_endpoints, public.business_events TO fh_app;
GRANT INSERT (id, project_id, version, status, content, content_hash, created_by, created_at,
              updated_at)
    ON public.integration_manifests TO fh_app;
GRANT UPDATE (status, content, content_hash, updated_at, validated_at, approved_by, approved_at,
              four_eyes, approval_base_version, approval_diff, rejected_by, rejected_at,
              rejection_reason, activated_at, superseded_at)
    ON public.integration_manifests TO fh_app;
GRANT INSERT (id, project_id, version, previous_version, activated_by, activated_at, materialized)
    ON public.integration_manifest_activations TO fh_app;
GRANT INSERT (id, project_id, fingerprint, endpoint_id, url, event_types, version, created_at)
    ON public.integration_webhook_endpoints TO fh_app;
GRANT INSERT (id, project_id, name, idempotency_key, payload_hash, payload, manifest_version,
              outcome, received_at)
    ON public.business_events TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute(
        "DROP TABLE public.business_events, public.integration_webhook_endpoints, "
        "public.integration_manifest_activations, public.integration_manifests"
    )
    op.execute("DROP FUNCTION public.integration_manifests_guard()")
