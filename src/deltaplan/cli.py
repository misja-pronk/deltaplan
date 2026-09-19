"""The `deltaplan` command line.

`validate` lints specs offline; `import` writes specs for what already exists;
`plan`, `show` and `drift` read; `apply` and `force-unlock` are the only commands
that write to a workspace.
"""

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

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
from deltaplan.model.volume import Volume
from deltaplan.planning import PlanningError, plan_tables
from deltaplan.render.json import PlanFileError
from deltaplan.render.json import dumps as plan_json
from deltaplan.render.json import loads as plan_loads
from deltaplan.render.markdown import render_markdown
from deltaplan.render.rich import RISK_STYLE, TITLE_WIDTH, render_plan
from deltaplan.spec_schema import MODELINE, project_schema, spec_schema
from deltaplan.sqlspec import dump_sql_spec, sql_cannot_say

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

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
ParallelOption = Annotated[
    int,
    typer.Option(
        "--parallel",
        min=1,
        help="How many per-table queries run at once while reading live state.",
    ),
]

ProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile",
        "-p",
        help="~/.databrickscfg profile to connect with (default: the target's).",
    ),
]


class SchemaKind(StrEnum):
    """Which JSON Schema `deltaplan schema` prints."""

    spec = "spec"
    project = "project"


class SpecFormat(StrEnum):
    """Which spec format `import` writes."""

    yaml = "yaml"
    sql = "sql"


class Format(StrEnum):
    """How to render a plan."""

    rich = "rich"
    md = "md"
    json = "json"


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"deltaplan {package_version()}")
        raise typer.Exit


@app.callback()
def cli(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            is_eager=True,
            callback=_print_version,
            help="Print the deltaplan version and exit.",
        ),
    ] = False,
) -> None:
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
    chosen: Target | None = None
    if paths:
        files = tuple(paths)
        if target:
            chosen = _target(_project(config), target)
    else:
        project = _project(config)
        chosen = _target(project, target)
        files = spec_files(project)
    variables = chosen.variables_map() if chosen else {}
    unresolved = chosen.unresolved_map() if chosen else {}

    if not files:
        err.print("[yellow]No specs found.[/]")
        raise typer.Exit(1)

    problems = 0
    for path in files:
        try:
            table = load_spec(path, variables, unresolved)
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


