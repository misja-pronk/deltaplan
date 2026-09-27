"""A step takes as long as it takes — and deltaplan never lies about one.

The bug these hold shut: every statement had a five-minute budget, including the
REPLACE of a table with four hundred gigabytes in it. At the deadline deltaplan
said the step had failed and moved on, while the statement kept running on the
warehouse — so the table was rewritten behind a run that reported a failure, the
lock stayed held until its TTL, and nothing was ever cancelled.

Now: a statement that outlives its budget is cancelled before it is reported;
the runner `apply` uses has no budget at all; Ctrl-C cancels what is running;
and while a step runs, whoever is watching hears about it every half minute and
the lock is kept alive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import pytest
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.connect import Connection
from deltaplan.executor import Executor, Heartbeating
from deltaplan.history import MemoryHistory
from deltaplan.introspect import (
    IntrospectionError,
    Introspector,
    Progress,
    WarehouseRunner,
    duration,
)
from deltaplan.model.plan import Step
from deltaplan.model.table import MANAGED_PROPERTY
from deltaplan.planning import plan_tables
from fake_warehouse import FakeWarehouse
from helpers import col, table

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

runner = CliRunner()


# ---------------------------------------------------------------------------
# a workspace client whose statements take a while
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, name: str) -> None:
        self.name = name


class _Status:
    def __init__(self, state: object, error: str | None = None) -> None:
        self.state = state
        self.error = type("Error", (), {"message": error})() if error else None


class _Response:
    def __init__(self, state: object, rows: list[list[str]] | None = None) -> None:
        from databricks.sdk.service.sql import StatementState

        self.statement_id = "01ef-abc"
        self.status = _Status(state)
        self.manifest = None
        self.result = None
        if state is StatementState.SUCCEEDED:
            columns = [type("Column", (), {"name": "n"})()]
            self.manifest = type(
                "Manifest", (), {"schema": type("Schema", (), {"columns": columns})()}
            )()
            self.result = type(
                "Result", (), {"data_array": rows or [["1"]], "next_chunk_index": None}
            )()


@dataclass
class SlowClient:
    """Answers RUNNING `polls` times, then whatever `then` is."""

    polls: int
    then: str = "SUCCEEDED"
    cancel_fails: bool = False
    interrupt_at: int | None = None
    asked: int = 0
    cancelled: list[str] = field(default_factory=list)

    @property
    def statement_execution(self) -> Any:
        return self

    def execute_statement(self, **_kwargs: object) -> _Response:
        from databricks.sdk.service.sql import StatementState

        return _Response(StatementState.RUNNING)

    def get_statement(self, statement_id: str) -> _Response:
        from databricks.sdk.service.sql import StatementState

        self.asked += 1
        if self.interrupt_at is not None and self.asked >= self.interrupt_at:
            raise KeyboardInterrupt
        if self.asked < self.polls:
            return _Response(StatementState.RUNNING)
        return _Response(getattr(StatementState, self.then))

    def cancel_execution(self, statement_id: str) -> None:
        if self.cancel_fails:
            raise RuntimeError("no such statement")
        self.cancelled.append(statement_id)


def patient(
    client: SlowClient,
    *,
    timeout_seconds: float | None,
    heartbeat: Callable[[Progress], None] | None = None,
    heartbeat_seconds: float = 30.0,
) -> WarehouseRunner:
    return WarehouseRunner(
        cast("WorkspaceClient", client),
        "w1",
        poll_seconds=0,
        timeout_seconds=timeout_seconds,
        heartbeat=heartbeat,
        heartbeat_seconds=heartbeat_seconds,
    )


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------


def test_a_statement_that_outlives_its_budget_is_cancelled_before_it_is_reported() -> (
    None
):
    """The bug: a budget that gave up on a statement without stopping it."""
    client = SlowClient(polls=1_000_000)
    with pytest.raises(IntrospectionError) as raised:
        patient(client, timeout_seconds=0).query("CREATE OR REPLACE TABLE t AS SELECT 1")
    assert client.cancelled == ["01ef-abc"], "the warehouse was told to stop it"
    said = str(raised.value)
    assert "cancelled it" in said
    assert "01ef-abc" in said, "the statement id, for whoever goes looking"
    assert "CREATE OR REPLACE TABLE t" in said


def test_a_cancel_that_fails_is_said_rather_than_hidden() -> None:
    client = SlowClient(polls=1_000_000, cancel_fails=True)
    with pytest.raises(IntrospectionError, match="could not cancel it"):
        patient(client, timeout_seconds=0).query("SELECT 1")


def test_without_a_budget_the_runner_waits_for_as_long_as_it_takes() -> None:
    client = SlowClient(polls=50)
    rows = patient(client, timeout_seconds=None).query("SELECT 1 AS n")
    assert rows == ({"n": "1"},)
    assert client.cancelled == []


def test_ctrl_c_cancels_the_statement_and_still_interrupts() -> None:
    """Whoever pressed it wants the statement stopped, not just the waiting: a
    REPLACE left running would finish behind their back."""
    client = SlowClient(polls=1_000_000, interrupt_at=3)
    with pytest.raises(KeyboardInterrupt):
        patient(client, timeout_seconds=None).query(
            "CREATE OR REPLACE TABLE t AS SELECT 1"
        )
    assert client.cancelled == ["01ef-abc"]


def test_the_heartbeat_hears_how_long_it_has_been() -> None:
    client = SlowClient(polls=5)
    beats: list[Progress] = []
    patient(
        client, timeout_seconds=None, heartbeat=beats.append, heartbeat_seconds=0
    ).query("SELECT 1 AS n")
    assert len(beats) == 5, "once per poll, with a zero interval"
    assert all(beat.statement_id == "01ef-abc" for beat in beats)
    assert beats[0].statement == "SELECT 1 AS n"
    assert beats[-1].elapsed_seconds >= beats[0].elapsed_seconds


def test_the_way_a_person_says_how_long() -> None:
    assert duration(47) == "47s"
    assert duration(312) == "5m 12s"
    assert duration(7380) == "2h 03m"


# ---------------------------------------------------------------------------
# the connection
# ---------------------------------------------------------------------------


def test_reads_have_a_budget_and_apply_has_none() -> None:
    connection = Connection(
        client=cast("WorkspaceClient", SlowClient(polls=0)), warehouse_id="w1"
    )
    reads = connection.runner
    steps = connection.patient()
    assert isinstance(reads, WarehouseRunner) and reads.timeout_seconds == 300.0
    assert isinstance(steps, WarehouseRunner) and steps.timeout_seconds is None


def test_a_hosts_own_runner_is_left_to_its_own_patience() -> None:
    fake = FakeWarehouse()
    assert Connection(runner=fake).patient() is fake


# ---------------------------------------------------------------------------
# the executor
# ---------------------------------------------------------------------------


@dataclass
class Beating:
    """A fake warehouse that reports progress the way `WarehouseRunner` does:
    a statement that 'takes' `ticks` heartbeats."""

    inner: FakeWarehouse
    ticks: int = 3
    heartbeat: Any = None
    beats: int = 0

    def query(self, statement: str) -> Any:
        if self.heartbeat is not None and not statement.upper().startswith(
            ("SELECT", "DESCRIBE", "SHOW")
        ):
            for tick in range(self.ticks):
                self.beats += 1
                self.heartbeat(Progress(statement, "01ef-abc", 1800.0 * (tick + 1)))
        return self.inner.query(statement)


def _plan_and_fake() -> tuple[FakeWarehouse, Any]:
    """A plan the executor will run: made the way `apply` makes one, against
    the fake it will then run against, so the fingerprint is the fake's own."""
    live = replace(
        table(col("id", "bigint"), name="main.sales.orders"),
        properties=((MANAGED_PROPERTY, "true"),),
    )
    desired = table(col("id", "bigint"), col("note", "string"), name="main.sales.orders")
    fake = FakeWarehouse.of(live)
    plan = plan_tables([desired], Introspector(fake), target="dev", tool_version="0")
    return fake, plan


