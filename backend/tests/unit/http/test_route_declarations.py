"""Every route declares its access: ``public()``, ``authenticated()`` or ``require(permission)``."""

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from financial_hub.main import create_app
from financial_hub.platform.http.auth import (
    authenticated,
    effective_routes,
    public,
    require,
    route_access,
    undeclared_routes,
    verify_route_declarations,
)
from financial_hub.platform.http.problems import register_exception_handlers
from financial_hub.platform.settings import Settings

EXPECTED = {
    ("GET", "/health/live"): ("public", None),
    ("GET", "/health/ready"): ("public", None),
    ("POST", "/v1/auth/login"): ("public", None),
    ("GET", "/v1/auth/demo-operators"): ("public", None),
    ("POST", "/v1/auth/demo-login"): ("public", None),
    ("POST", "/v1/auth/logout"): ("authenticated", None),
    ("GET", "/v1/auth/me"): ("authenticated", None),
    ("POST", "/v1/auth/password"): ("authenticated", None),
    ("GET", "/v1/projects"): ("authenticated", None),
    ("POST", "/v1/projects"): ("authenticated", None),
    ("GET", "/v1/projects/{project_id}"): ("permission", "projects:read"),
    ("PATCH", "/v1/projects/{project_id}"): ("permission", "projects:manage"),
    ("POST", "/v1/projects/{project_id}/api-keys"): ("permission", "api_keys:manage"),
    ("GET", "/v1/projects/{project_id}/api-keys"): ("permission", "api_keys:manage"),
    ("POST", "/v1/projects/{project_id}/api-keys/{key_id}/revoke"): (
        "permission",
        "api_keys:manage",
    ),
    ("GET", "/v1/projects/{project_id}/members"): ("permission", "members:read"),
    ("POST", "/v1/projects/{project_id}/members"): ("permission", "members:manage"),
    ("PATCH", "/v1/projects/{project_id}/members/{operator_id}"): ("permission", "members:manage"),
    ("DELETE", "/v1/projects/{project_id}/members/{operator_id}"): ("permission", "members:manage"),
    ("GET", "/v1/provider-catalog"): ("authenticated", None),
    ("POST", "/v1/projects/{project_id}/provider-accounts"): ("permission", "integrations:manage"),
    ("GET", "/v1/projects/{project_id}/provider-accounts"): ("permission", "integrations:read"),
    ("GET", "/v1/projects/{project_id}/provider-accounts/{account_id}"): (
        "permission",
        "integrations:read",
    ),
    ("PUT", "/v1/projects/{project_id}/provider-accounts/{account_id}/credentials"): (
        "permission",
        "integrations:manage",
    ),
    ("POST", "/v1/projects/{project_id}/provider-accounts/{account_id}/activate"): (
        "permission",
        "integrations:manage",
    ),
    ("POST", "/v1/projects/{project_id}/provider-accounts/{account_id}/deactivate"): (
        "permission",
        "integrations:manage",
    ),
    ("PUT", "/v1/projects/{project_id}/customers/{external_ref}"): (
        "permission",
        "customers:manage",
    ),
    ("GET", "/v1/projects/{project_id}/customers"): ("permission", "customers:read"),
    ("GET", "/v1/projects/{project_id}/customers/by-ref/{external_ref}"): (
        "permission",
        "customers:read",
    ),
    ("GET", "/v1/projects/{project_id}/customers/{customer_id}"): (
        "permission",
        "customers:read",
    ),
    ("POST", "/v1/projects/{project_id}/customers/{customer_id}/anonymize"): (
        "permission",
        "customers:manage",
    ),
    ("POST", "/v1/projects/{project_id}/metrics"): ("permission", "metering:manage"),
    ("GET", "/v1/projects/{project_id}/metrics"): ("permission", "metering:read"),
    ("GET", "/v1/projects/{project_id}/metrics/{code}"): ("permission", "metering:read"),
    ("POST", "/v1/projects/{project_id}/metrics/{code}/archive"): (
        "permission",
        "metering:manage",
    ),
    ("GET", "/v1/projects/{project_id}/usage/summary"): ("permission", "metering:read"),
    ("POST", "/v1/usage"): ("authenticated", None),
    ("POST", "/v1/usage/batch"): ("authenticated", None),
    ("POST", "/v1/projects/{project_id}/payments"): ("permission", "payments:write"),
    ("GET", "/v1/projects/{project_id}/payments"): ("permission", "payments:read"),
    ("GET", "/v1/projects/{project_id}/payments/{payment_id}"): ("permission", "payments:read"),
    ("POST", "/v1/projects/{project_id}/payments/{payment_id}/refunds"): (
        "permission",
        "payments:refund",
    ),
    # Inbound provider webhooks (W4.4): authenticated by the account's webhook token instead.
    ("POST", "/v1/webhooks/asaas/{provider_account_id}"): ("public", None),
    ("GET", "/v1/projects/{project_id}/stream"): ("permission", "events:read"),
    ("GET", "/v1/projects/{project_id}/events"): ("permission", "events:read"),
    ("GET", "/v1/projects/{project_id}/events/{event_id}"): ("permission", "events:read"),
    ("GET", "/v1/projects/{project_id}/events/{event_id}/causality"): (
        "permission",
        "events:read",
    ),
    ("GET", "/v1/projects/{project_id}/events/{event_id}/deliveries"): (
        "permission",
        "events:read",
    ),
    ("GET", "/v1/projects/{project_id}/correlations/{correlation_id}"): (
        "permission",
        "events:read",
    ),
    # Outbound webhooks (W6.3): operator-only permissions, no API key scope grants them.
    ("POST", "/v1/projects/{project_id}/webhook-endpoints"): ("permission", "webhooks:manage"),
    ("GET", "/v1/projects/{project_id}/webhook-endpoints"): ("permission", "webhooks:read"),
    ("GET", "/v1/projects/{project_id}/webhook-endpoints/{endpoint_id}"): (
        "permission",
        "webhooks:read",
    ),
    ("PATCH", "/v1/projects/{project_id}/webhook-endpoints/{endpoint_id}"): (
        "permission",
        "webhooks:manage",
    ),
    ("POST", "/v1/projects/{project_id}/webhook-endpoints/{endpoint_id}/disable"): (
        "permission",
        "webhooks:manage",
    ),
    ("POST", "/v1/projects/{project_id}/webhook-endpoints/{endpoint_id}/rotate-secret"): (
        "permission",
        "webhooks:manage",
    ),
    ("POST", "/v1/projects/{project_id}/webhook-endpoints/{endpoint_id}/test"): (
        "permission",
        "webhooks:manage",
    ),
    ("GET", "/v1/projects/{project_id}/webhook-endpoints/{endpoint_id}/attempts"): (
        "permission",
        "webhooks:read",
    ),
    ("POST", "/v1/projects/{project_id}/events/{event_id}/deliveries/{delivery_id}/retry"): (
        "permission",
        "webhooks:manage",
    ),
    ("GET", "/v1/projects/{project_id}/audit-records"): ("permission", "audit:read"),
    ("GET", "/v1/projects/{project_id}/ledger/accounts"): ("permission", "ledger:read"),
    ("GET", "/v1/projects/{project_id}/ledger/accounts/{account_id}"): (
        "permission",
        "ledger:read",
    ),
    ("GET", "/v1/projects/{project_id}/ledger/accounts/{account_id}/balance"): (
        "permission",
        "ledger:read",
    ),
    ("GET", "/v1/projects/{project_id}/ledger/entries"): ("permission", "ledger:read"),
    ("GET", "/v1/projects/{project_id}/ledger/entries/{entry_id}"): (
        "permission",
        "ledger:read",
    ),
    ("GET", "/v1/projects/{project_id}/ledger/trial-balance"): ("permission", "ledger:read"),
    ("POST", "/v1/projects/{project_id}/reconciliation/runs"): (
        "permission",
        "reconciliation:manage",
    ),
    ("GET", "/v1/projects/{project_id}/reconciliation/runs"): (
        "permission",
        "reconciliation:read",
    ),
    ("GET", "/v1/projects/{project_id}/reconciliation/runs/{run_id}"): (
        "permission",
        "reconciliation:read",
    ),
    ("GET", "/v1/projects/{project_id}/reconciliation/breaks"): (
        "permission",
        "reconciliation:read",
    ),
    ("GET", "/v1/projects/{project_id}/reconciliation/breaks/{break_id}"): (
        "permission",
        "reconciliation:read",
    ),
    ("POST", "/v1/projects/{project_id}/reconciliation/breaks/{break_id}/transition"): (
        "permission",
        "reconciliation:manage",
    ),
    ("POST", "/v1/projects/{project_id}/reconciliation/breaks/{break_id}/adjustments"): (
        "permission",
        "ledger:adjust",
    ),
    ("GET", "/v1/projects/{project_id}/reconciliation/adjustments"): (
        "permission",
        "reconciliation:read",
    ),
    ("GET", "/v1/projects/{project_id}/reconciliation/adjustments/{adjustment_id}"): (
        "permission",
        "reconciliation:read",
    ),
    ("POST", "/v1/projects/{project_id}/reconciliation/adjustments/{adjustment_id}/approve"): (
        "permission",
        "ledger:adjust",
    ),
    ("POST", "/v1/projects/{project_id}/reconciliation/adjustments/{adjustment_id}/reject"): (
        "permission",
        "ledger:adjust",
    ),
    ("POST", "/v1/projects/{project_id}/prices"): ("permission", "billing:manage"),
    ("GET", "/v1/projects/{project_id}/prices"): ("permission", "billing:read"),
    ("GET", "/v1/projects/{project_id}/prices/{code}"): ("permission", "billing:read"),
    ("POST", "/v1/projects/{project_id}/prices/{code}/versions"): (
        "permission",
        "billing:manage",
    ),
    ("POST", "/v1/projects/{project_id}/plans"): ("permission", "billing:manage"),
    ("GET", "/v1/projects/{project_id}/plans"): ("permission", "billing:read"),
    ("GET", "/v1/projects/{project_id}/plans/{code}"): ("permission", "billing:read"),
    ("POST", "/v1/projects/{project_id}/subscriptions"): ("permission", "billing:manage"),
    ("GET", "/v1/projects/{project_id}/subscriptions"): ("permission", "billing:read"),
    ("GET", "/v1/projects/{project_id}/subscriptions/{subscription_id}"): (
        "permission",
        "billing:read",
    ),
    ("POST", "/v1/projects/{project_id}/subscriptions/{subscription_id}/cancel"): (
        "permission",
        "billing:manage",
    ),
    ("GET", "/v1/projects/{project_id}/invoices"): ("permission", "billing:read"),
    ("GET", "/v1/projects/{project_id}/invoices/{invoice_id}"): ("permission", "billing:read"),
    ("POST", "/v1/projects/{project_id}/invoices/{invoice_id}/finalize"): (
        "permission",
        "billing:manage",
    ),
    ("POST", "/v1/projects/{project_id}/invoices/{invoice_id}/void"): (
        "permission",
        "billing:manage",
    ),
    # Integration manifests: drafting is configuration, approval/activation is integrations:approve
    # (owner, finance); business events come from API keys with events:write (checked inside).
    ("GET", "/v1/manifest-schema/v1"): ("authenticated", None),
    ("POST", "/v1/projects/{project_id}/manifests"): ("permission", "integrations:manage"),
    ("GET", "/v1/projects/{project_id}/manifests"): ("permission", "integrations:read"),
    ("GET", "/v1/projects/{project_id}/manifests/active"): ("permission", "integrations:read"),
    ("GET", "/v1/projects/{project_id}/manifests/activations"): (
        "permission",
        "integrations:read",
    ),
    ("GET", "/v1/projects/{project_id}/manifests/{version}"): ("permission", "integrations:read"),
    ("PUT", "/v1/projects/{project_id}/manifests/{version}"): (
        "permission",
        "integrations:manage",
    ),
    ("POST", "/v1/projects/{project_id}/manifests/{version}/validate"): (
        "permission",
        "integrations:manage",
    ),
    ("GET", "/v1/projects/{project_id}/manifests/{version}/diff"): (
        "permission",
        "integrations:read",
    ),
    ("POST", "/v1/projects/{project_id}/manifests/{version}/approve"): (
        "permission",
        "integrations:approve",
    ),
    ("POST", "/v1/projects/{project_id}/manifests/{version}/reject"): (
        "permission",
        "integrations:approve",
    ),
    ("POST", "/v1/projects/{project_id}/manifests/{version}/activate"): (
        "permission",
        "integrations:approve",
    ),
    ("POST", "/v1/projects/{project_id}/manifests/{version}/simulate"): (
        "permission",
        "integrations:read",
    ),
    ("GET", "/v1/projects/{project_id}/business-events"): ("permission", "integrations:read"),
    ("POST", "/v1/business-events"): ("authenticated", None),
}


