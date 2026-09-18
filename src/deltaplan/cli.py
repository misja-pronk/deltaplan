"""The `deltaplan` command line.

Milestone 1 is read-only: `validate` lints specs offline, `import` writes specs
for tables that already exist, and `plan` diffs specs against live Unity Catalog.
Nothing here writes to a workspace — `apply` arrives with milestone 2.
"""

import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from deltaplan.executor import ExecutionError, ExecutionResult, Executor
from deltaplan.history import DeltaHistory, HistoryStore, Status
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
    load_spec,
    load_specs,
    spec_files,
    validate_spec,
)
from deltaplan.model.plan import Plan, Step
from deltaplan.model.view import Relation
from deltaplan.planning import PlanningError, plan_tables
from deltaplan.render.json import PlanFileError
from deltaplan.render.json import dumps as plan_json
from deltaplan.render.json import loads as plan_loads
from deltaplan.render.markdown import render_markdown
from deltaplan.render.rich import RISK_STYLE, TITLE_WIDTH, plan_text, render_plan

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


#: Which workspace to talk to. Shared by every command that talks to one.
ProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile",
        "-p",
        help="~/.databrickscfg profile to connect with (default: the target's).",
    ),
]


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
            table = load_spec(path, variables)
        except SpecError as error:
            err.print(f"[red]{error}[/]")
            problems += 1
            continue
        for diagnostic in validate_spec(table, str(path)):
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
    profile: ProfileOption = None,
) -> None:
    """Write specs for tables that already exist."""
    parts = schema.split(".")
    if len(parts) != 2:
        err.print("[red]Give the schema as catalog.schema, e.g. main.sales[/]")
        raise typer.Exit(1)

    project = _optional_project(config)
    chosen = _target(project, target) if project else None
    live = _introspect(_warehouse(warehouse_id, chosen, profile), parts[0], parts[1])

    directory = output or (project.spec_paths[0] if project else Path("tables"))
    directory.mkdir(parents=True, exist_ok=True)

    variable = _catalog_variable(chosen, parts[0])
    relations: list[Relation] = [entry.table for entry in live.tables]
    relations.extend(live.views)
    for relation in relations:
        path = directory / f"{relation.short_name}.yml"
        path.write_text(dump_spec(relation, catalog_variable=variable), encoding="utf-8")
        out.print(f"[green]+[/] {path}")

    for name, reason in live.skipped:
        out.print(
            f"[dim]· skipped {name} ({reason}) — deltaplan manages Delta tables and "
            "views[/]"
        )

    if not relations:
        err.print(f"[yellow]No Delta tables or views found in {schema}.[/]")


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
    clone: Annotated[
        bool,
        typer.Option(
            "--clone", help="SHALLOW CLONE each table before a step that risks its data."
        ),
    ] = False,
    warehouse_id: Annotated[
        str | None, typer.Option("--warehouse-id", help="SQL warehouse to read through.")
    ] = None,
    profile: ProfileOption = None,
) -> None:
    """Diff your specs against live Unity Catalog and show what would change."""
    built = _plan_for(
        config,
        target,
        warehouse_id,
        profile=profile,
        check_order=check_order,
        clone=clone,
    )
    _output(built, output_format, output, heading="plan")


@app.command()
def show(
    plan_file: Annotated[
        Path, typer.Argument(help="A plan written by `deltaplan plan`.")
    ],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write it to a file.")
    ] = None,
    output_format: Annotated[
        Format, typer.Option("--format", "-f", help="How to render the plan.")
    ] = Format.rich,
) -> None:
    """Render a saved plan. What `apply` would run, without asking a warehouse."""
    _output(_read_plan(plan_file), output_format, output, heading="plan")


#: `drift`'s exit codes, the way `terraform plan -detailed-exitcode` has them.
IN_SYNC, FAILED, DRIFTED = 0, 1, 2


