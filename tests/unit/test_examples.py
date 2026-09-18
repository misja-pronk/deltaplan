"""The examples in `examples/` are documentation, so they must stay valid."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from deltaplan import cli

EXAMPLES = Path(__file__).parents[2] / "examples"

runner = CliRunner()


@pytest.mark.parametrize(
    ("config", "target"),
    [
        (EXAMPLES / "deltaplan.yml", "dev"),
        (EXAMPLES / "deltaplan.yml", "prod"),
        (EXAMPLES / "bundle" / "deltaplan.yml", None),
        (EXAMPLES / "bundle" / "deltaplan.yml", "prod"),
    ],
)
def test_the_examples_validate(config: Path, target: str | None) -> None:
    args = ["validate", "--config", str(config)]
    if target:
        args += ["-t", target]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
