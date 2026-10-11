"""CSRF: cookie-authenticated unsafe methods must echo the session's ``X-CSRF-Token``."""

from contextlib import aclosing

import pytest

from tests.integration.identity.support import PASSWORD, Api, csrf, logged_in, unique_slug

UNSAFE_CALLS = [
    ("/v1/auth/logout", None),
    ("/v1/auth/password", {"current_password": PASSWORD, "new_password": "another passphrase"}),
    ("/v1/projects", {"slug": "csrf-target", "name": "CSRF"}),
]


@pytest.mark.parametrize(("path", "body"), UNSAFE_CALLS)
@pytest.mark.parametrize("token", [None, "", "x" * 43])
async def test_unsafe_methods_without_the_right_token_are_403(
    api: Api, path: str, body: dict[str, str] | None, token: str | None
) -> None:
    operator = await api.operator()
    client, _ = await logged_in(api, operator)
    async with aclosing(client):
        headers = {} if token is None else csrf(token)
        response = await client.post(path, json=body, headers=headers)
        still_valid = await client.get("/v1/auth/me")

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing or invalid CSRF token."
    assert still_valid.status_code == 200  # nothing happened: not logged out, not changed


async def test_a_token_from_another_session_is_rejected(api: Api) -> None:
    operator = await api.operator()
    first, first_csrf = await logged_in(api, operator)
    second, second_csrf = await logged_in(api, operator)
    async with aclosing(first), aclosing(second):
        crossed = await first.post("/v1/auth/logout", headers=csrf(second_csrf))
        own = await first.post("/v1/auth/logout", headers=csrf(first_csrf))

    assert first_csrf != second_csrf
    assert crossed.status_code == 403
    assert own.status_code == 204


async def test_safe_methods_need_no_token(api: Api) -> None:
    operator = await api.operator()
    client, _ = await logged_in(api, operator)
    async with aclosing(client):
        assert (await client.get("/v1/auth/me")).status_code == 200
        assert (await client.get("/v1/projects")).status_code == 200


async def test_the_token_is_stable_for_the_session_and_served_by_me(api: Api) -> None:
    operator = await api.operator()
    client, csrf_token = await logged_in(api, operator)
    async with aclosing(client):
        me = (await client.get("/v1/auth/me")).json()
        created = await client.post(
            "/v1/projects",
            json={"slug": unique_slug(), "name": "With token"},
            headers=csrf(me["csrf_token"]),
        )

    assert me["csrf_token"] == csrf_token
    assert created.status_code == 201