@app.command()
def drift(
    target: Annotated[
        str | None, typer.Option("--target", "-t", help="Which target to check.")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write the plan to a file.")
    ] = None,
    output_format: Annotated[
        Format, typer.Option("--format", "-f", help="How to render the drift.")
    ] = Format.rich,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to deltaplan.yml.")
    ] = None,
    warehouse_id: Annotated[
        str | None, typer.Option("--warehouse-id", help="SQL warehouse to read through.")
    ] = None,
    profile: ProfileOption = None,
) -> None:
    """Check live tables against their specs. Exits 2 if they have drifted.

    Drift is anything `apply` would do: an edit made by hand, a table dropped
    outside deltaplan, a spec merged but never applied. Unmanaged objects are
    not drift — deltaplan never claimed them.
    """
    built = _plan_for(config, target, warehouse_id, profile=profile)
    _output(built, output_format, output, heading="drift")
    if built.empty:
        raise typer.Exit(IN_SYNC)
    drifted = len([diff for diff in built.diffs if diff.changes])
    err.print(
        f"[yellow]Drift: {drifted} table(s) differ from their specs.[/] "
        "Run `deltaplan plan` to see how to bring them back."
    )
    raise typer.Exit(DRIFTED)


def _plan_for(
    config: Path | None,
    target: str | None,
    warehouse_id: str | None,
    *,
    profile: str | None = None,
    check_order: bool = False,
    clone: bool = False,
) -> Plan:
    project = _project(config)
    chosen = _target(project, target)
    specs = _load(project, chosen)
    _abort_on_lint_errors(specs)
    runner = _warehouse(warehouse_id, chosen, profile)
    return _plan(project, chosen, specs, runner, check_order=check_order, clone=clone)


def _output(
    built: Plan, output_format: Format, output: Path | None, *, heading: str
) -> None:
    """Render a plan in the chosen format, to the terminal or to a file."""
    match output_format:
        case Format.json:
            text = plan_json(built)
        case Format.md:
            text = render_markdown(built, heading=heading)
        case Format.rich:
            render_plan(built, out)
            if output:
                output.write_text(plan_text(built), encoding="utf-8")
                out.print(f"\n[green]Wrote[/] {output}")
            return
    if output:
        output.write_text(text, encoding="utf-8")
        out.print(f"[green]Wrote[/] {output}")
    else:
        typer.echo(text, nl=False)


def _plan(
    project: Project,
    target: Target,
    specs: tuple[LoadedSpec, ...],
    runner: WarehouseRunner,
    *,
    check_order: bool = False,
    clone: bool = False,
) -> Plan:
    try:
        return plan_tables(
            [spec.table for spec in specs],
            Introspector(runner),
            target=target.name,
            tool_version=package_version(),
            mode_for=lambda schema: project.mode_for(target, schema),
            check_order=check_order,
            clone=clone,
        )
    except (PlanningError, IntrospectionError) as error:
        err.print(f"[red]{error}[/]")
        raise typer.Exit(1) from error


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


@app.command()
def apply(
    plan_file: Annotated[
        Path, typer.Argument(help="A plan written by `deltaplan plan`.")
    ],
    allow_destructive: Annotated[
        bool,
        typer.Option("--allow-destructive", help="Permit steps that drop something."),
    ] = False,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to deltaplan.yml.")
    ] = None,
    warehouse_id: Annotated[
        str | None, typer.Option("--warehouse-id", help="SQL warehouse to run on.")
    ] = None,
    profile: ProfileOption = None,
) -> None:
    """Run a plan. Resumes an interrupted one instead of starting over."""
    built = _read_plan(plan_file)
    project = _project(config)
    target = _target(project, built.target)
    runner = _warehouse(warehouse_id, target, profile)
    history = _history(project, target, runner)

    out.print(
        f"[bold]{built.target}[/] · {len(built.steps)} step(s) · "
        f"highest risk [{RISK_STYLE[built.highest_risk]}]{built.highest_risk}[/]"
    )

    executor = Executor(
        runner=runner,
        introspector=Introspector(runner),
        history=history,
        observer=_show_step,
    )
    try:
        result = executor.apply(built, allow_destructive=allow_destructive)
    except (ExecutionError, IntrospectionError) as error:
        err.print(f"[red]{error}[/]")
        raise typer.Exit(1) from error

    _report(result, built, plan_file)
    if not result.ok:
        raise typer.Exit(1)


