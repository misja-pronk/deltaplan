"""The pipeline: ownership, strict schemas, and what is left alone.

Run against the fake warehouse, so the whole path from specs to a plan — and
from a plan to the tables it describes — is exercised offline.
"""

import pytest

from deltaplan.differ import diff
from deltaplan.introspect import Introspector
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.planning import PlanningError, plan_tables
from fake_warehouse import FakeWarehouse
from helpers import col, run, table

MANAGED = ((MANAGED_PROPERTY, "true"),)

ORDERS = table(col("id", "bigint"), name="main.sales.orders", properties=MANAGED)
OURS_BUT_GONE = table(col("id", "bigint"), name="main.sales.retired", properties=MANAGED)
NOT_OURS = table(col("id", "bigint"), name="main.sales.theirs")


def planned(
    specs: list[Table], fake: FakeWarehouse, *, strict: bool = False, clone: bool = False
) -> Plan:
    return plan_tables(
        specs,
        Introspector(fake),
        target="test",
        tool_version="0.1.0",
        mode_for=lambda _schema: "strict" if strict else "additive",
        clone=clone,
    )


def kinds(plan: Plan) -> list[tuple[str, str]]:
    return [(c.table, c.kind) for c in plan.changes]


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------


def test_a_spec_for_someone_elses_table_claims_it() -> None:
    live = table(col("id", "bigint"), name="main.sales.orders")  # no marker
    fake = FakeWarehouse.of(live)
    plan = planned([table(col("id", "bigint"), name="main.sales.orders")], fake)

    assert kinds(plan) == [("main.sales.orders", "claim_table")]
    assert [s.title for s in plan.steps] == ["CLAIM ownership"]

    run(plan, fake)
    assert fake.tables["main.sales.orders"].managed
    # And once claimed, there is nothing more to say.
    assert planned([table(col("id", "bigint"), name="main.sales.orders")], fake).empty


def test_a_table_deltaplan_created_is_not_claimed_again() -> None:
    fake = FakeWarehouse.of(ORDERS)
    assert planned([table(col("id", "bigint"), name="main.sales.orders")], fake).empty


def test_a_new_table_needs_no_claim() -> None:
    # CREATE TABLE marks it managed as it creates it.
    plan = planned([ORDERS], FakeWarehouse())
    assert kinds(plan) == [("main.sales.orders", "create_table")]


# ---------------------------------------------------------------------------
# tables no spec describes
# ---------------------------------------------------------------------------


def test_additive_keeps_orphans_and_says_so() -> None:
    fake = FakeWarehouse.of(ORDERS, OURS_BUT_GONE, NOT_OURS)
    plan = planned([ORDERS], fake)
    assert plan.empty
    assert plan.orphaned_tables == ("main.sales.retired",)
    assert plan.unmanaged_tables == ("main.sales.theirs",)
    assert plan.summary.destroy == 0


def test_strict_drops_orphans_and_only_orphans() -> None:
    fake = FakeWarehouse.of(ORDERS, OURS_BUT_GONE, NOT_OURS)
    plan = planned([ORDERS], fake, strict=True)

    assert kinds(plan) == [("main.sales.retired", "drop_table")]
    assert [(s.title, s.risk) for s in plan.steps] == [("DROP TABLE", "destructive")]
    assert plan.steps[0].undo_hint == "UNDROP TABLE `main`.`sales`.`retired`"
    assert plan.summary.destroy == 1
    # A table deltaplan didn't create is never a drop candidate, strict or not.
    assert plan.unmanaged_tables == ("main.sales.theirs",)

    run(plan, fake)
    assert "main.sales.retired" not in fake.tables
    assert "main.sales.theirs" in fake.tables


def test_strictness_is_per_schema() -> None:
    elsewhere = table(
        col("id", "bigint"), name="main.archive.retired", properties=MANAGED
    )
    archived = table(col("id", "bigint"), name="main.archive.kept", properties=MANAGED)
    fake = FakeWarehouse.of(ORDERS, OURS_BUT_GONE, elsewhere, archived)
    plan = plan_tables(
        [ORDERS, archived],
        Introspector(fake),
        target="test",
        tool_version="0.1.0",
        mode_for=lambda schema: "strict" if schema == "main.sales" else "additive",
    )
    assert kinds(plan) == [("main.sales.retired", "drop_table")]
    assert plan.orphaned_tables == ("main.archive.retired",)


def test_a_dropped_table_counts_toward_the_fingerprint() -> None:
    # Otherwise a table that changed between plan and apply could be dropped from
    # under a plan that no longer describes it.
    fake = FakeWarehouse.of(ORDERS, OURS_BUT_GONE)
    plan = planned([ORDERS], fake, strict=True)
    fake.query("ALTER TABLE `main`.`sales`.`retired` ADD COLUMNS (`surprise` STRING)")
    assert planned([ORDERS], fake, strict=True).state_fingerprint != (
        plan.state_fingerprint
    )


# ---------------------------------------------------------------------------
# cloning before risky steps
# ---------------------------------------------------------------------------


def test_clone_comes_before_the_first_risky_step_only() -> None:
    live = table(
        col("id", "bigint"),
        col("a", "int"),
        col("b", "int"),
        name="main.sales.orders",
        properties=MANAGED,
    )
    desired = table(col("id", "bigint"), name="main.sales.orders")
    fake = FakeWarehouse.of(live)
    plan = planned([desired], fake, clone=True)

    titles = [(s.title, s.risk) for s in plan.steps]
    assert titles == [
        ("enable columnMapping", "feature"),
        ("CLONE backup", "meta"),
        ("DROP COLUMN", "destructive"),
        ("DROP COLUMN", "destructive"),
    ]
    backup = f"main.sales.orders__deltaplan_backup_{plan.state_fingerprint[:8]}"
    assert f"`main`.`sales`.`orders__deltaplan_backup_{plan.state_fingerprint[:8]}`" in (
        plan.steps[1].sql or ""
    )

    run(plan, fake)
    assert fake.tables[backup].column_names == ("id", "a", "b"), "the table as it was"
    assert fake.tables["main.sales.orders"].column_names == ("id",)


def test_no_clone_unless_asked() -> None:
    live = table(
        col("id", "bigint"), col("a", "int"), name="main.sales.orders", properties=MANAGED
    )
    plan = planned(
        [table(col("id", "bigint"), name="main.sales.orders")], FakeWarehouse.of(live)
    )
    assert "CLONE backup" not in [s.title for s in plan.steps]


def test_a_rewrite_is_cloned_before_it_starts() -> None:
    live = table(
        col("amount", "decimal(10,2)"), name="main.sales.orders", properties=MANAGED
    )
    desired = table(col("amount", "string"), name="main.sales.orders")
    fake = FakeWarehouse.of(live)
    plan = planned([desired], fake, clone=True)
    assert plan.steps[0].title == "CLONE backup"
    assert plan.steps[1].title == "STAGE rewritten data"
    run(plan, fake)
    after = Introspector(fake).table("main.sales.orders")
    assert after is not None and diff(desired, after.table) == ()


def test_a_bad_table_name_is_a_planning_error() -> None:
    with pytest.raises(PlanningError, match="catalog.schema.name"):
        planned([table(col("id", "bigint"), name="sales.orders")], FakeWarehouse())
