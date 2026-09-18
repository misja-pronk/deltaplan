"""The `deltaplan` command line.

Milestone 1 is read-only: `validate` lints specs offline, `import` writes specs
for tables that already exist, and `plan` diffs specs against live Unity Catalog.
Nothing here writes to a workspace — `apply` arrives with milestone 2.
"""

import os
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from deltaplan.differ import diff, unmanaged
from deltaplan.introspect import (
    IntrospectionError,
    Introspector,
    LiveSchema,
    WarehouseRunner,
)
from deltaplan.loader import (
    Diagnostic,
    LoadedSpec,
    Project,
    SpecError,
    Target,
    dump_spec,
    find_project_file,
    load_project,
    load_specs,
    load_table,
    spec_files,
    validate_table,
)
from deltaplan.model.plan import Plan, TableDiff, TableFacts, fingerprint
from deltaplan.model.table import Table
from deltaplan.planner import build_plan
from deltaplan.render.json import dumps as plan_json
from deltaplan.render.rich import plan_text, render_plan

app = typer.Typer(
    name="deltaplan",
    help="Declarative plan/apply for Databricks SQL tables.",
    no_args_is_help=True,
    add_completion=False,
)

out = Console()
# Errors carry paths and messages that people grep and paste; wrapping them
# mid-word helps nobody, so let the terminal decide.
err = Console(stderr=True, soft_wrap=True)


class Format(StrEnum):
    """How to render a plan."""

    rich = "rich"
    md = "md"
    json = "json"


@app.callback()
def cli() -> None:
    # Registering a callback keeps this a command *group* — without it Typer
    # collapses a one-command app into that single command.
    pass


