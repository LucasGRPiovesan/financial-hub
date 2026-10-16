"""Request facts of an audit record and the JSON normalisation of its diff (no database)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from starlette.requests import Request

from financial_hub.modules.audit.application.context import (
    AuditContext,
    _storable_ip,  # pyright: ignore[reportPrivateUsage]
    request_audit_context,
)
from financial_hub.modules.audit.application.recorder import (
    _normalise,  # pyright: ignore[reportPrivateUsage]
)
from financial_hub.modules.audit.domain import Actor, ActorType


class Color(StrEnum):
    RED = "red"


def make_request(
    *, client: tuple[str, int] | None, headers: dict[str, str], state: dict[str, str]
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": client,
        "state": state,
    }
    return Request(scope)


def test_storable_ip_keeps_valid_addresses_only() -> None:
    assert _storable_ip("10.0.0.1") == "10.0.0.1"
    assert _storable_ip("2001:DB8:0:0:0:0:0:1") == "2001:db8::1"
    assert _storable_ip("testclient") is None
    assert _storable_ip(None) is None


def test_the_request_context_carries_ids_ip_and_a_truncated_user_agent() -> None:
    request = make_request(
        client=("10.1.2.3", 5000),
        headers={"user-agent": "u" * 400},
        state={"correlation_id": "cid-1", "request_id": "rid-1"},
    )
    context = request_audit_context(request)
    assert context.actor == Actor.system()
    assert (context.correlation_id, context.request_id, context.ip) == (
        "cid-1",
        "rid-1",
        "10.1.2.3",
    )
    assert context.user_agent == "u" * 256


def test_the_request_context_tolerates_a_bare_request() -> None:
    context = request_audit_context(make_request(client=None, headers={}, state={}))
    assert context == AuditContext(Actor.system())


def test_acting_swaps_only_the_actor() -> None:
    base = AuditContext(Actor.system(), correlation_id="c", ip="10.0.0.1")
    acting = base.acting(Actor(ActorType.OPERATOR, "opr_1"))
    assert acting.actor == Actor(ActorType.OPERATOR, "opr_1")
    assert (acting.correlation_id, acting.ip) == ("c", "10.0.0.1")


def test_diffs_are_redacted_and_made_json_safe() -> None:
    diff = {
        "when": datetime(2026, 10, 13, tzinfo=UTC),
        "day": date(2026, 10, 13),
        "color": Color.RED,
        "id": UUID(int=1),
        "amount": Decimal("1.50"),
        "api_key": "never",
    }
    assert cast("dict[str, Any]", _normalise(diff)) == {
        "when": "2026-10-13T00:00:00+00:00",
        "day": "2026-10-13",
        "color": "red",
        "id": "00000000-0000-0000-0000-000000000001",
        "amount": "1.50",
        "api_key": "[redacted]",
    }
