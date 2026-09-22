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
from deltaplan.connect import Connection
from deltaplan.loader import load_table
from deltaplan.model.table import Table
from helpers import FakeRunner, Row, col, fake_runner, table

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
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake)
    )
    return fake


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("args", [["version"], ["--version"]])
def test_version_prints_the_package_version(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert result.stdout == f"deltaplan {package_version()}\n"


def test_bare_invocation_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Declarative plan/apply for Databricks SQL tables." in result.stdout


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_accepts_a_good_spec(project: Path) -> None:
    result = runner.invoke(app, ["validate", "--config", str(project / "deltaplan.yml")])
    assert result.exit_code == 0, result.output
    assert "1 spec OK" in result.output


def test_validate_reports_the_file_and_line(project: Path) -> None:
    spec = project / "tables" / "orders.yml"
    spec.write_text("table: main.sales.orders\ncolumns:\n  - name: a\n    typo: int\n")
    result = runner.invoke(app, ["validate", "--config", str(project / "deltaplan.yml")])
    assert result.exit_code == 1
    assert "orders.yml:4:5" in result.output
    assert "unknown key 'typo'" in result.output


def test_paths_are_shown_from_where_you_are(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "tables" / "orders.yml").write_text(
        "table: main.sales.orders\ncolumns:\n  - name: a\n    typo: int\n"
    )
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["validate"])
    assert result.output.startswith("tables/orders.yml:4:5: unknown key 'typo'")


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


@pytest.mark.usefixtures("live")
def test_plan_renders_the_diff(project: Path) -> None:
    result = runner.invoke(
        app, ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "sales.orders   ~ update  (412 GB)" in result.output
    assert "enable typeWidening" in result.output
    assert "→ customer_ref (was cust_id)" in result.output
    assert "Plan: 0 add, 1 change, 0 destroy" in result.output


@pytest.mark.usefixtures("live")
def test_plan_writes_json(project: Path, tmp_path: Path) -> None:
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
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake)
    )
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
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake)
    )
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
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake)
    )
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
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake)
    )
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


