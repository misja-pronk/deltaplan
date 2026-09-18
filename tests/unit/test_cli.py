"""The commands: `validate`, `import` and `plan`, driven end to end.

A fake `SqlRunner` stands in for the workspace, so these exercise the real
loader, differ, planner and renderers — only the network is replaced.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.cli import app, package_version
from deltaplan.loader import load_table
from deltaplan.model.table import Table
from helpers import FakeRunner, Row, fake_runner

runner = CliRunner()

CONFIG = """
version: 1
specs: [tables]
targets:
  dev:
    vars: {catalog: main}
    warehouse_id: abc123
"""

SPEC = """
table: ${catalog}.sales.orders
comment: Order facts
columns:
  - name: order_id
    type: bigint
    nullable: false
  - name: amount
    type: decimal(18,2)
  - name: customer_ref
    type: string
    renamed_from: cust_id
"""

LIVE_TABLE: tuple[Row, ...] = (
    {
        "table_name": "orders",
        "comment": "Order facts",
        "table_type": "MANAGED",
        "data_source_format": "DELTA",
    },
)
LIVE_COLUMNS: tuple[Row, ...] = (
    {
        "table_name": "orders",
        "column_name": "order_id",
        "full_data_type": "bigint",
        "is_nullable": "NO",
        "comment": None,
    },
    {
        "table_name": "orders",
        "column_name": "amount",
        "full_data_type": "decimal(10,2)",
        "is_nullable": "YES",
        "comment": None,
    },
    {
        "table_name": "orders",
        "column_name": "cust_id",
        "full_data_type": "string",
        "is_nullable": "YES",
        "comment": None,
    },
)
LIVE_DETAIL: tuple[Row, ...] = (
    {
        "clusteringColumns": "[]",
        "sizeInBytes": "442381631488",
        "properties": '{"deltaplan.managed":"true"}',
    },
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "deltaplan.yml").write_text(CONFIG)
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "orders.yml").write_text(SPEC)
    return tmp_path


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Every command that would talk to a warehouse gets this instead."""
    fake = fake_runner(
        tables=LIVE_TABLE,
        columns=LIVE_COLUMNS,
        detail=LIVE_DETAIL,
        history=({"version": "17"},),
    )
    monkeypatch.setattr(cli, "_warehouse", lambda *_args, **_kwargs: fake)
    return fake


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def test_version_command_prints_the_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"deltaplan {package_version()}" in result.stdout


def test_bare_invocation_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Declarative plan/apply for Databricks SQL tables." in result.stdout


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_accepts_a_good_spec(project: Path) -> None:
    result = runner.invoke(app, ["validate", "--config", str(project / "deltaplan.yml")])
    assert result.exit_code == 0, result.output
    assert "1 spec(s) OK" in result.output


def test_validate_reports_the_file_and_line(project: Path) -> None:
    spec = project / "tables" / "orders.yml"
    spec.write_text("table: main.sales.orders\ncolumns:\n  - name: a\n    typo: int\n")
    result = runner.invoke(app, ["validate", "--config", str(project / "deltaplan.yml")])
    assert result.exit_code == 1
    assert "orders.yml:4:5" in result.output
    assert "unknown key 'typo'" in result.output


def test_validate_fails_on_a_lint_error(project: Path) -> None:
    spec = project / "tables" / "orders.yml"
    spec.write_text(
        "table: ${catalog}.sales.orders\n"
        "columns:\n"
        "  - {name: id, type: bigint}\n"
        "constraints:\n"
        "  - primary_key: [id]\n"
    )
    result = runner.invoke(app, ["validate", "--config", str(project / "deltaplan.yml")])
    assert result.exit_code == 1
    assert "must be declared nullable: false" in result.output


