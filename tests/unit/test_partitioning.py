"""Partitioning: `partitioned_by: [day]`, and moving off it to liquid clustering.

Verified live (2026-09-19): DESCRIBE DETAIL's partitionColumns and SHOW CREATE
TABLE's PARTITIONED BY; Delta refuses a table that is both partitioned and
clustered, and `ALTER TABLE … CLUSTER BY` on a partitioned table; a replace
turns a partitioned table into a clustered one, while the way back needs
`CLUSTER BY NONE` first. The fake warehouse enforces all of it.
https://docs.databricks.com/aws/en/tables/partitions
"""

from dataclasses import replace
from pathlib import Path

import pytest

from deltaplan.introspect import Introspector
from deltaplan.loader import load_spec, validate_spec
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.planning import plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeSqlError, FakeWarehouse
from helpers import col, fake_runner, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
COLUMNS = (col("id", "bigint"), col("order_date", "date"), col("amount", "decimal(10,2)"))
PARTITIONED = replace(
    table(*COLUMNS, name=NAME, properties=MANAGED), partitioned_by=("order_date",)
)


def planned(desired: Table, fake: FakeWarehouse) -> Plan:
    return plan_tables([desired], Introspector(fake), target="t", tool_version="0")


def converge(desired: Table, fake: FakeWarehouse) -> Plan:
    plan = planned(desired, fake)
    run(plan, fake)
    assert planned(desired, fake).empty, plan_text(planned(desired, fake))
    return plan


def test_introspection_reads_the_partition_columns() -> None:
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
    assert live.table.partitioned_by == ("day",)
    assert live.unmodelled == ()


def test_a_table_is_created_partitioned() -> None:
    desired = replace(table(*COLUMNS, name=NAME), partitioned_by=("order_date",))
    fake = FakeWarehouse()
    plan = converge(desired, fake)
    [create] = [s for s in plan.steps if s.title == "CREATE TABLE orders"]
    assert "PARTITIONED BY (`order_date`)" in (create.sql or "")
    assert fake.tables[NAME].partitioned_by == ("order_date",)


def test_a_spec_that_does_not_say_leaves_it_alone() -> None:
    """So no plan rewrites a partitioned table because its spec predates
    `partitioned_by`."""
    fake = FakeWarehouse.of(PARTITIONED)
    desired = table(*COLUMNS, col("notes", "string"), name=NAME)
    assert [s.title for s in planned(desired, fake).steps] == ["ADD COLUMN notes"]


def test_a_rewrite_keeps_the_partitions_it_was_not_asked_to_change() -> None:
    fake = FakeWarehouse.of(PARTITIONED)
    desired = table(*COLUMNS[:2], col("amount", "string"), name=NAME)
    plan = converge(desired, fake)
    [replace_step] = [s for s in plan.steps if s.title == "REPLACE TABLE"]
    assert "PARTITIONED BY (`order_date`)" in (replace_step.sql or "")
    assert fake.tables[NAME].partitioned_by == ("order_date",)


def test_moving_to_liquid_clustering_is_a_rewrite() -> None:
    """The usual migration: take partitioning out, put clustering keys in."""
    fake = FakeWarehouse.of(PARTITIONED)
    desired = replace(table(*COLUMNS, name=NAME), cluster_by=("order_date",))
    plan = converge(desired, fake)
    assert "~ partitioned by (order_date) → none" in plan_text(plan)
    assert {s.risk for s in plan.steps} >= {"rewrite"}
    after = fake.tables[NAME]
    assert (after.partitioned_by, after.cluster_by) == (None, ("order_date",))


def test_partitioning_a_clustered_table_unclusters_it_first() -> None:
    fake = FakeWarehouse.of(
        replace(table(*COLUMNS, name=NAME, properties=MANAGED), cluster_by=("id",))
    )
    desired = replace(table(*COLUMNS, name=NAME), partitioned_by=("order_date",))
    plan = converge(desired, fake)
    titles = [s.title for s in plan.steps]
    assert titles.index("CLUSTER BY NONE") < titles.index("REPLACE TABLE")
    assert fake.tables[NAME].partitioned_by == ("order_date",)


def test_an_empty_list_takes_partitioning_away() -> None:
    fake = FakeWarehouse.of(PARTITIONED)
    converge(replace(table(*COLUMNS, name=NAME), partitioned_by=()), fake)
    assert fake.tables[NAME].partitioned_by is None


def test_a_partition_change_survives_the_plan_file() -> None:
    fake = FakeWarehouse.of(PARTITIONED)
    plan = planned(replace(table(*COLUMNS, name=NAME), partitioned_by=()), fake)
    assert loads(dumps(plan)) == plan


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------


def errors(spec: Table) -> list[str]:
    return [d.message for d in validate_spec(spec, "s.yml") if d.severity == "error"]


def test_partitioning_and_clustering_together_is_an_error() -> None:
    spec = replace(
        table(*COLUMNS, name=NAME), partitioned_by=("order_date",), cluster_by=("id",)
    )
    assert any("Delta takes one or the other" in e for e in errors(spec))


def test_a_partition_column_must_be_a_column() -> None:
    spec = replace(table(*COLUMNS, name=NAME), partitioned_by=("day",))
    assert errors(spec) == ["partitioned_by column 'day' is not in the spec"]


def test_the_spec_says_it_in_yaml_and_sql(tmp_path: Path) -> None:
    yaml_spec = tmp_path / "orders.yml"
    yaml_spec.write_text(
        "table: main.sales.orders\npartitioned_by: [day]\n"
        "columns:\n  - {name: day, type: date}\n"
    )
    sql_spec = tmp_path / "orders.sql"
    sql_spec.write_text(
        "CREATE TABLE main.sales.orders (day DATE) PARTITIONED BY (day);\n"
    )
    loaded = [load_spec(yaml_spec), load_spec(sql_spec)]
    assert [getattr(spec, "partitioned_by", None) for spec in loaded] == [
        ("day",),
        ("day",),
    ]


def test_the_fake_refuses_to_cluster_a_partitioned_table() -> None:
    with pytest.raises(FakeSqlError, match="CLUSTER_BY_ON_PARTITIONED_TABLE"):
        FakeWarehouse.of(PARTITIONED).query(
            "ALTER TABLE `main`.`sales`.`orders` CLUSTER BY (`id`)"
        )