def declared(app: FastAPI) -> dict[tuple[str, str], tuple[str, str | None]]:
    result: dict[tuple[str, str], tuple[str, str | None]] = {}
    for route in effective_routes(app):
        if isinstance(route.route, APIRoute):
            [declaration] = route_access(route)
            for method in route.methods:
                result[method, route.path] = (declaration.kind, declaration.permission)
    return result


def test_every_app_route_is_declared() -> None:
    app = create_app()
    assert undeclared_routes(app) == []
    assert declared(app) == EXPECTED


def test_demo_login_routes_are_public_and_registered_in_every_mode() -> None:
    disabled = create_app()
    enabled = create_app(
        Settings(database_url=SecretStr("postgresql+psycopg://x/y"), demo_login=True)
    )
    for app in (disabled, enabled):
        assert undeclared_routes(app) == []
        verify_route_declarations(app)
        assert declared(app) == EXPECTED
        assert {"/v1/auth/demo-operators", "/v1/auth/demo-login"} <= set(app.openapi()["paths"])
    assert EXPECTED["GET", "/v1/auth/demo-operators"] == ("public", None)
    assert EXPECTED["POST", "/v1/auth/demo-login"] == ("public", None)


def bare_app() -> FastAPI:
    return FastAPI()


def test_an_undeclared_route_fails_verification() -> None:
    app = bare_app()

    @app.get("/v1/leak")
    async def leak() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    assert undeclared_routes(app) == ["GET /v1/leak: 0 access declarations (need exactly 1)"]
    with pytest.raises(RuntimeError, match="GET /v1/leak"):
        verify_route_declarations(app)


