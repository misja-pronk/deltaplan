"""Column masks and row filters: security controls, handled like it.

* Additive: a mask or filter the spec declares is set (or replaced if it
  differs); one the spec doesn't mention is reported and left alone. deltaplan
  never removes a security control on its own initiative.
* A new table gets them inline in CREATE TABLE, so it never exists unprotected.
* An existing table gets them by ALTER, refused up front if the function is
  missing.
* A protected table is never rewritten: the staging copy would hold whatever the
  applying principal can see, in a table without the protection.

https://docs.databricks.com/aws/en/tables/row-and-column-filters
"""

from pathlib import Path

import pytest

from deltaplan.differ import diff, is_applied, unmanaged
from deltaplan.executor import ExecutionError, Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector
from deltaplan.loader import dump_spec, load_table, validate_table
from deltaplan.model.table import MANAGED_PROPERTY, RowFilter, Table
from deltaplan.model.types import Field, Mask, Primitive
from deltaplan.render.json import dumps, loads
from helpers import col, plan_against, run

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
SSN_MASK = Mask("main.security.mask_ssn")
REGION_FILTER = RowFilter("main.security.by_region", ("region",))


def ssn(mask: Mask | None = None) -> Field:
    return Field("ssn", Primitive("string"), mask=mask)


def orders(*, mask: Mask | None = None, row_filter: RowFilter | None = None) -> Table:
    return Table(
        name=NAME,
        columns=(col("id", "bigint"), col("region", "string"), ssn(mask)),
        properties=MANAGED,
        row_filter=row_filter,
    )


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------


def test_the_spec_declares_them(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: ${catalog}.sales.orders\n"
        "columns:\n"
        "  - {name: id, type: bigint}\n"
        "  - {name: region, type: string}\n"
        "  - name: ssn\n"
        "    type: string\n"
        "    mask: ${catalog}.security.mask_ssn\n"
        "  - name: email\n"
        "    type: string\n"
        "    mask:\n"
        "      function: ${catalog}.security.mask_email\n"
        "      using_columns: [region]\n"
        "row_filter:\n"
        "  function: ${catalog}.security.by_region\n"
        "  columns: [region]\n"
    )
    loaded = load_table(path, {"catalog": "main"})
    ssn_column, email_column = loaded.column("ssn"), loaded.column("email")
    assert ssn_column is not None and ssn_column.mask == SSN_MASK
    assert email_column is not None
    assert email_column.mask == Mask("main.security.mask_email", ("region",))
    assert loaded.row_filter == REGION_FILTER
    assert validate_table(loaded, "spec.yml") == ()


def test_a_function_must_be_fully_qualified(tmp_path: Path) -> None:
    from deltaplan.loader import SpecError

    path = tmp_path / "orders.yml"
    path.write_text("table: c.s.t\ncolumns: [{name: a, type: string, mask: mask_a}]\n")
    with pytest.raises(SpecError, match="catalog.schema.function"):
        load_table(path)


def test_columns_they_use_must_exist(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: c.s.t\n"
        "columns:\n"
        "  - {name: a, type: string, mask: {function: c.s.f, using_columns: [nope]}}\n"
        "row_filter: {function: c.s.g, columns: [gone]}\n"
    )
    messages = [d.message for d in validate_table(load_table(path), "spec.yml")]
    assert any("mask uses column 'nope'" in m for m in messages)
    assert any("row filter column 'gone'" in m for m in messages)


# ---------------------------------------------------------------------------
# diffing: additive, never weakening
# ---------------------------------------------------------------------------


def test_declared_controls_are_set() -> None:
    changes = diff(orders(mask=SSN_MASK, row_filter=REGION_FILTER), orders())
    assert [(c.kind, c.path) for c in changes] == [
        ("set_mask", "ssn"),
        ("set_row_filter", ""),
    ]


def test_a_changed_control_is_replaced() -> None:
    other = Mask("main.security.mask_ssn_v2")
    changes = diff(orders(mask=other), orders(mask=SSN_MASK))
    assert [(c.kind, c.before, c.after) for c in changes] == [
        ("set_mask", SSN_MASK, other)
    ]


