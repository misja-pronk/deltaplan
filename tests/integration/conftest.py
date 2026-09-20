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
import time
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
    recount(runner)


def recount(runner: WarehouseRunner, *, wait_seconds: float = 90) -> None:
    """Ask Unity Catalog to count the metastore's tables again.

    The quota count only goes up by itself: "Unity Catalog updates the quota
    count only during resource creation. The count might be out of date if only
    delete operations have been performed." A suite that creates and drops
    hundreds of tables in a day therefore meets QUOTA_EXCEEDED on a metastore
    that is nearly empty — 523 of 500, with ninety tables actually there.

    Reading the quota is what triggers a refresh, and the refresh is
    asynchronous ("new counts might not be returned in the first call"), so it
    is read until the number settles or the wait runs out. Never fatal: if the
    count really is at the limit, the test that needs a table says so.
    https://docs.databricks.com/aws/en/data-governance/unity-catalog/resource-quotas
    """
    try:
        metastore = runner.client.metastores.current().metastore_id
        if metastore is None:
            return
        deadline = time.monotonic() + wait_seconds
        while True:
            quota = runner.client.resource_quotas.get_quota(
                parent_securable_type="metastore",
                parent_full_name=metastore,
                quota_name="table-quota",
            ).quota_info
            if quota is None:
                return
            count, limit = quota.quota_count, quota.quota_limit
            print(f"the metastore counts {count} tables of {limit}")
            if count is None or limit is None or count < limit:
                return
            if time.monotonic() > deadline:
                print("  still at the limit; the tests will say what that costs")
                return
            time.sleep(10)
    except Exception as error:  # noqa: BLE001 - a recount must never fail the suite
        print(f"could not ask for a recount: {error}")


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
