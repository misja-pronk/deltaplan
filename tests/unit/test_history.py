"""The history tables: the SQL they use, and how the adapter reads it back.

The statements are asserted verbatim — they are the contract with the warehouse,
and the integration suite is what proves the warehouse accepts them.
"""

from dataclasses import dataclass, field

import pytest
from syrupy.assertion import SnapshotAssertion

from deltaplan.history import DeltaHistory, MemoryHistory, StepOutcome

Row = dict[str, str | None]


@dataclass
class ScriptRunner:
    """Returns canned answers in order, and remembers what it was asked."""

    answers: list[tuple[Row, ...]] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)

    def query(self, statement: str) -> tuple[Row, ...]:
        self.statements.append(statement)
        return self.answers.pop(0) if self.answers else ()


def history(*answers: tuple[Row, ...]) -> tuple[DeltaHistory, ScriptRunner]:
    runner = ScriptRunner(list(answers))
    return DeltaHistory(runner, "main.deltaplan"), runner


def test_ensure_creates_three_tables(snapshot: SnapshotAssertion) -> None:
    store, runner = history()
    store.ensure()
    assert [s.split("(")[0].strip() for s in runner.statements] == [
        "CREATE TABLE IF NOT EXISTS `main`.`deltaplan`.`runs`",
        "CREATE TABLE IF NOT EXISTS `main`.`deltaplan`.`steps`",
        "CREATE TABLE IF NOT EXISTS `main`.`deltaplan`.`lock`",
    ]
    assert "\n\n".join(runner.statements) == snapshot


def test_run_and_step_records(snapshot: SnapshotAssertion) -> None:
    store, runner = history()
    store.start_run("run1", "spec:live", "prod", "0.1.0")
    store.record_step(
        "run1",
        StepOutcome(
            step_id=6,
            table="main.sales.orders",
            sql="ALTER TABLE `main`.`sales`.`orders` DROP COLUMN `legacy_flag`",
            status="failed",
            error="it's fine, I said it's fine",
            delta_version_before=17,
        ),
    )
    store.finish_run("run1", "failed")
    assert "\n\n".join(runner.statements) == snapshot
    # An error message with a quote in it can't break out of the literal.
    assert "'it''s fine, I said it''s fine'" in runner.statements[1]


def test_a_step_with_no_version_or_error_writes_typed_nulls() -> None:
    store, runner = history()
    store.record_step("run1", StepOutcome(1, "main.sales.orders", None, "skipped"))
    assert "CAST(NULL AS STRING)" in runner.statements[0]
    assert "CAST(NULL AS BIGINT)" in runner.statements[0]


def test_resumable_run_and_finished_steps() -> None:
    store, runner = history(({"run_id": "run7"},), ({"step_id": "1"}, {"step_id": "3"}))
    assert store.resumable_run("spec:live", "prod") == "run7"
    # A run that succeeded is not resumable — hence the <> rather than an =.
    assert "status <> 'succeeded'" in runner.statements[0]
    assert store.finished_steps("run7") == frozenset({1, 3})
    assert "status IN ('succeeded', 'skipped')" in runner.statements[1]


def test_no_resumable_run() -> None:
    store, _ = history(())
    assert store.resumable_run("spec:live", "prod") is None


# ---------------------------------------------------------------------------
# locking
# ---------------------------------------------------------------------------


def test_acquiring_the_lock(snapshot: SnapshotAssertion) -> None:
    store, runner = history((), (), ({"holder": "run1"},))
    assert store.acquire_lock("prod", "run1", 60) is True
    assert "\n\n".join(runner.statements) == snapshot


def test_the_lock_is_not_taken_when_someone_else_holds_it() -> None:
    # The conditional UPDATE matched nothing, so the read-back shows the other
    # run. Trusting the read-back rather than an affected-row count keeps this
    # independent of how a warehouse reports DML.
    store, _ = history((), (), ({"holder": "someone-else"},))
    assert store.acquire_lock("prod", "run1", 60) is False


def test_an_expired_lock_can_be_taken() -> None:
    store, runner = history((), (), ({"holder": "run1"},))
    store.acquire_lock("prod", "run1", 30)
    claim = runner.statements[1]
    assert "holder IS NULL OR expires_at < current_timestamp()" in claim
    assert "INTERVAL 30 MINUTES" in claim


def test_releasing_only_releases_your_own() -> None:
    store, runner = history()
    store.release_lock("prod", "run1")
    assert "holder = 'run1'" in runner.statements[0]


def test_force_unlock_reports_who_held_it() -> None:
    store, runner = history(({"holder": "run9"},))
    assert store.force_unlock("prod") == "run9"
    assert "SET holder = NULL" in runner.statements[1]


def test_force_unlock_on_a_free_lock() -> None:
    store, runner = history(({"holder": None},))
    assert store.force_unlock("prod") is None
    assert len(runner.statements) == 1, "nothing to release"


def test_schema_names_are_quoted() -> None:
    store, runner = DeltaHistory(ScriptRunner(), "odd catalog.odd schema"), None
    store.ensure()
    assert isinstance(store.runner, ScriptRunner)
    assert "`odd catalog`.`odd schema`.`runs`" in store.runner.statements[0]
    del runner


# ---------------------------------------------------------------------------
# the in-memory one honours the same contract
# ---------------------------------------------------------------------------


@pytest.fixture
def memory() -> MemoryHistory:
    return MemoryHistory()


def test_memory_history_round_trip(memory: MemoryHistory) -> None:
    memory.ensure()
    memory.start_run("run1", "spec:live", "prod", "0.1.0")
    assert memory.resumable_run("spec:live", "prod") == "run1"

    memory.record_step("run1", StepOutcome(1, "t", "SQL", "succeeded"))
    memory.record_step("run1", StepOutcome(2, "t", "SQL", "skipped"))
    memory.record_step("run1", StepOutcome(3, "t", "SQL", "failed", "boom"))
    assert memory.finished_steps("run1") == frozenset({1, 2})

    memory.finish_run("run1", "succeeded")
    assert memory.resumable_run("spec:live", "prod") is None


def test_memory_lock(memory: MemoryHistory) -> None:
    assert memory.acquire_lock("prod", "run1", 60)
    assert not memory.acquire_lock("prod", "run2", 60)
    assert memory.acquire_lock("prod", "run1", 60), "re-entrant for its own holder"
    assert memory.lock_holder("prod") == "run1"

    memory.release_lock("prod", "run2")
    assert memory.lock_holder("prod") == "run1", "only the holder can release it"
    memory.release_lock("prod", "run1")
    assert memory.lock_holder("prod") is None

    memory.acquire_lock("prod", "run3", 60)
    assert memory.force_unlock("prod") == "run3"
    assert memory.force_unlock("prod") is None
