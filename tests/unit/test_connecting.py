"""Which workspace a command talks to.

Dev and prod are usually different workspaces, so a target can name a
`~/.databrickscfg` profile, and `--profile` overrides it. Without either, the
Databricks SDK's own defaults apply.
"""

from pathlib import Path
from typing import Any

import pytest
import typer

from deltaplan import cli
from deltaplan.loader import Target, load_project


class RecordingClient:
    """Stands in for WorkspaceClient, remembering how it was made."""

    made: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        RecordingClient.made.append(kwargs)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> type[RecordingClient]:
    import databricks.sdk

    RecordingClient.made = []
    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", RecordingClient)
    return RecordingClient


def test_a_target_can_name_its_profile(tmp_path: Path) -> None:
    (tmp_path / "deltaplan.yml").write_text(
        "targets:\n  dev: {profile: dev-workspace, warehouse_id: abc}\n  prod: {}\n"
    )
    project = load_project(tmp_path / "deltaplan.yml")
    assert project.target("dev").profile == "dev-workspace"
    assert project.target("prod").profile is None


def test_the_targets_profile_is_used(client: type[RecordingClient]) -> None:
    cli._warehouse(None, Target("dev", warehouse_id="abc", profile="dev-workspace"))
    assert client.made == [{"profile": "dev-workspace"}]


def test_profile_on_the_command_line_wins(client: type[RecordingClient]) -> None:
    cli._warehouse(
        None, Target("dev", warehouse_id="abc", profile="dev-workspace"), "other"
    )
    assert client.made == [{"profile": "other"}]


def test_without_a_profile_the_sdk_decides(client: type[RecordingClient]) -> None:
    cli._warehouse("abc", Target("dev"))
    assert client.made == [{}], "no profile argument at all, so env vars still work"


def test_a_workspace_that_cant_be_reached_is_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import databricks.sdk

    def unconfigured(**_kwargs: Any) -> None:
        raise ValueError("default auth: cannot configure default credentials")

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", unconfigured)
    with pytest.raises(typer.Exit):
        cli._warehouse("abc", Target("dev", profile="missing"))
    printed = capsys.readouterr().err
    assert "Can't connect to a Databricks workspace using profile 'missing'" in printed
    assert "cannot configure default credentials" in printed
