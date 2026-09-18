"""Identity, generated and default columns: how a column gets a value it wasn't given.

Databricks treats them differently, and so does the plan:

* a default can be set, changed and dropped at any time, once the table has the
  allowColumnDefaults feature — which is a prerequisite step, like column mapping;
* identity and generated columns exist only from table creation, so CREATE TABLE
  has them and anything else is a step deltaplan won't run, with the reason;
* a rewrite carries defaults across, but a table with identity or generated
  columns is never rewritten — they would come back as plain columns.

https://docs.databricks.com/aws/en/delta/generated-columns
https://docs.databricks.com/aws/en/delta/default-columns
"""

from pathlib import Path

import pytest

from deltaplan.differ import diff
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_table, validate_table
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.model.types import Field, Identity, Primitive
from deltaplan.render.json import dumps, loads
from helpers import fake_runner, plan_against, run

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)


def bigint(name: str, *, identity: Identity | None = None) -> Field:
    return Field(name, Primitive("bigint"), identity=identity)


def string(
    name: str, *, default: str | None = None, generated: str | None = None
) -> Field:
    return Field(name, Primitive("string"), default=default, generated=generated)


def orders(*columns: Field, managed: bool = True) -> Table:
    return Table(name=NAME, columns=columns, properties=MANAGED if managed else ())


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------


def test_the_spec_declares_them(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: c.s.orders\n"
        "columns:\n"
        "  - {name: id, type: bigint, identity: always}\n"
        "  - name: seq\n"
        "    type: bigint\n"
        "    identity: {generated: by_default, start: 100, increment: 10}\n"
        "  - {name: ts, type: timestamp}\n"
        "  - {name: day, type: date, generated: CAST(ts AS DATE)}\n"
        "  - {name: status, type: string, default: \"'new'\"}\n"
    )
    loaded = load_table(path)
    columns = {c.name: c for c in loaded.columns}
    assert columns["id"].identity == Identity(always=True)
    assert columns["seq"].identity == Identity(always=False, start=100, increment=10)
    assert columns["day"].generated == "CAST(ts AS DATE)"
    assert columns["status"].default == "'new'"
    assert validate_table(loaded, "orders.yml") == ()


@pytest.mark.parametrize(
    ("column", "message"),
    [
        ("{name: id, type: int, identity: always}", "must be bigint"),
        ("{name: id, type: bigint, identity: always, default: '1'}", "one of identity"),
    ],
)
def test_lint(tmp_path: Path, column: str, message: str) -> None:
    path = tmp_path / "t.yml"
    path.write_text(f"table: c.s.t\ncolumns:\n  - {column}\n")
    assert any(message in d.message for d in validate_table(load_table(path), "t.yml"))


def test_a_bad_identity_is_a_load_error(tmp_path: Path) -> None:
    path = tmp_path / "t.yml"
    path.write_text(
        "table: c.s.t\ncolumns: [{name: id, type: bigint, identity: sometimes}]\n"
    )
    with pytest.raises(SpecError, match="'always' or 'by_default'"):
        load_table(path)


# ---------------------------------------------------------------------------
# creating
# ---------------------------------------------------------------------------


def test_create_table_has_them_all() -> None:
    desired = Table(
        name=NAME,
        columns=(
            bigint("id", identity=Identity(always=True, start=1, increment=1)),
            Field("ts", Primitive("timestamp")),
            Field("day", Primitive("date"), generated="CAST(ts AS DATE)"),
            string("status", default="'new'"),
        ),
    )
    fake, plan = plan_against(desired)
    sql = plan.steps[0].sql or ""
    assert "`id` BIGINT GENERATED ALWAYS AS IDENTITY (START WITH 1 INCREMENT BY 1)" in sql
    assert "`day` DATE GENERATED ALWAYS AS (CAST(ts AS DATE))" in sql
    assert "`status` STRING DEFAULT 'new'" in sql
    # Defaults need their table feature, from the start.
    assert "'delta.feature.allowColumnDefaults' = 'supported'" in sql
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()


# ---------------------------------------------------------------------------
# defaults change freely
# ---------------------------------------------------------------------------


def test_a_default_is_set_after_enabling_its_feature() -> None:
    live = orders(bigint("id"), string("status"))
    desired = orders(bigint("id"), string("status", default="'new'"), managed=False)
    fake, plan = plan_against(desired, live)
    assert [(s.title, s.risk) for s in plan.steps] == [
        ("enable allowColumnDefaults", "feature"),
        ("SET DEFAULT", "meta"),
    ]
    assert plan.steps[1].sql == (
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `status` SET DEFAULT 'new'"
    )
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()


def test_a_default_the_spec_leaves_out_is_dropped() -> None:
    # Modelled now, so a spec without one means the column has none — like a comment.
    live = orders(bigint("id"), string("status", default="'new'"))
    desired = orders(bigint("id"), string("status"), managed=False)
    _, plan = plan_against(desired, live)
    assert [s.sql for s in plan.steps] == [
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `status` DROP DEFAULT"
    ]


