"""What Delta won't let a change touch — each found by dogfooding (2026-09-19).

- A column a CHECK uses can't change type, be renamed or be dropped
  (DELTA_CONSTRAINT_DEPENDENT_COLUMN_CHANGE): the CHECK is dropped first and put
  back after, as the spec has it.
- A column a generated column uses can't change type or be renamed
  (DELTA_GENERATED_COLUMNS_DEPENDENT_COLUMN_CHANGE), and a generated column
  can't be made again: refused, with the reason.
- A name with a space and the like needs column mapping
  (DELTA_INVALID_CHARACTERS_IN_COLUMN_NAMES), from creation or before the add.
- NOT NULL inside an array or map is refused (DELTA_NESTED_NOT_NULL_CONSTRAINT).

The fake warehouse enforces all four, so these plans are proven to run.
https://docs.databricks.com/aws/en/tables/constraints
"""

from dataclasses import replace

import pytest

from deltaplan.differ import diff
from deltaplan.introspect import Introspector
from deltaplan.loader import validate_spec
from deltaplan.model.table import MANAGED_PROPERTY, Check, Table
from deltaplan.model.types import Field, Primitive
from fake_warehouse import FakeSqlError, FakeWarehouse
from helpers import col, plan_against, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
POSITIVE = Check("positive", "amount >= 0 AND qty > 0")
LIVE = table(
    col("id", "bigint"),
    col("amount", "decimal(10,2)"),
    col("qty", "int"),
    col("note", "string"),
    name=NAME,
    properties=MANAGED,
    constraints=(POSITIVE,),
)


def converge(desired: Table, live: Table) -> list[str]:
    fake, plan = plan_against(desired, live)
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()
    return [step.title for step in plan.steps]


def test_widening_a_column_a_check_uses() -> None:
    wider = replace(
        LIVE,
        columns=(LIVE.columns[0], col("amount", "decimal(18,2)"), *LIVE.columns[2:]),
    )
    assert converge(wider, LIVE) == [
        "DROP CONSTRAINT positive",
        "enable typeWidening",
        "ALTER COLUMN TYPE",
        "ADD CONSTRAINT positive CHECK",
    ]


def test_renaming_a_column_a_check_uses() -> None:
    renamed = replace(
        LIVE,
        columns=(
            *LIVE.columns[:2],
            col("quantity", "int", renamed_from="qty"),
            LIVE.columns[3],
        ),
        constraints=(Check("positive", "amount >= 0 AND quantity > 0"),),
    )
    titles = converge(renamed, LIVE)
    assert titles.index("DROP CONSTRAINT positive") < titles.index("RENAME COLUMN")
    assert titles.index("RENAME COLUMN") < titles.index("ADD CONSTRAINT positive CHECK")


def test_dropping_a_column_a_check_uses() -> None:
    fewer = replace(
        LIVE,
        columns=(LIVE.columns[0], *LIVE.columns[2:]),
        constraints=(Check("positive", "qty > 0"),),
    )
    titles = converge(fewer, LIVE)
    assert titles.index("DROP CONSTRAINT positive") < titles.index("DROP COLUMN")


def test_a_check_that_does_not_use_the_column_is_left_alone() -> None:
    unrelated = replace(LIVE, constraints=(Check("has_id", "id IS NOT NULL"),))
    wider = replace(
        unrelated,
        columns=(LIVE.columns[0], col("amount", "decimal(18,2)"), *LIVE.columns[2:]),
    )
    assert converge(wider, unrelated) == ["enable typeWidening", "ALTER COLUMN TYPE"]


def test_a_change_a_generated_column_blocks_is_refused() -> None:
    live = replace(
        LIVE,
        columns=(
            *LIVE.columns,
            Field("doubled", Primitive("bigint"), generated="CAST(qty AS BIGINT) * 2"),
        ),
        constraints=(),
    )
    wider = replace(
        live, columns=(*live.columns[:2], col("qty", "bigint"), *live.columns[3:])
    )
    _, plan = plan_against(wider, live)
    [step] = [s for s in plan.steps if s.path == "qty"]
    assert step.sql is None
    assert step.note is not None and "doubled is generated from qty" in step.note


