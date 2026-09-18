"""`apply` and `force-unlock`, end to end against the fake warehouse.

The history store is swapped for the in-memory one, because what these tests are
about is the wiring: plan file in, statements out, resume on re-run. The Delta
history's own SQL is asserted in `test_history.py` and run for real by the
integration suite.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.cli import app
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector
from fake_warehouse import FakeWarehouse
from helpers import col, table

runner = CliRunner()

NAME = "main.sales.orders"

CONFIG = """
version: 1
specs: [tables]
history_schema: main.deltaplan
targets:
  dev:
    vars: {catalog: main}
    warehouse_id: abc123
"""

SPEC = """
table: ${catalog}.sales.orders
comment: Order facts
columns:
  - {name: order_id, type: bigint, nullable: false}
  - {name: amount, type: 'decimal(18,2)'}
  - {name: customer_ref, type: string, renamed_from: cust_id}
"""

LIVE = table(
    col("order_id", "bigint", nullable=False),
    col("amount", "decimal(10,2)"),
    col("cust_id", "string"),
    name=NAME,
    comment="Order facts",
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "deltaplan.yml").write_text(CONFIG)
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "orders.yml").write_text(SPEC)
    return tmp_path


@pytest.fixture
def history() -> MemoryHistory:
    return MemoryHistory()


@pytest.fixture
def warehouse(monkeypatch: pytest.MonkeyPatch, history: MemoryHistory) -> FakeWarehouse:
    fake = FakeWarehouse.of(LIVE)
    monkeypatch.setattr(cli, "_warehouse", lambda *_a, **_k: fake)
    monkeypatch.setattr(cli, "_history", lambda *_a, **_k: history)
    return fake


def write_plan(project: Path, destination: Path) -> None:
    result = runner.invoke(
        app,
        [
            "plan",
            "-t",
            "dev",
            "--config",
            str(project / "deltaplan.yml"),
            "--format",
            "json",
            "-o",
            str(destination),
        ],
    )
    assert result.exit_code == 0, result.output


def test_plan_then_apply(project: Path, warehouse: FakeWarehouse, tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)

    result = runner.invoke(
        app, ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    # The table was created by someone else; writing its spec claims it first.
    assert "CLAIM ownership" in result.output
    assert "enable typeWidening" in result.output
    assert "Applied 5 step(s)" in result.output

    live = Introspector(warehouse).table(NAME)
    assert live is not None
    assert live.table.column_names == ("order_id", "amount", "customer_ref")
    amount = live.table.column("amount")
    assert amount is not None
    from deltaplan.model.types import Decimal

    assert amount.type == Decimal(18, 2)


def test_a_plan_that_already_ran_is_refused_as_stale(
    project: Path, warehouse: FakeWarehouse, tmp_path: Path
) -> None:
    """Applying the same file twice is applying a stale plan, and is refused.

    The plan described a journey from one state to another; the journey has been
    made. Re-running the file can only be a mistake, so deltaplan says so rather
    than quietly doing nothing.
    """
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)
    arguments = ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]
    assert runner.invoke(app, arguments).exit_code == 0

    ddl = len(warehouse.ddl)
    again = runner.invoke(app, arguments)
    assert again.exit_code == 1
    assert "changed since this plan was made" in again.output
    assert len(warehouse.ddl) == ddl, "nothing may run a second time"


def test_replanning_after_an_apply_finds_nothing_to_do(
    project: Path, warehouse: FakeWarehouse, tmp_path: Path
) -> None:
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)
    assert (
        runner.invoke(
            app, ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]
        ).exit_code
        == 0
    )

    result = runner.invoke(
        app, ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "No changes. Live tables match your specs." in result.output


def test_a_failed_step_is_reported_and_resumes(
    project: Path, warehouse: FakeWarehouse, tmp_path: Path
) -> None:
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)
    arguments = ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]

    warehouse.failures["RENAME COLUMN"] = "connection reset"
    result = runner.invoke(app, arguments)
    assert result.exit_code == 1
    assert "Failed at step 5 of 5" in result.output
    assert "connection reset" in result.output
    assert "resumes" in result.output

    del warehouse.failures["RENAME COLUMN"]
    again = runner.invoke(app, arguments)
    assert again.exit_code == 0, again.output
    assert "Applied 1 step(s), skipped 4" in again.output


def test_a_destructive_plan_needs_the_flag(
    project: Path, warehouse: FakeWarehouse, tmp_path: Path
) -> None:
    (project / "tables" / "orders.yml").write_text(
        "table: ${catalog}.sales.orders\n"
        "comment: Order facts\n"
        "columns:\n"
        "  - {name: order_id, type: bigint, nullable: false}\n"
        "  - {name: amount, type: 'decimal(10,2)'}\n"
    )
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)
    arguments = ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]

    refused = runner.invoke(app, arguments)
    assert refused.exit_code == 1
    assert "--allow-destructive" in refused.output
    assert warehouse.ddl == []

    allowed = runner.invoke(app, [*arguments, "--allow-destructive"])
    assert allowed.exit_code == 0, allowed.output
    live = Introspector(warehouse).table(NAME)
    assert live is not None and "cust_id" not in live.table.column_names


def test_a_stale_plan_is_refused(
    project: Path, warehouse: FakeWarehouse, tmp_path: Path
) -> None:
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)
    warehouse.query("ALTER TABLE `main`.`sales`.`orders` ADD COLUMNS (`surprise` STRING)")

    result = runner.invoke(
        app, ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 1
    assert "changed since this plan was made" in result.output


def test_an_unreadable_plan_file(project: Path, tmp_path: Path) -> None:
    broken = tmp_path / "plan.json"
    broken.write_text(json.dumps({"format_version": 99}))
    result = runner.invoke(
        app, ["apply", str(broken), "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 1
    assert "plan format version" in result.output

    missing = runner.invoke(
        app,
        [
            "apply",
            str(tmp_path / "nope.json"),
            "--config",
            str(project / "deltaplan.yml"),
        ],
    )
    assert missing.exit_code == 1
    assert "cannot read" in missing.output


def test_apply_needs_a_history_schema(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeWarehouse.of(LIVE)
    monkeypatch.setattr(cli, "_warehouse", lambda *_a, **_k: fake)
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)
    (project / "deltaplan.yml").write_text(
        CONFIG.replace("history_schema: main.deltaplan\n", "")
    )

    result = runner.invoke(
        app, ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 1
    assert "history_schema" in result.output


def test_force_unlock(
    project: Path, warehouse: FakeWarehouse, history: MemoryHistory
) -> None:
    del warehouse  # the fixture is what points the CLI at the fake
    arguments = ["force-unlock", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    free = runner.invoke(app, arguments)
    assert free.exit_code == 0
    assert "was not locked" in free.output

    history.acquire_lock("dev", "run-that-died", 60)
    held = runner.invoke(app, arguments)
    assert held.exit_code == 0
    assert "run-that-died" in held.output


class StoppedWarehouse(FakeWarehouse):
    """A warehouse that answers nothing, the way a stopped one fails."""

    def query(self, statement: str) -> tuple[dict[str, str | None], ...]:
        from deltaplan.introspect import IntrospectionError

        raise IntrospectionError(f"FAILED: the warehouse is stopped\n  {statement}")


def test_a_warehouse_error_is_a_message_not_a_traceback(
    project: Path,
    warehouse: FakeWarehouse,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)
    del warehouse  # planned against the fixture; applied against a stopped one
    monkeypatch.setattr(cli, "_warehouse", lambda *_a, **_k: StoppedWarehouse())
    result = runner.invoke(
        app, ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 1
    assert "the warehouse is stopped" in result.output
    assert "Traceback" not in result.output
