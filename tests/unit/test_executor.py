"""Running a plan: skip, resume, refuse, lock, record.

Everything here runs against `tests/fake_warehouse.py` and an in-memory history,
so the executor's machinery is tested without a workspace. What the fake cannot
tell us — whether Databricks accepts the statements — is what the integration
suite is for.
"""

import pytest

from deltaplan.executor import ExecutionError, Executor, plan_identity
from deltaplan.history import MemoryHistory, StepOutcome
from deltaplan.introspect import Introspector
from deltaplan.model.plan import Plan, Step, TableDiff, TableFacts
from deltaplan.model.table import Table
from fake_warehouse import FakeSqlError, FakeWarehouse
from helpers import col, plan_against, table

NAME = "main.sales.orders"

LIVE = table(
    col("order_id", "bigint", nullable=False),
    col("amount", "decimal(10,2)"),
    col("cust_id", "string"),
    col("legacy_flag", "boolean"),
    name=NAME,
    comment="Order facts",
)

DESIRED = table(
    col("order_id", "bigint", nullable=False),
    col("amount", "decimal(18,2)"),
    col("customer_ref", "string", renamed_from="cust_id"),
    col("legacy_flag", "boolean"),
    name=NAME,
    comment="Order facts",
)


def executor(fake: FakeWarehouse, history: MemoryHistory | None = None) -> Executor:
    return Executor(
        runner=fake,
        introspector=Introspector(fake),
        history=history or MemoryHistory(),
        new_run_id=lambda: "run1",
    )


def planned(
    desired: Table = DESIRED, live: Table | None = LIVE
) -> tuple[FakeWarehouse, Plan]:
    """A plan whose fingerprint matches the fake it was built against."""
    fake, plan = plan_against(desired, live)
    return fake, _with_real_fingerprint(fake, plan)


def _with_real_fingerprint(fake: FakeWarehouse, plan: Plan) -> Plan:
    from dataclasses import replace

    from deltaplan.model.plan import fingerprint

    live = Introspector(fake).tables([diff.table for diff in plan.diffs])
    return replace(plan, state_fingerprint=fingerprint(live.values()))


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_a_plan_runs_and_is_recorded() -> None:
    fake, plan = planned()
    history = MemoryHistory()
    result = executor(fake, history).apply(plan)

    assert result.ok
    assert result.ran == tuple(step.id for step in plan.steps)
    assert result.skipped == ()
    assert history.created
    assert history.runs["run1"]["status"] == "succeeded"
    assert history.runs["run1"]["plan_hash"] == plan_identity(plan)
    assert [outcome.status for outcome in history.steps["run1"]] == ["succeeded"] * 4
    # And the world actually changed.
    from deltaplan.differ import diff

    live = Introspector(fake).table(NAME)
    assert live is not None and diff(DESIRED, live.table) == ()


def test_the_lock_is_taken_and_given_back() -> None:
    fake, plan = planned()
    history = MemoryHistory()
    assert executor(fake, history).apply(plan).ok
    assert history.lock_holder("test") is None


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_a_destructive_plan_needs_the_flag() -> None:
    desired = table(
        *[c for c in LIVE.columns if c.name != "legacy_flag"],
        name=NAME,
        comment=LIVE.comment,
    )
    fake, plan = planned(desired)
    with pytest.raises(ExecutionError, match="--allow-destructive"):
        executor(fake).apply(plan)
    assert fake.ddl == [], "nothing may run before the refusal"

    result = executor(fake).apply(plan, allow_destructive=True)
    assert result.ok
    live = Introspector(fake).table(NAME)
    assert live is not None and "legacy_flag" not in live.table.column_names


def test_a_rewrite_plan_is_refused_for_now() -> None:
    desired = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "string"),  # decimal -> string is no widening
        col("cust_id", "string"),
        col("legacy_flag", "boolean"),
        name=NAME,
        comment=LIVE.comment,
    )
    fake, plan = planned(desired)
    with pytest.raises(ExecutionError, match="rewrite"):
        executor(fake).apply(plan)


def test_a_stale_plan_is_refused() -> None:
    fake, plan = planned()
    # Someone edits the table after the plan was made.
    fake.query("ALTER TABLE `main`.`sales`.`orders` ADD COLUMNS (`surprise` STRING)")
    with pytest.raises(ExecutionError, match="changed since this plan was made"):
        executor(fake).apply(plan)
    assert fake.ddl == [
        "ALTER TABLE `main`.`sales`.`orders` ADD COLUMNS (`surprise` STRING)"
    ]


def test_a_locked_target_is_refused() -> None:
    fake, plan = planned()
    history = MemoryHistory()
    history.acquire_lock("test", "someone-else", 60)
    with pytest.raises(ExecutionError, match="locked by run someone-else"):
        executor(fake, history).apply(plan)


def test_a_blocked_precheck_stops_the_step() -> None:
    desired = table(
        *[c for c in LIVE.columns if c.name != "cust_id"],
        col("cust_id", "string", nullable=False),
        name=NAME,
        comment=LIVE.comment,
    )
    fake, plan = planned(desired)
    fake.blocked = True  # the column still has NULLs in it
    result = executor(fake).apply(plan)
    assert not result.ok
    assert result.error is not None and "every existing row" in result.error
    assert fake.ddl == [], "a blocked step must not run its statement"


