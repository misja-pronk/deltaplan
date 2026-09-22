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

from collections.abc import Callable, Sequence

from deltaplan.connect import Connection
from deltaplan.errors import DeltaplanError
from deltaplan.executor import ExecutionResult, Executor, Status
from deltaplan.history import DeltaHistory, HistoryStore
from deltaplan.loader import Diagnostic, Project, Target
from deltaplan.model.plan import Plan, Step
from deltaplan.planning import plan_tables


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
) -> Plan:
    """What deltaplan would do to make the live objects match the specs.

    `select` narrows it to some of them: a predicate, or the names themselves.
    `check_order` also compares column order; `clone` takes a zero-copy backup
    of every table a risky step is about to touch.

    Raises `SpecErrors` if a spec can't be read, `PlanningError` if the specs
    can't be planned together, and `IntrospectionError` if the workspace can't
    be read.
    """
    specs = project.load_specs(target)
    chosen = _selector(select)
    return plan_tables(
        specs.relations,
        connection.introspector(project.manage, parallel),
        target=target.name,
        tool_version=_version(),
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
    store = history if history is not None else _history(project, target, connection)
    executor = Executor(
        runner=connection.runner,
        introspector=connection.introspector(),
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

    return bool(stale_tables(plan, connection.introspector()))


def _history(
    project: Project | None, target: Target | None, connection: Connection
) -> HistoryStore:
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


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("deltaplan")
    except PackageNotFoundError:  # pragma: no cover - running from a checkout
        return "0.0.0"
