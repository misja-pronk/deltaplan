"""The `deltaplan` command line.

Milestone 1 grows this into `validate`, `import` and `plan`; for now it only
reports its own version, so packaging and the entry point are testable.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version

import typer

app = typer.Typer(
    name="deltaplan",
    help="Declarative plan/apply for Databricks SQL tables.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def cli() -> None:
    # Registering a callback keeps this a command *group* — without it Typer
    # collapses a one-command app into that single command.
    pass


def package_version() -> str:
    """The installed distribution version, or a marker when running from a source tree."""
    try:
        return installed_version("deltaplan")
    except PackageNotFoundError:  # pragma: no cover - only hit outside an install
        return "unknown"


@app.command()
def version() -> None:
    """Print the deltaplan version."""
    typer.echo(f"deltaplan {package_version()}")


def main() -> None:
    app()
