"""The verbs, for a program that runs deltaplan as part of something larger.

Each one is what a command does, minus the arguments and the printing:

```python
project  = deltaplan.Project.find()
resolved = project.resolve(project.target("prod"))
conn     = deltaplan.Connection.from_target(resolved)

plan = deltaplan.plan(project, resolved, conn)
if not plan.empty and not plan.is_destructive:
    run = deltaplan.apply(plan, conn, project=project, target=resolved)
```

The plan comes back as the same frozen `Plan` the CLI renders, so a host can
summarise it, save it, send it somewhere else and apply it there. Everything
here raises `DeltaplanError` and nothing else on purpose.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace

from deltaplan.connect import Connection
from deltaplan.errors import DeltaplanError
from deltaplan.executor import ExecutionResult, Executor, Status
from deltaplan.history import DeltaHistory, HistoryStore
from deltaplan.loader import Diagnostic, Project, Specs, Target
from deltaplan.manage import EVERYTHING, Manage
from deltaplan.model.plan import Plan, Step
from deltaplan.model.view import Relation
from deltaplan.model.volume import Volume
from deltaplan.planning import PlanningError, plan_tables


class NoHistory(DeltaplanError):
    """`apply` records every run, and this project says nowhere to record it."""


def plan(
    project: Project,
    target: Target,
    connection: Connection,
    *,
    select: Callable[[str], bool] | Sequence[str] | str | None = None,
    check_order: bool = False,
    clone: bool = False,
    parallel: int = 8,
    specs: Specs | None = None,
) -> Plan:
    """What deltaplan would do to make the live objects match the specs.

    `select` narrows it to some of them: a predicate, or the names themselves.
    `check_order` also compares column order; `clone` takes a zero-copy backup
    of every table a risky step is about to touch.

    `specs` is for a caller that has already read them — to lint them first,
    say — so they aren't read twice.

    Raises `SpecErrors` if a spec can't be read, `PlanningError` if the specs
    can't be planned together, and `IntrospectionError` if the workspace can't
    be read.
    """
    specs = specs if specs is not None else project.load_specs(target)
    chosen = _selector(select)
    return plan_tables(
        specs.relations,
        connection.introspector(project.manage, parallel),
        target=target.name,
        tool_version=package_version(),
        mode_for=lambda schema: project.mode_for(target, schema),
        check_order=check_order,
        clone=clone,
        select=chosen,
        owned_elsewhere=target.owned_by_the_bundle(),
        manage=project.manage,
    )


def drift(
    project: Project,
    target: Target,
    connection: Connection,
    *,
    select: Callable[[str], bool] | Sequence[str] | str | None = None,
    parallel: int = 8,
) -> Plan:
    """The plan that would be made right now — for asking whether anything moved.

    Same work as `plan`; the name is the question. An empty plan means the
    workspace matches the specs. What a host does about a plan that isn't
    empty — fail a build, open a pull request, page someone — is its own
    business.
    """
    return plan(project, target, connection, select=select, parallel=parallel)


def validate(project: Project, target: Target) -> tuple[Diagnostic, ...]:
    """Lint every spec, without a workspace. Raises `SpecErrors` if one can't
    be read; what parses but is wrong comes back as diagnostics."""
    return project.load_specs(target).diagnostics


def apply(
    plan: Plan,
    connection: Connection,
    *,
    project: Project | None = None,
    target: Target | None = None,
    history: HistoryStore | None = None,
    allow_destructive: bool = False,
    observer: Callable[[Step, Status, str | None], None] | None = None,
) -> ExecutionResult:
    """Run a plan, and record what it did.

    The run is written to the project's `history_schema`, unless a host passes
    its own `history` — `MemoryHistory` for a test, its own store otherwise.

    Raises `StalePlan` if the live objects have moved since the plan was made,
    `DestructiveRefused` if it would destroy something and `allow_destructive`
    says nothing, `ExecutionError` for anything else that stops it. A step that
    fails once it is running doesn't raise: the result says which one, so the
    rest of the run is on record.
    """
    store = history if history is not None else history_for(project, target, connection)
    executor = Executor(
        runner=connection.runner,
        # The same reading of live state the plan was made with: anything
        # else and the two disagree by what the project handed over.
        introspector=connection.introspector(plan.manage),
        history=store,
        observer=observer or (lambda *_: None),
    )
    return executor.apply(plan, allow_destructive=allow_destructive)


def is_stale(plan: Plan, connection: Connection) -> bool:
    """Whether the live objects have moved since this plan was made.

    `apply` refuses a stale plan; this asks first, so a host can plan again
    before it puts a question to a person.
    """
    from deltaplan.executor import stale_tables

    return bool(stale_tables(plan, connection.introspector(plan.manage)))


def history_for(
    project: Project | None, target: Target | None, connection: Connection
) -> HistoryStore:
    """Where this project records what `apply` did.

    Raises `NoHistory` when the project says nowhere — `apply` keeps a record
    of every run, and won't run without somewhere to keep it.
    """
    if project is None or target is None:
        raise NoHistory(
            "apply records every run: pass `project` and `target` so deltaplan "
            "knows where, or pass a `history` store of your own."
        )
    schema = project.history_schema_for(target)
    if not schema:
        raise NoHistory(
            "apply records every run in Delta tables, and this project says "
            "nowhere to keep them. Set `history_schema` in deltaplan.yml, or "
            "pass a `history` store of your own."
        )
    return DeltaHistory(connection.runner, schema)


def _selector(
    select: Callable[[str], bool] | Sequence[str] | str | None,
) -> Callable[[str], bool] | None:
    """Names or a predicate, both meaning the same thing to the planner."""
    if select is None:
        return None
    if isinstance(select, str):
        select = [select]
    if isinstance(select, Sequence):
        wanted = {name.casefold() for name in select}
        return lambda name: name.casefold() in wanted
    return select


def package_version() -> str:
    """The installed version of deltaplan, recorded in every plan it makes."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        return installed("deltaplan")
    except PackageNotFoundError:  # pragma: no cover - only outside an install
        return "unknown"


