"""The console entry point exists, stays a command group, and reports a version."""

from typer.testing import CliRunner

from deltaplan.cli import app, package_version

runner = CliRunner()


def test_version_command_prints_the_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"deltaplan {package_version()}" in result.stdout


def test_bare_invocation_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Declarative plan/apply for Databricks SQL tables." in result.stdout
