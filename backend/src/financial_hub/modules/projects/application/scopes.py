"""API key scopes -> permissions (docs/SECURITY.md).

A key is a machine credential for one project's integration traffic. It can never hold the
administrative permissions (``api_keys:manage``, ``members:manage``, ``integrations:manage``,
``projects:manage``, money movement beyond what its own scopes name...), so a leaked key cannot
mint other keys, read or replace provider credentials, or change who has access.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final

from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.projects.domain import ApiKeyScope

# Write scopes without a ``Permission`` of their own use the scope string as the permission.
SCOPE_PERMISSIONS: Final[Mapping[ApiKeyScope, frozenset[str]]] = MappingProxyType(
    {
        ApiKeyScope.PROJECTS_READ: frozenset({Permission.PROJECTS_READ.value}),
        ApiKeyScope.CUSTOMERS_READ: frozenset({Permission.CUSTOMERS_READ.value}),
        ApiKeyScope.CUSTOMERS_WRITE: frozenset(
            {Permission.CUSTOMERS_READ.value, Permission.CUSTOMERS_MANAGE.value}
        ),
        ApiKeyScope.LEDGER_READ: frozenset({Permission.LEDGER_READ.value}),
        ApiKeyScope.PAYMENTS_READ: frozenset({Permission.PAYMENTS_READ.value}),
        ApiKeyScope.PAYMENTS_WRITE: frozenset(
            {Permission.PAYMENTS_READ.value, ApiKeyScope.PAYMENTS_WRITE.value}
        ),
        ApiKeyScope.BILLING_READ: frozenset({Permission.BILLING_READ.value}),
        ApiKeyScope.BILLING_WRITE: frozenset(
            {Permission.BILLING_READ.value, Permission.BILLING_MANAGE.value}
        ),
        ApiKeyScope.EVENTS_READ: frozenset({Permission.EVENTS_READ.value}),
    }
)

# Never grantable to a key, whatever scopes it carries.
ADMIN_ONLY: Final = frozenset(
    {
        Permission.API_KEYS_MANAGE.value,
        Permission.MEMBERS_READ.value,
        Permission.MEMBERS_MANAGE.value,
        Permission.INTEGRATIONS_READ.value,
        Permission.INTEGRATIONS_MANAGE.value,
        Permission.PROJECTS_MANAGE.value,
        Permission.WEBHOOKS_MANAGE.value,
        Permission.AUDIT_READ.value,
    }
)


def permissions_for_scopes(scopes: Iterable[ApiKeyScope]) -> frozenset[str]:
    granted: set[str] = set()
    for scope in scopes:
        granted |= SCOPE_PERMISSIONS[scope]
    return frozenset(granted - ADMIN_ONLY)
