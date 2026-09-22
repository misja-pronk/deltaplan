"""The `deltaplan` command line.

`validate` lints specs offline; `import` writes specs for what already exists;
`plan`, `show` and `drift` read; `apply` and `force-unlock` are the only commands
that write to a workspace.
"""

import json
import os
from collections.abc import Callable
from dataclasses import replace
from enum import StrEnum
from fnmatch import fnmatch
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.markup import escape

from deltaplan.bundle import BundleError
from deltaplan.executor import (
    DestructiveRefused,
    ExecutionError,
    ExecutionResult,
    Executor,
)
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
    as_deployed,
    dump_spec,
    find_project_file,
    load_project,
    load_spec,
    load_specs,
    spec_files,
    validate_spec,
)
from deltaplan.manage import EVERYTHING, Manage
from deltaplan.model.plan import Plan, Step
from deltaplan.model.view import Relation
from deltaplan.model.volume import Volume
from deltaplan.planning import PlanningError, plan_tables
from deltaplan.render.json import PlanFileError
from deltaplan.render.json import dumps as plan_json
from deltaplan.render.json import loads as plan_loads
from deltaplan.render.labels import count
from deltaplan.render.markdown import render_markdown
from deltaplan.render.rich import RISK_STYLE, TITLE_WIDTH, number_width, render_plan
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

out = Console(highlight=False)
# Errors carry paths and messages that people grep and paste; wrapping them
# mid-word helps nobody, so let the terminal decide.
err = Console(stderr=True, soft_wrap=True, highlight=False)


#: Which workspace to talk to. Shared by every command that talks to one.
ParallelOption = Annotated[
    int,
    typer.Option(
        "--parallel",
        min=1,
        help="How many per-table queries run at once while reading live state.",
    ),
]

