from collections.abc import AsyncIterator
from typing import Annotated

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from financial_hub.main import create_app
from financial_hub.platform.http.problems import (
    PROBLEM_MEDIA_TYPE,
    json_pointer,
    status_for,
    validation_field_errors,
)
from financial_hub.shared.errors import (
    Conflict,
    DomainError,
    FieldError,
    Forbidden,
    IdempotencyConflict,
    IdempotencyKeyReused,
    InsufficientFunds,
    NotFound,
    RateLimited,
    Unauthorized,
    ValidationFailed,
)

BASE = "https://financial-hub.dev/problems/"


class Line(BaseModel):
    quantity: int = Field(gt=0)


class Order(BaseModel):
    customer_id: str
    lines: list[Line]
    password: str | None = None


class CustomError(InsufficientFunds):
    slug = "wallet-empty"
    title = "Wallet is empty"


def build_app() -> FastAPI:
    app = create_app()

    @app.get("/domain/not-found")
    async def not_found() -> None:  # pyright: ignore[reportUnusedFunction]
        raise NotFound("Invoice inv_123 does not exist.")

    @app.get("/domain/invalid")
    async def invalid() -> None:  # pyright: ignore[reportUnusedFunction]
        raise ValidationFailed(
            errors=[FieldError(pointer="/lines/0/quantity", detail="too big", code="max")]
        )

    @app.get("/domain/rate-limited")
    async def rate_limited() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RateLimited(retry_after_seconds=30)

    @app.get("/domain/custom")
    async def custom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise CustomError

    @app.post("/orders")
    async def create_order(order: Order) -> Order:  # pyright: ignore[reportUnusedFunction]
        return order

    @app.get("/items")
    async def list_items(  # pyright: ignore[reportUnusedFunction]
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, int]:
        return {"limit": limit}

    @app.get("/http/teapot")
    async def teapot() -> None:  # pyright: ignore[reportUnusedFunction]
        raise HTTPException(status_code=418, detail="I'm a teapot")

    @app.get("/http/conflict")
    async def http_conflict() -> None:  # pyright: ignore[reportUnusedFunction]
        raise HTTPException(status_code=409, detail="Already exists", headers={"X-Hint": "1"})

    @app.get("/http/unknown-status")
    async def unknown_status() -> None:  # pyright: ignore[reportUnusedFunction]
        raise HTTPException(status_code=599, detail="internal secret detail")

    @app.get("/boom")
    async def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("database password is hunter2")

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def assert_problem(response: httpx.Response, status: int, slug: str) -> dict[str, object]:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["type"] == f"{BASE}{slug}"
    assert body["status"] == status
    assert body["title"]
    assert body["correlation_id"] == response.headers["x-correlation-id"]
    assert body["instance"] == response.request.url.path
    return body


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (DomainError(), 400),
        (ValidationFailed(), 422),
        (Unauthorized(), 401),
        (Forbidden(), 403),
        (NotFound(), 404),
        (Conflict(), 409),
        (IdempotencyConflict(), 409),
        (IdempotencyKeyReused(), 422),
        (InsufficientFunds(), 422),
        (RateLimited(), 429),
        (CustomError(), 422),
    ],
)
def test_status_mapping(error: DomainError, status: int) -> None:
    assert status_for(error) == status


def test_domain_error_defaults() -> None:
    error = NotFound()

    assert error.detail is None
    assert error.errors == ()
    assert str(error) == NotFound.title


def test_json_pointer_escapes_rfc6901() -> None:
    assert json_pointer([]) == ""
    assert json_pointer(["lines", 0, "quantity"]) == "/lines/0/quantity"
    assert json_pointer(["a/b", "m~n"]) == "/a~1b/m~0n"


def test_validation_field_errors_never_expose_input() -> None:
    errors = validation_field_errors(
        [
            {"loc": ("body", "password"), "msg": "too short", "type": "x", "input": "hunter2"},
            {"loc": ("custom",), "msg": "bad", "type": "y"},
            {},
        ]
    )

    assert [e.model_dump() for e in errors] == [
        {"pointer": "/password", "detail": "too short", "code": "x", "location": "body"},
        {"pointer": "/custom", "detail": "bad", "code": "y", "location": None},
        {"pointer": "", "detail": "invalid value", "code": None, "location": None},
    ]


