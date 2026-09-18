"""Governance against a real workspace: what the fake can only assume.

Each test here settles a TODO(verify) in the code — that Unity Catalog accepts
the statement, and reads back what was written.

  tags        https://docs.databricks.com/aws/en/database-objects/tags
  grants      https://docs.databricks.com/aws/en/data-governance/unity-catalog/manage-privileges/privileges
  masks       https://docs.databricks.com/aws/en/tables/row-and-column-filters
  views       https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-view
  functions   https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-sql-function
"""

from __future__ import annotations

import os

import pytest

from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.model.function import Function, Parameter
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


def test_a_function_reads_back_as_its_spec(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """routines.routine_definition is the body as written, parameters come back
    in order with their types, and an EXECUTE grant reads back as direct —
    otherwise every plan would replace a function that hasn't changed."""
    principal = os.environ.get("DELTAPLAN_TEST_PRINCIPAL", "account users")
    function = Function(
        f"{schema}.price",
        (
            Parameter("amount", Primitive("double")),
            Parameter("rate", Primitive("double")),
        ),
        Primitive("double"),
        "amount * rate",
        comment="Amount at a rate",
        grants=(Grant(principal, ("EXECUTE",)),),
    )
    apply(planned([function], introspector), runner, introspector)
    assert planned([function], introspector).empty


def test_a_replaced_function_keeps_its_grants(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    principal = os.environ.get("DELTAPLAN_TEST_PRINCIPAL", "account users")
    first = Function(
        f"{schema}.label",
        (Parameter("value", Primitive("string")),),
        Primitive("string"),
        "upper(value)",
        grants=(Grant(principal, ("EXECUTE",)),),
    )
    apply(planned([first], introspector), runner, introspector)
    second = Function(
        first.name, first.parameters, first.returns, "lower(value)", grants=first.grants
    )
    plan = planned([second], introspector)
    assert plan.steps[0].title == "REPLACE FUNCTION label"
    apply(plan, runner, introspector)
    assert planned([second], introspector).empty


def test_a_function_and_the_mask_that_calls_it_in_one_plan(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    function = Function(
        f"{schema}.mask_value",
        (Parameter("value", Primitive("string")),),
        Primitive("string"),
        "'***'",
    )
    spec = Table(
        name=f"{schema}.masked",
        columns=(Field("value", Primitive("string"), mask=Mask(function.name)),),
    )
    apply(planned([spec, function], introspector), runner, introspector)
    assert planned([spec, function], introspector).empty


def test_quotes_in_strings_survive(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """Databricks reads `'It''s'` as two literals joined — `Its` — so doubling
    quotes silently dropped every apostrophe deltaplan wrote. Literals are
    backslash-escaped now; here is every place deltaplan writes one.
    https://docs.databricks.com/aws/en/sql/language-manual/data-types/string-type
    """
    from deltaplan.model.types import Struct

    spec = Table(
        name=f"{schema}.quoted",
        comment="It's the table's comment",
        columns=(
            Field("id", Primitive("int"), comment="the id's comment"),
            Field(
                "address",
                Struct((Field("street", Primitive("string"), comment="it's nested"),)),
            ),
        ),
        tags=(("owner", "o'brien"),),
        properties=(("note", "don't \\ panic"),),
    )
    apply(planned([spec], introspector), runner, introspector)
    again = planned([spec], introspector)
    assert again.empty, [
        (c.kind, c.path, c.before, c.after) for d in again.diffs for c in d.changes
    ]


def test_a_schema_spec_reads_back(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """A schema's comment, tags and grants round-trip through
    information_schema.schemata / schema_tags / schema_privileges.
    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-comment
    """
    from deltaplan.model.schema import Schema

    principal = os.environ.get("DELTAPLAN_TEST_PRINCIPAL", "account users")
    spec = Schema(
        schema,
        comment="It's the test schema",
        tags=(("domain", "testing"),),
        grants=(Grant(principal, ("USE SCHEMA", "CREATE TABLE")),),
    )
    apply(planned([spec], introspector), runner, introspector)
    again = planned([spec], introspector)
    assert again.empty, [
        (c.kind, c.path, c.after) for d in again.diffs for c in d.changes
    ]
