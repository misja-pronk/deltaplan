"""Fixtures for the live suite.

Every test here runs against a real workspace in a schema created for the run and
dropped afterwards. Without credentials the whole suite skips, so a fork, a
laptop and CI all stay green.

Credentials come from the Databricks SDK's unified auth — DATABRICKS_HOST and a
token, a `~/.databrickscfg` profile named by DATABRICKS_CONFIG_PROFILE, OAuth.
Also set DATABRICKS_WAREHOUSE_ID, and optionally DELTAPLAN_TEST_CATALOG (default
`main`): every test creates and drops its own schema there.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.sql import quote_ident, quote_qualified


@pytest.fixture(scope="session")
def warehouse_id() -> str:
    value = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not value:
        pytest.skip("DATABRICKS_WAREHOUSE_ID is not set")
    return value


@pytest.fixture(scope="session")
def catalog() -> str:
    return os.environ.get("DELTAPLAN_TEST_CATALOG", "main")


@pytest.fixture(scope="session")
def runner(warehouse_id: str) -> WarehouseRunner:
    from databricks.sdk import WorkspaceClient

    # Not a check for DATABRICKS_HOST: a profile is just as good, and requiring
    # the variable would skip the whole suite for anyone who uses one.
    try:
        client = WorkspaceClient()
    except Exception as error:  # the SDK raises ValueError when nothing is configured
        pytest.skip(f"no Databricks workspace configured: {error}")
    return WarehouseRunner(client, warehouse_id)


#: How long a test schema may live before it can only be a leak. No test here
#: holds one for more than a couple of minutes.
STALE_AFTER = "30 MINUTES"


@pytest.fixture(scope="session", autouse=True)
def sweep(runner: WarehouseRunner, catalog: str) -> None:
    """Drop the schemas a killed run left behind.

    Every test drops its own schema when it finishes, but a cancelled CI run
    never gets to — a new push to a pull request cancels the suite mid-test.
    What is left keeps its tables, and Unity Catalog counts those against the
    metastore's quota (500 by default), so a few cancelled runs are enough to
    make every later run fail with QUOTA_EXCEEDED. This sweeps them first.
    https://docs.databricks.com/aws/en/data-governance/unity-catalog/index.html
    """
    try:
        stale = runner.query(
            f"SELECT schema_name FROM {quote_ident(catalog)}."
            "information_schema.schemata WHERE schema_name LIKE 'deltaplan_it_%' "
            f"AND created < current_timestamp() - INTERVAL {STALE_AFTER}"
        )
    except Exception as error:  # noqa: BLE001 - a sweep must never fail the suite
        print(f"could not look for stale test schemas: {error}")
        return
    for row in stale:
        name = row["schema_name"]
        if not name:
            continue
        print(f"dropping the stale test schema {catalog}.{name}")
        try:
            runner.query(f"DROP SCHEMA {quote_qualified(f'{catalog}.{name}')} CASCADE")
        except Exception as error:  # noqa: BLE001
            print(f"  could not drop it: {error}")


@pytest.fixture
def schema(runner: WarehouseRunner, catalog: str) -> Iterator[str]:
    """An empty schema, dropped with everything in it when the test finishes."""
    name = f"deltaplan_it_{uuid.uuid4().hex[:8]}"
    full = f"{catalog}.{name}"
    runner.query(f"CREATE SCHEMA {quote_qualified(full)}")
    try:
        yield full
    finally:
        runner.query(f"DROP SCHEMA {quote_qualified(full)} CASCADE")


@pytest.fixture
def introspector(runner: WarehouseRunner) -> Introspector:
    return Introspector(runner)
