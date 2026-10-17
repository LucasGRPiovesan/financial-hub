"""Manifest lifecycle over HTTP: draft -> validated -> approved -> active -> superseded, rejected.

Four-eyes is mandatory when a version changes splits, prices or the wallet; a description-only
change may be approved by its author. Activation materializes metrics and webhook endpoints and
returns their signing secrets once, to the caller only; rollback re-activates an older version.
"""

from contextlib import aclosing

from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from tests.integration.identity.support import Api, csrf
from tests.integration.integrations.helpers import HOOK_URL, MANIFEST, count, manifest, store
from tests.integration.metering.helpers import scalar
from tests.integration.projects.helpers import member, problem_type


async def test_a_version_moves_through_the_lifecycle_and_freezes_once_validated(api: Api) -> None:
    async with store(api) as s:
        client, headers = s.as_owner()
        created = await s.create(MANIFEST)
        version = created["version"]
        assert (version, created["status"]) == (1, "draft")
        edited = await client.put(
            f"{s.base}/{version}",
            json={"content": manifest(description="Edited while a draft")},
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        validated = await s.validate(version)
        assert validated["status"] == "validated"
        frozen = await client.put(
            f"{s.base}/{version}", json={"content": MANIFEST}, headers=headers
        )
        stale = await s.approve(version, "0" * 64, by_finance=True)
        early = await s.activate(version)
        approved = await s.approve(version, validated["content_hash"], by_finance=True)
        fetched = await client.get(f"{s.base}/{version}")

    assert frozen.status_code == 409, frozen.text
    assert stale.status_code == 409, stale.text
    assert early.status_code == 409, early.text  # validated, not approved
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert (body["status"], body["four_eyes"]) == ("approved", True)
    assert body["approval_diff"]["money_sensitive"] is True
    assert fetched.json()["content"]["description"] == "Edited while a draft"


async def test_validation_reports_unknown_references_with_pointers(api: Api) -> None:
    async with store(api) as s:
        created = await s.create(manifest(prices=[{"code": "nope"}]))
        client, headers = s.as_owner()
        response = await client.post(f"{s.base}/{created['version']}/validate", headers=headers)
        floats = await client.post(
            s.base,
            json={"content": manifest(splits=[{"payee": "x", "basis_points": 1.5}])},
            headers=headers,
        )
        draft = await s.create({"schema_version": "manifest.v2"})  # a draft may be incomplete
        invalid = await client.post(f"{s.base}/{draft['version']}/validate", headers=headers)

    assert response.status_code == 422, response.text
    assert "/prices/0/code" in response.text
    assert floats.status_code == 422, floats.text
    assert "/content/splits/0/basis_points" in floats.text
    assert invalid.status_code == 422, invalid.text
    assert "/schema_version" in invalid.text


async def test_a_rejected_version_cannot_be_approved(api: Api) -> None:
    async with store(api) as s:
        created = await s.create(MANIFEST)
        validated = await s.validate(created["version"])
        client, headers = s.as_finance()
        rejected = await client.post(
            f"{s.base}/{created['version']}/reject", json={"reason": "Wrong split"}, headers=headers
        )
        approved = await s.approve(created["version"], validated["content_hash"], by_finance=True)

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert approved.status_code == 409, approved.text


async def test_the_author_cannot_approve_a_money_change(api: Api) -> None:
    async with store(api) as s:
        await s.live(MANIFEST)
        split = await s.create(manifest(splits=[{"payee": "clinic-1", "basis_points": 6500}]))
        validated = await s.validate(split["version"])
        own = await s.approve(split["version"], validated["content_hash"])
        diff = await s.owner.get(f"{s.base}/{split['version']}/diff")
        other = await s.approve(split["version"], validated["content_hash"], by_finance=True)

    assert own.status_code == 403, own.text
    assert problem_type(own).endswith("four-eyes-required")
    assert diff.status_code == 200, diff.text
    assert diff.json()["money_sensitive"] is True
    assert other.status_code == 200, other.text
    assert other.json()["four_eyes"] is True


async def test_a_description_only_change_needs_no_second_operator(api: Api) -> None:
    async with store(api) as s:
        await s.live(MANIFEST)
        cosmetic = await s.create(manifest(description="Only the wording changed"))
        validated = await s.validate(cosmetic["version"])
        own = await s.approve(cosmetic["version"], validated["content_hash"])

    assert own.status_code == 200, own.text
    body = own.json()
    assert body["four_eyes"] is False
    assert body["approval_diff"]["money_sensitive"] is False


async def test_activation_materializes_metrics_and_endpoints_and_shows_secrets_once(
    api: Api, owner_engine: AsyncEngine
) -> None:
    async with store(api) as s:
        activated = await s.live(MANIFEST)
        project = s.meter.project_uuid
        metrics = await scalar(
            owner_engine,
            "SELECT count(*) FROM public.billable_metrics "
            "WHERE project_id = :project AND code = 'consultations'",
            project=project,
        )
        endpoints = await count(owner_engine, "webhook_endpoints", project)
        events = await scalar(
            owner_engine,
            "SELECT count(*) FROM public.events "
            "WHERE project_id = :project AND type = 'manifest.activated'",
            project=project,
        )
        active = await s.owner.get(f"{s.base}/active")
        history = await s.owner.get(f"{s.base}/activations")
        stored = await s.owner.get(f"{s.base}/1")

    (secret,) = activated["webhook_secrets"]
    assert secret["url"] == HOOK_URL and secret["secret"]
    assert activated["manifest"]["status"] == "active"
    assert (metrics, endpoints, events) == (1, 1, 1)
    assert active.json()["version"] == 1
    assert [row["version"] for row in history.json()] == [1]
    for response in (active, history, stored):
        assert secret["secret"] not in response.text  # never stored in the manifest


async def test_rollback_reactivates_an_older_version_without_touching_history(
    api: Api, owner_engine: AsyncEngine
) -> None:
    async with store(api) as s:
        await s.live(MANIFEST)
        second = await s.live(manifest(description="Second"))
        rolled = await s.activate(1, by_finance=True)
        first = await s.owner.get(f"{s.base}/1")
        later = await s.owner.get(f"{s.base}/2")
        history = await s.owner.get(f"{s.base}/activations")
        endpoints = await count(owner_engine, "webhook_endpoints", s.meter.project_uuid)

    assert second["activation"]["previous_version"] == 1
    assert second["webhook_secrets"] == []  # the endpoint already exists
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["activation"]["previous_version"] == 2
    assert first.json()["status"] == "active"
    assert later.json()["status"] == "superseded"
    assert later.json()["content"]["description"] == "Second"  # content never changes
    assert sorted(row["version"] for row in history.json()) == [1, 1, 2]
    assert endpoints == 1


async def test_only_owner_and_finance_approve_and_activate(api: Api) -> None:
    async with store(api) as s:
        created = await s.create(MANIFEST)
        validated = await s.validate(created["version"])
        path = f"{s.base}/{created['version']}"
        statuses: dict[Role, tuple[int, int]] = {}
        for role in (Role.ADMIN, Role.VIEWER):
            _, client, token = await member(api, s.meter.project_id, role)
            async with aclosing(client):
                approve = await client.post(
                    f"{path}/approve",
                    json={"content_hash": validated["content_hash"]},
                    headers=csrf(token),
                )
                activate = await client.post(f"{path}/activate", headers=csrf(token))
            statuses[role] = (approve.status_code, activate.status_code)
        finance = await s.approve(created["version"], validated["content_hash"], by_finance=True)

    assert statuses == {Role.ADMIN: (403, 403), Role.VIEWER: (403, 403)}
    assert finance.status_code == 200, finance.text


async def test_the_schema_is_served_to_signed_in_callers(api: Api) -> None:
    async with store(api) as s:
        served = await s.owner.get("/v1/manifest-schema/v1")
        async with api.client() as anonymous:
            denied = await anonymous.get("/v1/manifest-schema/v1")

    assert served.status_code == 200, served.text
    assert served.json()["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert denied.status_code == 401
