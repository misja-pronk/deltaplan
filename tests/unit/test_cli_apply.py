"""`apply` and `force-unlock`, end to end against the fake warehouse.

The history store is swapped for the in-memory one, because what these tests are
about is the wiring: plan file in, statements out, resume on re-run. The Delta
history's own SQL is asserted in `test_history.py` and run for real by the
integration suite.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from deltaplan import cli
from deltaplan.cli import app
from deltaplan.connect import Connection
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
    monkeypatch.setattr(cli, "_connect", lambda *_a, **_k: Connection(runner=fake))
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
    assert "Applied 5 steps" in result.output

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


@pytest.mark.usefixtures("warehouse")
def test_replanning_after_an_apply_finds_nothing_to_do(
    project: Path, tmp_path: Path
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
    assert "Applied 1 step, skipped 4" in again.output


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


def test_apply_without_a_history_schema_says_what_that_costs(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It applies — and says once that nothing is recorded and nothing locked."""
    fake = FakeWarehouse.of(LIVE)
    monkeypatch.setattr(cli, "_connect", lambda *_a, **_k: Connection(runner=fake))
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)
    (project / "deltaplan.yml").write_text(
        CONFIG.replace("history_schema: main.deltaplan\n", "")
    )

    result = runner.invoke(
        app, ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "No history_schema" in result.output
    assert "takes no lock" in result.output
    assert not [name for name in fake.schemas if "deltaplan" in name]


def test_force_unlock_without_a_history_schema_has_nothing_to_unlock(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWarehouse.of(LIVE)
    monkeypatch.setattr(cli, "_connect", lambda *_a, **_k: Connection(runner=fake))
    (project / "deltaplan.yml").write_text(
        CONFIG.replace("history_schema: main.deltaplan\n", "")
    )
    result = runner.invoke(
        app, ["force-unlock", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "no lock" in result.output.lower()


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
    monkeypatch.setattr(
        cli, "_connect", lambda *_a, **_k: Connection(runner=StoppedWarehouse())
    )
    result = runner.invoke(
        app, ["apply", str(plan_file), "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 1
    assert "the warehouse is stopped" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# apply without a plan file: plan, show, ask, run
# ---------------------------------------------------------------------------


def apply_now(project: Path, *args: str, answer: str | None = None) -> Result:
    config = ["--config", str(project / "deltaplan.yml")]
    return runner.invoke(app, ["apply", *config, *args], input=answer)


def test_apply_plans_shows_and_asks(project: Path, warehouse: FakeWarehouse) -> None:
    result = apply_now(project, answer="y\n")
    assert result.exit_code == 0, result.output
    assert "sales.orders   ~ update" in result.output, "the plan is shown first"
    assert "Apply 5 steps to dev?" in result.output
    assert "Applied 5 steps" in result.output
    assert warehouse.tables[NAME].column_names == ("order_id", "amount", "customer_ref")


@pytest.mark.parametrize("answer", ["n\n", None])
def test_no_answer_is_no(
    project: Path, warehouse: FakeWarehouse, answer: str | None
) -> None:
    """`n`, or a closed stdin as in CI without --yes: nothing runs."""
    result = apply_now(project, answer=answer)
    assert result.exit_code == 1
    assert "Nothing applied." in result.output
    assert warehouse.tables[NAME].column_names == ("order_id", "amount", "cust_id")


def test_yes_skips_the_question(project: Path, warehouse: FakeWarehouse) -> None:
    result = apply_now(project, "--yes")
    assert result.exit_code == 0, result.output
    assert "Apply 5 steps" not in result.output
    assert "Applied 5 steps" in result.output


def test_nothing_to_do_asks_nothing(project: Path, warehouse: FakeWarehouse) -> None:
    assert apply_now(project, "--yes").exit_code == 0
    again = apply_now(project)
    assert again.exit_code == 0
    assert "No changes." in again.output and "Apply" not in again.output


def test_a_destructive_plan_is_refused_before_asking(
    project: Path, warehouse: FakeWarehouse
) -> None:
    (project / "tables" / "orders.yml").write_text(
        SPEC.replace(
            "  - {name: customer_ref, type: string, renamed_from: cust_id}\n", ""
        )
    )
    result = apply_now(project, answer="y\n")
    assert result.exit_code == 1
    assert "--allow-destructive" in result.output and "Apply" not in result.output
    assert "cust_id" in warehouse.tables[NAME].column_names


def test_a_saved_plan_takes_no_selection(
    project: Path, warehouse: FakeWarehouse, tmp_path: Path
) -> None:
    plan_file = tmp_path / "plan.json"
    write_plan(project, plan_file)
    result = apply_now(project, str(plan_file), "--select", "orders")
    assert result.exit_code == 1
    assert "A saved plan already says what it does" in result.output


# ---------------------------------------------------------------------------
# --select
# ---------------------------------------------------------------------------

STRICT = CONFIG + "schemas:\n  main.sales: strict\n"
OTHER = "table: ${catalog}.sales.customers\ncolumns:\n  - {name: id, type: bigint}\n"


@pytest.fixture
def two_tables(project: Path, warehouse: FakeWarehouse) -> Path:
    (project / "deltaplan.yml").write_text(STRICT)
    (project / "tables" / "customers.yml").write_text(OTHER)
    assert apply_now(project, "--yes").exit_code == 0
    return project


@pytest.mark.parametrize("pattern", ["customers", "sales.customers", "main.sales.cust*"])
def test_select_plans_only_what_it_names(
    two_tables: Path, warehouse: FakeWarehouse, pattern: str
) -> None:
    (two_tables / "tables" / "customers.yml").write_text(
        OTHER.replace("bigint}", "bigint, comment: Customer id}")
    )
    (two_tables / "tables" / "orders.yml").write_text(
        SPEC.replace("comment: Order facts", "comment: Changed")
    )
    config = ["--config", str(two_tables / "deltaplan.yml")]
    result = runner.invoke(app, ["plan", *config, "--select", pattern])
    assert result.exit_code == 0, result.output
    assert "sales.customers" in result.output
    assert "sales.orders" not in result.output


def test_a_selection_never_drops_what_it_leaves_out(
    two_tables: Path, warehouse: FakeWarehouse
) -> None:
    """The schema is strict, and `orders` is managed: planned without it, a
    selection must not take it for a table whose spec is gone."""
    config = ["--config", str(two_tables / "deltaplan.yml")]
    result = runner.invoke(app, ["plan", *config, "--select", "customers"])
    assert result.exit_code == 0, result.output
    assert "destroy" not in result.output.replace("0 destroy", "")
    assert "No changes." in result.output


def test_a_selection_that_names_nothing_is_an_error(
    project: Path, warehouse: FakeWarehouse
) -> None:
    config = ["--config", str(project / "deltaplan.yml")]
    result = runner.invoke(app, ["plan", *config, "--select", "ordrs"])
    assert result.exit_code == 1
    assert "--select ordrs matches no spec." in result.output
