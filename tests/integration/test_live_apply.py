"""`apply` against a real workspace: does the executor's machinery hold up?

The offline suite already asserts that a plan converges (`test_convergence.py`)
and that the executor skips, resumes and refuses (`test_executor.py`), against a
fake that implements deltaplan's own reading of the manual. What only a real
warehouse can tell us is whether the statements are accepted at all — including
the history and lock SQL, which the fake never sees.
"""

from __future__ import annotations

import pytest

from deltaplan.differ import diff
from deltaplan.executor import ExecutionError, Executor
from deltaplan.history import DeltaHistory
from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.model.plan import Plan, TableDiff, TableFacts, fingerprint
from deltaplan.model.table import Table
from deltaplan.planner import build_plan, create_table_sql
from deltaplan.sql import quote_literal, quote_qualified
from helpers import col, table

pytestmark = pytest.mark.integration


def plan_for(desired: Table, introspector: Introspector) -> Plan:
    live = introspector.table(desired.name)
    live_table = live.table if live else None
    return build_plan(
        [
            TableDiff(
                desired.name,
                diff(desired, live_table),
                TableFacts(
                    desired.name,
                    exists=live_table is not None,
                    properties=live_table.properties if live_table else (),
                    size_bytes=live.size_bytes if live else None,
                ),
            )
        ],
        target="integration",
        tool_version="0.1.0",
        spec_hash="spec",
        state_fingerprint=fingerprint([live_table]),
    )


def executor(runner: WarehouseRunner, schema: str) -> Executor:
    return Executor(
        runner=runner,
        introspector=Introspector(runner),
        history=DeltaHistory(runner, schema),
    )


ORIGINAL = (
    col("order_id", "bigint", nullable=False),
    col("amount", "decimal(10,2)"),
    col("cust_id", "string"),
    col("address", "struct<street:string>"),
    col("legacy_flag", "boolean"),
)


def desired_for(schema: str) -> Table:
    return table(
        col("order_id", "bigint", nullable=False),
        col("amount", "decimal(18,2)"),  # widening -> enables typeWidening
        col("customer_ref", "string", renamed_from="cust_id"),  # -> columnMapping
        col("address", "struct<street:string,zip:string>"),  # nested add
        col("shipped_at", "timestamp", comment="When it left"),
        name=f"{schema}.orders",
        comment="Order facts, one row per order",
        cluster_by=("order_id",),
        tags=(("domain", "sales"),),
        properties=(("delta.enableChangeDataFeed", "true"),),
    )


def test_apply_converges_and_records_what_it_did(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    runner.query(create_table_sql(table(*ORIGINAL, name=f"{schema}.orders")))
    desired = desired_for(schema)

    plan = plan_for(desired, introspector)
    assert plan.steps
    result = executor(runner, schema).apply(plan, allow_destructive=True)

    assert result.ok, result.error
    assert len(result.ran) == len(plan.steps)

    live = introspector.table(desired.name)
    assert live is not None
    assert diff(desired, live.table) == (), "re-planning after apply must be empty"

    # The history tables exist and hold this run.
    runs = runner.query(
        f"SELECT status FROM {quote_qualified(f'{schema}.runs')} "
        f"WHERE run_id = {quote_literal(result.run_id)}"
    )
    assert [row["status"] for row in runs] == ["succeeded"]
    steps = runner.query(
        f"SELECT step_id, status FROM {quote_qualified(f'{schema}.steps')} "
        f"WHERE run_id = {quote_literal(result.run_id)}"
    )
    assert len(steps) == len(plan.steps)
    assert {row["status"] for row in steps} == {"succeeded"}

    # And the lock was handed back.
    assert DeltaHistory(runner, schema).lock_holder("integration") is None


def test_a_plan_that_already_ran_is_refused(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    runner.query(create_table_sql(table(*ORIGINAL, name=f"{schema}.orders")))
    desired = desired_for(schema)
    plan = plan_for(desired, introspector)
    assert executor(runner, schema).apply(plan, allow_destructive=True).ok

    with pytest.raises(ExecutionError, match="changed since this plan was made"):
        executor(runner, schema).apply(plan, allow_destructive=True)


def test_a_held_lock_stops_a_second_run(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    runner.query(create_table_sql(table(*ORIGINAL, name=f"{schema}.orders")))
    plan = plan_for(desired_for(schema), introspector)

    history = DeltaHistory(runner, schema)
    history.ensure()
    assert history.acquire_lock("integration", "someone-else", 60)

    with pytest.raises(ExecutionError, match="locked by run someone-else"):
        executor(runner, schema).apply(plan, allow_destructive=True)

    assert history.force_unlock("integration") == "someone-else"
    assert executor(runner, schema).apply(plan, allow_destructive=True).ok


def test_a_rewrite_converts_the_data_it_moves(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """The one thing no fake can check: that the projection means what we think.

    A rewrite stages the converted data, replaces the table from it, and drops the
    staging table. Here there are actual rows in it, so a cast that silently
    produced NULLs — or a struct rebuilt by position — shows up.
    """
    name = f"{schema}.orders"
    original = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "decimal(10,2)"),
        col("address", "struct<street:string,old_zip:string>"),
        col("lines", "array<struct<sku:string,qty:int>>"),
        name=name,
    )
    runner.query(create_table_sql(original))
    runner.query(
        f"INSERT INTO {quote_qualified(name)} VALUES "
        "(1, 10.50, named_struct('street', 'High St', 'old_zip', '1234'), "
        "array(named_struct('sku', 'A', 'qty', 2)))"
    )

    from deltaplan.model.types import Field, Primitive, Struct

    desired = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "string"),  # a cast, and not a widening
        Field(
            "address",
            Struct(
                (
                    Field("street", Primitive("string")),
                    Field("zip", Primitive("string"), renamed_from="old_zip"),
                    Field("country", Primitive("string")),  # new, so NULL
                )
            ),
        ),
        col("lines", "array<struct<sku:string,qty:string>>"),  # element cast
        name=name,
        comment="Rewritten",
    )

    plan = plan_for(desired, introspector)
    assert [step.risk for step in plan.steps][:2] == ["rewrite", "rewrite"]
    assert executor(runner, schema).apply(plan).ok

    live = introspector.table(name)
    assert live is not None
    assert diff(desired, live.table) == (), "the new table must match the spec"

    rows = runner.query(
        "SELECT order_id, amount, address.street, address.zip, address.country, "
        f"lines[0].qty AS qty FROM {quote_qualified(name)}"
    )
    assert len(rows) == 1, "the rewrite must not lose or duplicate rows"
    row = rows[0]
    assert row["order_id"] == "1"
    assert row["amount"] == "10.50"
    assert row["street"] == "High St"
    assert row["zip"] == "1234", "the renamed field keeps its value"
    assert row["country"] is None, "a new field starts empty"
    assert row["qty"] == "2"

    # The staging table is cleaned up.
    staging = introspector.table(f"{name}__deltaplan_rewrite")
    assert staging is None
