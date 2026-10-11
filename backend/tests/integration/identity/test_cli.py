"""``python -m financial_hub.cli create-operator``: password from env/prompt only, never argv."""

import io
from contextlib import aclosing

import pytest

from financial_hub.cli import PASSWORD_ENV, CliError, bootstrap_operator, main, read_password
from financial_hub.shared.errors import Conflict, ValidationFailed
from tests.integration.identity.support import (
    PASSWORD,
    Api,
    Operator,
    logged_in,
    unique_email,
    unique_slug,
)


def run(argv: list[str], environ: dict[str, str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, environ=environ, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_create_operator_with_a_project() -> None:
    email, slug = unique_email("cli"), unique_slug("cli")
    code, out, err = run(
        [
            "create-operator",
            "--email", email,
            "--display-name", "Bootstrap Admin",
            "--project-slug", slug,
            "--project-name", "First Project",
        ],
        {PASSWORD_ENV: PASSWORD},
    )  # fmt: skip

    assert (code, err) == (0, "")
    assert f"<{email}>" in out
    assert f"({slug}); role owner" in out
    assert PASSWORD not in out


def test_duplicate_email_fails_cleanly() -> None:
    email = unique_email("dup")
    argv = ["create-operator", "--email", email, "--display-name", "Dup"]
    assert run(argv, {PASSWORD_ENV: PASSWORD})[0] == 0

    code, out, err = run(argv, {PASSWORD_ENV: PASSWORD})

    assert (code, out) == (1, "")
    assert err.startswith("error: An operator with email")


def test_invalid_input_lists_the_fields() -> None:
    code, _, err = run(
        ["create-operator", "--email", "not-an-email", "--display-name", "X"],
        {PASSWORD_ENV: "short"},
    )

    assert code == 1
    assert "email: not a valid email address" in err
    assert "short" not in err


def test_missing_password_without_a_terminal_fails() -> None:
    code, _, err = run(["create-operator", "--email", unique_email(), "--display-name", "X"], {})

    assert code == 1
    assert PASSWORD_ENV in err


def test_password_is_not_accepted_on_the_command_line(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            ["create-operator", "--email", unique_email(), "--display-name", "X", "--password", "p"]
        )

    assert exit_info.value.code == 2
    assert "unrecognized arguments: --password" in capsys.readouterr().err


def test_project_slug_needs_a_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(
            ["create-operator", "--email", unique_email(), "--display-name", "X",
             "--project-slug", unique_slug()],
            environ={PASSWORD_ENV: PASSWORD},
        )  # fmt: skip

    assert "--project-slug and --project-name must be given together" in capsys.readouterr().err


def test_read_password_prompts_twice_and_compares() -> None:
    answers = iter(["typed passphrase", "typed passphrase"])
    assert read_password({}, interactive=True, prompt=lambda _: next(answers)) == "typed passphrase"

    mismatch = iter(["one passphrase", "other passphrase"])
    with pytest.raises(CliError, match="do not match"):
        read_password({}, interactive=True, prompt=lambda _: next(mismatch))


def test_read_password_prefers_the_environment() -> None:
    def no_prompt(_: str) -> str:
        raise AssertionError("must not prompt")

    assert read_password({PASSWORD_ENV: "from env"}, interactive=True, prompt=no_prompt) == (
        "from env"
    )


async def test_bootstrapped_operator_can_log_in_and_owns_the_project(api: Api) -> None:
    email, slug = unique_email("boot"), unique_slug("boot")
    result = await bootstrap_operator(
        api.factory,
        email=email.upper(),
        display_name="  Boot  ",
        password=PASSWORD,
        project_slug=slug,
        project_name="Boot Project",
        hasher=api.hasher,
        clock=api.clock,
    )
    assert result.project is not None
    assert (result.operator.email, result.operator.display_name) == (email, "Boot")

    client, _ = await logged_in(
        api, Operator(id=result.operator.id, email=email, password=PASSWORD)
    )
    async with aclosing(client):
        me = (await client.get("/v1/auth/me")).json()

    assert me["memberships"] == [
        {"project_id": result.project.id, "project_name": "Boot Project", "role": "owner"}
    ]


async def test_bootstrap_is_all_or_nothing(api: Api) -> None:
    taken = unique_slug("taken")
    first = await api.operator()
    client, csrf_token = await logged_in(api, first)
    async with aclosing(client):
        created = await client.post(
            "/v1/projects",
            json={"slug": taken, "name": "Taken"},
            headers={"X-CSRF-Token": csrf_token},
        )
    assert created.status_code == 201
    email = unique_email("atomic")

    with pytest.raises(Conflict):
        await bootstrap_operator(
            api.factory,
            email=email,
            display_name="Atomic",
            password=PASSWORD,
            project_slug=taken,
            project_name="Again",
            hasher=api.hasher,
            clock=api.clock,
        )
    # The operator insert was rolled back with the failed project: the email is still free.
    retry = await bootstrap_operator(
        api.factory, email=email, display_name="Atomic", password=PASSWORD, hasher=api.hasher
    )
    assert retry.operator.email == email


async def test_bootstrap_rejects_a_bad_display_name(api: Api) -> None:
    with pytest.raises(ValidationFailed) as info:
        await bootstrap_operator(
            api.factory, email=unique_email(), display_name="   ", password=PASSWORD
        )

    assert [error.pointer for error in info.value.errors] == ["/display_name"]
