"""Every way the setup was wrong in this project's first week of real use.

Each check exists because something surfaced late and unhelpfully once: a
warehouse that wouldn't start became an error halfway through an apply, a
metastore counting dropped tables became QUOTA_EXCEEDED on the fifth step, a
typo in `specs:` became a traceback. These hold the checks to saying what was
found *and* what to do — and to changing nothing while they look.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.connect import Connection, NotConnected
from deltaplan.doctor import Finding, look, worst
from deltaplan.loader import Project
from fake_warehouse import FakeWarehouse

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

PROJECT = """\
specs: [tables]
history_schema: ${catalog}.deltaplan
targets:
  dev:
    default: true
    vars: {catalog: main}
"""
SPEC = "table: ${catalog}.sales.orders\ncolumns:\n  - {name: id, type: bigint}\n"


@dataclass
class _Warehouse:
    name: str = "Serverless Starter"
    state: str = "RUNNING"
    id: str = "w1"


@dataclass
class _Quota:
    quota_count: int = 90
    quota_limit: int = 500


RUNNING = _Warehouse()
ROOM_TO_SPARE = _Quota()


class FakeClient:
    """Just enough workspace to answer what `doctor` asks."""

    def __init__(
        self,
        *,
        warehouse: _Warehouse | None = RUNNING,
        quota: _Quota | None = ROOM_TO_SPARE,
        user: str = "you@example.com",
    ) -> None:
        self._warehouse = warehouse
        self._quota = quota
        self._user = user
        self.config = type("Config", (), {"host": "https://example.databricks.com"})()

    @property
    def current_user(self) -> Any:
        me = type("Me", (), {"user_name": self._user})()
        return type("Api", (), {"me": staticmethod(lambda: me)})()

    @property
    def warehouses(self) -> Any:
        found = self._warehouse

        def get(_id: str) -> _Warehouse:
            if found is None:
                raise RuntimeError("no such warehouse")
            return found

        return type("Api", (), {"get": staticmethod(get)})()

    @property
    def metastores(self) -> Any:
        current = type("Metastore", (), {"metastore_id": "m1"})()
        return type("Api", (), {"current": staticmethod(lambda: current)})()

    @property
    def resource_quotas(self) -> Any:
        quota = self._quota

        def get_quota(**_kwargs: object) -> Any:
            return type("Answer", (), {"quota_info": quota})()

        return type("Api", (), {"get_quota": staticmethod(get_quota)})()


def project_at(tmp_path: Path, config: str = PROJECT, spec: str | None = SPEC) -> Project:
    (tmp_path / "tables").mkdir(exist_ok=True)
    (tmp_path / "deltaplan.yml").write_text(config)
    if spec is not None:
        (tmp_path / "tables" / "orders.yml").write_text(spec)
    return Project.load(tmp_path / "deltaplan.yml")


def findings(
    project: Project | None,
    client: FakeClient | None = None,
    fake: FakeWarehouse | None = None,
) -> dict[str, Finding]:
    target = project.default if project else None

    def connect() -> Connection:
        if client is None:
            raise NotConnected(
                "can't connect: default auth: cannot configure default credentials"
            )
        warehouse = fake if fake is not None else FakeWarehouse()
        return Connection(
            client=cast("WorkspaceClient", client),
            runner=warehouse,
            warehouse_id="w1",
        )

    return {f.about: f for f in look(project, target, connect)}


def test_a_healthy_setup_is_all_ticks(tmp_path: Path) -> None:
    fake = FakeWarehouse()
    fake.schemas.add("main.deltaplan")
    found = findings(project_at(tmp_path), FakeClient(), fake)
    assert [f.verdict for f in found.values()] == ["ok"] * len(found), found
    assert worst(list(found.values())) == "ok"
    assert found["project"].found.endswith("1 spec in " + str(tmp_path / "tables"))


def test_no_project_says_how_to_start_one() -> None:
    [finding] = look(None, None, None)
    assert finding.verdict == "problem"
    assert finding.remedy and "deltaplan import" in finding.remedy


def test_a_specs_entry_that_isnt_there_is_a_problem(tmp_path: Path) -> None:
    project = project_at(tmp_path, PROJECT.replace("[tables]", "[tabels]"))
    found = findings(project)
    assert found["project"].verdict == "problem"
    assert "tabels" in found["project"].found


def test_a_project_with_no_specs_yet_is_a_warning(tmp_path: Path) -> None:
    found = findings(project_at(tmp_path, spec=None))
    assert found["project"].verdict == "warning"


def test_a_workspace_that_cant_be_reached_carries_the_advice(tmp_path: Path) -> None:
    found = findings(project_at(tmp_path), client=None)
    assert found["workspace"].verdict == "problem"
    assert found["workspace"].remedy and "Set a profile" in found["workspace"].remedy


def test_a_stopped_warehouse_is_a_warning_not_a_problem(tmp_path: Path) -> None:
    """It starts on the first statement — unless the workspace can't."""
    client = FakeClient(warehouse=_Warehouse(state="STOPPED"))
    found = findings(project_at(tmp_path), client)
    assert found["warehouse"].verdict == "warning"
    assert (
        found["warehouse"].remedy and "can't give it compute" in found["warehouse"].remedy
    )