# ---------------------------------------------------------------------------
# idempotency and resume
# ---------------------------------------------------------------------------


def test_steps_already_applied_are_skipped() -> None:
    fake, plan = planned()
    assert executor(fake).apply(plan).ok

    # Running the same plan again, with a fresh history: every change is already
    # true of the live table, so nothing runs a second time. (The fingerprint is
    # recomputed against the new state, so this plan has to be re-fingerprinted
    # the way a fresh `deltaplan plan` would.)
    ddl_before = len(fake.ddl)
    again = executor(fake).apply(_with_real_fingerprint(fake, plan))
    assert again.ok
    assert again.ran == ()
    assert again.skipped == tuple(step.id for step in plan.steps)
    assert len(fake.ddl) == ddl_before


def test_a_failed_run_resumes_where_it_stopped() -> None:
    fake, plan = planned()
    history = MemoryHistory()

    # The rename fails — a dropped connection, a permissions blip, anything.
    fake.failures["RENAME COLUMN"] = "connection reset"
    first = executor(fake, history).apply(plan)
    assert not first.ok
    assert first.failed == 4
    assert history.runs["run1"]["status"] == "failed"
    assert [o.status for o in history.steps["run1"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
        "failed",
    ]

    # Fix whatever it was and run the same plan again: the first three steps are
    # known to be done, so only the rename is attempted.
    del fake.failures["RENAME COLUMN"]
    second = Executor(
        runner=fake,
        introspector=Introspector(fake),
        history=history,
        new_run_id=lambda: "run2",
    ).apply(plan)

    assert second.ok
    assert second.resumed
    assert second.run_id == "run1", "a resume continues the run it is resuming"
    assert second.ran == (4,)
    assert second.skipped == (1, 2, 3)


def test_a_resume_does_not_recheck_the_fingerprint() -> None:
    # Of course the live tables changed: the first half of the plan changed them.
    fake, plan = planned()
    history = MemoryHistory()
    fake.failures["RENAME COLUMN"] = "connection reset"
    assert not executor(fake, history).apply(plan).ok
    del fake.failures["RENAME COLUMN"]
    assert executor(fake, history).apply(plan).ok


def test_a_step_whose_effect_survived_a_crash_is_not_repeated() -> None:
    """The step took, but the run died before recording it.

    History says nothing about it; the live table says it is done. Asking the
    model rather than only the history is what makes that safe.
    """
    fake, plan = planned()
    history = MemoryHistory()
    history.start_run("run1", plan_identity(plan), "test", "0.1.0")

    # Steps 1-3 ran for real, but only step 1 made it into the history table.
    for step in plan.steps[:3]:
        fake.query(step.sql or "")
    history.record_step("run1", StepOutcome(1, NAME, plan.steps[0].sql, "succeeded"))

    result = executor(fake, history).apply(plan)
    assert result.ok
    assert result.resumed
    # 1 is skipped on the history's word; 2 because the widening it performs is
    # already true of the live table, which history knew nothing about.
    assert result.skipped == (1, 2)
    # 3 is the rename's prerequisite. The rename is still outstanding, so it is
    # attempted again — setting a property that already has that value is a
    # no-op, which is why prerequisites are safe to repeat.
    assert result.ran == (3, 4)


# ---------------------------------------------------------------------------
# restore points and postchecks
# ---------------------------------------------------------------------------


def test_a_destructive_step_records_a_restore_point() -> None:
    desired = table(
        *[c for c in LIVE.columns if c.name != "legacy_flag"],
        name=NAME,
        comment=LIVE.comment,
    )
    fake, plan = planned(desired)
    history = MemoryHistory()
    executor(fake, history).apply(plan, allow_destructive=True)

    drop = next(o for o in history.steps["run1"] if o.sql and "DROP COLUMN" in o.sql)
    assert drop.delta_version_before == fake.versions[NAME] - 1
    mapping = next(o for o in history.steps["run1"] if o.sql and "TBLPROPERTIES" in o.sql)
    assert mapping.delta_version_before is None, "only risky steps need one"


def test_a_postcheck_that_fails_fails_the_run() -> None:
    step = Step(
        id=1,
        table=NAME,
        title="ALTER something",
        risk="meta",
        sql="ALTER TABLE `main`.`sales`.`orders` SET TBLPROPERTIES ('a' = 'b')",
        postcheck="SELECT false AS ok",
    )
    plan = Plan(
        tool_version="0.1.0",
        target="test",
        spec_hash="spec",
        state_fingerprint="live",
        diffs=(TableDiff(NAME, (), TableFacts(NAME)),),
        steps=(step,),
    )
    fake = FakeWarehouse.of(LIVE)
    plan = _with_real_fingerprint(fake, plan)
    result = executor(fake).apply(plan)
    assert not result.ok
    assert result.error is not None and "postcheck" in result.error


# ---------------------------------------------------------------------------
# the fake itself
# ---------------------------------------------------------------------------


def test_the_fake_refuses_statements_it_does_not_know() -> None:
    # Which is what stops a new statement shape from going untested.
    fake = FakeWarehouse.of(LIVE)
    with pytest.raises(FakeSqlError, match="does not know"):
        fake.query("OPTIMIZE `main`.`sales`.`orders` ZORDER BY (order_id)")
