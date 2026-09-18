"""Names follow the catalog's case rules, or deltaplan plans the wrong thing.

Unity Catalog stores catalog, schema, table, view and function names in lower
case, whatever case they were written in. Delta keeps the case a column was
written in, but resolves column and field names ignoring it, and won't hold two
that differ only by case.

Getting this wrong isn't cosmetic. A spec that says `Orders` for the live
`orders`, in a strict schema, planned creating `Orders` — a no-op, because it
resolves to the existing table — and dropping `orders`, the real one.

https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-names
"""

from pathlib import Path

from deltaplan.differ import diff
from deltaplan.introspect import Introspector
from deltaplan.loader import load_project, load_table, validate_table
from deltaplan.model.table import MANAGED_PROPERTY, RowFilter, Table
from deltaplan.model.types import Field, Mask, Primitive
from deltaplan.model.view import View
from deltaplan.planning import plan_tables
from fake_warehouse import FakeWarehouse
from helpers import col, plan_against, run, table

MANAGED = ((MANAGED_PROPERTY, "true"),)
LIVE = table(
    col("id", "bigint"),
    col("amount", "decimal(18,2)"),
    name="main.sales.orders",
    properties=MANAGED,
)


def test_a_differently_cased_spec_never_drops_the_live_table() -> None:
    spec = table(
        col("id", "bigint"), col("amount", "decimal(18,2)"), name="Main.Sales.Orders"
    )
    plan = plan_tables(
        [spec],
        Introspector(FakeWarehouse.of(LIVE)),
        target="prod",
        tool_version="0",
        mode_for=lambda _schema: "strict",
    )
    assert plan.empty, [(s.title, s.table) for s in plan.steps]
    assert plan.summary.destroy == 0


def test_object_names_are_stored_lower_case() -> None:
    assert (
        table(col("id", "bigint"), name="Main.Sales.Orders").name == "main.sales.orders"
    )
    assert View("Main.Sales.Big", "SELECT 1").name == "main.sales.big"
    assert Mask("Main.Security.Mask_SSN").function == "main.security.mask_ssn"
    assert RowFilter("Main.Security.By_Region", ("region",)).function == (
        "main.security.by_region"
    )


def test_a_column_differing_only_by_case_is_the_same_column() -> None:
    # Not an add of `Amount` and a drop of `amount`: Delta would refuse the add,
    # and the drop would lose the data.
    spec = table(
        col("ID", "bigint"), col("Amount", "decimal(18,2)"), name="main.sales.orders"
    )
    assert diff(spec, LIVE) == ()


def test_changes_to_a_differently_cased_column_still_apply() -> None:
    spec = table(
        col("id", "bigint"),
        col("Amount", "decimal(18,2)", comment="In euros"),
        name="main.sales.orders",
    )
    fake, plan = plan_against(spec, LIVE)
    assert [c.kind for c in plan.changes] == ["set_comment"]
    run(plan, fake)
    after = Introspector(fake).table("main.sales.orders")
    assert after is not None and diff(spec, after.table) == ()


def test_nested_fields_ignore_case_too() -> None:
    live = table(col("address", "struct<street:string,zip:string>"), name="main.s.t")
    spec = table(col("address", "struct<Street:string,ZIP:string>"), name="main.s.t")
    assert diff(spec, live) == ()


def test_a_rename_hint_ignores_case() -> None:
    live = table(col("Cust_ID", "string"), name="main.s.t")
    spec = table(
        Field("customer_ref", Primitive("string"), renamed_from="cust_id"),
        name="main.s.t",
    )
    changes = diff(spec, live)
    assert [(c.kind, c.before) for c in changes] == [("rename_column", "Cust_ID")]


def test_a_mask_function_in_another_case_converges() -> None:
    live = Table(
        name="main.s.t",
        columns=(Field("ssn", Primitive("string"), mask=Mask("main.security.mask_ssn")),),
        properties=MANAGED,
    )
    spec = Table(
        name="main.s.t",
        columns=(Field("ssn", Primitive("string"), mask=Mask("Main.Security.Mask_SSN")),),
    )
    assert diff(spec, live) == ()


def test_columns_that_differ_only_by_case_are_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "t.yml"
    path.write_text(
        "table: c.s.t\ncolumns:\n  - {name: id, type: int}\n  - {name: ID, type: int}\n"
    )
    messages = [d.message for d in validate_table(load_table(path), "t.yml")]
    assert any("duplicate column 'ID'" in m for m in messages)


def test_strict_schema_patterns_ignore_case(tmp_path: Path) -> None:
    (tmp_path / "deltaplan.yml").write_text(
        "targets:\n  prod:\n    vars: {catalog: Main}\n"
        "schemas:\n  ${catalog}.Sales: strict\n"
    )
    project = load_project(tmp_path / "deltaplan.yml")
    assert project.mode_for(project.target("prod"), "main.sales") == "strict"
