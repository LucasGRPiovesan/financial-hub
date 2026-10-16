"""Roles and permissions (docs/SECURITY.md, Authorization row).

A role is held per project (``project_memberships``). Permissions are ``<area>:<action>`` strings
routes declare with ``platform.http.auth.require``. Separation of duties: ``admin`` configures a
project but cannot move money; ``finance`` moves money but cannot change configuration or members;
``owner`` can do everything; ``viewer`` only reads. Money movement still needs four-eyes approval
on top of the permission (maker-checker, later milestones).
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    FINANCE = "finance"
    VIEWER = "viewer"


class Permission(StrEnum):
    PROJECTS_READ = "projects:read"
    PROJECTS_MANAGE = "projects:manage"
    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    API_KEYS_MANAGE = "api_keys:manage"
    CUSTOMERS_READ = "customers:read"
    CUSTOMERS_MANAGE = "customers:manage"
    LEDGER_READ = "ledger:read"
    LEDGER_ADJUST = "ledger:adjust"
    PAYMENTS_READ = "payments:read"
    PAYMENTS_WRITE = "payments:write"
    PAYMENTS_REFUND = "payments:refund"
    METERING_READ = "metering:read"
    METERING_MANAGE = "metering:manage"
    USAGE_WRITE = "usage:write"
    PAYOUTS_MANAGE = "payouts:manage"
    BILLING_READ = "billing:read"
    BILLING_MANAGE = "billing:manage"
    RECONCILIATION_READ = "reconciliation:read"
    RECONCILIATION_MANAGE = "reconciliation:manage"
    INTEGRATIONS_READ = "integrations:read"
    INTEGRATIONS_MANAGE = "integrations:manage"
    WEBHOOKS_READ = "webhooks:read"
    WEBHOOKS_MANAGE = "webhooks:manage"
    EVENTS_READ = "events:read"
    AUDIT_READ = "audit:read"


READ_PERMISSIONS: Final = frozenset(p for p in Permission if p.value.endswith(":read"))

MONEY_MOVEMENT: Final = frozenset(
    {
        Permission.LEDGER_ADJUST,
        Permission.PAYMENTS_WRITE,
        Permission.PAYMENTS_REFUND,
        Permission.PAYOUTS_MANAGE,
        Permission.BILLING_MANAGE,
        Permission.RECONCILIATION_MANAGE,
    }
)

CONFIGURATION: Final = frozenset(
    {
        Permission.PROJECTS_MANAGE,
        Permission.MEMBERS_MANAGE,
        Permission.API_KEYS_MANAGE,
        Permission.INTEGRATIONS_MANAGE,
        Permission.WEBHOOKS_MANAGE,
    }
)

ROLE_PERMISSIONS: Final[Mapping[Role, frozenset[Permission]]] = MappingProxyType(
    {
        Role.OWNER: frozenset(Permission),
        Role.ADMIN: READ_PERMISSIONS
        | CONFIGURATION
        | {Permission.CUSTOMERS_MANAGE, Permission.METERING_MANAGE},
        Role.FINANCE: READ_PERMISSIONS
        | MONEY_MOVEMENT
        | {Permission.CUSTOMERS_MANAGE, Permission.METERING_MANAGE},
        Role.VIEWER: READ_PERMISSIONS - {Permission.AUDIT_READ, Permission.WEBHOOKS_READ},
    }
)


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role]
