"""A rewrite writes the table's data once unless something is really converted.

Verified live (2026-09-20): `CREATE OR REPLACE TABLE t … AS SELECT … FROM t` is
allowed — the table reads itself and is replaced in one statement, with every
row still there afterwards. So a rewrite that only moves the data around (new
partitioning, a rename, a dropped column) skips the staging copy, and writes
half as many bytes. A conversion keeps its staging table: it is the one thing a
rewrite can get quietly wrong, and staging is what lets the plan check it while
the original is still there.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-table-using
"""

from dataclasses import replace

from deltaplan.introspect import Introspector
from deltaplan.model.plan import Plan, Step
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.planner import STAGING_SUFFIX
from deltaplan.planning import plan_tables
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeWarehouse
from helpers import col, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
COLUMNS = (col("id", "bigint"), col("order_date", "date"), col("amount", "decimal(10,2)"))
PARTITIONED = replace(
    table(*COLUMNS, name=NAME, properties=MANAGED), partitioned_by=("order_date",)
)


def converge(desired: Table, fake: FakeWarehouse) -> Plan:
    def planned() -> Plan:
        return plan_tables([desired], Introspector(fake), target="t", tool_version="0")

    plan = planned()
    run(plan, fake)
    assert planned().empty, plan_text(planned())
    return plan


def replace_step(plan: Plan) -> Step:
    [step] = [s for s in plan.steps if s.title == "REPLACE TABLE"]
    return step


def staged(plan: Plan) -> list[str]:
    return [s.title for s in plan.steps if STAGING_SUFFIX in (s.sql or "")]


def test_a_partitioning_change_reads_the_table_and_replaces_it() -> None:
    fake = FakeWarehouse.of(PARTITIONED)
    desired = replace(table(*COLUMNS, name=NAME), cluster_by=("order_date",))
    plan = converge(desired, fake)
    assert staged(plan) == []
    sql = replace_step(plan).sql or ""
    assert sql.endswith("FROM `main`.`sales`.`orders`")
    assert "`amount` AS `amount`" in sql


def test_one_statement_means_the_data_is_written_once() -> None:
    """The cost the plan shows is the cost of one pass over the table."""
    fake = FakeWarehouse.of(replace(PARTITIONED, partitioned_by=("id",)))
    fake.sizes[NAME] = 4_000
    plan = converge(
        replace(table(*COLUMNS, name=NAME), partitioned_by=("order_date",)), fake
    )
    assert sum(step.est_bytes or 0 for step in plan.steps) == 4_000
    assert "written once" in (replace_step(plan).note or "")


def test_a_rename_is_a_copy_too() -> None:
    fake = FakeWarehouse.of(PARTITIONED)
    renamed = col("ordered_on", "date", renamed_from="order_date")
    desired = replace(
        table(COLUMNS[0], renamed, COLUMNS[2], name=NAME), partitioned_by=("ordered_on",)
    )
    plan = converge(desired, fake)
    assert staged(plan) == []
    assert "`order_date` AS `ordered_on`" in (replace_step(plan).sql or "")


def test_a_dropped_column_is_still_destructive_without_staging() -> None:
    fake = FakeWarehouse.of(PARTITIONED)
    desired = replace(table(*COLUMNS[:2], name=NAME), partitioned_by=())
    plan = converge(desired, fake)
    step = replace_step(plan)
    assert staged(plan) == []
    assert step.risk == "destructive"
    assert step.warnings and "amount" in step.warnings[0]
    assert step.undo_hint  # the restore point is the only way back


def test_a_type_conversion_keeps_its_staging_table() -> None:
    """Staging earns its second write: a cast is checked before the table moves."""
    fake = FakeWarehouse.of(PARTITIONED)
    desired = table(*COLUMNS[:2], col("amount", "string"), name=NAME)
    plan = converge(desired, fake)
    assert staged(plan) == ["STAGE rewritten data", "REPLACE TABLE", "DROP staging"]
    [stage] = [s for s in plan.steps if s.title == "STAGE rewritten data"]
    assert stage.postcheck and "count_if" in stage.postcheck


def test_a_using_expression_is_not_a_copy() -> None:
    """Same type, computed value: only `using:` knows whether it can go wrong."""
    fake = FakeWarehouse.of(PARTITIONED)
    computed = replace(COLUMNS[2], using="CAST(amount AS DECIMAL(10,2))")
    desired = replace(table(*COLUMNS[:2], computed, name=NAME), partitioned_by=("id",))
    plan = converge(desired, fake)
    assert "STAGE rewritten data" in staged(plan)
