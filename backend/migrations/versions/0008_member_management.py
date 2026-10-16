"""project_memberships: role changes and removals for member management (PRO-61, PRO-275)

Migration ``0003_identity`` left ``fh_app`` with SELECT and INSERT only, "until member management
arrives". It arrives now: the runtime role may change a membership's ``role`` (never who or which
project it belongs to) and delete a membership. Both are audited by the application in the same
transaction (``member.role_changed`` / ``member.removed``), and the last owner of a project is
protected there, not here.

Revision ID: 0008_member_management
Revises: 0007_customers
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_member_management"
down_revision: str | None = "0007_customers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute("GRANT UPDATE (role) ON public.project_memberships TO fh_app")
    op.execute("GRANT DELETE ON public.project_memberships TO fh_app")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE fh_owner")
    op.execute("REVOKE DELETE ON public.project_memberships FROM fh_app")
    op.execute("REVOKE UPDATE (role) ON public.project_memberships FROM fh_app")
    op.execute("RESET ROLE")