def test_a_new_table_with_a_space_in_a_name_gets_column_mapping() -> None:
    spaced = table(col("id", "bigint"), col("Display Name", "string"), name=NAME)
    fake, plan = plan_against(spaced, None)
    assert "'delta.columnMapping.mode' = 'name'" in (plan.steps[0].sql or "")
    run(plan, fake)


def test_adding_such_a_column_turns_column_mapping_on_first() -> None:
    plain = table(col("id", "bigint"), name=NAME, properties=MANAGED)
    spaced = table(col("id", "bigint"), col("Display Name", "string"), name=NAME)
    titles = converge(spaced, plain)
    assert titles == ["enable columnMapping", "ADD COLUMN Display Name"]


def test_a_rewrite_of_such_a_table_keeps_column_mapping() -> None:
    live = table(
        col("id", "bigint"),
        col("Display Name", "string"),
        name=NAME,
        properties=(*MANAGED, ("delta.columnMapping.mode", "name")),
    )
    converted = table(col("id", "string"), col("Display Name", "string"), name=NAME)
    fake, plan = plan_against(converted, live)
    stage = next(s for s in plan.steps if s.title == "STAGE rewritten data")
    assert "'delta.columnMapping.mode' = 'name'" in (stage.sql or "")
    run(plan, fake)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def messages(spec: Table) -> list[str]:
    return [d.message for d in validate_spec(spec, "s.yml") if d.severity == "error"]


@pytest.mark.parametrize(
    "type_",
    [
        "array<struct<sku:string not null>>",
        "map<string,struct<x:int not null>>",
    ],
)
def test_not_null_inside_an_array_or_map_is_an_error(type_: str) -> None:
    spec = table(col("id", "bigint"), col("lines", type_), name=NAME)
    [message] = messages(spec)
    assert "inside an array or map" in message


def test_not_null_in_a_plain_struct_is_fine() -> None:
    spec = table(col("shipping", "struct<street:string not null>"), name=NAME)
    assert messages(spec) == []


def test_a_check_on_a_column_the_spec_does_not_have_is_an_error() -> None:
    spec = replace(LIVE, constraints=(Check("positive", "amount >= 0 AND qty2 > 0"),))
    assert messages(spec) == ["check 'positive' uses 'qty2', which isn't a column"]


def test_a_check_on_a_nested_field_names_its_column() -> None:
    spec = table(
        col("shipping", "struct<zip:string>"),
        name=NAME,
        constraints=(Check("has_zip", "shipping.zip IS NOT NULL"),),
    )
    assert messages(spec) == []


def test_a_generated_column_on_a_missing_column_is_an_error() -> None:
    spec = table(
        Field("day", Primitive("date"), generated="CAST(placed_at AS DATE)"), name=NAME
    )
    assert messages(spec) == [
        "generated column 'day' uses 'placed_at', which isn't a column"
    ]


# ---------------------------------------------------------------------------
# the fake enforces them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `amount` TYPE DECIMAL(18,2)",
        "ALTER TABLE `main`.`sales`.`orders` RENAME COLUMN `qty` TO `quantity`",
        "ALTER TABLE `main`.`sales`.`orders` DROP COLUMN `qty`",
    ],
)
def test_the_fake_refuses_a_change_a_check_blocks(statement: str) -> None:
    with pytest.raises(FakeSqlError, match="DELTA_CONSTRAINT_DEPENDENT_COLUMN_CHANGE"):
        FakeWarehouse.of(LIVE).query(statement)


def test_the_fake_refuses_a_space_without_column_mapping() -> None:
    with pytest.raises(FakeSqlError, match="DELTA_INVALID_CHARACTERS_IN_COLUMN_NAMES"):
        FakeWarehouse.of(LIVE).query(
            "ALTER TABLE `main`.`sales`.`orders` ADD COLUMNS (`a b` STRING)"
        )