SelectOption = Annotated[
    list[str] | None,
    typer.Option(
        "--select",
        "-s",
        help=(
            "Only these: a table, view, function, schema or volume by name — "
            "`orders`, `sales.orders`, or a pattern like `sales.*`. Repeatable."
        ),
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
    found: Project | None = None
    if paths:
        files = tuple(paths)
        # A project is optional here, but if there is one its line between
        # deltaplan and other tools holds for these files too.
        found = _optional_project(config)
        if target and found:
            chosen = _target(found, target)
    else:
        found = _project(config)
        chosen = _target(found, target)
        files = spec_files(found)
    variables = chosen.variables_map() if chosen else {}
    unresolved = chosen.unresolved_map() if chosen else {}
    manage = found.manage if found else EVERYTHING

    if not files:
        err.print("[yellow]No specs found.[/]")
        raise typer.Exit(1)

    problems = 0
    for path in files:
        try:
            table = load_spec(path, variables, unresolved, manage)
        except SpecError as error:
            err.print(f"[red]{escape(str(error))}[/]")
            problems += 1
            continue
        for diagnostic in validate_spec(table, str(path)):
            _print_diagnostic(diagnostic)
            problems += diagnostic.severity == "error"

    if problems:
        err.print(f"[red]{count(problems, 'problem')} in {count(len(files), 'spec')}.[/]")
        raise typer.Exit(1)
    out.print(f"[green]{count(len(files), 'spec')} OK.[/]")


def _print_diagnostic(diagnostic: Diagnostic) -> None:
    colour = "red" if diagnostic.severity == "error" else "yellow"
    err.print(f"[{colour}]{escape(str(diagnostic))}[/]")


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
    """Print the JSON Schema editors use for completion and inline errors.

    In a project that hands something to another tool (`manage:`), the spec
    schema leaves those keys out — so an editor stops offering what `validate`
    would refuse. Write it next to your specs and point your editor at it.
    """
    project = _optional_project(None)
    manage = project.manage if project else EVERYTHING
    schema = spec_schema(manage) if kind is SchemaKind.spec else project_schema()
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
    manage = project.manage if project else EVERYTHING
    live = _introspect(
        _warehouse(warehouse_id, chosen, profile), parts[0], parts[1], parallel, manage
    )

    if project is None and config is None and output is None:
        # A first import is a first project: write the file that makes
        # `plan` and `apply` work next, once reading the schema has worked.
        # With -o the caller has a layout in mind, so nothing is added to it.
        project_file = Path(PROJECT_FILE)
        project_file.write_text(
            starter_project(
                parts[0],
                specs=Path("tables"),
                warehouse_id=warehouse_id,
                profile=profile,
            ),
            encoding="utf-8",
        )
        out.print(
            f"[green]+[/] {PROJECT_FILE} [dim](target dev: catalog {escape(parts[0])})[/]"
        )
        project = _project(project_file)
        chosen = _target(project, None)

    directory = output or (project.spec_paths[0] if project else Path("tables"))
    directory.mkdir(parents=True, exist_ok=True)

    variable = _catalog_variable(chosen, parts[0])
    # Owners stay out of imported specs: often a person's email, and not the
    # same across workspaces. A spec that names one has it enforced.
    definition = replace(live.definition, owner=None) if live.definition else None
    owned = chosen.owned_by_the_bundle() if chosen else {}
    if definition is not None and definition.name.lower() in owned:
        out.print(
            f"[dim]· {escape(definition.name)} is the bundle's "
            f"{escape(owned[definition.name.lower()])} — no spec written for it[/]"
        )
        definition = None
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
            text = (
                MODELINE
                + "\n"
                + dump_spec(definition, catalog_variable=variable, manage=manage)
            )
        path.write_text(text, encoding="utf-8")
        note = f" [dim](YAML: SQL can't say {reason})[/]" if reason else ""
        out.print(f"[green]+[/] {escape(str(path))}{note}")

    relations: list[Relation] = [
        replace(relation, owner=None)
        for relation in (
            *(entry.table for entry in live.tables),
            *live.views,
            *live.functions,
            *live.volumes,
        )
    ]
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
            text = (
                MODELINE
                + "\n"
                + dump_spec(relation, catalog_variable=variable, manage=manage)
            )
        path.write_text(text, encoding="utf-8")
        note = f" [dim](YAML: SQL can't say {reason})[/]" if reason else ""
        out.print(f"[green]+[/] {escape(str(path))}{note}")

    for name, reason in live.skipped:
        out.print(
            f"[dim]· skipped {name} ({reason}) — deltaplan manages Delta tables, "
            "views and SQL functions[/]"
        )

    if not relations:
        err.print(
            f"[yellow]No Delta tables, views or functions found in {escape(schema)}.[/]"
        )
        return
    out.print(
        "\nNext: [bold]deltaplan plan[/] shows what adopting them means — a claim "
        "per table, nothing else — and [bold]deltaplan apply[/] does it."
    )


PROJECT_FILE = "deltaplan.yml"


def starter_project(
    catalog: str,
    *,
    specs: Path,
    warehouse_id: str | None = None,
    profile: str | None = None,
) -> str:
    """The `deltaplan.yml` a first `import` writes: one target, `dev`, whose
    `catalog` is the one imported from — so the specs say `${catalog}` and the
    next target is one more block."""
    connection = "".join(
        f"    {key}: {value}\n"
        for key, value in (("profile", profile), ("warehouse_id", warehouse_id))
        if value
    )
    return f"""\
# yaml-language-server: $schema=https://misja-pronk.github.io/deltaplan/schema/project.json
# Written by `deltaplan import`. Every key is explained at
# https://misja-pronk.github.io/deltaplan/spec/#the-project-file
version: 1
specs: [{specs.as_posix()}]

# Where `apply` records its runs and holds its lock; created on first use.
history_schema: ${{catalog}}.deltaplan

targets:
  dev:
    vars:
      catalog: {catalog}
{connection}"""


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
    select: SelectOption = None,
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
        select=select,
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
        f"[yellow]Drift: {count(drifted, 'table')} "
        f"{'differs from its spec' if drifted == 1 else 'differ from their specs'}.[/] "
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
    select: list[str] | None = None,
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
        select=select,
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
                out.print(f"\n[green]Wrote[/] {escape(str(output))}")
            return
    if output:
        output.write_text(text, encoding="utf-8")
        out.print(f"[green]Wrote[/] {escape(str(output))}")
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
    select: list[str] | None = None,
) -> Plan:
    relations = [spec.table for spec in specs]
    chosen = _selection(select, [relation.name for relation in relations])
    try:
        return plan_tables(
            relations,
            Introspector(runner, project.manage, parallel=parallel),
            target=target.name,
            tool_version=package_version(),
            mode_for=lambda schema: project.mode_for(target, schema),
            check_order=check_order,
            clone=clone,
            select=chosen,
            owned_elsewhere=target.owned_by_the_bundle(),
            manage=project.manage,
        )
    except (PlanningError, IntrospectionError) as error:
        err.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(1) from error


def _selection(
    patterns: list[str] | None, names: list[str]
) -> Callable[[str], bool] | None:
    """What `--select` accepts: a name from its last part up to all three —
    `orders`, `sales.orders`, `dev.sales.orders` — or a pattern (`sales.*`).
    A pattern that names nothing is an error, not an empty plan."""
    if not patterns:
        return None
    wanted = [pattern.lower() for pattern in patterns]

    def matches(name: str, pattern: str) -> bool:
        parts = name.lower().split(".")
        return any(
            fnmatch(".".join(parts[start:]), pattern) for start in range(len(parts))
        )

    for pattern in wanted:
        if not any(matches(name, pattern) for name in names):
            err.print(f"[red]--select {escape(pattern)} matches no spec.[/]")
            raise typer.Exit(1)
    return lambda name: any(matches(name, pattern) for pattern in wanted)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


@app.command()
def apply(
    plan_file: Annotated[
        Path | None,
        typer.Argument(
            help="A plan written by `deltaplan plan -o`. Without one, plans now, "
            "shows the plan and asks before running it."
        ),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Which target, when planning now."),
    ] = None,
    select: SelectOption = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Don't ask; apply what was planned.")
    ] = False,
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
    parallel: ParallelOption = 8,
) -> None:
    """Apply your specs: plan, show, ask, run — or run a saved plan.

    A saved plan (`deltaplan plan -o plan.json`) is what CI reviews and applies;
    it resumes where an interrupted run stopped. Without one, apply plans now
    and asks before it changes anything.
    """
    project = _project(config)
    if plan_file is not None:
        if target is not None or select:
            err.print(
                "[red]A saved plan already says what it does: --target and --select "
                "are for planning now.[/]"
            )
            raise typer.Exit(1)
        built = _read_plan(plan_file)
        chosen = _target(project, built.target)
        runner = _warehouse(warehouse_id, chosen, profile)
    else:
        chosen = _target(project, target)
        specs = _load(project, chosen)
        _abort_on_lint_errors(specs)
        runner = _warehouse(warehouse_id, chosen, profile)
        built = _plan(project, chosen, specs, runner, parallel=parallel, select=select)
        render_plan(built, out)
        if built.empty:
            return
        if any(s.risk == "destructive" for s in built.steps) and not allow_destructive:
            err.print(
                "\n[red]This plan destroys something. Run it again with "
                "--allow-destructive if that is what you want.[/]"
            )
            raise typer.Exit(1)
        if not yes and not _confirm(built):
            out.print("Nothing applied.")
            raise typer.Exit(1)
        out.print()
    history = _history(project, chosen, runner)

    out.print(
        f"[bold]{built.target}[/] · {count(len(built.steps), 'step')} · "
        f"highest risk [{RISK_STYLE[built.highest_risk]}]{built.highest_risk}[/]"
    )

    executor = Executor(
        runner=runner,
        introspector=Introspector(runner),
        history=history,
        observer=partial(_show_step, width=number_width(built)),
    )
    try:
        result = executor.apply(built, allow_destructive=allow_destructive)
    except DestructiveRefused as error:
        # The refusal is the library's; the flag that lifts it is this CLI's.
        err.print(f"[red]{escape(str(error))} Re-run with --allow-destructive.[/]")
        raise typer.Exit(1) from error
    except (ExecutionError, IntrospectionError) as error:
        err.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(1) from error

    _report(result, built, plan_file)
    if not result.ok:
        raise typer.Exit(1)


