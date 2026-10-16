"""Rate limits over HTTP: per principal, per client IP for rejected credentials and logins."""

from contextlib import aclosing
from datetime import timedelta

import httpx

from tests.integration.identity.support import logged_in, login, random_ip, running_api
from tests.integration.projects.helpers import bearer, create_key, create_project, problem_type

RATE_LIMITED = "https://financial-hub.dev/problems/rate-limited"
# Practically no refill during a test: the capacity is all a bucket gets.
SLOW = "0.0001"
UNKNOWN_KEY = f"fh_test_{'z' * 8}_{'A' * 43}"


def assert_limited(response: httpx.Response, limit: int) -> None:
    assert response.status_code == 429, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert problem_type(response) == RATE_LIMITED
    assert response.json()["status"] == 429
    retry_after = response.headers["Retry-After"]
    assert retry_after.isdigit() and int(retry_after) >= 1
    assert response.headers["RateLimit-Limit"] == str(limit)
    assert response.headers["RateLimit-Remaining"] == "0"


async def test_an_operator_session_is_limited() -> None:
    async with running_api(
        rate_limit_operator_capacity=3, rate_limit_operator_refill_per_second=SLOW
    ) as api:
        client, _ = await logged_in(api, await api.operator())
        async with aclosing(client):
            responses = [await client.get("/v1/auth/me") for _ in range(4)]

    assert [r.status_code for r in responses[:3]] == [200, 200, 200]
    assert [r.headers["RateLimit-Remaining"] for r in responses[:3]] == ["2", "1", "0"]
    assert all(r.headers["RateLimit-Limit"] == "3" for r in responses[:3])
    assert "Retry-After" not in responses[0].headers
    assert_limited(responses[3], limit=3)


async def test_an_api_key_is_limited_on_its_own_bucket() -> None:
    async with running_api(
        rate_limit_api_key_capacity=2, rate_limit_api_key_refill_per_second=SLOW
    ) as api:
        owner = await api.operator()
        project = await create_project(api, owner)
        client, csrf_token = await logged_in(api, owner)
        async with aclosing(client):
            first = (await create_key(client, csrf_token, project["id"]))["key"]
            second = (await create_key(client, csrf_token, project["id"]))["key"]
        path = f"/v1/projects/{project['id']}"
        async with api.client() as caller:
            spent = [(await caller.get(path, headers=bearer(first))).status_code for _ in range(3)]
            fresh = await caller.get(path, headers=bearer(second))

    assert spent[:2] == [200, 200] and spent[2] == 429
    assert fresh.status_code == 200


async def test_invalid_api_key_spam_from_one_ip_is_limited() -> None:
    async with running_api(
        rate_limit_auth_ip_capacity=3, rate_limit_auth_ip_refill_per_second=SLOW
    ) as api:
        spammer = random_ip()
        async with api.client(spammer) as client:
            responses = [
                await client.get("/v1/projects", headers=bearer(UNKNOWN_KEY)) for _ in range(4)
            ]
            health = await client.get("/health/live", headers=bearer(UNKNOWN_KEY))
        async with api.client(random_ip()) as elsewhere:
            other_ip = await elsewhere.get("/v1/projects", headers=bearer(UNKNOWN_KEY))

    assert [r.status_code for r in responses[:3]] == [401, 401, 401]
    assert_limited(responses[3], limit=3)
    assert health.status_code == 200
    assert other_ip.status_code == 401


async def test_login_attempts_are_limited_per_ip() -> None:
    async with running_api(
        rate_limit_auth_ip_capacity=3, rate_limit_auth_ip_refill_per_second=SLOW
    ) as api:
        operator = await api.operator()
        async with api.client(random_ip()) as client:
            attempts = [await login(client, operator, password="wrong") for _ in range(3)]
            limited = await login(client, operator)

    assert [r.status_code for r in attempts] == [401, 401, 401]
    assert attempts[0].headers["RateLimit-Limit"] == "3"
    assert_limited(limited, limit=3)


async def test_limits_refill_with_the_clock() -> None:
    async with (
        running_api(rate_limit_auth_ip_capacity=1, rate_limit_auth_ip_refill_per_second="1") as api,
        api.client(random_ip()) as client,
    ):
        first = await client.get("/v1/projects", headers=bearer(UNKNOWN_KEY))
        denied = await client.get("/v1/projects", headers=bearer(UNKNOWN_KEY))
        api.clock.advance(timedelta(seconds=1))
        refilled = await client.get("/v1/projects", headers=bearer(UNKNOWN_KEY))

    assert first.status_code == 401
    assert_limited(denied, limit=1)
    assert denied.headers["Retry-After"] == "1"
    assert refilled.status_code == 401


async def test_health_checks_are_exempt() -> None:
    async with running_api(
        rate_limit_operator_capacity=1, rate_limit_operator_refill_per_second=SLOW
    ) as api:
        client, _ = await logged_in(api, await api.operator())
        async with aclosing(client):
            statuses = [(await client.get("/health/live")).status_code for _ in range(5)]
            ready = await client.get("/health/ready")

    assert statuses == [200] * 5
    assert ready.status_code == 200
    assert "RateLimit-Limit" not in ready.headers


async def test_anonymous_requests_without_credentials_are_not_counted() -> None:
    async with (
        running_api(
            rate_limit_auth_ip_capacity=1, rate_limit_auth_ip_refill_per_second=SLOW
        ) as api,
        api.client(random_ip()) as client,
    ):
        statuses = [(await client.get("/v1/projects")).status_code for _ in range(3)]
    assert statuses == [401, 401, 401]


async def test_rate_limiting_can_be_disabled() -> None:
    async with running_api(
        rate_limit_enabled=False,
        rate_limit_auth_ip_capacity=1,
        rate_limit_auth_ip_refill_per_second=SLOW,
    ) as api:
        operator = await api.operator()
        async with api.client(random_ip()) as client:
            statuses = [
                (await client.get("/v1/projects", headers=bearer(UNKNOWN_KEY))).status_code
                for _ in range(3)
            ]
            logins = [(await login(client, operator, password="no")).status_code for _ in range(3)]
            me = await client.get("/v1/auth/me")
    assert statuses == [401, 401, 401]
    assert logins == [401, 401, 401]
    assert "RateLimit-Limit" not in me.headers


async def test_a_csrf_failure_is_not_an_authentication_attempt() -> None:
    async with running_api(
        rate_limit_auth_ip_capacity=1, rate_limit_auth_ip_refill_per_second=SLOW
    ) as api:
        client, _ = await logged_in(api, await api.operator())
        async with aclosing(client):
            statuses = [
                (await client.post("/v1/projects", json={"slug": "x", "name": "x"})).status_code
                for _ in range(3)
            ]
    assert statuses == [403, 403, 403]
