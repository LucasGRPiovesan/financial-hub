"""Session lifecycle: fixation, idle/absolute expiry, logout, password change, hashed storage."""

import base64
import hashlib
from contextlib import aclosing
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.identity.support import (
    PASSWORD,
    Api,
    csrf,
    logged_in,
    login,
    running_api,
)

NEW_PASSWORD = "a brand new passphrase"


async def test_me_returns_operator_memberships_and_csrf(api: Api) -> None:
    operator = await api.operator(display_name="Me")
    client, csrf_token = await logged_in(api, operator)
    async with aclosing(client):
        zeta = await client.post(
            "/v1/projects",
            json={"slug": "zeta-" + operator.id[-8:].lower(), "name": "Zeta"},
            headers=csrf(csrf_token),
        )
        alpha = await client.post(
            "/v1/projects",
            json={"slug": "alpha-" + operator.id[-8:].lower(), "name": "alpha"},
            headers=csrf(csrf_token),
        )
        response = await client.get("/v1/auth/me")

    assert zeta.status_code == alpha.status_code == 201
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "operator": {"id": operator.id, "email": operator.email, "display_name": "Me"},
        "memberships": [
            {"project_id": alpha.json()["id"], "project_name": "alpha", "role": "owner"},
            {"project_id": zeta.json()["id"], "project_name": "Zeta", "role": "owner"},
        ],
        "csrf_token": csrf_token,
    }


async def test_me_without_a_valid_session_is_401(api: Api) -> None:
    async with api.client() as client:
        anonymous = await client.get("/v1/auth/me")
        client.cookies.set("fh_session", "not-a-token")
        garbage = await client.get("/v1/auth/me")
        client.cookies.set("fh_session", "A" * 43)  # well formed, never issued
        unknown = await client.get("/v1/auth/me")

    assert anonymous.status_code == garbage.status_code == unknown.status_code == 401
    assert anonymous.json()["detail"] == "Authentication required."


async def test_login_rotates_the_session_and_revokes_the_presented_one(api: Api) -> None:
    operator = await api.operator()
    async with api.client() as client:
        assert (await login(client, operator)).status_code == 200
        first = client.cookies["fh_session"]
        assert (await login(client, operator)).status_code == 200
        second = client.cookies["fh_session"]

    assert first != second
    async with api.client() as replay:
        replay.cookies.set("fh_session", first)
        assert (await replay.get("/v1/auth/me")).status_code == 401
        replay.cookies.set("fh_session", second)
        assert (await replay.get("/v1/auth/me")).status_code == 200


async def test_a_planted_cookie_never_becomes_the_session(api: Api) -> None:
    operator = await api.operator()
    planted = "B" * 43
    async with api.client() as client:
        client.cookies.set("fh_session", planted)
        response = await login(client, operator)
        assert response.status_code == 200
        issued = response.cookies["fh_session"]

    assert issued != planted
    async with api.client() as replay:
        replay.cookies.set("fh_session", planted)
        assert (await replay.get("/v1/auth/me")).status_code == 401


async def test_idle_timeout_slides_with_activity_and_then_expires(api: Api) -> None:
    operator = await api.operator()
    client, _ = await logged_in(api, operator)
    async with aclosing(client):
        for _ in range(3):
            api.clock.advance(timedelta(minutes=29))
            assert (await client.get("/v1/auth/me")).status_code == 200
        api.clock.advance(timedelta(minutes=30))
        assert (await client.get("/v1/auth/me")).status_code == 401


async def test_absolute_timeout_ends_even_an_active_session(api: Api) -> None:
    operator = await api.operator()
    client, _ = await logged_in(api, operator)
    async with aclosing(client):
        for _ in range(35):  # 35 x 20 min = 11 h 40 min of continuous activity
            api.clock.advance(timedelta(minutes=20))
            assert (await client.get("/v1/auth/me")).status_code == 200
        api.clock.advance(timedelta(minutes=20))  # 12 h
        assert (await client.get("/v1/auth/me")).status_code == 401


async def test_last_seen_is_written_at_most_once_per_interval(
    api: Api, app_engine: AsyncEngine
) -> None:
    operator = await api.operator()
    client, _ = await logged_in(api, operator)
    async with aclosing(client):
        created = await last_seen(app_engine, client.cookies["fh_session"])
        api.clock.advance(timedelta(seconds=30))
        assert (await client.get("/v1/auth/me")).status_code == 200
        unchanged = await last_seen(app_engine, client.cookies["fh_session"])
        api.clock.advance(timedelta(seconds=31))
        assert (await client.get("/v1/auth/me")).status_code == 200
        touched = await last_seen(app_engine, client.cookies["fh_session"])

    assert unchanged == created
    assert touched - created == timedelta(seconds=61)