def test_validate_takes_explicit_paths(project: Path) -> None:
    result = runner.invoke(app, ["validate", str(project / "tables" / "orders.yml")])
    # Without a target the ${catalog} variable is undefined, which is an error
    # worth reporting rather than silently substituting an empty string.
    assert result.exit_code == 1
    assert "undefined variable ${catalog}" in result.output


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_plan_renders_the_diff(project: Path, live: FakeRunner) -> None:
    result = runner.invoke(
        app, ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "sales.orders   ~ update  (412 GB)" in result.output
    assert "enable typeWidening" in result.output
    assert "→ customer_ref (was cust_id)" in result.output
    assert "Plan: 0 add, 1 change, 0 destroy" in result.output


def test_plan_writes_json(project: Path, live: FakeRunner, tmp_path: Path) -> None:
    destination = tmp_path / "plan.json"
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
    document = json.loads(destination.read_text())
    assert document["target"] == "dev"
    assert document["summary"]["highest_risk"] == "feature"
    assert document["state_fingerprint"] and document["spec_hash"]
    titles = [step["title"] for step in document["steps"]]
    assert titles == [
        "enable typeWidening",
        "ALTER COLUMN TYPE",
        "enable columnMapping",
        "RENAME COLUMN",
    ]
    # The restore point is only queried for tables that actually change.
    assert document["tables"][0]["facts"]["delta_version"] == 17


def test_plan_with_no_changes(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "tables" / "orders.yml").write_text(
        "table: ${catalog}.sales.orders\n"
        "comment: Order facts\n"
        "columns:\n"
        "  - {name: order_id, type: bigint, nullable: false}\n"
        "  - {name: amount, type: 'decimal(10,2)'}\n"
        "  - {name: cust_id, type: string}\n"
    )
    fake = fake_runner(tables=LIVE_TABLE, columns=LIVE_COLUMNS, detail=LIVE_DETAIL)
    monkeypatch.setattr(cli, "_warehouse", lambda *_args, **_kwargs: fake)
    result = runner.invoke(
        app, ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "No changes. Live tables match your specs." in result.output


def _live_orders(*, managed: bool) -> Table:
    from helpers import col, table

    return table(
        col("order_id", "bigint", nullable=False),
        col("amount", "decimal(10,2)"),
        col("cust_id", "string"),
        name="main.sales.orders",
        comment="Order facts",
        properties=(("deltaplan.managed", "true"),) if managed else (),
    )


def _stranger(*, managed: bool) -> Table:
    from helpers import col, table

    return table(
        col("id", "bigint"),
        name="main.sales.someone_elses",
        properties=(("deltaplan.managed", "true"),) if managed else (),
    )


def test_plan_reports_live_tables_no_spec_describes(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_warehouse import FakeWarehouse

    fake = FakeWarehouse.of(_live_orders(managed=True), _stranger(managed=False))
    monkeypatch.setattr(cli, "_warehouse", lambda *_args, **_kwargs: fake)
    result = runner.invoke(
        app, ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "1 unmanaged table in these schemas, left untouched" in result.output
    assert "main.sales.someone_elses" in result.output
    assert "0 destroy" in result.output


def test_an_orphaned_table_stays_in_an_additive_schema(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_warehouse import FakeWarehouse

    # deltaplan created it, but its spec is gone.
    fake = FakeWarehouse.of(_live_orders(managed=True), _stranger(managed=True))
    monkeypatch.setattr(cli, "_warehouse", lambda *_args, **_kwargs: fake)
    result = runner.invoke(
        app, ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "1 managed table has no spec; the schema is additive, so they stay" in (
        result.output
    )
    assert "0 destroy" in result.output


def test_an_orphaned_table_is_dropped_in_a_strict_schema(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_warehouse import FakeWarehouse

    (project / "deltaplan.yml").write_text(
        CONFIG + "schemas:\n  ${catalog}.sales: strict\n"
    )
    fake = FakeWarehouse.of(
        _live_orders(managed=True), _stranger(managed=True), _stranger_unmanaged()
    )
    monkeypatch.setattr(cli, "_warehouse", lambda *_args, **_kwargs: fake)
    result = runner.invoke(
        app, ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "sales.someone_elses   - destroy" in result.output
    assert "DROP TABLE" in result.output
    assert "1 destroy" in result.output
    # Strict never reaches a table deltaplan didn't create.
    assert "main.sales.not_ours" in result.output
    assert "sales.not_ours   - destroy" not in result.output


def _stranger_unmanaged() -> Table:
    from helpers import col, table

    return table(col("id", "bigint"), name="main.sales.not_ours")


def test_plan_needs_a_target_when_there_are_several(project: Path) -> None:
    (project / "deltaplan.yml").write_text(
        CONFIG + "  prod:\n    vars: {catalog: prod}\n"
    )
    result = runner.invoke(app, ["plan", "--config", str(project / "deltaplan.yml")])
    assert result.exit_code == 1
    assert "Pick a target with -t" in result.output


def test_plan_refuses_to_run_with_spec_errors(project: Path, live: FakeRunner) -> None:
    (project / "tables" / "orders.yml").write_text(
        "table: ${catalog}.sales.orders\ncluster_by: [nope]\n"
        "columns: [{name: id, type: bigint}]\n"
    )
    result = runner.invoke(
        app, ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 1
    assert "Refusing to plan" in result.output


def test_markdown_is_not_here_yet(project: Path) -> None:
    result = runner.invoke(
        app,
        ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml"), "-f", "md"],
    )
    assert result.exit_code == 1
    assert "Markdown renderer arrives with the CI milestone" in result.output


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def test_import_writes_specs_that_load_back(
    project: Path, live: FakeRunner, tmp_path: Path
) -> None:
    destination = tmp_path / "imported"
    result = runner.invoke(
        app,
        [
            "import",
            "main.sales",
            "-o",
            str(destination),
            "-t",
            "dev",
            "--config",
            str(project / "deltaplan.yml"),
        ],
    )
    assert result.exit_code == 0, result.output
    written = destination / "orders.yml"
    assert written.exists()

    text = written.read_text()
    # The target's catalog comes back out as a variable, so one spec fits all.
    assert text.startswith("table: ${catalog}.sales.orders")
    # deltaplan's own marker is not something you should have to write.
    assert "deltaplan.managed" not in text

    table = load_table(written, {"catalog": "main"})
    assert table.name == "main.sales.orders"
    assert table.column_names == ("order_id", "amount", "cust_id")
    assert table.comment == "Order facts"
    order_id = table.column("order_id")
    assert order_id is not None and order_id.nullable is False


def test_import_needs_a_qualified_schema(project: Path) -> None:
    result = runner.invoke(app, ["import", "sales"])
    assert result.exit_code == 1
    assert "catalog.schema" in result.output


def test_import_reports_what_it_skipped(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = fake_runner(
        tables=(
            {"table_name": "v_orders", "table_type": "VIEW", "data_source_format": None},
        ),
    )
    monkeypatch.setattr(cli, "_warehouse", lambda *_args, **_kwargs: fake)
    result = runner.invoke(app, ["import", "main.sales", "-o", str(tmp_path / "out")])
    assert "skipped main.sales.v_orders" in result.output
    assert "No Delta tables found" in result.output
