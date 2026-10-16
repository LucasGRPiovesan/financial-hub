"""Demo login: off by default (404), and a real session by operator selection when enabled."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.cli.demo import seed_demo
from financial_hub.modules.identity.application.api import DEMO_EMAIL_DOMAIN, Role
from tests.integration.audit.helpers import only_row, tagged
from tests.integration.identity.support import (
    Api,
    csrf,
    random_ip,
    running_api,
    unique_demo_email,
)
from tests.integration.projects.helpers import create_project


async def test_both_endpoints_are_404_when_the_flag_is_off(api: Api) -> None:
    operator = await api.operator()
    async with api.client() as client:
        listing = await client.get("/v1/auth/demo-operators")
        attempt = await client.post("/v1/auth/demo-login", json={"operator_id": operator.id})

    assert listing.status_code == 404
    assert attempt.status_code == 404
    assert "fh_session" not in attempt.cookies


async def test_listing_shows_operators_with_their_memberships() -> None:
    async with running_api(demo_login=True) as api:
        owner = await api.operator(email=unique_demo_email())
        project = await create_project(api, owner, "Listed")
        async with api.client() as client:
            response = await client.get("/v1/auth/demo-operators")

    assert response.status_code == 200
    entry = next(o for o in response.json()["operators"] if o["id"] == owner.id)
    assert entry["email"] == owner.email
    assert entry["memberships"] == [
        {"project_id": project["id"], "project_name": "Listed", "role": "owner"}
    ]
    assert "password" not in str(response.json())


async def test_demo_login_opens_a_real_session_and_logout_ends_it(app_engine: AsyncEngine) -> None:
    async with running_api(demo_login=True) as api:
        operator = await api.operator(email=unique_demo_email())
        async with api.client() as client:
            response = await client.post("/v1/auth/demo-login", json={"operator_id": operator.id})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["operator"]["id"] == operator.id
            cookie = response.headers["set-cookie"]
            assert "fh_session=" in cookie and "HttpOnly" in cookie

            me = await client.get("/v1/auth/me")
            assert me.status_code == 200 and me.json()["operator"]["id"] == operator.id

            no_csrf = await client.post("/v1/auth/logout")
            assert no_csrf.status_code in (401, 403)
            out = await client.post("/v1/auth/logout", headers=csrf(body["csrf_token"]))
            assert out.status_code == 204
            assert (await client.get("/v1/auth/me")).status_code == 401


async def test_unknown_malformed_and_disabled_operators_are_rejected(
    owner_engine: AsyncEngine,
) -> None:
    async with running_api(demo_login=True) as api:
        operator = await api.operator(email=unique_demo_email())
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE operators SET status = 'disabled' WHERE id = :id"),
                {"id": operator.uuid},
            )
        async with api.client() as client:
            unknown = await client.post(
                "/v1/auth/demo-login", json={"operator_id": "opr_" + "0" * 26}
            )
            malformed = await client.post("/v1/auth/demo-login", json={"operator_id": "nope"})
            disabled = await client.post("/v1/auth/demo-login", json={"operator_id": operator.id})
            listing = await client.get("/v1/auth/demo-operators")

    assert malformed.status_code == 422
    assert malformed.json()["errors"][0]["pointer"] == "/operator_id"
    assert unknown.status_code in (404, 422)
    assert disabled.status_code == 404
    assert operator.id not in [o["id"] for o in listing.json()["operators"]]


async def test_demo_login_is_audited_with_method_demo(app_engine: AsyncEngine) -> None:
    async with running_api(demo_login=True) as api:
        operator = await api.operator(email=unique_demo_email())
        async with api.client() as client:
            cid = tagged(client)
            response = await client.post("/v1/auth/demo-login", json={"operator_id": operator.id})
    assert response.status_code == 200
    record = await only_row(app_engine, cid)
    assert (record["actor_id"], record["action"]) == (operator.id, "operator.login_succeeded")
    assert record["diff"] == {"method": "demo"}


async def test_demo_login_is_rate_limited_like_login() -> None:
    async with running_api(
        demo_login=True, rate_limit_auth_ip_capacity=2, rate_limit_auth_ip_refill_per_second="0.001"
    ) as api:
        operator = await api.operator(email=unique_demo_email())
        async with api.client(random_ip()) as client:
            codes = [
                (
                    await client.post("/v1/auth/demo-login", json={"operator_id": operator.id})
                ).status_code
                for _ in range(3)
            ]

    assert codes == [200, 200, 429]


async def test_a_viewer_demo_session_cannot_create_api_keys() -> None:
    async with running_api(demo_login=True) as api:
        owner = await api.operator()
        project = await create_project(api, owner)
        viewer = await api.operator(email=unique_demo_email())
        await api.grant(viewer, project["id"], Role.VIEWER)
        async with api.client() as client:
            session = await client.post("/v1/auth/demo-login", json={"operator_id": viewer.id})
            response = await client.post(
                f"/v1/projects/{project['id']}/api-keys",
                json={"name": "nope", "scopes": ["projects:read"]},
                headers=csrf(session.json()["csrf_token"]),
            )

    assert response.status_code == 403


async def test_a_non_demo_operator_is_rejected_exactly_like_an_unknown_id() -> None:
    async with running_api(demo_login=True) as api:
        real = await api.operator()  # an ordinary (non-demo) active operator
        async with api.client() as client:
            unknown = await client.post(
                "/v1/auth/demo-login", json={"operator_id": "opr_" + "0" * 26}
            )
            non_demo = await client.post("/v1/auth/demo-login", json={"operator_id": real.id})

    assert non_demo.status_code == unknown.status_code == 404
    assert "fh_session" not in non_demo.cookies
    assert (non_demo.json()["title"], non_demo.json()["detail"], non_demo.json()["type"]) == (
        unknown.json()["title"],
        unknown.json()["detail"],
        unknown.json()["type"],
    )


async def test_the_picker_lists_only_the_seeded_personas_even_with_other_operators() -> None:
    async with running_api(demo_login=True) as api:
        await seed_demo(api.factory, hasher=api.hasher, clock=api.clock)
        stranger = await api.operator()
        extra = await api.operator(email=unique_demo_email())  # demo domain, but not a persona
        async with api.client() as client:
            response = await client.get("/v1/auth/demo-operators")

    listed = response.json()["operators"]
    emails = [o["email"] for o in listed]
    assert stranger.email not in emails
    assert all(email.endswith(f"@{DEMO_EMAIL_DOMAIN}") for email in emails)
    personas = [e for e in emails if e.split("@")[0] in {"ana", "bruno", "carla", "diego"}]
    assert personas == [
        f"{name}@{DEMO_EMAIL_DOMAIN}" for name in ("ana", "bruno", "carla", "diego")
    ]
    assert extra.email in emails  # in-domain operators are, by definition, demo operators