@pytest.mark.usefixtures("live")
def test_plan_refuses_to_run_with_spec_errors(project: Path) -> None:
    (project / "tables" / "orders.yml").write_text(
        "table: ${catalog}.sales.orders\ncluster_by: [nope]\n"
        "columns: [{name: id, type: bigint}]\n"
    )
    result = runner.invoke(
        app, ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 1
    assert "Refusing to plan" in result.output


@pytest.mark.usefixtures("live")
def test_plan_as_markdown(project: Path) -> None:
    result = runner.invoke(
        app,
        ["plan", "-t", "dev", "--config", str(project / "deltaplan.yml"), "-f", "md"],
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith("<!-- deltaplan:plan:dev -->")
    assert "```diff" in result.output


@pytest.mark.usefixtures("live")
def test_show_renders_a_saved_plan_without_a_warehouse(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_file = tmp_path / "plan.json"
    written = runner.invoke(
        app,
        [
            "plan",
            "-t",
            "dev",
            "--config",
            str(project / "deltaplan.yml"),
            "-f",
            "json",
            "-o",
            str(plan_file),
        ],
    )
    assert written.exit_code == 0, written.output

    # `show` must not reach for a warehouse: the plan file is the whole story.
    def no_warehouse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("show asked for a warehouse")

    monkeypatch.setattr(cli, "_connect", no_warehouse)
    shown = runner.invoke(app, ["show", str(plan_file)])
    assert shown.exit_code == 0, shown.output
    assert "sales.orders   ~ update  (412 GB)" in shown.output

    as_markdown = runner.invoke(app, ["show", str(plan_file), "-f", "md"])
    assert as_markdown.exit_code == 0
    assert "### 🟡 deltaplan plan · `dev`" in as_markdown.output


@pytest.mark.usefixtures("live")
def test_the_plan_file_is_the_plan_whatever_the_screen_shows(
    project: Path, tmp_path: Path
) -> None:
    """`plan -o plan.json` then `apply plan.json`, as the docs have it: the
    terminal gets the readable plan, the file gets what `apply` reads."""
    plan_file = tmp_path / "plan.json"
    config = str(project / "deltaplan.yml")
    result = runner.invoke(app, ["plan", "-t", "dev", "-c", config, "-o", str(plan_file)])
    assert result.exit_code == 0, result.output
    assert "sales.orders   ~ update" in result.output
    assert json.loads(plan_file.read_text())["target"] == "dev"


@pytest.mark.usefixtures("live")
def test_drift_exits_2_when_live_tables_have_moved(project: Path) -> None:
    result = runner.invoke(
        app, ["drift", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 2, result.output
    assert "Drift: 1 table differs from its spec." in result.output


def test_drift_exits_0_when_in_sync(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_warehouse import FakeWarehouse

    (project / "tables" / "orders.yml").write_text(
        "table: ${catalog}.sales.orders\n"
        "comment: Order facts\n"
        "columns:\n"
        "  - {name: order_id, type: bigint, nullable: false}\n"
        "  - {name: amount, type: 'decimal(10,2)'}\n"
        "  - {name: cust_id, type: string}\n"
    )
    fake = FakeWarehouse.of(_live_orders(managed=True))
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake)
    )
    result = runner.invoke(
        app, ["drift", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "No changes" in result.output


def test_drift_ignores_what_deltaplan_never_claimed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_warehouse import FakeWarehouse

    (project / "tables" / "orders.yml").write_text(
        "table: ${catalog}.sales.orders\n"
        "comment: Order facts\n"
        "columns:\n"
        "  - {name: order_id, type: bigint, nullable: false}\n"
        "  - {name: amount, type: 'decimal(10,2)'}\n"
        "  - {name: cust_id, type: string}\n"
    )
    # An unmanaged table beside it is not drift: nobody asked deltaplan to keep it.
    fake = FakeWarehouse.of(_live_orders(managed=True), _stranger(managed=False))
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake)
    )
    result = runner.invoke(
        app, ["drift", "-t", "dev", "--config", str(project / "deltaplan.yml")]
    )
    assert result.exit_code == 0, result.output


@pytest.mark.usefixtures("live")
def test_drift_as_markdown_has_its_own_marker(project: Path) -> None:
    result = runner.invoke(
        app,
        ["drift", "-t", "dev", "--config", str(project / "deltaplan.yml"), "-f", "md"],
    )
    assert result.exit_code == 2
    assert result.output.startswith("<!-- deltaplan:drift:dev -->")


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("live")
def test_import_writes_specs_that_load_back(project: Path, tmp_path: Path) -> None:
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
    # An editor finds the schema from the first line; the spec follows.
    first, rest = text.split("\n", 1)
    assert first.startswith("# yaml-language-server: $schema=")
    assert rest.startswith("table: ${catalog}.sales.orders")
    # deltaplan's own marker is not something you should have to write.
    assert "deltaplan.managed" not in text

    table = load_table(written, {"catalog": "main"})
    assert table.name == "main.sales.orders"
    assert table.column_names == ("order_id", "amount", "cust_id")
    assert table.comment == "Order facts"
    order_id = table.column("order_id")
    assert order_id is not None and order_id.nullable is False


@pytest.mark.usefixtures("project")
def test_import_needs_a_qualified_schema() -> None:
    result = runner.invoke(app, ["import", "sales"])
    assert result.exit_code == 1
    assert "catalog.schema" in result.output


@pytest.mark.usefixtures("project")
def test_import_writes_views_and_reports_what_it_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = fake_runner(
        tables=(
            {"table_name": "v_orders", "table_type": "VIEW", "data_source_format": None},
            {
                "table_name": "daily",
                "table_type": "MATERIALIZED_VIEW",
                "data_source_format": "DELTA",
            },
        ),
    )
    fake.responses["information_schema.views"] = (
        {"table_name": "v_orders", "view_definition": "SELECT * FROM main.sales.orders"},
    )
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake)
    )
    result = runner.invoke(app, ["import", "main.sales", "-o", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    written = (tmp_path / "out" / "v_orders.yml").read_text()
    assert written.split("\n", 1)[1].startswith("view: main.sales.v_orders")
    assert "query: |" in written, "a query reads like SQL, not one long line"
    assert "skipped main.sales.daily (materialized view)" in result.output


@pytest.mark.usefixtures("project")
def test_import_with_nothing_to_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake_runner())
    )
    result = runner.invoke(app, ["import", "main.sales", "-o", str(tmp_path / "out")])
    assert "No Delta tables, views or functions found" in result.output


def test_import_as_sql_falls_back_to_yaml_for_what_sql_cannot_say(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace

    from deltaplan.loader import load_spec
    from deltaplan.model.types import Field, Mask, Primitive
    from fake_warehouse import FakeWarehouse

    plain = table(col("id", "bigint"), name="main.sales.orders", comment="It's plain")
    masked = replace(
        table(col("id", "bigint"), name="main.sales.people"),
        columns=(Field("email", Primitive("string"), mask=Mask("main.sales.m")),),
    )
    fake = FakeWarehouse.of(plain, masked)
    monkeypatch.setattr(
        cli, "_connect", lambda *_args, **_kwargs: Connection(runner=fake)
    )
    destination = tmp_path / "imported"
    result = runner.invoke(
        app,
        [
            "import",
            "main.sales",
            "-o",
            str(destination),
            "-f",
            "sql",
            "-t",
            "dev",
            "--config",
            str(project / "deltaplan.yml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (destination / "orders.sql").exists()
    assert (destination / "people.yml").exists()
    # Collapsed: the terminal wraps long paths.
    assert "YAML: SQL can't say column masks" in " ".join(result.output.split())
    loaded = load_spec(destination / "orders.sql", {"catalog": "main"})
    assert loaded.comment == "It's plain"


@pytest.mark.parametrize("kind", ["spec", "project"])
def test_schema_prints_the_json_schema(kind: str) -> None:
    import json

    from deltaplan import spec_schema

    result = runner.invoke(app, ["schema", kind])
    assert result.exit_code == 0, result.output
    expected = (
        spec_schema.spec_schema() if kind == "spec" else spec_schema.project_schema()
    )
    assert json.loads(result.output) == expected


# ---------------------------------------------------------------------------
# what the terminal shows
# ---------------------------------------------------------------------------


def test_apply_shows_each_steps_risk(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[meta]` is also Rich markup: printed as is, it vanished."""
    from deltaplan.history import MemoryHistory
    from fake_warehouse import FakeWarehouse

    monkeypatch.setattr(
        cli, "_connect", lambda *_a, **_k: Connection(runner=FakeWarehouse())
    )
    monkeypatch.setattr(cli, "_history", lambda *_a, **_k: MemoryHistory())
    config, plan_file = str(project / "deltaplan.yml"), str(tmp_path / "plan.json")
    assert runner.invoke(app, ["plan", "-t", "dev", "-c", config, "-o", plan_file])
    result = runner.invoke(app, ["apply", plan_file, "-c", config])
    assert result.exit_code == 0, result.output
    assert "CREATE TABLE orders" in result.output
    assert "[meta] ok" in result.output


def test_an_error_quotes_the_spec_as_written(project: Path) -> None:
    """`array[int]` is a common slip, and `[int]` is Rich markup: printed as is,
    the message said `array` and its caret pointed at nothing."""
    (project / "tables" / "orders.yml").write_text(
        "table: main.sales.orders\ncolumns:\n  - name: tags\n    type: array[int]\n"
    )
    result = runner.invoke(app, ["validate", "--config", str(project / "deltaplan.yml")])
    assert result.exit_code == 1
    assert "  array[int]\n       ^" in result.output