async def last_seen(engine: AsyncEngine, cookie: str) -> datetime:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT last_seen_at, idle_expires_at FROM sessions WHERE id = :id"),
            {"id": session_id(cookie)},
        )
        seen, idle = result.one()
    assert idle - seen == timedelta(minutes=30)  # the idle window always slides from last_seen
    return seen


def raw_token(cookie: str) -> bytes:
    return base64.urlsafe_b64decode(cookie + "=")


def session_id(cookie: str) -> bytes:
    return hashlib.sha256(raw_token(cookie)).digest()


async def test_logout_revokes_the_session_and_clears_the_cookie(api: Api) -> None:
    operator = await api.operator()
    client, csrf_token = await logged_in(api, operator)
    async with aclosing(client):
        cookie = client.cookies["fh_session"]
        response = await client.post("/v1/auth/logout", headers=csrf(csrf_token))
        after = await client.get("/v1/auth/me")

    assert response.status_code == 204
    assert response.headers["set-cookie"].startswith('fh_session=""')
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert after.status_code == 401
    async with api.client() as replay:
        replay.cookies.set("fh_session", cookie)
        assert (await replay.get("/v1/auth/me")).status_code == 401


async def test_logout_without_a_session_is_401(api: Api) -> None:
    async with api.client() as client:
        assert (await client.post("/v1/auth/logout")).status_code == 401


async def test_password_change_revokes_every_other_session(api: Api) -> None:
    operator = await api.operator()
    laptop, laptop_csrf = await logged_in(api, operator)
    phone, _ = await logged_in(api, operator)
    async with aclosing(laptop), aclosing(phone):
        response = await laptop.post(
            "/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=csrf(laptop_csrf),
        )
        assert response.status_code == 204
        assert (await laptop.get("/v1/auth/me")).status_code == 200
        assert (await phone.get("/v1/auth/me")).status_code == 401

    async with api.client() as client:
        assert (await login(client, operator)).status_code == 401
        assert (await login(client, operator, password=NEW_PASSWORD)).status_code == 200


async def test_password_change_validates_both_passwords(api: Api) -> None:
    operator = await api.operator()
    client, csrf_token = await logged_in(api, operator)
    async with aclosing(client):
        short = await client.post(
            "/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": "too short"},
            headers=csrf(csrf_token),
        )
        wrong = await client.post(
            "/v1/auth/password",
            json={"current_password": "not my password", "new_password": NEW_PASSWORD},
            headers=csrf(csrf_token),
        )

    assert short.status_code == wrong.status_code == 422
    assert [e["pointer"] for e in short.json()["errors"]] == ["/new_password"]
    assert [e["pointer"] for e in wrong.json()["errors"]] == ["/current_password"]
    assert "too short" not in short.text
    assert "not my password" not in wrong.text


async def test_password_guessing_through_change_is_throttled(api: Api) -> None:
    operator = await api.operator()
    client, csrf_token = await logged_in(api, operator)
    async with aclosing(client):
        for _ in range(5):
            response = await client.post(
                "/v1/auth/password",
                json={"current_password": "guess guess guess", "new_password": NEW_PASSWORD},
                headers=csrf(csrf_token),
            )
            assert response.status_code == 422
        blocked = await client.post(
            "/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=csrf(csrf_token),
        )

    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == str(15 * 60)


async def test_tokens_are_stored_only_as_hashes(api: Api, app_engine: AsyncEngine) -> None:
    operator = await api.operator()
    client, csrf_token = await logged_in(api, operator)
    async with aclosing(client):
        cookie = client.cookies["fh_session"]
    raw = raw_token(cookie)

    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, csrf_token_hash, ip FROM sessions WHERE operator_id = :id"),
                {"id": operator.uuid},
            )
        ).all()

    assert len(rows) == 1
    stored_id, stored_csrf, ip = rows[0]
    assert bytes(stored_id) == hashlib.sha256(raw).digest()
    assert bytes(stored_csrf) == hashlib.sha256(csrf_token.encode()).digest()
    assert raw not in (bytes(stored_id), bytes(stored_csrf))
    assert str(ip) == api.ip


async def test_short_idle_timeout_setting_is_honoured() -> None:
    async with running_api(session_idle_timeout_minutes=5) as api:
        operator = await api.operator()
        client, _ = await logged_in(api, operator)
        async with aclosing(client):
            api.clock.advance(timedelta(minutes=5))
            assert (await client.get("/v1/auth/me")).status_code == 401
