"""Administrative command line: ``python -m financial_hub.cli <command>``.

``seed-demo`` creates the idempotent demo workspace (``cli.demo``). ``create-operator`` bootstraps
an operator (and optionally a first project it owns). The password
is read from ``FH_BOOTSTRAP_PASSWORD`` or an interactive prompt, never from argv, where it would
leak into shell history and process listings.
"""

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TextIO

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.cli.demo import DemoSeedError, SeedResult, seed_demo
from financial_hub.modules.identity.application.service import (
    OperatorView,
    PasswordHasher,
    create_operator,
    default_password_hasher,
)
from financial_hub.modules.projects.application.service import ProjectService, ProjectView
from financial_hub.platform.db import UnitOfWork, create_engine, create_session_factory
from financial_hub.platform.settings import get_settings
from financial_hub.shared.clock import Clock, SystemClock
from financial_hub.shared.errors import DomainError
from financial_hub.shared.ids import IdPrefix, parse_id

PASSWORD_ENV: Final = "FH_BOOTSTRAP_PASSWORD"  # noqa: S105 - the variable's name, not a secret


class CliError(Exception):
    """A user-facing error: printed without a traceback, exit status 1."""


@dataclass(frozen=True, slots=True)
class Bootstrap:
    operator: OperatorView
    project: ProjectView | None


def read_password(
    environ: Mapping[str, str],
    *,
    interactive: bool,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    """``FH_BOOTSTRAP_PASSWORD`` if set, else a confirmed prompt on a terminal."""
    password = environ.get(PASSWORD_ENV)
    if password:
        return password
    if not interactive:
        raise CliError(f"set {PASSWORD_ENV} or run the command in an interactive terminal")
    password = prompt("Password: ")
    if prompt("Repeat password: ") != password:
        raise CliError("passwords do not match")
    return password


async def bootstrap_operator(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email: str,
    display_name: str,
    password: str,
    project_slug: str | None = None,
    project_name: str | None = None,
    hasher: PasswordHasher | None = None,
    clock: Clock | None = None,
) -> Bootstrap:
    """Operator and optional owned project in one transaction (all or nothing)."""
    async with UnitOfWork(session_factory) as uow:
        operator = await create_operator(
            uow,
            hasher=hasher or default_password_hasher(),
            clock=clock or SystemClock(),
            email=email,
            display_name=display_name,
            password=password,
        )
        project = None
        if project_slug is not None and project_name is not None:
            project = await ProjectService(uow).create(
                owner_operator_id=parse_id(operator.id, IdPrefix.OPERATOR),
                slug=project_slug,
                name=project_name,
            )
    return Bootstrap(operator=operator, project=project)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m financial_hub.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create-operator",
        help=f"create an operator (password from ${PASSWORD_ENV} or a prompt)",
    )
    create.add_argument("--email", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--project-slug", help="also create this project, owned by the operator")
    create.add_argument("--project-name", help="name of the project (with --project-slug)")
    commands.add_parser(
        "seed-demo",
        help="create the demo project and one operator per role (idempotent)",
    )
    return parser


async def _run_create_operator(args: argparse.Namespace, password: str) -> Bootstrap:
    engine = create_engine(get_settings())
    try:
        return await bootstrap_operator(
            create_session_factory(engine),
            email=args.email,
            display_name=args.display_name,
            password=password,
            project_slug=args.project_slug,
            project_name=args.project_name,
        )
    finally:
        await engine.dispose()


def _describe(error: DomainError) -> str:
    lines = [str(error.detail or error.title)]
    lines.extend(f"  {item.pointer.lstrip('/')}: {item.detail}" for item in error.errors)
    return "\n".join(lines)


async def _run_seed_demo() -> SeedResult:
    engine = create_engine(get_settings())
    try:
        return await seed_demo(create_session_factory(engine))
    finally:
        await engine.dispose()


def _seed_demo(out: TextIO, err: TextIO) -> int:
    try:
        result = asyncio.run(_run_seed_demo())
    except DemoSeedError as error:
        print(f"error: {error}", file=err)
        return 1
    except DomainError as error:
        print(f"error: {_describe(error)}", file=err)
        return 1
    if not result.changed:
        print(
            "demo workspace already seeded; nothing to do "
            f"(customers={result.customers_present}, payments={result.payments_present}, "
            f"api keys={result.api_keys_present})",
            file=out,
        )
        return 0
    print(
        f"seeded demo workspace: project created={result.project_created}, "
        f"operators created={result.operators_created}, "
        f"memberships created={result.memberships_created}, "
        f"customers created={result.customers_created}, "
        f"payments created={result.payments_created}, "
        f"api key created={result.api_key_created}",
        file=out,
    )
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "seed-demo":
        return _seed_demo(out, err)
    if (args.project_slug is None) != (args.project_name is None):
        parser.error("--project-slug and --project-name must be given together")
    try:
        password = read_password(
            os.environ if environ is None else environ, interactive=sys.stdin.isatty()
        )
        result = asyncio.run(_run_create_operator(args, password))
    except CliError as error:
        print(f"error: {error}", file=err)
        return 1
    except DomainError as error:
        print(f"error: {_describe(error)}", file=err)
        return 1
    print(f"created operator {result.operator.id} <{result.operator.email}>", file=out)
    if result.project is not None:
        print(
            f"created project {result.project.id} ({result.project.slug}); role owner",
            file=out,
        )
    return 0
