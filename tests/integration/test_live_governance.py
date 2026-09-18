"""Governance against a real workspace: what the fake can only assume.

Each test here settles a TODO(verify) in the code — that Unity Catalog accepts
the statement, and reads back what was written.

  tags        https://docs.databricks.com/aws/en/database-objects/tags
  grants      https://docs.databricks.com/aws/en/data-governance/unity-catalog/manage-privileges/privileges
  masks       https://docs.databricks.com/aws/en/tables/row-and-column-filters
  views       https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-view
"""

from __future__ import annotations

import os

import pytest

from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.model.plan import Plan
from deltaplan.model.table import Grant, Table
from deltaplan.model.types import Field, Mask, Primitive
from deltaplan.model.view import Relation, View
from deltaplan.planning import plan_tables
from deltaplan.sql import quote_qualified
from helpers import col, table

pytestmark = pytest.mark.integration


def planned(specs: list[Relation], introspector: Introspector) -> Plan:
    return plan_tables(specs, introspector, target="integration", tool_version="0.1.0")


def apply(plan: Plan, runner: WarehouseRunner, introspector: Introspector) -> None:
    result = Executor(runner, introspector, MemoryHistory()).apply(plan)
    assert result.ok, result.error


def test_column_tags_and_table_tags_read_back(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    email = col("email", "string")
    spec = table(
        col("id", "bigint"),
        Field(email.name, email.type, tags=(("pii", "email"),)),
        name=f"{schema}.people",
        tags=(("domain", "crm"),),
    )
    apply(planned([spec], introspector), runner, introspector)
    assert planned([spec], introspector).empty, "tags must read back as written"


def test_grants_read_back(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    principal = os.environ.get("DELTAPLAN_TEST_PRINCIPAL", "account users")
    spec = table(
        col("id", "bigint"),
        name=f"{schema}.granted",
        grants=(Grant(principal, ("SELECT",)),),
    )
    apply(planned([spec], introspector), runner, introspector)
    assert planned([spec], introspector).empty, "a grant must read back as direct"


def test_a_view_reads_back_as_its_spec(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """The load-bearing assumption behind view diffs: view_definition is the
    query as written. If Unity Catalog rewrote it, every plan would show a
    replace that isn't one."""
    base = table(
        col("id", "bigint"), col("amount", "decimal(18,2)"), name=f"{schema}.orders"
    )
    view = View(
        f"{schema}.big_orders",
        f"SELECT id, amount FROM {quote_qualified(base.name)} WHERE amount > 1000",
        comment="Orders over 1000",
        tags=(("domain", "sales"),),
    )
    apply(planned([base, view], introspector), runner, introspector)
    assert planned([base, view], introspector).empty


def test_a_mask_is_set_and_reads_back(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    function = f"{schema}.mask_all"
    runner.query(
        f"CREATE OR REPLACE FUNCTION {quote_qualified(function)}(value STRING) "
        "RETURNS STRING RETURN '***'"
    )
    spec = Table(
        name=f"{schema}.secrets",
        columns=(Field("value", Primitive("string"), mask=Mask(function)),),
    )
    apply(planned([spec], introspector), runner, introspector)
    assert planned([spec], introspector).empty, "the mask must read back"