def test_a_warehouse_that_isnt_there_is_a_problem(tmp_path: Path) -> None:
    found = findings(project_at(tmp_path), FakeClient(warehouse=None))
    assert found["warehouse"].verdict == "problem"
    assert found["warehouse"].remedy and "Connection details" in found["warehouse"].remedy


def test_a_metastore_at_its_limit_says_why_it_probably_isnt(tmp_path: Path) -> None:
    client = FakeClient(quota=_Quota(quota_count=523, quota_limit=500))
    found = findings(project_at(tmp_path), client)
    assert found["metastore"].verdict == "problem"
    assert "523 of 500" in found["metastore"].found
    assert found["metastore"].remedy and "UNDROP" in found["metastore"].remedy


def test_a_history_schema_that_isnt_there_yet_says_what_it_needs(tmp_path: Path) -> None:
    found = findings(project_at(tmp_path), FakeClient(), FakeWarehouse())
    assert found["history"].verdict == "warning"
    assert found["history"].remedy and "CREATE SCHEMA" in found["history"].remedy


def test_no_history_schema_is_fine_and_says_what_that_costs(tmp_path: Path) -> None:
    project = project_at(
        tmp_path, PROJECT.replace("history_schema: ${catalog}.deltaplan\n", "")
    )
    found = findings(project, FakeClient())
    assert found["history"].verdict == "ok"
    assert "take no lock" in found["history"].found


def test_a_bundle_without_the_cli_is_a_warning(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "databricks.yml").write_text(
        "bundle:\n  name: shop\ntargets:\n  dev:\n    default: true\n"
    )
    project = project_at(
        tmp_path,
        "specs: [tables]\nbundle: databricks.yml\n",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    found = findings(project, FakeClient())
    assert found["bundle"].verdict == "warning"
    assert found["bundle"].remedy and "DATABRICKS_CLI_PATH" in found["bundle"].remedy


def test_what_is_handed_to_another_tool_is_reported(tmp_path: Path) -> None:
    project = project_at(tmp_path, PROJECT + "manage:\n  grants: false\n")
    found = findings(project, FakeClient())
    assert found["manage"].verdict == "ok"
    assert "grants left to another tool" in found["manage"].found


def test_the_command_exits_one_on_a_problem_and_prints_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_at(tmp_path)
    monkeypatch.setattr(
        cli,
        "_connect",
        lambda *_a, **_k: Connection(runner=FakeWarehouse(), warehouse_id="w1"),
    )
    result = CliRunner().invoke(
        cli.app, ["doctor", "-c", str(tmp_path / "deltaplan.yml")]
    )
    # No credentials in a test run, so the workspace check is the problem.
    assert result.exit_code == 1
    assert "✓ project" in result.output
    assert "→" in result.output, "a problem is followed by what to do"


def test_json_is_the_same_findings(tmp_path: Path) -> None:
    project_at(tmp_path)
    result = CliRunner().invoke(
        cli.app, ["doctor", "-c", str(tmp_path / "deltaplan.yml"), "--json"]
    )
    reported = json.loads(result.output)
    assert {entry["about"] for entry in reported} >= {"project", "target"}
    assert all(
        set(entry) == {"about", "verdict", "found", "remedy"} for entry in reported
    )


def test_looking_changes_nothing(tmp_path: Path) -> None:
    """It reads. No schema made, no warehouse started, no statement but a read."""
    fake = FakeWarehouse()
    fake.schemas.add("main.deltaplan")
    findings(project_at(tmp_path), FakeClient(), fake)
    assert fake.ddl == [], "doctor ran a DDL statement"
