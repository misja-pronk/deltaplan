"""Table features a statement needs switched on first — each found live.

TIMESTAMP_NTZ: CREATE TABLE turns the timestampNtz feature on by itself, but
ALTER TABLE — adding a column, nested or not, or widening to one — fails with
DELTA_FEATURES_REQUIRE_MANUAL_ENABLEMENT until it is. Verified on a serverless
SQL warehouse, 2026-09-18.
https://docs.databricks.com/aws/en/sql/language-manual/data-types/timestamp-ntz-type

Table features are listed in DESCRIBE DETAIL's `tableFeatures`, not among its
properties — so a check for `delta.feature.<name>` in the properties never saw
one, and planned the step again every time. Also verified live.
"""

from dataclasses import replace

import pytest

from deltaplan.differ import diff
from deltaplan.introspect import Introspector
from deltaplan.model.plan import TableFacts
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.model.types import Field, Primitive
from deltaplan.render.json import dumps, loads
from fake_warehouse import FakeSqlError, FakeWarehouse
from helpers import col, plan_against, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
LIVE = table(col("id", "bigint"), col("placed", "date"), name=NAME, properties=MANAGED)


def converged(desired: Table, fake: FakeWarehouse) -> None:
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()


@pytest.mark.parametrize(
    "added",
    [
        col("shipped_at", "timestamp_ntz"),
        col("events", "array<struct<at:timestamp_ntz>>"),
    ],
    ids=["column", "nested"],
)
def test_adding_timestamp_ntz_enables_the_feature_first(added: Field) -> None:
    desired = table(*LIVE.columns, added, name=NAME)
    fake, plan = plan_against(desired, LIVE)
    assert [(s.title, s.risk) for s in plan.steps] == [
        ("enable timestampNtz", "feature"),
        (f"ADD COLUMN {added.name}", "meta"),
    ]
    assert plan.steps[0].sql == (
        "ALTER TABLE `main`.`sales`.`orders` SET TBLPROPERTIES "
        "('delta.feature.timestampNtz' = 'supported')"
    )
    run(plan, fake)
    converged(desired, fake)


def test_widening_to_timestamp_ntz_enables_both_features() -> None:
    desired = table(col("id", "bigint"), col("placed", "timestamp_ntz"), name=NAME)
    fake, plan = plan_against(desired, LIVE)
    assert [s.title for s in plan.steps] == [
        "enable typeWidening",
        "enable timestampNtz",
        "ALTER COLUMN TYPE",
    ]
    run(plan, fake)
    converged(desired, fake)


def test_the_fake_refuses_it_the_way_a_warehouse_does() -> None:
    fake = FakeWarehouse.of(LIVE)
    with pytest.raises(FakeSqlError, match="timestampNtz"):
        fake.query("ALTER TABLE `main`.`sales`.`orders` ADD COLUMNS (`t` TIMESTAMP_NTZ)")


def test_a_table_that_has_the_feature_gets_no_step() -> None:
    having = replace(
        LIVE, properties=(*LIVE.properties, ("delta.feature.timestampNtz", "supported"))
    )
    desired = table(*LIVE.columns, col("shipped_at", "timestamp_ntz"), name=NAME)
    _, plan = plan_against(desired, having)
    assert [s.title for s in plan.steps] == ["ADD COLUMN shipped_at"]


def test_a_new_table_needs_no_step() -> None:
    desired = table(col("id", "bigint"), col("at", "timestamp_ntz"), name=NAME)
    fake, plan = plan_against(desired, None)
    assert [s.title for s in plan.steps] == ["CREATE TABLE orders"]
    run(plan, fake)
    detail = fake.query("DESCRIBE DETAIL `main`.`sales`.`orders`")[0]
    assert "timestampNtz" in (detail["tableFeatures"] or "")


def test_features_come_from_table_features_not_properties() -> None:
    """A column default needs allowColumnDefaults; a table listing it in
    tableFeatures has it, whatever its properties say."""
    having = replace(
        LIVE,
        properties=(*LIVE.properties, ("delta.feature.allowColumnDefaults", "supported")),
    )
    fake = FakeWarehouse.of(having)
    live = Introspector(fake).table(NAME)
    assert live is not None
    assert "allowColumnDefaults" in live.features
    assert "delta.feature.allowColumnDefaults" not in live.table.properties_map()

    desired = table(
        *LIVE.columns,
        name=NAME,
    )
    defaulted = replace(
        desired,
        columns=(
            desired.columns[0],
            Field("placed", Primitive("date"), default="current_date()"),
        ),
    )
    _, plan = plan_against(defaulted, having)
    assert "enable allowColumnDefaults" not in [s.title for s in plan.steps]


def test_facts_survive_the_plan_file() -> None:
    desired = table(*LIVE.columns, col("shipped_at", "timestamp_ntz"), name=NAME)
    _, plan = plan_against(desired, LIVE)
    moved = replace(
        plan,
        diffs=(
            replace(
                plan.diffs[0],
                facts=replace(
                    plan.diffs[0].facts, schema_exists=False, features=("clustering",)
                ),
            ),
        ),
    )
    back = loads(dumps(moved))
    assert back.diffs[0].facts == moved.diffs[0].facts
    assert isinstance(back.diffs[0].facts, TableFacts)