async def test_domain_not_found(client: httpx.AsyncClient) -> None:
    body = assert_problem(await client.get("/domain/not-found"), 404, "not-found")

    assert body["detail"] == "Invoice inv_123 does not exist."
    assert "errors" not in body


async def test_domain_validation_errors_carry_pointers(client: httpx.AsyncClient) -> None:
    body = assert_problem(await client.get("/domain/invalid"), 422, "validation-failed")

    assert body["errors"] == [{"pointer": "/lines/0/quantity", "detail": "too big", "code": "max"}]
    assert "detail" not in body


async def test_rate_limited_sets_retry_after(client: httpx.AsyncClient) -> None:
    response = await client.get("/domain/rate-limited")

    assert_problem(response, 429, "rate-limited")
    assert response.headers["retry-after"] == "30"


async def test_subclass_uses_own_slug_and_parent_status(client: httpx.AsyncClient) -> None:
    body = assert_problem(await client.get("/domain/custom"), 422, "wallet-empty")

    assert body["title"] == "Wallet is empty"


async def test_body_validation_maps_to_json_pointers(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/orders", json={"lines": [{"quantity": 1}, {"quantity": 0}], "password": 123456}
    )

    body = assert_problem(response, 422, "validation-failed")
    errors = body["errors"]
    assert isinstance(errors, list)
    pointers = {(e["location"], e["pointer"]) for e in errors}  # pyright: ignore[reportUnknownVariableType]
    assert pointers == {
        ("body", "/customer_id"),
        ("body", "/lines/1/quantity"),
        ("body", "/password"),
    }
    assert "123456" not in response.text


async def test_query_validation_maps_to_json_pointers(client: httpx.AsyncClient) -> None:
    body = assert_problem(await client.get("/items?limit=500"), 422, "validation-failed")

    assert body["errors"] == [
        {
            "pointer": "/limit",
            "detail": "Input should be less than or equal to 200",
            "code": "less_than_equal",
            "location": "query",
        }
    ]


async def test_malformed_json_body(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/orders", content=b"{not json", headers={"content-type": "application/json"}
    )

    assert_problem(response, 422, "validation-failed")


async def test_unknown_route_is_problem_404(client: httpx.AsyncClient) -> None:
    body = assert_problem(await client.get("/nope"), 404, "not-found")

    assert body["title"] == "Not Found"
    assert "detail" not in body


async def test_wrong_method_is_problem_405(client: httpx.AsyncClient) -> None:
    response = await client.delete("/orders")

    assert_problem(response, 405, "method-not-allowed")
    assert "POST" in response.headers["allow"]


async def test_http_exception_without_slug_is_about_blank(client: httpx.AsyncClient) -> None:
    response = await client.get("/http/teapot")

    assert response.status_code == 418
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "I'm a Teapot"
    assert body["detail"] == "I'm a teapot"


async def test_http_exception_keeps_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/http/conflict")

    body = assert_problem(response, 409, "conflict")
    assert body["detail"] == "Already exists"
    assert response.headers["x-hint"] == "1"


async def test_http_5xx_detail_is_hidden(client: httpx.AsyncClient) -> None:
    response = await client.get("/http/unknown-status")

    assert response.status_code == 599
    body = response.json()
    assert body == {
        "type": "about:blank",
        "title": "Error",
        "status": 599,
        "instance": "/http/unknown-status",
        "correlation_id": response.headers["x-correlation-id"],
    }


async def test_unhandled_error_is_generic_500(client: httpx.AsyncClient) -> None:
    response = await client.get("/boom", headers={"X-Correlation-Id": "boom-1"})

    body = assert_problem(response, 500, "internal-error")
    assert body["correlation_id"] == "boom-1"
    assert "hunter2" not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text