@dataclass(frozen=True, slots=True)
class ImportedSchema:
    """What `import_schema` found: specs to write, and what it left alone.

    Iterating gives the specs, which is what most callers want; `skipped` says
    what in the schema deltaplan doesn't manage and why, so a host can tell
    someone rather than leave them wondering.
    """

    specs: tuple[ImportedSpec, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    def __iter__(self) -> Iterator[ImportedSpec]:
        return iter(self.specs)

    def __len__(self) -> int:
        return len(self.specs)


@dataclass(frozen=True, slots=True)
class ImportedSpec:
    """One spec `import` would write, and what it is about.

    `filename` is what the CLI calls the file; a host that keeps specs
    somewhere else can ignore it and use `relation.name`.
    """

    filename: str
    text: str
    relation: Relation


def import_schema(
    connection: Connection,
    schema: str,
    *,
    manage: Manage = EVERYTHING,
    catalog_variable: str | None = None,
    owned_elsewhere: Mapping[str, str] | None = None,
    spec_format: str = "yaml",
    parallel: int = 8,
) -> ImportedSchema:
    """Specs for what already exists in `catalog.schema`, as text.

    Nothing is written: a host decides where these go, whether that is a
    directory, a pull request or a review screen. The schema's own spec comes
    first when it has anything to say, then tables, views, functions and
    volumes.

    Owners are left out — an owner is usually a person's email, and rarely the
    same in two workspaces — as is anything `manage` hands to another tool, and
    anything an Asset Bundle declares (`owned_elsewhere`, which
    `target.owned_by_the_bundle()` gives you). `catalog_variable` writes the
    catalog as `${name}`, so one spec serves every target.

    Raises `IntrospectionError` if the schema can't be read.
    """
    from deltaplan.loader import dump_spec
    from deltaplan.sqlspec import dump_sql_spec, sql_cannot_say

    catalog, _, name = schema.partition(".")
    if not name or "." in name:
        raise PlanningError(f"a schema is named catalog.schema, not {schema!r}")
    live = connection.introspector(manage, parallel).schema(catalog, name)
    owned = {key.lower(): value for key, value in (owned_elsewhere or {}).items()}

    def written(relation: Relation, stem: str) -> ImportedSpec:
        reason = sql_cannot_say(relation) if spec_format == "sql" else None
        if spec_format == "sql" and reason is None:
            return ImportedSpec(
                f"{stem}.sql",
                dump_sql_spec(relation, catalog_variable=catalog_variable),
                relation,
            )
        return ImportedSpec(
            f"{stem}.yml",
            dump_spec(relation, catalog_variable=catalog_variable, manage=manage),
            relation,
        )

    specs: list[ImportedSpec] = []
    definition = replace(live.definition, owner=None) if live.definition else None
    if (
        definition is not None
        and definition.name.lower() not in owned
        and (definition.comment or definition.tags or definition.grants)
    ):
        specs.append(written(definition, "_schema"))

    seen: set[str] = set()
    for relation in (
        *(entry.table for entry in live.tables),
        *live.views,
        *live.functions,
        *live.volumes,
    ):
        if relation.name.lower() in owned:
            continue
        stem = relation.short_name
        if stem in seen:
            # Functions and volumes don't share a namespace with tables, so one
            # may have a table's name; its file then says what it is.
            stem = f"{stem}.{'volume' if isinstance(relation, Volume) else 'function'}"
        seen.add(stem)
        specs.append(written(replace(relation, owner=None), stem))
    return ImportedSchema(tuple(specs), tuple(live.skipped))