def test_the_executor_hears_a_beating_runner_and_tells_the_observer() -> None:
    """`running`, with how long it has been — so a long rewrite is never silence."""
    fake, plan = _plan_and_fake()
    beating = Beating(fake)
    assert isinstance(beating, Heartbeating)
    heard: list[tuple[int, str, str | None]] = []
    executor = Executor(
        beating,
        Introspector(fake),
        MemoryHistory(),
        observer=lambda step, status, note: heard.append((step.id, status, note)),
    )
    result = executor.apply(plan)
    assert result.ok, result.error
    assert (1, "running", "30m 00s") in heard
    assert (1, "running", "1h 30m") in heard
    assert heard[-1][1] == "succeeded"
    assert beating.heartbeat is None, "put back when the run ends"


def test_a_long_step_keeps_the_lock_alive() -> None:
    """A rewrite can outlast the lock's TTL, and a second apply must not start
    halfway through this one: the heartbeat renews it past half the TTL."""
    fake, plan = _plan_and_fake()
    renewed: list[str] = []

    class Counting(MemoryHistory):
        def renew_lock(self, target: str, run_id: str, minutes: int) -> bool:
            renewed.append(run_id)
            return super().renew_lock(target, run_id, minutes)

    executor = Executor(
        Beating(fake, ticks=3), Introspector(fake), Counting(), lock_minutes=60
    )
    # Every beat here is thirty minutes further into the statement, so the
    # second and third each cross half the TTL since the last renewal.
    result = executor.apply(plan)
    assert result.ok, result.error
    assert len(renewed) >= 2, renewed


