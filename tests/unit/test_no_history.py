"""Applying without a history schema.

The history tables give three things: a lock, a resume, and a record of who ran
what. A host deploying many products into many catalogs may not want three Delta
tables of deltaplan's bookkeeping in each of them — and doesn't have to. What
deltaplan needs to work is on the tables themselves, as properties.

These say what is given up, and prove what isn't: it still applies, still
converges, still refuses what it should, and still takes a restore point before
anything risky — reported instead of stored.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from deltaplan import api
from deltaplan.connect import Connection
from deltaplan.executor import ExecutionError, StalePlan
from deltaplan.history import MemoryHistory, NoHistory
from deltaplan.introspect import Introspector
from deltaplan.model.plan import Plan, Step
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.planning import plan_tables
from fake_warehouse import FakeWarehouse
from helpers import col, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
LIVE = table(col("id", "bigint"), col("legacy", "string"), name=NAME, properties=MANAGED)


def planned(desired: Table, fake: FakeWarehouse) -> Plan:
    return plan_tables([desired], Introspector(fake), target="dev", tool_version="0")


def test_nothing_is_created_and_the_change_is_made() -> None:
    fake = FakeWarehouse()
    fake.schemas.add("main.sales")
    desired = table(col("id", "bigint"), name=NAME)
    result = api.apply(
        planned(desired, fake), Connection(runner=fake), history=NoHistory()
    )
    assert result.ok
    assert NAME in fake.tables
    assert [name for name in fake.schemas if "deltaplan" in name] == []


def test_a_second_apply_of_the_same_plan_changes_nothing() -> None:
    """Refused as stale, exactly as it is with a history schema.

    The first apply moved the world the plan was made against, so the plan no
    longer describes it. Without a record of the run there is nothing else to
    tell deltaplan that — and nothing else is needed: the live tables say it.
    """
    fake = FakeWarehouse.of(LIVE)
    desired = replace(LIVE, comment="Order facts")
    plan = planned(desired, fake)
    assert api.apply(plan, Connection(runner=fake), history=NoHistory()).ok
    before = fake.tables[NAME]
    with pytest.raises(StalePlan) as raised:
        api.apply(plan, Connection(runner=fake), history=NoHistory())
    assert raised.value.tables == (NAME,)
    assert fake.tables[NAME] == before, "and it changed nothing on the way out"


def test_an_interrupted_run_is_finished_by_planning_again() -> None:
    """The resume a history gives is replaced by a fresh plan — which skips
    what is already there and does the rest."""
    fake = FakeWarehouse.of(LIVE)
    desired = table(
        col("id", "bigint"),
        col("legacy", "string"),
        col("region", "string"),
        name=NAME,
        comment="Order facts",
    )
    plan = planned(desired, fake)
    assert len(plan.steps) > 1

    stop_at = plan.steps[-1]

    class Interrupted:
        """The warehouse, until the last step — then it goes away."""

        def __init__(self, warehouse: FakeWarehouse) -> None:
            self.warehouse = warehouse

        def query(self, statement: str) -> tuple[dict[str, str | None], ...]:
            if stop_at.sql and statement.strip() == stop_at.sql.strip():
                raise RuntimeError("the warehouse went away")
            return self.warehouse.query(statement)

    first = api.apply(plan, Connection(runner=Interrupted(fake)), history=NoHistory())
    assert not first.ok and first.failed_step == stop_at.id

    second = api.apply(
        planned(desired, fake), Connection(runner=fake), history=NoHistory()
    )
    assert second.ok
    assert planned(desired, fake).empty, "and it converged"


def test_a_risky_step_still_takes_a_restore_point() -> None:
    """Kept on the run rather than in a table: `RESTORE` is still one command."""
    fake = FakeWarehouse.of(LIVE)
    fake.versions[NAME] = 41
    desired = table(col("id", "bigint"), name=NAME)  # drops `legacy`
    seen: list[tuple[Step, str, str | None]] = []
    result = api.apply(
        planned(desired, fake),
        Connection(runner=fake),
        history=NoHistory(),
        allow_destructive=True,
        observer=lambda step, status, note: seen.append((step, status, note)),
    )
    assert result.ok
    [(where, version)] = result.restore_points
    assert where == NAME
    assert any(note and f"version {version}" in note for _, _, note in seen)


def test_a_project_without_a_history_schema_gets_one_that_keeps_nothing() -> None:
    from pathlib import Path
    from tempfile import mkdtemp

    from deltaplan.loader import Project

    directory = Path(mkdtemp())
    (directory / "tables").mkdir()
    (directory / "deltaplan.yml").write_text("specs: [tables]\ntargets:\n  dev: {}\n")
    project = Project.load(directory / "deltaplan.yml")
    store = api.history_for(project, project.default, Connection(runner=FakeWarehouse()))
    assert isinstance(store, NoHistory)
    assert store.resumable_run("plan", "dev") is None
    assert store.acquire_lock("dev", "run", 30) is True
    assert store.force_unlock("dev") is None


def test_a_history_schema_still_records_everything() -> None:
    """The other half of the contract: nothing about this changed."""
    fake = FakeWarehouse.of(LIVE)
    history = MemoryHistory()
    desired = replace(LIVE, comment="Order facts")
    result = api.apply(planned(desired, fake), Connection(runner=fake), history=history)
    assert result.ok
    assert history.created and history.runs
    assert history.steps[result.run_id]


def test_a_destructive_plan_is_still_refused_without_a_history() -> None:
    fake = FakeWarehouse.of(LIVE)
    desired = table(col("id", "bigint"), name=NAME)
    with pytest.raises(ExecutionError):
        api.apply(planned(desired, fake), Connection(runner=fake), history=NoHistory())
