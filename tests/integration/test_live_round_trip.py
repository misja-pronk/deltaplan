"""What Databricks actually does — the only place that can settle it.

Each test states an assumption deltaplan makes, so a runtime change shows up as
a failing test rather than a failing `apply`.

  column mapping  https://docs.databricks.com/aws/en/delta/column-mapping
  type widening   https://docs.databricks.com/aws/en/delta/type-widening
  ALTER TABLE     https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table
"""

from __future__ import annotations

import pytest

from deltaplan.differ import diff
from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.model.plan import Plan, TableDiff, TableFacts
from deltaplan.model.table import Check, PrimaryKey, Table
from deltaplan.planner import build_plan, create_table_sql
from deltaplan.sql import quote_qualified
from helpers import col, table

pytestmark = pytest.mark.integration


def plan_for(desired: Table, live: Table | None, facts: TableFacts) -> Plan:
    return build_plan(
        [TableDiff(desired.name, diff(desired, live), facts, desired=desired, live=live)],
        target="integration",
        tool_version="0.1.0",
        spec_hash="spec",
        state_fingerprint="live",
    )


def run_plan(plan: Plan, runner: WarehouseRunner) -> None:
    for step in plan.steps:
        assert step.sql is not None, f"step {step.id} ({step.title}) has no SQL"
        runner.query(step.sql)


def live_table(introspector: Introspector, name: str) -> tuple[Table, TableFacts]:
    live = introspector.table(name)
    assert live is not None, f"{name} was not found after creating it"
    return live.table, TableFacts(
        name,
        exists=True,
        properties=live.table.properties,
        size_bytes=live.size_bytes,
        features=live.features,
    )


def test_a_created_table_reads_back_as_the_spec_that_made_it(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """CREATE TABLE, introspection and the differ agree on the same model."""
    desired = table(
        col("order_id", "bigint", nullable=False, comment="Surrogate key"),
        col("amount", "decimal(10,2)"),
        col("address", "struct<street:string,zip:string>"),
        col("lines", "array<struct<sku:string,qty:int>>"),
        col("by_code", "map<string,int>"),
        name=f"{schema}.orders",
        comment="Order facts",
        cluster_by=("order_id",),
        properties=(("delta.enableChangeDataFeed", "true"),),
        constraints=(PrimaryKey(("order_id",), "orders_pk"),),
    )
    runner.query(create_table_sql(desired))

    live, _ = live_table(introspector, desired.name)
    assert live.managed, "CREATE TABLE must mark the table as deltaplan-managed"
    assert diff(desired, live) == (), "a fresh table should need no changes"


def test_metadata_changes_converge(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """Plan -> run -> re-plan is empty, for every meta and feature step.

    This is the assumption the whole tool rests on: what the planner emits is
    both accepted by Databricks and enough to close the diff.
    """
    original = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "decimal(10,2)"),
        col("cust_id", "string"),
        col("address", "struct<street:string>"),
        col("legacy_flag", "boolean"),
        name=f"{schema}.orders",
        comment="Order facts",
    )
    runner.query(create_table_sql(original))

    desired = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "decimal(18,2)"),  # widening -> typeWidening + ALTER
        col("customer_ref", "string", renamed_from="cust_id"),  # columnMapping
        col("address", "struct<street:string,zip:string>"),  # nested add
        col("shipped_at", "timestamp", comment="When it left"),
        name=f"{schema}.orders",
        comment="Order facts, one row per order",  # table comment
        cluster_by=("order_id",),
        tags=(("domain", "sales"),),
        properties=(("delta.enableChangeDataFeed", "true"),),
        constraints=(Check("positive_amount", "amount > 0"),),
    )

    live, facts = live_table(introspector, desired.name)
    plan = plan_for(desired, live, facts)
    assert plan.steps, "the plan should not be empty"
    run_plan(plan, runner)

    live_after, _ = live_table(introspector, desired.name)
    assert diff(desired, live_after) == (), "re-planning after apply must be empty"


def test_dropping_a_column_needs_column_mapping(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """DROP COLUMN is refused until column mapping is on — hence the prerequisite.

    https://docs.databricks.com/aws/en/delta/column-mapping
    """
    original = table(
        col("order_id", "bigint", nullable=False),
        col("legacy_flag", "boolean"),
        name=f"{schema}.orders",
    )
    runner.query(create_table_sql(original))

    with pytest.raises(Exception, match="(?i)column.?mapping"):
        runner.query(
            f"ALTER TABLE {quote_qualified(original.name)} DROP COLUMN `legacy_flag`"
        )

    desired = table(col("order_id", "bigint", nullable=False), name=f"{schema}.orders")
    live, facts = live_table(introspector, desired.name)
    plan = plan_for(desired, live, facts)
    assert [step.risk for step in plan.steps] == ["feature", "destructive"]
    run_plan(plan, runner)

    live_after, _ = live_table(introspector, desired.name)
    assert live_after.column_names == ("order_id",)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("int", "bigint"),
        ("decimal(10,2)", "decimal(18,2)"),
        ("float", "double"),
        ("date", "timestamp_ntz"),
    ],
)
def test_the_widenings_we_claim_are_supported(
    runner: WarehouseRunner,
    introspector: Introspector,
    schema: str,
    before: str,
    after: str,
) -> None:
    """Every entry in `widens()` has to be one Databricks actually allows.

    https://docs.databricks.com/aws/en/delta/type-widening
    """
    original = table(col("value", before), name=f"{schema}.orders")
    runner.query(create_table_sql(original))

    desired = table(col("value", after), name=f"{schema}.orders")
    live, facts = live_table(introspector, desired.name)
    plan = plan_for(desired, live, facts)
    # timestamp_ntz also needs its own feature before ALTER TABLE may use it.
    features = 2 if after == "timestamp_ntz" else 1
    assert [step.risk for step in plan.steps] == ["feature"] * features + ["meta"]
    run_plan(plan, runner)

    live_after, _ = live_table(introspector, desired.name)
    assert diff(desired, live_after) == ()


def test_automatic_clustering_round_trips(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """Create with AUTO, read it back as AUTO; switch to keys and back. Settles
    the TODO(verify) on `_clustering_clause` for a workspace with predictive
    optimization. https://docs.databricks.com/aws/en/delta/clustering
    """
    from dataclasses import replace

    from deltaplan.executor import Executor
    from deltaplan.history import MemoryHistory
    from deltaplan.planning import plan_tables

    def apply(spec: Table) -> None:
        plan = plan_tables([spec], introspector, target="it", tool_version="0")
        result = Executor(runner, introspector, MemoryHistory()).apply(plan)
        assert result.ok, result.error
        again = plan_tables([spec], introspector, target="it", tool_version="0")
        assert again.empty, [c.kind for d in again.diffs for c in d.changes]

    auto = replace(
        table(col("id", "bigint"), col("placed", "date"), name=f"{schema}.orders"),
        cluster_auto=True,
    )
    apply(auto)
    apply(replace(auto, cluster_auto=False, cluster_by=("placed",)))
    apply(auto)