def package_version() -> str:
    """The installed distribution version, or a marker in a source tree."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed_version

    try:
        return installed_version("deltaplan")
    except PackageNotFoundError:  # pragma: no cover - only outside an install
        return "unknown"


@app.command()
def version() -> None:
    """Print the deltaplan version."""
    typer.echo(f"deltaplan {package_version()}")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@app.command()
def validate(
    paths: Annotated[
        list[Path] | None, typer.Argument(help="Spec files to lint (default: all).")
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Render variables for this target first."),
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to deltaplan.yml.")
    ] = None,
) -> None:
    """Lint specs. No workspace, no network — safe in a pre-commit hook."""
    variables: dict[str, str] = {}
    if paths:
        files = tuple(paths)
        if target:
            variables = _project(config).target(target).variables_map()
    else:
        project = _project(config)
        chosen = _target(project, target)
        variables = chosen.variables_map()
        files = spec_files(project)

    if not files:
        err.print("[yellow]No specs found.[/]")
        raise typer.Exit(1)

    problems = 0
    for path in files:
        try:
            table = load_table(path, variables)
        except SpecError as error:
            err.print(f"[red]{error}[/]")
            problems += 1
            continue
        for diagnostic in validate_table(table, str(path)):
            _print_diagnostic(diagnostic)
            problems += diagnostic.severity == "error"

    if problems:
        err.print(f"[red]{problems} problem(s) in {len(files)} spec(s).[/]")
        raise typer.Exit(1)
    out.print(f"[green]{len(files)} spec(s) OK.[/]")


def _print_diagnostic(diagnostic: Diagnostic) -> None:
    colour = "red" if diagnostic.severity == "error" else "yellow"
    err.print(f"[{colour}]{diagnostic}[/]")


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


@app.command("import")
def import_schema(
    schema: Annotated[
        str, typer.Argument(help="The schema to import, as catalog.schema.")
    ],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Directory to write specs into.")
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Write ${var} for this target's catalog."),
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to deltaplan.yml.")
    ] = None,
    warehouse_id: Annotated[
        str | None, typer.Option("--warehouse-id", help="SQL warehouse to read through.")
    ] = None,
) -> None:
    """Write specs for tables that already exist."""
    parts = schema.split(".")
    if len(parts) != 2:
        err.print("[red]Give the schema as catalog.schema, e.g. main.sales[/]")
        raise typer.Exit(1)

    project = _optional_project(config)
    chosen = _target(project, target) if project else None
    live = _introspect(_warehouse(warehouse_id, chosen), parts[0], parts[1])

    directory = output or (project.spec_paths[0] if project else Path("tables"))
    directory.mkdir(parents=True, exist_ok=True)

    variable = _catalog_variable(chosen, parts[0])
    for table in (entry.table for entry in live.tables):
        path = directory / f"{table.short_name}.yml"
        path.write_text(dump_spec(table, catalog_variable=variable), encoding="utf-8")
        out.print(f"[green]+[/] {path}")

    for name, reason in live.skipped:
        out.print(f"[dim]· skipped {name} ({reason}) — deltaplan manages Delta tables[/]")

    if not live.tables:
        err.print(f"[yellow]No Delta tables found in {schema}.[/]")


def _catalog_variable(target: Target | None, catalog: str) -> str | None:
    """The variable whose value is this catalog, so specs stay target-neutral."""
    if target is None:
        return None
    for name, value in target.variables:
        if value == catalog:
            return name
    return None


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@app.command()
def plan(
    target: Annotated[
        str | None, typer.Option("--target", "-t", help="Which target to plan for.")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write the plan to a file.")
    ] = None,
    output_format: Annotated[
        Format, typer.Option("--format", "-f", help="How to render the plan.")
    ] = Format.rich,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to deltaplan.yml.")
    ] = None,
    check_order: Annotated[
        bool, typer.Option("--check-order", help="Also diff column order.")
    ] = False,
    warehouse_id: Annotated[
        str | None, typer.Option("--warehouse-id", help="SQL warehouse to read through.")
    ] = None,
) -> None:
    """Diff your specs against live Unity Catalog and show what would change."""
    if output_format is Format.md:
        err.print(
            "[red]The Markdown renderer arrives with the CI milestone. "
            "Use --format rich or --format json.[/]"
        )
        raise typer.Exit(1)

    project = _project(config)
    chosen = _target(project, target)
    specs = _load(project, chosen)
    _abort_on_lint_errors(specs)

    runner = _warehouse(warehouse_id, chosen)
    built = _plan(specs, runner, chosen, check_order=check_order)

    if output_format is Format.json:
        text = plan_json(built)
        if output:
            output.write_text(text, encoding="utf-8")
            out.print(f"[green]Wrote[/] {output}")
        else:
            typer.echo(text, nl=False)
        return

    render_plan(built, out)
    if output:
        output.write_text(plan_text(built), encoding="utf-8")
        out.print(f"\n[green]Wrote[/] {output}")


def _plan(
    specs: tuple[LoadedSpec, ...],
    runner: WarehouseRunner,
    target: Target,
    *,
    check_order: bool,
) -> Plan:
    """Introspect once per schema, diff every spec, then plan."""
    introspector = Introspector(runner)
    schemas: dict[tuple[str, str], LiveSchema] = {}
    for spec in specs:
        parts = spec.table.parts
        if len(parts) != 3:
            err.print(
                f"[red]{spec.path}: table name {spec.table.name!r} must be "
                "catalog.schema.table[/]"
            )
            raise typer.Exit(1)
        key = (parts[0], parts[1])
        if key not in schemas:
            schemas[key] = _introspect(runner, *key)

    diffs: list[TableDiff] = []
    live_tables: list[Table | None] = []
    for spec in specs:
        parts = spec.table.parts
        live = schemas[(parts[0], parts[1])].get(spec.table.name)
        live_table = live.table if live else None
        live_tables.append(live_table)
        changes = diff(spec.table, live_table, compare_order=check_order)
        facts = TableFacts(
            spec.table.name,
            exists=live is not None,
            properties=live_table.properties if live_table else (),
            size_bytes=live.size_bytes if live else None,
            # Only worth a query for tables that are actually changing.
            delta_version=(
                introspector.latest_version(spec.table.name)
                if live_table is not None and changes
                else None
            ),
        )
        diffs.append(
            TableDiff(
                spec.table.name,
                changes,
                facts,
                unmanaged(spec.table, live_table) if live_table else (),
            )
        )

    planned = {spec.table.name for spec in specs}
    live_only = tuple(
        sorted(
            name
            for schema in schemas.values()
            for name in schema.names
            if name not in planned
        )
    )
    built = build_plan(
        diffs,
        target=target.name,
        tool_version=package_version(),
        spec_hash=fingerprint(spec.table for spec in specs),
        state_fingerprint=fingerprint(live_tables),
    )
    return replace(built, unmanaged_tables=live_only)


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _project(config: Path | None) -> Project:
    project = _optional_project(config)
    if project is None:
        err.print(
            f"[red]No deltaplan.yml found in {Path.cwd()} or any parent directory.[/]"
        )
        raise typer.Exit(1)
    return project


def _optional_project(config: Path | None) -> Project | None:
    """`import` works without a project; everything else needs one."""
    try:
        path = config or find_project_file(Path.cwd())
    except FileNotFoundError:
        return None
    try:
        return load_project(path)
    except SpecError as error:
        err.print(f"[red]{error}[/]")
        raise typer.Exit(1) from error


def _target(project: Project, name: str | None) -> Target:
    if name is None:
        if len(project.targets) == 1:
            return project.targets[0]
        known = ", ".join(t.name for t in project.targets) or "none defined"
        err.print(f"[red]Pick a target with -t (known: {known}).[/]")
        raise typer.Exit(1)
    try:
        return project.target(name)
    except KeyError as error:
        err.print(f"[red]{error.args[0]}[/]")
        raise typer.Exit(1) from error


def _load(project: Project, target: Target) -> tuple[LoadedSpec, ...]:
    try:
        specs = load_specs(project, target)
    except (SpecError, FileNotFoundError) as error:
        err.print(f"[red]{error}[/]")
        raise typer.Exit(1) from error
    if not specs:
        err.print("[yellow]No specs found.[/]")
        raise typer.Exit(1)
    return specs


def _abort_on_lint_errors(specs: tuple[LoadedSpec, ...]) -> None:
    errors = 0
    for spec in specs:
        for diagnostic in validate_table(spec.table, str(spec.path)):
            _print_diagnostic(diagnostic)
            errors += diagnostic.severity == "error"
    if errors:
        err.print(f"[red]Refusing to plan: {errors} spec error(s).[/]")
        raise typer.Exit(1)


def _warehouse(warehouse_id: str | None, target: Target | None) -> WarehouseRunner:
    chosen = (
        warehouse_id
        or (target.warehouse_id if target else None)
        or os.environ.get("DATABRICKS_WAREHOUSE_ID")
    )
    if not chosen:
        err.print(
            "[red]No SQL warehouse. Pass --warehouse-id, set warehouse_id on the "
            "target, or export DATABRICKS_WAREHOUSE_ID.[/]"
        )
        raise typer.Exit(1)
    from databricks.sdk import WorkspaceClient

    return WarehouseRunner(WorkspaceClient(), chosen)


def _introspect(runner: WarehouseRunner, catalog: str, schema: str) -> LiveSchema:
    try:
        return Introspector(runner).schema(catalog, schema)
    except IntrospectionError as error:
        err.print(f"[red]{error}[/]")
        raise typer.Exit(1) from error


def main() -> None:
    app()
