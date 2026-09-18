"""Filling a new column, and running SQL around a table's changes.

The case that needs it: adding a NOT NULL column to a table that has rows. The
column arrives empty, so SET NOT NULL can only fail. `using:` — already "how to
get this column's value from the rest of the row" for rewrites — fills the rows
that are there, in between.

Table `hooks:` are the design's "simple pre/post SQL hooks": the escape hatch for
what a spec can't say. They run only when the table has changes in the plan.
"""

from pathlib import Path

from deltaplan.differ import diff
from deltaplan.introspect import Introspector
from deltaplan.loader import load_table
from deltaplan.model.table import MANAGED_PROPERTY, Hooks, Table
from deltaplan.model.types import Field, Primitive
from deltaplan.render.rich import plan_text
from helpers import col, plan_against, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
LIVE = table(col("id", "bigint"), col("country", "string"), name=NAME, properties=MANAGED)


def region(*, nullable: bool = True, using: str | None = None) -> Field:
    return Field("region", Primitive("string"), nullable=nullable, using=using)


def test_a_not_null_column_is_added_filled_then_constrained() -> None:
    desired = table(
        *LIVE.columns,
        region(nullable=False, using="coalesce(country, 'unknown')"),
        name=NAME,
    )
    fake, plan = plan_against(desired, LIVE, size_bytes=1_000)
    assert [(s.title, s.risk) for s in plan.steps] == [
        ("ADD COLUMN region", "meta"),
        ("BACKFILL region", "rewrite"),
        ("SET NOT NULL", "meta"),
    ]
    assert plan.steps[1].sql == (
        "UPDATE `main`.`sales`.`orders` SET `region` = coalesce(country, 'unknown') "
        "WHERE `region` IS NULL"
    )
    # Filled first, so the NOT NULL step no longer warns that it will fail.
    assert plan.steps[2].warnings == ()
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()


def test_without_using_the_plan_says_what_is_missing() -> None:
    desired = table(*LIVE.columns, region(nullable=False), name=NAME)
    _, plan = plan_against(desired, LIVE)
    assert "give the column a `using:` expression" in plan.steps[1].warnings[0]


def test_a_nullable_column_can_be_backfilled_too() -> None:
    desired = table(*LIVE.columns, region(using="upper(country)"), name=NAME)
    _, plan = plan_against(desired, LIVE)
    assert [s.title for s in plan.steps] == ["ADD COLUMN region", "BACKFILL region"]


def test_a_new_table_has_nothing_to_backfill() -> None:
    desired = table(col("id", "bigint"), region(nullable=False, using="'x'"), name=NAME)
    _, plan = plan_against(desired)
    assert "BACKFILL" not in " ".join(s.title for s in plan.steps)


def test_hooks_run_around_a_tables_changes() -> None:
    desired = Table(
        name=NAME,
        columns=(*LIVE.columns, col("notes", "string")),
        hooks=Hooks(
            before="DELETE FROM main.sales.orders WHERE id IS NULL",
            after="UPDATE main.sales.orders SET notes = '' WHERE notes IS NULL;",
        ),
    )
    fake, plan = plan_against(desired, LIVE)
    assert [s.title for s in plan.steps] == [
        "BEFORE hook",
        "ADD COLUMN notes",
        "AFTER hook",
    ]
    assert plan.steps[0].warnings == (
        "runs your SQL as written — deltaplan can't tell what it does",
    )
    assert (
        plan.steps[2].sql == "UPDATE main.sales.orders SET notes = '' WHERE notes IS NULL"
    )
    assert "↻ hooks" in plan_text(plan)
    run(plan, fake)


def test_hooks_dont_run_when_the_table_has_nothing_to_do() -> None:
    desired = Table(name=NAME, columns=LIVE.columns, hooks=Hooks(before="SELECT 1"))
    _, plan = plan_against(desired, LIVE)
    assert plan.empty


def test_hooks_are_not_state() -> None:
    # Nothing in the catalog records a hook, so it can't make a spec differ from
    # the table it describes.
    with_hooks = Table(name=NAME, columns=LIVE.columns, hooks=Hooks(after="SELECT 1"))
    assert with_hooks == Table(name=NAME, columns=LIVE.columns)


def test_hooks_in_a_spec(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: ${catalog}.sales.orders\n"
        "columns: [{name: id, type: bigint}]\n"
        "hooks:\n"
        "  before: DELETE FROM ${catalog}.sales.orders WHERE id IS NULL\n"
    )
    loaded = load_table(path, {"catalog": "main"})
    assert loaded.hooks == Hooks(before="DELETE FROM main.sales.orders WHERE id IS NULL")