def _show_step(step: Step, status: Status, note: str | None) -> None:
    colour = {"succeeded": "green", "skipped": "dim", "failed": "red"}[status]
    label = {"succeeded": "ok", "skipped": "skipped", "failed": "failed"}[status]
    line = (
        f"  [dim]{step.id}.[/] {step.title.ljust(TITLE_WIDTH)} "
        f"[{RISK_STYLE[step.risk]}][{step.risk}][/] [{colour}]{label}[/]"
    )
    if status == "skipped" and note:
        line += f" [dim]({note})[/]"
    out.print(line)
    if status == "failed" and note:
        err.print(f"     [red]{note}[/]")


def _report(result: ExecutionResult, built: Plan, plan_file: Path) -> None:
    ran, skipped = len(result.ran), len(result.skipped)
    if result.ok:
        out.print(
            f"\n[green]Applied[/] {ran} step(s), skipped {skipped} · run "
            f"[bold]{result.run_id}[/]"
        )
        return
    err.print(
        f"\n[red]Failed at step {result.failed} of {len(built.steps)}[/] · run "
        f"[bold]{result.run_id}[/]\n"
        f"Fix the cause and run `deltaplan apply {plan_file}` again — it resumes "
        "from here rather than starting over."
    )


# ---------------------------------------------------------------------------
# force-unlock
# ---------------------------------------------------------------------------


@app.command("force-unlock")
def force_unlock(
    target: Annotated[
        str | None, typer.Option("--target", "-t", help="Which target to unlock.")
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to deltaplan.yml.")
    ] = None,
    warehouse_id: Annotated[
        str | None, typer.Option("--warehouse-id", help="SQL warehouse to run on.")
    ] = None,
    profile: ProfileOption = None,
) -> None:
    """Release the apply lock after a run died holding it."""
    project = _project(config)
    chosen = _target(project, target)
    runner = _warehouse(warehouse_id, chosen, profile)
    try:
        holder = _history(project, chosen, runner).force_unlock(chosen.name)
    except IntrospectionError as error:
        err.print(f"[red]{error}[/]")
        raise typer.Exit(1) from error
    if holder is None:
        out.print(f"[green]{chosen.name} was not locked.[/]")
        return
    out.print(f"[yellow]Released[/] {chosen.name}, which run [bold]{holder}[/] held.")


def _read_plan(path: Path) -> Plan:
    try:
        return plan_loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        err.print(f"[red]cannot read {path}: {error}[/]")
        raise typer.Exit(1) from error
    except PlanFileError as error:
        err.print(f"[red]{path}: {error}[/]")
        raise typer.Exit(1) from error


def _history(project: Project, target: Target, runner: WarehouseRunner) -> HistoryStore:
    try:
        schema = project.history_schema_for(target)
    except KeyError as error:
        err.print(f"[red]history_schema: {error.args[0]}[/]")
        raise typer.Exit(1) from error
    if not schema:
        err.print(
            "[red]No history_schema in deltaplan.yml. `apply` records every run "
            "in Delta tables; tell it which schema to keep them in, e.g.\n"
            "  history_schema: ${catalog}.deltaplan[/]"
        )
        raise typer.Exit(1)
    return DeltaHistory(runner, schema)


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
        for diagnostic in validate_spec(spec.table, str(spec.path)):
            _print_diagnostic(diagnostic)
            errors += diagnostic.severity == "error"
    if errors:
        err.print(f"[red]Refusing to plan: {errors} spec error(s).[/]")
        raise typer.Exit(1)


def _warehouse(
    warehouse_id: str | None, target: Target | None, profile: str | None = None
) -> WarehouseRunner:
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

    use = profile or (target.profile if target else None)
    try:
        client = WorkspaceClient(profile=use) if use else WorkspaceClient()
    except Exception as error:  # the SDK raises ValueError for most config problems
        where = f"profile {use!r}" if use else "the environment or the DEFAULT profile"
        err.print(
            f"[red]Can't connect to a Databricks workspace using {where}: {error}[/]\n"
            "Set `profile:` on the target, pass --profile, or export DATABRICKS_HOST "
            "and a token."
        )
        raise typer.Exit(1) from error
    return WarehouseRunner(client, chosen)


def _introspect(runner: WarehouseRunner, catalog: str, schema: str) -> LiveSchema:
    try:
        return Introspector(runner).schema(catalog, schema)
    except IntrospectionError as error:
        err.print(f"[red]{error}[/]")
        raise typer.Exit(1) from error


def main() -> None:
    app()
