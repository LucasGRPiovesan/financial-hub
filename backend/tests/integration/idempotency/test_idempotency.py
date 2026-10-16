"""``Idempotency-Key`` protocol (ADR-0004) on a probe route, against real Postgres as ``fh_app``."""

import asyncio
import secrets
from contextlib import aclosing
from datetime import timedelta

import httpx
import pytest

from financial_hub.platform.http.protection.idempotency import IdempotencyStore
from tests.integration.idempotency.support import Probe, running_probe
from tests.integration.identity.support import logged_in
from tests.integration.projects.helpers import bearer, create_key, create_project, problem_type

PROBLEMS = "https://financial-hub.dev/problems/"
CONCURRENT = 10
ROUNDS = 4


def new_key() -> str:
    return f"idem-{secrets.token_hex(8)}"


def is_replay(response: httpx.Response) -> bool:
    return response.headers.get("Idempotent-Replayed") == "true"


async def test_concurrent_requests_with_one_key_have_exactly_one_effect() -> None:
    async with running_probe() as probe:
        for _ in range(ROUNDS):
            key = new_key()
            before = len(probe.effects)
            responses = await asyncio.gather(
                *(probe.post(key, value=key, delay_ms=50) for _ in range(CONCURRENT))
            )

            assert len(probe.effects) == before + 1
            originals = [r for r in responses if r.status_code == 201 and not is_replay(r)]
            replays = [r for r in responses if r.status_code == 201 and is_replay(r)]
            conflicts = [r for r in responses if r.status_code == 409]
            assert len(originals) == 1
            assert len(originals) + len(replays) + len(conflicts) == CONCURRENT
            assert all(r.json() == originals[0].json() for r in replays)
            for conflict in conflicts:
                assert problem_type(conflict) == f"{PROBLEMS}idempotency-conflict"
                assert conflict.headers["Retry-After"] == "1"


async def test_a_completed_key_replays_the_stored_response() -> None:
    async with running_probe() as probe:
        key = new_key()
        first = await probe.post(key, value="a")
        second = await probe.post(key, value="a")

        assert probe.effects == ["a"]
        assert first.status_code == second.status_code == 201
        assert second.content == first.content
        assert is_replay(second) and not is_replay(first)
        assert second.headers["location"] == first.headers["location"]
        assert second.headers["content-type"] == first.headers["content-type"]
        assert "x-not-stored" not in second.headers


async def test_the_same_key_with_another_body_is_422() -> None:
    async with running_probe() as probe:
        key = new_key()
        assert (await probe.post(key, value="a")).status_code == 201
        reused = await probe.post(key, value="b")

        assert reused.status_code == 422
        assert problem_type(reused) == f"{PROBLEMS}idempotency-key-reused"
        assert probe.effects == ["a"]


async def test_4xx_answers_are_stored_and_replayed() -> None:
    async with running_probe() as probe:
        key = new_key()
        first = await probe.post(key, value="a", status=409)
        second = await probe.post(key, value="a", status=409)

        assert first.status_code == second.status_code == 409
        assert is_replay(second)
        assert probe.effects == ["a"]


async def test_a_5xx_answer_releases_the_key() -> None:
    async with running_probe() as probe:
        key = new_key()
        failed = await probe.post(key, value="a", status=503)
        retried = await probe.post(key, value="a", status=503)

        assert failed.status_code == retried.status_code == 503
        assert not is_replay(retried)
        assert probe.effects == ["a", "a"]


async def test_an_unhandled_exception_releases_the_key() -> None:
    async with running_probe() as probe, aclosing(probe.lenient_client()) as lenient:
        key = new_key()
        crashed = await lenient.post(
            "/test/idempotency/optional",
            json={"value": "a", "explode": True},
            headers=probe.headers(key),
        )
        assert crashed.status_code == 500

        retried = await probe.post(key, value="a", explode=False)
        assert retried.status_code == 201 and not is_replay(retried)
        assert probe.effects == ["a", "a"]


