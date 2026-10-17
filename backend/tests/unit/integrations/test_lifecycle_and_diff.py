"""Version lifecycle and the review diff: which changes are money-sensitive (four-eyes)."""

import itertools

import pytest

from financial_hub.modules.integrations.domain import (
    TRANSITIONS,
    IllegalTransition,
    ManifestStatus,
    check_transition,
    diff_manifests,
    four_eyes_required,
    is_immutable,
    parse_manifest,
)
from tests.unit.integrations.samples import SAMPLE, sample

S = ManifestStatus
ALLOWED = {
    (S.DRAFT, S.VALIDATED),
    (S.DRAFT, S.REJECTED),
    (S.VALIDATED, S.APPROVED),
    (S.VALIDATED, S.REJECTED),
    (S.APPROVED, S.ACTIVE),
    (S.APPROVED, S.REJECTED),
    (S.ACTIVE, S.SUPERSEDED),
    (S.SUPERSEDED, S.ACTIVE),  # rollback
}


@pytest.mark.parametrize(("current", "target"), list(itertools.product(S, S)))
def test_transitions(current: ManifestStatus, target: ManifestStatus) -> None:
    if (current, target) in ALLOWED:
        check_transition(current, target)
    else:
        with pytest.raises(IllegalTransition):
            check_transition(current, target)


def test_rejected_is_final_and_only_drafts_are_mutable() -> None:
    assert TRANSITIONS[S.REJECTED] == frozenset()
    assert not is_immutable(S.DRAFT)
    assert all(is_immutable(status) for status in S if status is not S.DRAFT)


def test_first_version_diff_marks_money_sections() -> None:
    diff = diff_manifests(None, parse_manifest(SAMPLE))
    assert diff.base_version is None
    assert diff.money_sensitive
    assert set(diff.sensitive_sections) == {"prices", "splits"}


def test_description_only_change_is_not_money_sensitive() -> None:
    before = parse_manifest(SAMPLE)
    after = parse_manifest(sample(description="New wording"))
    diff = diff_manifests(before, after, base_version=1)
    assert [change.section for change in diff.changes] == ["description"]
    assert not diff.money_sensitive
    assert not four_eyes_required(money_sensitive=diff.money_sensitive, always=False)
    assert four_eyes_required(money_sensitive=diff.money_sensitive, always=True)


@pytest.mark.parametrize(
    ("changes", "section"),
    [
        ({"splits": [{"payee": "clinic-1", "basis_points": 6500}]}, "splits"),
        ({"prices": []}, "prices"),
        ({"wallet": {"mode": "wallet_first"}}, "wallet"),
    ],
)
def test_split_price_and_wallet_changes_need_four_eyes(
    changes: dict[str, object], section: str
) -> None:
    diff = diff_manifests(parse_manifest(SAMPLE), parse_manifest(sample(**changes)), base_version=1)
    assert diff.sensitive_sections == (section,)
    assert four_eyes_required(money_sensitive=diff.money_sensitive, always=False)
    [change] = [item for item in diff.changes if item.section == section]
    assert change.as_json()["money_sensitive"] is True


def test_identical_versions_have_no_changes() -> None:
    manifest = parse_manifest(SAMPLE)
    assert diff_manifests(manifest, manifest, base_version=3).as_json() == {
        "base_version": 3,
        "money_sensitive": False,
        "sensitive_sections": [],
        "changes": [],
    }
