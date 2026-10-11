"""POST /v1/auth/login: contract shape, cookie flags, no enumeration, throttling, rehash."""

from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.infrastructure.passwords import Argon2PasswordHasher
from tests.integration.identity.support import (
    PASSWORD,
    Api,
    SpyHasher,
    login,
    random_ip,
    running_api,
    unique_email,
)

PROBLEM_KEYS = ("type", "title", "status", "detail")


def problem(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    return {key: body.get(key) for key in PROBLEM_KEYS}


async def test_login_returns_the_contract_and_sets_the_session_cookie(api: Api) -> None:
    operator = await api.operator(display_name="Ana Operator")
    async with api.client() as client:
        response = await login(client, operator)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"operator", "csrf_token"}
    assert body["operator"] == {
        "id": operator.id,
        "email": operator.email,
        "display_name": "Ana Operator",
    }
    assert operator.id.startswith("opr_")
    assert len(body["csrf_token"]) == 43
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("fh_session=")
    flags = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"httponly", "path=/", "samesite=lax", "max-age=43200"} <= flags
    assert "secure" not in flags  # FH_APP_ENV=dev serves plain http
    assert response.headers["cache-control"] == "no-store"


async def test_cookie_is_secure_outside_dev() -> None:
    async with running_api(app_env="prod") as api:
        operator = await api.operator()
        async with api.client() as client:
            response = await login(client, operator)

    assert response.status_code == 200
    assert "secure" in {p.strip().lower() for p in response.headers["set-cookie"].split(";")}


async def test_email_is_case_and_space_insensitive(api: Api) -> None:
    operator = await api.operator()
    async with api.client() as client:
        response = await client.post(
            "/v1/auth/login",
            json={"email": f"  {operator.email.upper()} ", "password": PASSWORD},
        )

    assert response.status_code == 200
    assert response.json()["operator"]["email"] == operator.email


async def test_wrong_password_and_unknown_email_look_the_same(api: Api) -> None:
    operator = await api.operator()
    async with api.client() as client:
        wrong = await login(client, operator, password="not the password")
        api.hasher.verified.clear()
        unknown = await client.post(
            "/v1/auth/login", json={"email": unknown_email(), "password": PASSWORD}
        )

    assert wrong.status_code == unknown.status_code == 401
    assert problem(wrong) == problem(unknown)
    assert problem(wrong)["detail"] == "Invalid email or password."
    assert "set-cookie" not in wrong.headers
    assert "set-cookie" not in unknown.headers
    # Unknown emails still pay for one Argon2 verification, against the dummy hash.
    assert api.hasher.verified == [api.hasher.dummy_hash]


def unknown_email() -> str:
    return unique_email("nobody")


async def test_disabled_operator_gets_the_generic_error(
    api: Api, owner_engine: AsyncEngine
) -> None:
    operator = await api.operator()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE operators SET status = 'disabled' WHERE id = :id"), {"id": operator.uuid}
        )
    async with api.client() as client:
        response = await login(client, operator)

    assert response.status_code == 401
    assert problem(response)["detail"] == "Invalid email or password."


async def test_failures_lock_the_email_with_429_and_retry_after(api: Api) -> None:
    operator = await api.operator()
    async with api.client() as client:
        for _ in range(5):  # FH_LOGIN_MAX_FAILURES
            assert (await login(client, operator, password="wrong password!")).status_code == 401
        api.hasher.verified.clear()
        blocked = await login(client, operator)  # even the right password
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == str(15 * 60)
        assert api.hasher.verified == []  # rejected before any password check

        api.clock.advance(timedelta(minutes=10))
        later = await login(client, operator)
        assert later.status_code == 429
        assert later.headers["retry-after"] == str(5 * 60)

        api.clock.advance(timedelta(minutes=5))
        assert (await login(client, operator)).status_code == 200
        # Success resets the counter: four new failures do not lock again.
        for _ in range(4):
            assert (await login(client, operator, password="wrong password!")).status_code == 401
        assert (await login(client, operator)).status_code == 200


async def test_unknown_emails_are_throttled_exactly_like_known_ones(api: Api) -> None:
    email = unknown_email()
    async with api.client() as client:
        statuses = [
            (await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD}))
            for _ in range(6)
        ]

    assert [r.status_code for r in statuses] == [401] * 5 + [429]
    assert statuses[-1].headers["retry-after"] == str(15 * 60)


async def test_failures_spread_over_the_window_do_not_lock(api: Api) -> None:
    operator = await api.operator()
    async with api.client() as client:
        for _ in range(4):
            assert (await login(client, operator, password="wrong password!")).status_code == 401
        api.clock.advance(timedelta(minutes=16))  # window over: counting restarts
        for _ in range(4):
            assert (await login(client, operator, password="wrong password!")).status_code == 401
        assert (await login(client, operator)).status_code == 200


async def test_one_ip_spraying_many_emails_is_throttled() -> None:
    async with running_api(login_ip_max_attempts=3) as api:
        operator = await api.operator()
        async with api.client() as client:
            for _ in range(3):
                response = await client.post(
                    "/v1/auth/login", json={"email": unknown_email(), "password": PASSWORD}
                )
                assert response.status_code == 401
            blocked = await login(client, operator)
        async with api.client(ip=random_ip()) as other:
            elsewhere = await login(other, operator)

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) == 15 * 60
    assert elsewhere.status_code == 200  # the block is per address


async def test_login_without_client_address_still_works() -> None:
    async with running_api() as api:
        operator = await api.operator()
        transport = httpx.ASGITransport(app=api.app, client=("not-an-ip", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await login(client, operator)

    assert response.status_code == 200


async def test_form_posts_cannot_log_in(api: Api) -> None:
    """Login CSRF: a cross-site HTML form can only send form/text bodies, never JSON."""
    operator = await api.operator()
    async with api.client() as client:
        form = await client.post(
            "/v1/auth/login", data={"email": operator.email, "password": PASSWORD}
        )
        plain = await client.post(
            "/v1/auth/login",
            content=f'{{"email": "{operator.email}", "password": "{PASSWORD}"}}',
            headers={"content-type": "text/plain"},
        )

    assert form.status_code == plain.status_code == 422
    assert "set-cookie" not in form.headers
    assert "set-cookie" not in plain.headers


async def test_validation_errors_never_echo_the_password(api: Api) -> None:
    secret = "s3cr3t-" + "x" * 2000
    async with api.client() as client:
        response = await client.post(
            "/v1/auth/login", json={"email": unknown_email(), "password": secret}
        )

    assert response.status_code == 422
    assert "s3cr3t" not in response.text


async def test_outdated_hashes_are_upgraded_on_login(app_engine: AsyncEngine) -> None:
    weak = SpyHasher()  # FAST_ARGON2 parameters
    async with running_api(hasher=SpyHasher(Argon2PasswordHasher())) as api:
        api.hasher = weak
        operator = await api.operator()
        async with app_engine.connect() as conn:
            before = await stored_hash(conn, operator.email)
        async with api.client() as client:
            assert (await login(client, operator)).status_code == 200
            async with app_engine.connect() as conn:
                after = await stored_hash(conn, operator.email)
            # The upgraded hash still verifies.
            assert (await login(client, operator)).status_code == 200

    assert before.startswith("$argon2id$v=19$m=1024,t=1,p=1$")
    assert after.startswith("$argon2id$v=19$m=19456,t=2,p=1$")


async def stored_hash(conn: Any, email: str) -> str:
    result = await conn.execute(
        text("SELECT password_hash FROM operators WHERE email = :email"), {"email": email}
    )
    return result.scalar_one()