def test_a_quiet_runner_is_simply_quiet() -> None:
    """The fake warehouse, a replay, a host's runner: nothing to install on."""
    fake, plan = _plan_and_fake()
    heard: list[str] = []
    Executor(
        fake,
        Introspector(fake),
        MemoryHistory(),
        observer=lambda _s, status, _n: heard.append(status),
    ).apply(plan)
    assert "running" not in heard
    assert heard[-1] == "succeeded"


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def _step() -> Step:
    return Step(id=1, table="main.sales.orders", title="REPLACE TABLE", risk="rewrite")


def test_a_running_step_is_a_line_a_log_reader_can_follow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not a terminal here, so: one line at the start and one every five
    minutes — a half-minute cadence would drown a CI log."""
    cli._running.stop()
    cli._show_step(_step(), "running", "30s")
    cli._show_step(_step(), "running", "1m 00s")
    cli._show_step(_step(), "running", "5m 30s")
    cli._show_step(_step(), "succeeded", None)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    running = [line for line in lines if "running" in line]
    assert len(running) == 2, lines
    assert "30s" in running[0] and "5m 30s" in running[1]
    assert lines[-1].rstrip().endswith("ok")


def test_ctrl_c_during_apply_says_what_state_things_are_in(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    (tmp_path / "deltaplan.yml").write_text(
        "version: 1\nspecs: [tables]\ntargets:\n  dev:\n    default: true\n"
        "    vars: {catalog: main}\n    warehouse_id: w1\n"
    )
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "orders.yml").write_text(
        "table: ${catalog}.sales.orders\ncolumns:\n  - {name: id, type: bigint}\n"
    )
    fake = FakeWarehouse()
    fake.schemas.add("main.sales")
    monkeypatch.setattr(cli, "_connect", lambda *_a, **_k: Connection(runner=fake))
    monkeypatch.chdir(Path(tmp_path))

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.api, "apply", interrupted)
    result = runner.invoke(cli.app, ["apply", "--yes"])
    assert result.exit_code == 130, result.output
    assert "cancelled on the warehouse" in result.output
    assert "resumes from this step" in result.output