def test_an_undeclared_control_is_never_removed() -> None:
    live = orders(mask=SSN_MASK, row_filter=REGION_FILTER)
    desired = orders()
    assert diff(desired, live) == ()
    assert unmanaged(desired, live) == ("row filter", "mask on column ssn")


# ---------------------------------------------------------------------------
# planning and applying
# ---------------------------------------------------------------------------


def test_sql_warnings_and_undo() -> None:
    fake, plan = plan_against(orders(mask=SSN_MASK, row_filter=REGION_FILTER), orders())
    mask_step, filter_step = plan.steps
    assert mask_step.sql == (
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `ssn` "
        "SET MASK `main`.`security`.`mask_ssn`"
    )
    assert mask_step.undo_hint == (
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `ssn` DROP MASK"
    )
    assert mask_step.warnings == (
        "readers see what main.security.mask_ssn returns for ssn, from now on",
    )
    assert filter_step.sql == (
        "ALTER TABLE `main`.`sales`.`orders` SET ROW FILTER "
        "`main`.`security`.`by_region` ON (`region`)"
    )
    assert filter_step.undo_hint == "ALTER TABLE `main`.`sales`.`orders` DROP ROW FILTER"

    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None
    assert diff(orders(mask=SSN_MASK, row_filter=REGION_FILTER), after.table) == ()
    for change in plan.changes:
        assert is_applied(change, after.table)


def test_a_missing_function_is_refused_before_anything_runs() -> None:
    fake, plan = plan_against(orders(mask=SSN_MASK), orders())
    assert plan.steps[0].precheck is not None
    assert "information_schema.routines" in plan.steps[0].precheck
    fake.blocked = True  # the function isn't there
    from dataclasses import replace

    from deltaplan.model.plan import fingerprint

    live = Introspector(fake).tables([NAME])
    plan = replace(plan, state_fingerprint=fingerprint(live.values()))
    result = Executor(fake, Introspector(fake), MemoryHistory()).apply(plan)
    assert not result.ok
    assert result.error == (
        "refused before running: the masking function main.security.mask_ssn "
        "does not exist"
    )
    assert fake.ddl == []


def test_a_new_table_is_never_unprotected() -> None:
    # Inline in CREATE TABLE: there is no moment the table exists without them.
    desired = Table(
        name=NAME,
        columns=(col("id", "bigint"), col("region", "string"), ssn(SSN_MASK)),
        row_filter=REGION_FILTER,
    )
    fake, plan = plan_against(desired)
    assert [s.title for s in plan.steps] == ["CREATE TABLE orders"]
    sql = plan.steps[0].sql or ""
    assert "`ssn` STRING MASK `main`.`security`.`mask_ssn`" in sql
    assert "WITH ROW FILTER `main`.`security`.`by_region` ON (`region`)" in sql
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()


def test_a_protected_table_is_never_rewritten() -> None:
    live = Table(
        name=NAME,
        columns=(col("amount", "decimal(10,2)"), ssn(SSN_MASK)),
        properties=MANAGED,
    )
    desired = Table(name=NAME, columns=(col("amount", "string"), ssn(SSN_MASK)))
    fake, plan = plan_against(desired, live)
    assert [(s.title, s.sql) for s in plan.steps] == [("REWRITE", None)]
    assert "without them" in (plan.steps[0].note or "")
    with pytest.raises(ExecutionError, match="can't run"):
        Executor(fake, Introspector(fake), MemoryHistory()).apply(plan)
    assert not any("__deltaplan_rewrite" in s for s in fake.statements)


def test_they_survive_the_plan_file_and_import() -> None:
    desired = orders(
        mask=Mask("main.security.mask_ssn", ("region",)), row_filter=REGION_FILTER
    )
    _, plan = plan_against(desired, orders())
    assert loads(dumps(plan)) == plan
    spec = dump_spec(desired)
    assert "function: main.security.by_region" in spec
    assert "using_columns:" in spec