def test_create_app_refuses_to_start_with_an_undeclared_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import financial_hub.main as main_module

    original = main_module.verify_route_declarations

    def with_extra_route(app: FastAPI) -> None:
        app.add_api_route("/v1/sneaky", lambda: {}, methods=["GET"])
        original(app)

    monkeypatch.setattr(main_module, "verify_route_declarations", with_extra_route)
    with pytest.raises(RuntimeError, match="/v1/sneaky"):
        create_app()


def test_double_declarations_are_rejected() -> None:
    app = bare_app()

    @app.get("/v1/both", dependencies=[public(), authenticated()])
    async def both() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    assert undeclared_routes(app) == ["GET /v1/both: 2 access declarations (need exactly 1)"]


def test_require_needs_a_project_path_parameter() -> None:
    app = bare_app()

    @app.get("/v1/things", dependencies=[require("things:read")])
    async def things() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    assert undeclared_routes(app) == [
        "GET /v1/things: require() needs a {project_id} path parameter"
    ]


def test_included_router_routes_are_checked_with_prefix() -> None:
    router = APIRouter()

    @router.get("/hidden")
    async def hidden() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    app = bare_app()
    app.include_router(router, prefix="/v1/nested")

    assert undeclared_routes(app) == [
        "GET /v1/nested/hidden: 0 access declarations (need exactly 1)"
    ]


def test_include_router_dependencies_count_as_declarations() -> None:
    router = APIRouter()

    @router.get("/fine")
    async def fine() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    @router.get("/twice", dependencies=[public()])
    async def twice() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    app = bare_app()
    app.include_router(router, prefix="/v1", dependencies=[authenticated()])

    assert undeclared_routes(app) == ["GET /v1/twice: 2 access declarations (need exactly 1)"]


async def test_include_router_declarations_are_enforced_at_request_time() -> None:
    router = APIRouter()

    @router.get("/guarded")
    async def guarded() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    app = bare_app()
    app.include_router(router, prefix="/v1", dependencies=[authenticated()])
    register_exception_handlers(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/guarded")
    assert response.status_code == 401


async def raw(_: Request) -> PlainTextResponse:
    return PlainTextResponse("x")


def test_non_api_routes_cannot_declare_access() -> None:
    app = bare_app()
    app.router.routes.append(Route("/raw", raw))

    assert undeclared_routes(app) == ["/raw: not an API route, cannot declare access"]


def test_framework_docs_routes_are_allowed() -> None:
    app = FastAPI(docs_url="/docs", redoc_url="/redoc")
    assert undeclared_routes(app) == []
