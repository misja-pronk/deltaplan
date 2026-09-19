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
from dataclasses import replace

import pytest

from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.model.function import Function, Parameter
from deltaplan.model.plan import Plan
from deltaplan.model.table import ForeignKey, Grant, PrimaryKey, RowFilter, Table
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
    replace that isn't one. A view's properties come from SHOW TBLPROPERTIES,
    whose columns are `key` and `value`."""
    base = table(
        col("id", "bigint"), col("amount", "decimal(18,2)"), name=f"{schema}.orders"
    )
    view = View(
        f"{schema}.big_orders",
        f"SELECT id, amount FROM {quote_qualified(base.name)} WHERE amount > 1000",
        comment="Orders over 1000",
        properties=(("team", "sales"),),
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


def test_a_row_filter_set_at_creation_reads_back(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """CREATE TABLE puts WITH ROW FILTER after TBLPROPERTIES."""
    only_eu = Function(
        f"{schema}.only_eu",
        (Parameter("region", Primitive("string")),),
        Primitive("boolean"),
        "region = 'eu'",
    )
    spec = replace(
        table(col("id", "bigint"), col("region", "string"), name=f"{schema}.sales"),
        comment="Filtered",
        row_filter=RowFilter(only_eu.name, ("region",)),
    )
    plan = planned([spec, only_eu], introspector)
    [create] = [step for step in plan.steps if step.title == "CREATE TABLE sales"]
    assert "WITH ROW FILTER" in (create.sql or "")
    apply(plan, runner, introspector)
    assert planned([spec, only_eu], introspector).empty


def test_a_foreign_key_reads_back(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """Foreign keys come from referential_constraints, and the referenced key's
    columns from key_column_usage.
    https://docs.databricks.com/aws/en/sql/language-manual/information-schema/referential_constraints
    """
    customers = table(
        col("customer_id", "bigint", nullable=False),
        name=f"{schema}.customers",
        constraints=(PrimaryKey(("customer_id",), "customers_pk"),),
    )
    orders = table(
        col("order_id", "bigint"),
        col("customer_id", "bigint"),
        name=f"{schema}.orders",
        constraints=(
            ForeignKey(
                ("customer_id",), customers.name, ("customer_id",), "orders_customer_fk"
            ),
        ),
    )
    apply(planned([customers, orders], introspector), runner, introspector)
    live = introspector.table(orders.name)
    assert live is not None and live.table.foreign_keys() == orders.foreign_keys()
    assert planned([customers, orders], introspector).empty


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


def test_a_volume_spec_reads_back(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """A managed volume's comment, tags and grants round-trip through
    information_schema.volumes / volume_tags / volume_privileges.
    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-volume
    """
    from deltaplan.model.volume import Volume

    principal = os.environ.get("DELTAPLAN_TEST_PRINCIPAL", "account users")
    spec = Volume(
        f"{schema}.landing",
        comment="It's where files land",
        tags=(("domain", "testing"),),
        grants=(Grant(principal, ("READ VOLUME",)),),
    )
    apply(planned([spec], introspector), runner, introspector)
    again = planned([spec], introspector)
    assert again.empty, [
        (c.kind, c.path, c.after) for d in again.diffs for c in d.changes
    ]


def test_null_removes_a_tag_and_a_property(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """`tags: {pii: null}` plans UNSET TAGS / UNSET TBLPROPERTIES, and removing
    what isn't there is a no-op, so the plan converges and re-running is safe.
    https://docs.databricks.com/aws/en/database-objects/tags
    """
    email = col("email", "string")
    tagged = table(
        col("id", "bigint"),
        Field(email.name, email.type, tags=(("pii", "email"),)),
        name=f"{schema}.people",
        tags=(("domain", "crm"), ("legacy", "true")),
        properties=(("team", "crm"),),
    )
    apply(planned([tagged], introspector), runner, introspector)

    removed = replace(
        tagged,
        columns=(
            tagged.columns[0],
            Field(email.name, email.type, removed_tags=("pii",)),
        ),
        tags=(("domain", "crm"),),
        removed_tags=("legacy",),
        properties=(),
        removed_properties=("team",),
    )
    plan = planned([removed], introspector)
    assert {s.title for s in plan.steps} == {
        "UNSET TAGS",
        "UNSET COLUMN TAGS",
        "UNSET TBLPROPERTIES",
    }
    apply(plan, runner, introspector)
    assert planned([removed], introspector).empty
    live = introspector.table(tagged.name)
    assert live is not None
    assert dict(live.table.tags) == {"domain": "crm"}
    assert live.table.columns[1].tags == ()
    assert "team" not in live.table.properties_map()


def test_owners_are_set_and_survive_a_replace(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """`owner:` on all five kinds reads back from information_schema; a replaced
    view or function would belong to whoever replaced it, so the owner is put
    back after — here with a group the test principal belongs to, so it keeps
    the right to clean up.
    https://docs.databricks.com/aws/en/data-governance/unity-catalog/manage-privileges/ownership
    """
    from deltaplan.model.schema import Schema
    from deltaplan.model.volume import Volume

    owner = os.environ.get("DELTAPLAN_TEST_PRINCIPAL", "account users")
    base = replace(table(col("id", "bigint"), name=f"{schema}.orders"), owner=owner)
    view = View(
        f"{schema}.recent",
        f"SELECT id FROM {quote_qualified(base.name)}",
        owner=owner,
    )
    function = Function(
        f"{schema}.twice",
        (Parameter("x", Primitive("bigint")),),
        Primitive("bigint"),
        "x * 2",
        owner=owner,
    )
    specs: list[Relation] = [
        base,
        view,
        function,
        Volume(f"{schema}.landing", owner=owner),
        Schema(schema, owner=owner),
    ]
    apply(planned(specs, introspector), runner, introspector)
    assert planned(specs, introspector).empty

    replaced = replace(view, query=f"{view.query} WHERE id > 0")
    rewritten = replace(function, body="x * 3")
    changed = [
        replaced if s is view else rewritten if s is function else s for s in specs
    ]
    plan = planned(changed, introspector)
    assert [s.title for s in plan.steps].count("SET OWNER") == 2, "put back after both"
    apply(plan, runner, introspector)
    assert planned(changed, introspector).empty
