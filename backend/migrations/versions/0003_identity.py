"""identity: projects (core), operators, sessions, memberships, login throttle (task 1.1)

Objects are created as ``fh_owner`` (``SET ROLE``), like the previous revisions. The runtime role
``fh_app`` gets only what login, sessions and project creation need, and no DELETE/TRUNCATE
anywhere (expired sessions and stale throttle rows are pruned by an owner job, *Planned*):

- ``projects``: SELECT, INSERT, UPDATE(name, status, updated_at). API keys and provider accounts
  are task 1.2.
- ``operators``: SELECT, INSERT, UPDATE of the credential and lockout columns only; ``email``,
  ``status`` and ``id`` are immutable for the runtime.
- ``sessions``: SELECT, INSERT, UPDATE(last_seen_at, idle_expires_at, revoked_at). The primary key
  is the SHA-256 of the opaque cookie token (the raw token is never stored); a session can be
  revoked or slid forward but never re-pointed to another operator or given a new CSRF token.
- ``project_memberships``: SELECT, INSERT (role changes and removals arrive with member
  management).
- ``login_throttle``: SELECT, INSERT, UPDATE of the counter columns. Keys are SHA-256 digests of
  the normalized email / client IP, never the plain values.

Revision ID: 0003_identity
Revises: 0002_events
Create Date: 2026-10-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_identity"
down_revision: str | None = "0002_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE public.projects (
    id uuid NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_projects PRIMARY KEY (id),
    CONSTRAINT uq_projects_slug UNIQUE (slug),
    CONSTRAINT ck_projects_slug CHECK (slug ~ '^[a-z][a-z0-9-]{1,48}[a-z0-9]$'),
    CONSTRAINT ck_projects_name CHECK (length(name) BETWEEN 1 AND 100 AND name = btrim(name)),
    CONSTRAINT ck_projects_status CHECK (status IN ('active', 'archived'))
);
CREATE INDEX ix_projects_created_at_id ON public.projects (created_at, id);

CREATE TABLE public.operators (
    id uuid NOT NULL,
    email text NOT NULL,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    failed_login_count integer NOT NULL DEFAULT 0,
    locked_until timestamptz,
    created_at timestamptz NOT NULL,
    password_changed_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_operators PRIMARY KEY (id),
    -- Case-insensitive uniqueness: only the normalized (trimmed, lower-case) form is stored.
    CONSTRAINT uq_operators_email UNIQUE (email),
    CONSTRAINT ck_operators_email CHECK (
        email = lower(btrim(email)) AND length(email) BETWEEN 3 AND 254 AND email LIKE '%_@_%'
    ),
    CONSTRAINT ck_operators_display_name
        CHECK (length(display_name) BETWEEN 1 AND 100 AND display_name = btrim(display_name)),
    CONSTRAINT ck_operators_password_hash CHECK (password_hash LIKE '$argon2id$%'),
    CONSTRAINT ck_operators_status CHECK (status IN ('active', 'disabled')),
    CONSTRAINT ck_operators_failed_login_count CHECK (failed_login_count >= 0)
);

CREATE TABLE public.sessions (
    id bytea NOT NULL,
    operator_id uuid NOT NULL,
    csrf_token_hash bytea NOT NULL,
    created_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    idle_expires_at timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    user_agent text,
    ip inet,
    CONSTRAINT pk_sessions PRIMARY KEY (id),
    CONSTRAINT fk_sessions_operator_id_operators
        FOREIGN KEY (operator_id) REFERENCES public.operators (id),
    CONSTRAINT ck_sessions_id CHECK (length(id) = 32),
    CONSTRAINT ck_sessions_csrf_token_hash CHECK (length(csrf_token_hash) = 32),
    CONSTRAINT ck_sessions_expiry CHECK (
        created_at <= last_seen_at
        AND idle_expires_at <= absolute_expires_at
        AND created_at < absolute_expires_at
    ),
    CONSTRAINT ck_sessions_user_agent CHECK (length(user_agent) <= 512)
);
CREATE INDEX ix_sessions_operator_id ON public.sessions (operator_id) WHERE revoked_at IS NULL;

CREATE TABLE public.project_memberships (
    operator_id uuid NOT NULL,
    project_id uuid NOT NULL,
    role text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_project_memberships PRIMARY KEY (operator_id, project_id),
    CONSTRAINT fk_project_memberships_operator_id_operators
        FOREIGN KEY (operator_id) REFERENCES public.operators (id),
    CONSTRAINT fk_project_memberships_project_id_projects
        FOREIGN KEY (project_id) REFERENCES public.projects (id),
    CONSTRAINT ck_project_memberships_role CHECK (role IN ('owner', 'admin', 'finance', 'viewer'))
);
CREATE INDEX ix_project_memberships_project_id ON public.project_memberships (project_id);

CREATE TABLE public.login_throttle (
    scope text NOT NULL,
    key_hash bytea NOT NULL,
    window_started_at timestamptz NOT NULL,
    attempts integer NOT NULL,
    blocked_until timestamptz,
    updated_at timestamptz NOT NULL,
    CONSTRAINT pk_login_throttle PRIMARY KEY (scope, key_hash),
    CONSTRAINT ck_login_throttle_scope CHECK (scope IN ('email', 'ip')),
    CONSTRAINT ck_login_throttle_key_hash CHECK (length(key_hash) = 32),
    CONSTRAINT ck_login_throttle_attempts CHECK (attempts >= 0)
);
"""

GRANTS = """
REVOKE ALL ON public.projects, public.operators, public.sessions, public.project_memberships,
    public.login_throttle FROM PUBLIC;

GRANT SELECT ON public.projects, public.operators, public.sessions, public.project_memberships,
    public.login_throttle TO fh_app;
GRANT INSERT (id, slug, name, status) ON public.projects TO fh_app;
GRANT UPDATE (name, status, updated_at) ON public.projects TO fh_app;
GRANT INSERT (id, email, display_name, password_hash, status, created_at, password_changed_at)
    ON public.operators TO fh_app;
GRANT UPDATE (password_hash, password_changed_at, failed_login_count, locked_until, updated_at)
    ON public.operators TO fh_app;
GRANT INSERT (id, operator_id, csrf_token_hash, created_at, last_seen_at, idle_expires_at,
              absolute_expires_at, user_agent, ip)
    ON public.sessions TO fh_app;
GRANT UPDATE (last_seen_at, idle_expires_at, revoked_at) ON public.sessions TO fh_app;
GRANT INSERT (operator_id, project_id, role) ON public.project_memberships TO fh_app;
GRANT INSERT (scope, key_hash, window_started_at, attempts, blocked_until, updated_at)
    ON public.login_throttle TO fh_app;
GRANT UPDATE (window_started_at, attempts, blocked_until, updated_at)
    ON public.login_throttle TO fh_app;
"""


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute(TABLES)
    op.execute(GRANTS)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute(
        "DROP TABLE public.login_throttle, public.project_memberships, public.sessions, "
        "public.operators, public.projects"
    )