@app.command("schema")
def schema_command(
    kind: Annotated[
        SchemaKind,
        typer.Argument(
            help="spec (a table, view or function) or project (deltaplan.yml)."
        ),
    ] = SchemaKind.spec,
) -> None:
    """Print the JSON Schema editors use for completion and inline errors."""
    schema = spec_schema() if kind is SchemaKind.spec else project_schema()
    typer.echo(json.dumps(schema, indent=2))


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
    parallel: ParallelOption = 8,
    spec_format: Annotated[
        SpecFormat,
        typer.Option(
            "--format",
            "-f",
            help="yaml, or sql — which falls back to YAML for what SQL can't say.",
        ),
    ] = SpecFormat.yaml,
) -> None:
    """Write specs for tables that already exist."""
    parts = schema.split(".")
    if len(parts) != 2:
        err.print("[red]Give the schema as catalog.schema, e.g. main.sales[/]")
        raise typer.Exit(1)

    project = _optional_project(config)
    chosen = _target(project, target) if project else None
    live = _introspect(
        _warehouse(warehouse_id, chosen, profile), parts[0], parts[1], parallel
    )

    directory = output or (project.spec_paths[0] if project else Path("tables"))
    directory.mkdir(parents=True, exist_ok=True)

    variable = _catalog_variable(chosen, parts[0])
    definition = live.definition
    if definition is not None and (
        definition.comment or definition.tags or definition.grants
    ):
        # The schema's own comment, tags and grants, when it has any.
        reason = sql_cannot_say(definition) if spec_format is SpecFormat.sql else None
        if spec_format is SpecFormat.sql and reason is None:
            path = directory / "_schema.sql"
            text = dump_sql_spec(definition, catalog_variable=variable)
        else:
            path = directory / "_schema.yml"
            text = MODELINE + "\n" + dump_spec(definition, catalog_variable=variable)
        path.write_text(text, encoding="utf-8")
        note = f" [dim](YAML: SQL can't say {reason})[/]" if reason else ""
        out.print(f"[green]+[/] {path}{note}")

    relations: list[Relation] = [entry.table for entry in live.tables]
    relations.extend(live.views)
    relations.extend(live.functions)
    relations.extend(live.volumes)
    written: set[str] = set()
    for relation in relations:
        # Functions and volumes don't share a namespace with tables and views,
        # so one may have a table's name; its file then says what it is.
        stem = relation.short_name
        if stem in written:
            stem = f"{stem}.{'volume' if isinstance(relation, Volume) else 'function'}"
        written.add(stem)
        reason = sql_cannot_say(relation) if spec_format is SpecFormat.sql else None
        if spec_format is SpecFormat.sql and reason is None:
            path = directory / f"{stem}.sql"
            text = dump_sql_spec(relation, catalog_variable=variable)
        else:
            path = directory / f"{stem}.yml"
            # The first line points an editor at the schema: completion and
            # inline errors from the moment the file is opened.
            text = MODELINE + "\n" + dump_spec(relation, catalog_variable=variable)
        path.write_text(text, encoding="utf-8")
        note = f" [dim](YAML: SQL can't say {reason})[/]" if reason else ""
        out.print(f"[green]+[/] {path}{note}")

    for name, reason in live.skipped:
        out.print(
            f"[dim]· skipped {name} ({reason}) — deltaplan manages Delta tables, "
            "views and SQL functions[/]"
        )

    if not relations:
        err.print(f"[yellow]No Delta tables, views or functions found in {schema}.[/]")


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
    parallel: ParallelOption = 8,
) -> None:
    """Diff your specs against live Unity Catalog and show what would change."""
    built = _plan_for(
        config,
        target,
        warehouse_id,
        profile=profile,
        check_order=check_order,
        clone=clone,
        parallel=parallel,
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
    parallel: ParallelOption = 8,
) -> None:
    """Check live tables against their specs. Exits 2 if they have drifted.

    Drift is anything `apply` would do: an edit made by hand, a table dropped
    outside deltaplan, a spec merged but never applied. Unmanaged objects are
    not drift — deltaplan never claimed them.
    """
    built = _plan_for(config, target, warehouse_id, profile=profile, parallel=parallel)
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
    parallel: int = 8,
) -> Plan:
    project = _project(config)
    chosen = _target(project, target)
    specs = _load(project, chosen)
    _abort_on_lint_errors(specs)
    runner = _warehouse(warehouse_id, chosen, profile)
    return _plan(
        project,
        chosen,
        specs,
        runner,
        check_order=check_order,
        clone=clone,
        parallel=parallel,
    )


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
            # The terminal view is for reading; the file is for `apply` and
            # `show`, so it is the plan object, as the docs' `plan -o plan.json`
            # followed by `apply plan.json` expects.
            render_plan(built, out)
            if output:
                output.write_text(plan_json(built), encoding="utf-8")
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
    parallel: int = 8,
) -> Plan:
    try:
        return plan_tables(
            [spec.table for spec in specs],
            Introspector(runner, parallel=parallel),
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
        return load_project(path, os.environ)
    except SpecError as error:
        err.print(f"[red]{error}[/]")
        raise typer.Exit(1) from error


def _target(project: Project, name: str | None) -> Target:
    if name is None:
        if project.default_target is not None:
            return project.target(project.default_target)
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
    lookup = target.warehouse_lookup if target else None
    if not chosen and not lookup:
        err.print(
            "[red]No SQL warehouse. Pass --warehouse-id, set warehouse_id on the "
            "target, or export DATABRICKS_WAREHOUSE_ID.[/]"
        )
        raise typer.Exit(1)
    client = _client(target, profile)
    if not chosen:
        assert lookup is not None
        chosen = _find_warehouse(client, lookup)
    return WarehouseRunner(client, chosen)


def _client(target: Target | None, profile: str | None) -> "WorkspaceClient":
    """A workspace client: the --profile, else the target's profile, else the
    target's host (from a bundle), else the SDK's own defaults.

    TODO(verify): that a host alone authenticates the way the Databricks CLI
    does after `databricks auth login --host` — the SDK's `databricks-cli`
    credentials provider is meant to cover it.
    https://docs.databricks.com/aws/en/dev-tools/auth/unified-auth
    """
    from databricks.sdk import WorkspaceClient

    use = profile or (target.profile if target else None)
    host = None if use else (target.host if target else None)
    try:
        if use:
            return WorkspaceClient(profile=use)
        if host:
            return WorkspaceClient(host=host)
        return WorkspaceClient()
    except Exception as error:  # the SDK raises ValueError for most config problems
        where = (
            f"profile {use!r}"
            if use
            else f"host {host}"
            if host
            else "the environment or the DEFAULT profile"
        )
        err.print(
            f"[red]Can't connect to a Databricks workspace using {where}: {error}[/]\n"
            "Set `profile:` on the target, pass --profile, or export DATABRICKS_HOST "
            "and a token."
        )
        raise typer.Exit(1) from error


def _find_warehouse(client: "WorkspaceClient", name: str) -> str:
    """The id of the SQL warehouse a bundle's `warehouse_id` lookup names."""
    found = [w.id for w in client.warehouses.list() if w.name == name and w.id]
    if len(found) != 1:
        problem = "no SQL warehouse" if not found else "more than one SQL warehouse"
        err.print(
            f"[red]The bundle looks up the warehouse by name, and there is {problem} "
            f"called {name!r}. Set warehouse_id on the target in deltaplan.yml.[/]"
        )
        raise typer.Exit(1)
    return found[0]


def _introspect(
    runner: WarehouseRunner, catalog: str, schema: str, parallel: int = 8
) -> LiveSchema:
    try:
        return Introspector(runner, parallel=parallel).schema(catalog, schema)
    except IntrospectionError as error:
        err.print(f"[red]{error}[/]")
        raise typer.Exit(1) from error


def main() -> None:
    app()