def _confirm(built: Plan) -> bool:
    """Ask before changing anything. No answer — a closed stdin, as in CI —
    is no."""
    from rich.prompt import Confirm

    out.print()
    try:
        return Confirm.ask(
            f"Apply {count(len(built.steps), 'step')} to [bold]{built.target}[/]?",
            console=out,
            default=False,
        )
    except EOFError:
        out.print()
        return False


def _show_step(step: Step, status: Status, note: str | None, *, width: int = 1) -> None:
    colour = {"succeeded": "green", "skipped": "dim", "failed": "red"}[status]
    label = {"succeeded": "ok", "skipped": "skipped", "failed": "failed"}[status]
    line = (
        f"  [dim]{step.id:>{width}}.[/] {step.title.ljust(TITLE_WIDTH)} "
        f"[{RISK_STYLE[step.risk]}]\\[{step.risk}][/] [{colour}]{label}[/]"
    )
    if status == "skipped" and note:
        line += f" [dim]({escape(note)})[/]"
    out.print(line)
    if status == "failed" and note:
        err.print(f"     [red]{escape(note)}[/]")


def _report(result: ExecutionResult, built: Plan, plan_file: Path | None) -> None:
    ran, skipped = len(result.ran), len(result.skipped)
    if result.ok:
        out.print(
            f"\n[green]Applied[/] {count(ran, 'step')}, skipped {skipped} · run "
            f"[bold]{result.run_id}[/]"
        )
        return
    err.print(
        f"\n[red]Failed at step {result.failed} of {len(built.steps)}[/] · run "
        f"[bold]{result.run_id}[/]\n"
        + (
            f"Fix the cause and run `deltaplan apply {plan_file}` again — it resumes "
            "from here rather than starting over."
            if plan_file is not None
            else "Fix the cause and run `deltaplan apply` again — it plans from where "
            "the tables are now."
        )
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
        err.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(1) from error
    if holder is None:
        out.print(f"[green]{chosen.name} was not locked.[/]")
        return
    out.print(f"[yellow]Released[/] {chosen.name}, which run [bold]{holder}[/] held.")


def _read_plan(path: Path) -> Plan:
    try:
        return plan_loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        err.print(f"[red]cannot read {escape(f'{path}: {error}')}[/]")
        raise typer.Exit(1) from error
    except PlanFileError as error:
        err.print(f"[red]{escape(f'{path}: {error}')}[/]")
        raise typer.Exit(1) from error


def _history(project: Project, target: Target, runner: WarehouseRunner) -> HistoryStore:
    try:
        schema = project.history_schema_for(target)
    except KeyError as error:
        err.print(f"[red]history_schema: {escape(str(error.args[0]))}[/]")
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
        path = config or _from_here(find_project_file(Path.cwd()))
    except FileNotFoundError:
        return None
    try:
        return load_project(path, os.environ)
    except SpecError as error:
        err.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(1) from error


def _from_here(path: Path) -> Path:
    """A path relative to the working directory when it is under it, so the
    spec paths in messages read `tables/orders.yml:6:5`, not the whole disk."""
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


def _target(project: Project, name: str | None) -> Target:
    if name is None and project.default_target is None:
        known = ", ".join(t.name for t in project.targets) or "none defined"
        err.print(f"[red]Pick a target with -t (known: {known}).[/]")
        raise typer.Exit(1)
    try:
        return _named(project, name or str(project.default_target))
    except KeyError as error:
        err.print(f"[red]{escape(str(error.args[0]))}[/]")
        raise typer.Exit(1) from error
    except BundleError as error:
        # The Databricks CLI answered with an error; it is the error.
        err.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(1) from error


def _named(project: Project, name: str) -> Target:
    """The target, as the Databricks CLI resolves its bundle.

    Variables, lookups and the names a deploy really uses are the CLI's to
    settle, so it is asked once per command. Without it — not installed, or no
    credentials for it to look anything up with — what deltaplan read from the
    bundle file stands in, and says *unknown* rather than guessing.
    """
    return as_deployed(project, project.target(name))


def _load(project: Project, target: Target) -> tuple[LoadedSpec, ...]:
    try:
        specs = load_specs(project, target)
    except (SpecError, FileNotFoundError) as error:
        err.print(f"[red]{escape(str(error))}[/]")
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
        err.print(f"[red]Refusing to plan: {count(errors, 'spec error')}.[/]")
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
    runner: WarehouseRunner,
    catalog: str,
    schema: str,
    parallel: int = 8,
    manage: Manage = EVERYTHING,
) -> LiveSchema:
    try:
        return Introspector(runner, manage, parallel=parallel).schema(catalog, schema)
    except IntrospectionError as error:
        err.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(1) from error


def main() -> None:
    app()