def test_a_new_column_with_a_default() -> None:
    live = orders(bigint("id"))
    desired = orders(bigint("id"), string("status", default="'new'"), managed=False)
    fake, plan = plan_against(desired, live)
    assert [s.title for s in plan.steps] == [
        "ADD COLUMN status",
        "enable allowColumnDefaults",
        "SET DEFAULT",
    ]
    run(plan, fake)


def test_default_expressions_compare_loosely() -> None:
    live = orders(string("status", default="('new')"))
    desired = orders(string("status", default="'new'"), managed=False)
    assert diff(desired, live) == ()


# ---------------------------------------------------------------------------
# identity and generated: creation only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("live_column", "desired_column", "what"),
    [
        (bigint("id"), bigint("id", identity=Identity()), "an identity"),
        (bigint("id", identity=Identity()), bigint("id"), "an identity"),
        (
            bigint("id", identity=Identity(start=1)),
            bigint("id", identity=Identity(start=100)),
            "an identity",
        ),
        (string("x"), string("x", generated="upper(y)"), "a generated column"),
    ],
)
def test_changing_generation_on_an_existing_column_is_refused(
    live_column: Field, desired_column: Field, what: str
) -> None:
    _, plan = plan_against(orders(desired_column, managed=False), orders(live_column))
    assert [(s.title, s.sql) for s in plan.steps] == [("CHANGE GENERATION", None)]
    assert what in (plan.steps[0].note or "")


def test_an_identity_column_cant_be_added_to_an_existing_table() -> None:
    _, plan = plan_against(
        orders(bigint("id"), bigint("seq", identity=Identity()), managed=False),
        orders(bigint("id")),
    )
    assert [(s.title, s.sql) for s in plan.steps] == [("CHANGE GENERATION", None)]


def test_a_table_with_identity_is_never_rewritten() -> None:
    live = orders(bigint("id", identity=Identity()), Field("amount", Primitive("int")))
    desired = orders(
        bigint("id", identity=Identity()),
        Field("amount", Primitive("string")),
        managed=False,
    )
    _, plan = plan_against(desired, live)
    assert [(s.title, s.sql) for s in plan.steps] == [("REWRITE", None)]
    assert "would come back as plain columns" in (plan.steps[0].note or "")


def test_a_rewrite_keeps_defaults() -> None:
    live = orders(string("status", default="'new'"), Field("amount", Primitive("int")))
    desired = orders(
        string("status", default="'new'"),
        Field("amount", Primitive("string")),
        managed=False,
    )
    fake, plan = plan_against(desired, live)
    assert "SET DEFAULT" in [s.title for s in plan.steps]
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()


# ---------------------------------------------------------------------------
# reading, writing, carrying
# ---------------------------------------------------------------------------


def test_introspection_reads_them() -> None:
    runner = fake_runner(
        tables=(
            {
                "table_name": "orders",
                "table_type": "MANAGED",
                "data_source_format": "DELTA",
            },
        ),
        columns=(
            {
                "table_name": "orders",
                "column_name": "id",
                "full_data_type": "bigint",
                "is_identity": "YES",
                "identity_generation": "BY DEFAULT",
                "identity_start": "100",
                "identity_increment": "10",
            },
            {
                "table_name": "orders",
                "column_name": "day",
                "full_data_type": "date",
                "is_generated": "ALWAYS",
                "generation_expression": "CAST(ts AS DATE)",
            },
            {
                "table_name": "orders",
                "column_name": "status",
                "full_data_type": "string",
                "column_default": "'new'",
            },
        ),
    )
    live = Introspector(runner).schema("main", "sales").get(NAME)
    assert live is not None
    columns = {c.name: c for c in live.table.columns}
    assert columns["id"].identity == Identity(always=False, start=100, increment=10)
    assert columns["day"].generated == "CAST(ts AS DATE)"
    assert columns["status"].default == "'new'"
    assert live.unmodelled == (), "all three are modelled now"


def test_import_writes_them_so_an_imported_spec_plans_nothing(tmp_path: Path) -> None:
    live = orders(
        bigint("id", identity=Identity(always=False, start=100, increment=10)),
        Field("day", Primitive("date"), generated="CAST(ts AS DATE)"),
        string("status", default="'new'"),
    )
    path = tmp_path / "orders.yml"
    path.write_text(dump_spec(live))
    assert diff(load_table(path), live) == ()


def test_they_survive_the_plan_file() -> None:
    live = orders(bigint("id"), string("status"))
    desired = orders(
        bigint("id", identity=Identity()),
        string("status", default="'new'"),
        managed=False,
    )
    _, plan = plan_against(desired, live)
    assert loads(dumps(plan)) == plan
