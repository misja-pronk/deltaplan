"""What deltaplan doesn't model is reported, left alone, and never rewritten away.

The design: "Features seen on a live table that the model does not cover are
shown as 'unmanaged feature, left untouched' — never diffed away." Partitioning is
such a feature. Ordinary ALTERs leave it be; a rewrite would not — it rebuilds the
table from a query — so a partitioned table is never rewritten.

(Identity, generated and default columns used to be here too; they are modelled
now, and tested in test_generation.py.)
"""

from deltaplan.introspect import Introspector
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.planning import plan_tables
from deltaplan.render.json import dumps, loads
from fake_warehouse import FakeWarehouse
from helpers import col, fake_runner, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
LIVE = table(
    col("id", "bigint"),
    col("order_date", "date"),
    col("amount", "decimal(10,2)"),
    name=NAME,
    properties=MANAGED,
)


def planned(desired: Table, fake: FakeWarehouse) -> Plan:
    return plan_tables([desired], Introspector(fake), target="test", tool_version="0")


def rewritten() -> Table:
    return table(
        col("id", "bigint"), col("order_date", "date"), col("amount", "string"), name=NAME
    )


def test_partitioning_is_reported_and_blocks_a_rewrite() -> None:
    fake = FakeWarehouse.of(LIVE)
    fake.partitions[NAME] = ("order_date",)
    plan = planned(rewritten(), fake)

    assert "partitioned by (order_date) (not modelled)" in plan.diffs[0].unmanaged
    assert [(s.title, s.sql) for s in plan.steps] == [("REWRITE", None)]
    assert "partitioned by (order_date)" in (plan.steps[0].note or "")


def test_ordinary_changes_still_go_ahead() -> None:
    # ALTERs don't touch partitioning, so a partitioned table is otherwise normal.
    fake = FakeWarehouse.of(LIVE)
    fake.partitions[NAME] = ("order_date",)
    plan = planned(table(*LIVE.columns, col("notes", "string"), name=NAME), fake)
    assert [s.title for s in plan.steps] == ["ADD COLUMN notes"]


def test_introspection_reads_partitioning() -> None:
    runner = fake_runner(
        tables=(
            {
                "table_name": "orders",
                "table_type": "MANAGED",
                "data_source_format": "DELTA",
            },
        ),
        columns=(
            {"table_name": "orders", "column_name": "day", "full_data_type": "date"},
        ),
        detail=({"partitionColumns": '["day"]', "properties": "{}"},),
    )
    live = Introspector(runner).schema("main", "sales").get(NAME)
    assert live is not None
    assert live.unmodelled == ("partitioned by (day)",)


def test_they_survive_the_plan_file() -> None:
    fake = FakeWarehouse.of(LIVE)
    fake.partitions[NAME] = ("order_date",)
    plan = planned(rewritten(), fake)
    assert loads(dumps(plan)) == plan
