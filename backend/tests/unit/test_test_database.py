"""The suite runs on its own database, never on the developer one (tests/testdb.py)."""

import os

import pytest
from sqlalchemy.engine import make_url

from tests import testdb
from tests.testdb import DEV_NAME_ENV, TEST_SUFFIX


def test_name_is_derived_and_idempotent() -> None:
    assert testdb.test_database_name("financial_hub") == "financial_hub_test"
    assert testdb.test_database_name("financial_hub_test") == "financial_hub_test"


def test_run_uses_a_database_different_from_the_dev_one() -> None:
    dev_name = os.environ.get(DEV_NAME_ENV)
    if dev_name is None:
        pytest.skip("no Postgres server reachable: the test database was not prepared")
    current = make_url(os.environ["FH_DATABASE_URL"]).database
    assert current is not None
    assert current != dev_name
    assert current.endswith(TEST_SUFFIX)
    app_url = os.environ.get("FH_APP_TEST_DATABASE_URL")
    if app_url:
        assert make_url(app_url).database == current
