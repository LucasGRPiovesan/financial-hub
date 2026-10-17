import pytest

from financial_hub.modules.identity.domain import (
    CONFIGURATION,
    MONEY_MOVEMENT,
    READ_PERMISSIONS,
    Permission,
    Role,
    permissions_for,
)


def test_owner_has_every_permission() -> None:
    assert permissions_for(Role.OWNER) == frozenset(Permission)


def test_every_role_reads_the_project() -> None:
    for role in Role:
        assert Permission.PROJECTS_READ in permissions_for(role)


@pytest.mark.parametrize("role", [Role.ADMIN, Role.FINANCE, Role.VIEWER])
def test_no_role_but_owner_both_configures_and_moves_money(role: Role) -> None:
    granted = permissions_for(role)
    assert not (granted & CONFIGURATION and granted & MONEY_MOVEMENT)


def test_separation_of_duties() -> None:
    admin, finance = permissions_for(Role.ADMIN), permissions_for(Role.FINANCE)
    assert admin >= CONFIGURATION
    assert not admin & MONEY_MOVEMENT
    assert finance >= MONEY_MOVEMENT
    assert not finance & CONFIGURATION


def test_viewer_only_reads_and_not_the_audit_log() -> None:
    viewer = permissions_for(Role.VIEWER)
    assert viewer <= READ_PERMISSIONS
    assert Permission.AUDIT_READ not in viewer
    assert Permission.PROJECTS_MANAGE not in viewer


def test_permission_strings_are_area_colon_action() -> None:
    for permission in Permission:
        area, _, action = permission.value.partition(":")
        assert area and action in {"read", "manage", "adjust", "refund", "write", "approve"}


def test_members_are_readable_by_every_role_but_managed_by_owner_and_admin_only() -> None:
    for role in Role:
        assert Permission.MEMBERS_READ in permissions_for(role)
    assert Permission.MEMBERS_MANAGE in permissions_for(Role.OWNER)
    assert Permission.MEMBERS_MANAGE in permissions_for(Role.ADMIN)
    assert Permission.MEMBERS_MANAGE not in permissions_for(Role.FINANCE)
    assert Permission.MEMBERS_MANAGE not in permissions_for(Role.VIEWER)