async def test_an_expired_key_can_be_used_again() -> None:
    # An API key principal: operator sessions would idle out over the TTL.
    async with running_probe(idempotency_ttl_hours=1) as probe:
        owner_key = await _api_key(probe)
        key = new_key()

        async with probe.api.client() as client:
            first = await client.post(
                "/test/idempotency/optional",
                json={"value": "a"},
                headers={**bearer(owner_key), "Idempotency-Key": key},
            )
            probe.api.clock.advance(timedelta(hours=1))
            again = await client.post(
                "/test/idempotency/optional",
                json={"value": "b"},
                headers={**bearer(owner_key), "Idempotency-Key": key},
            )

        assert first.status_code == again.status_code == 201
        assert not is_replay(again)
        assert probe.effects == ["a", "b"]


async def _api_key(probe: Probe) -> str:
    """The secret of a new API key, in a new project of a new owner."""
    owner = await probe.api.operator()
    project = await create_project(probe.api, owner)
    client, csrf_token = await logged_in(probe.api, owner)
    async with aclosing(client):
        return (await create_key(client, csrf_token, project["id"]))["key"]


async def test_keys_are_scoped_to_the_principal() -> None:
    async with running_probe() as probe, running_probe() as other:
        key = new_key()
        mine = await probe.post(key, value="a")
        theirs = await other.post(key, value="b")

        assert mine.status_code == theirs.status_code == 201
        assert not is_replay(theirs)
        assert probe.effects == ["a"] and other.effects == ["b"]


async def test_a_required_key_must_be_present() -> None:
    async with running_probe() as probe:
        missing = await probe.post(None, route="required")
        assert missing.status_code == 400
        assert problem_type(missing) == f"{PROBLEMS}idempotency-key-required"

        present = await probe.post(new_key(), route="required")
        assert present.status_code == 201
        assert probe.effects == ["effect"]


async def test_an_optional_key_may_be_absent() -> None:
    async with running_probe() as probe:
        first = await probe.post(None, value="a")
        second = await probe.post(None, value="a")
        assert first.status_code == second.status_code == 201
        assert probe.effects == ["a", "a"]


@pytest.mark.parametrize("key", [b"", b"has space", b"x" * 256, "acentuação".encode()])
async def test_a_malformed_key_is_400(key: bytes) -> None:
    async with running_probe() as probe:
        headers: dict[str, str | bytes] = {**probe.headers(None), "Idempotency-Key": key}
        response = await probe.client.post("/test/idempotency/optional", json={}, headers=headers)
        assert response.status_code == 400
        assert problem_type(response) == f"{PROBLEMS}idempotency-key-invalid"
        assert probe.effects == []


async def test_a_body_above_the_cap_is_not_replayed() -> None:
    async with running_probe(idempotency_max_body_bytes=64) as probe:
        key = new_key()
        first = await probe.post(key, value="a", pad=200)
        second = await probe.post(key, value="a", pad=200)

        assert first.status_code == 201
        assert second.status_code == 409
        assert problem_type(second) == f"{PROBLEMS}idempotency-response-unavailable"
        assert is_replay(second)
        assert probe.effects == ["a"]


async def test_a_failed_store_keeps_the_key_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken_complete(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database unavailable")

    async with running_probe() as probe:
        key = new_key()
        with monkeypatch.context() as patch:
            patch.setattr(IdempotencyStore, "complete", broken_complete)
            first = await probe.post(key, value="a")
        second = await probe.post(key, value="a")

        assert first.status_code == 201  # the effect happened; the client still gets its answer
        assert second.status_code == 409
        assert problem_type(second) == f"{PROBLEMS}idempotency-conflict"
        assert probe.effects == ["a"]


async def test_a_failed_release_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken_release(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database unavailable")

    async with running_probe() as probe:
        monkeypatch.setattr(IdempotencyStore, "release", broken_release)
        response = await probe.post(new_key(), value="a", status=500)
        assert response.status_code == 500


async def test_requests_without_a_key_stream_through_the_middleware() -> None:
    async with running_probe() as probe:
        response = await probe.client.get("/v1/auth/me")
        assert response.status_code == 200
        assert "Idempotent-Replayed" not in response.headers
