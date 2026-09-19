"""The first hour: an empty directory, `import`, `plan`, `apply`.

`import` without a project writes `deltaplan.yml` — one target whose catalog is
the one imported from, so the specs say `${catalog}` — and the commands after it
work as they are, with no editing. This is the path the getting-started page
walks, so it is tested end to end against the fake warehouse.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.history import MemoryHistory
from deltaplan.model.table import Grant
from fake_warehouse import FakeWarehouse
from helpers import col, table

runner = CliRunner()


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeWarehouse:
    fake = FakeWarehouse.of(
        table(
            col("customer_id", "bigint", nullable=False),
            col("email", "string"),
            name="main.crm.customers",
            comment="One row per customer",
            grants=(Grant("analysts", ("SELECT",)),),
        ),
        table(col("event_id", "bigint"), name="main.crm.events"),
    )
    history = MemoryHistory()
    monkeypatch.setattr(cli, "_warehouse", lambda *_a, **_k: fake)
    monkeypatch.setattr(cli, "_history", lambda *_a, **_k: history)
    monkeypatch.chdir(tmp_path)
    return fake


def test_import_plan_apply_from_nothing(workspace: FakeWarehouse, tmp_path: Path) -> None:
    imported = runner.invoke(cli.app, ["import", "main.crm", "--warehouse-id", "abc123"])
    assert imported.exit_code == 0, imported.output
    assert "+ deltaplan.yml (target dev: catalog main)" in imported.output
    assert "Next: deltaplan plan" in imported.output

    project = (tmp_path / "deltaplan.yml").read_text()
    assert "catalog: main" in project and "warehouse_id: abc123" in project
    spec = (tmp_path / "tables" / "customers.yml").read_text()
    assert "table: ${catalog}.crm.customers" in spec

    planned = runner.invoke(cli.app, ["plan"])
    assert planned.exit_code == 0, planned.output
    assert "CLAIM ownership" in planned.output
    assert "2 change, 0 destroy · 2 steps" in planned.output

    applied = runner.invoke(cli.app, ["apply", "--yes"])
    assert applied.exit_code == 0, applied.output
    assert "No changes." in runner.invoke(cli.app, ["plan"]).output


def test_an_existing_project_is_left_as_it_is(
    workspace: FakeWarehouse, tmp_path: Path
) -> None:
    (tmp_path / "deltaplan.yml").write_text(
        "version: 1\nspecs: [specs]\ntargets:\n  prod:\n    vars: {catalog: main}\n"
    )
    result = runner.invoke(cli.app, ["import", "main.crm"])
    assert result.exit_code == 0, result.output
    assert "deltaplan.yml" not in result.output
    assert (tmp_path / "specs" / "customers.yml").exists()
