"""Isolated test database (docs/TESTING.md).

Tests never touch the developer database (the one ``api``/``web``/``seed-demo`` use): at the start
of every run ``prepare_test_database`` derives ``<dev database>_test`` on the same server, creates
it when missing, migrates it to the head present in the working tree (as the owner login, so the
``fh_app`` roles and grants are exactly the migrations' ones) and points ``FH_DATABASE_URL`` and
``FH_APP_TEST_DATABASE_URL`` at it. ``FH_TEST_DB_RECREATE=1`` drops it first.
"""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from financial_hub.platform.settings import get_settings

OWNER_URL_ENV = "FH_DATABASE_URL"
APP_URL_ENV = "FH_APP_TEST_DATABASE_URL"
DEV_NAME_ENV = "FH_DEV_DATABASE_NAME"
RECREATE_ENV = "FH_TEST_DB_RECREATE"
APP_PASSWORD_ENV = "FH_APP_DB_PASSWORD"
TEST_SUFFIX = "_test"
BACKEND_DIR = Path(__file__).resolve().parent.parent


def test_database_name(dev_name: str) -> str:
    """The isolated database's name for ``dev_name`` (idempotent)."""
    return dev_name if dev_name.endswith(TEST_SUFFIX) else dev_name + TEST_SUFFIX


def _with_database(url: URL, name: str) -> URL:
    return url.set(database=name)


def _dsn(url: URL, database: str | None = None) -> str:
    return url.set(drivername="postgresql", database=database or url.database).render_as_string(
        hide_password=False
    )


def _ensure_database(owner: URL, test_name: str, *, recreate: bool) -> None:
    # Connect to the dev database (it exists: compose creates it) to issue CREATE/DROP DATABASE.
    with psycopg.connect(_dsn(owner), autocommit=True) as conn:
        if recreate:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(test_name))
            )
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (test_name,)
        ).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_name)))


def _migrate(env: dict[str, str]) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed on the test database:\n{done.stderr}")


def prepare_test_database() -> str | None:
    """Create/migrate the isolated database and redirect the environment; ``None`` if no server.

    A missing server is not an error here: unit tests do not need one, and tests that do will say
    so themselves (they never fall back to the developer database).
    """
    owner = make_url(os.environ[OWNER_URL_ENV])
    dev_name = owner.database or ""
    test_name = test_database_name(dev_name)
    recreate = os.environ.get(RECREATE_ENV) == "1"
    try:
        _ensure_database(owner, test_name, recreate=recreate)
    except psycopg.OperationalError:
        return None

    os.environ[DEV_NAME_ENV] = dev_name
    os.environ[OWNER_URL_ENV] = _with_database(owner, test_name).render_as_string(
        hide_password=False
    )
    app_raw = os.environ.get(APP_URL_ENV)
    app_password: str | None = None
    if app_raw:
        app = make_url(app_raw)
        app_password = app.password
        os.environ[APP_URL_ENV] = _with_database(app, test_name).render_as_string(
            hide_password=False
        )
    env = dict(os.environ)
    if app_password:
        env[APP_PASSWORD_ENV] = app_password  # migration 0001 sets the (shared) fh_app password
    _migrate(env)
    get_settings.cache_clear()  # conftests imported before this may already have read the dev URL
    return test_name
