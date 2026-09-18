"""Small builders, so tests read as tables rather than as constructor calls."""

from deltaplan.model.plan import Plan
from deltaplan.model.table import Constraint, Table
from deltaplan.model.types import Column, Field
from deltaplan.typeparser import parse_type
from fake_warehouse import FakeWarehouse


def col(
    name: str,
    type_text: str,
    *,
    nullable: bool = True,
    comment: str | None = None,
    renamed_from: str | None = None,
) -> Column:
    return Field(
        name,
        parse_type(type_text),
        nullable=nullable,
        comment=comment,
        renamed_from=renamed_from,
    )


def table(
    *columns: Column,
    name: str = "main.sales.orders",
    comment: str | None = None,
    cluster_by: tuple[str, ...] = (),
    properties: tuple[tuple[str, str], ...] = (),
    tags: tuple[tuple[str, str], ...] = (),
    constraints: tuple[Constraint, ...] = (),
) -> Table:
    return Table(
        name=name,
        columns=columns,
        comment=comment,
        cluster_by=cluster_by,
        properties=properties,
        tags=tags,
        constraints=constraints,
    )


Row = dict[str, str | None]

#: Which query each canned result set answers, by a fragment of the statement.
_FRAGMENTS = {
    "tables": "information_schema.tables",
    "columns": "information_schema.columns",
    "tags": "information_schema.table_tags",
    "constraints": "information_schema.table_constraints",
    "keys": "information_schema.key_column_usage",
    "detail": "DESCRIBE DETAIL",
    "history": "DESCRIBE HISTORY",
}


class FakeRunner:
    """A `SqlRunner` that answers by matching a fragment of the statement."""

    def __init__(self, responses: dict[str, tuple[Row, ...]]) -> None:
        self.responses = responses
        self.statements: list[str] = []

    def query(self, statement: str) -> tuple[Row, ...]:
        self.statements.append(statement)
        for fragment, rows in self.responses.items():
            if fragment in statement:
                return rows
        return ()


def fake_runner(**rows: tuple[Row, ...]) -> FakeRunner:
    """`fake_runner(tables=..., columns=..., detail=...)` — see `_FRAGMENTS`."""
    unknown = set(rows) - set(_FRAGMENTS)
    if unknown:
        raise ValueError(f"no query fragment for {sorted(unknown)}")
    return FakeRunner(
        {fragment: rows.get(key, ()) for key, fragment in _FRAGMENTS.items()}
    )


def plan_against(
    desired: Table,
    live: Table | None = None,
    *,
    check_order: bool = False,
    size_bytes: int | None = None,
) -> tuple[FakeWarehouse, Plan]:
    """Introspect a fake warehouse, diff a spec against it, and plan.

    The same shape as the CLI's pipeline and the integration suite's, so an
    offline test and a live one assert the same thing.
    """
    from deltaplan.differ import diff, unmanaged
    from deltaplan.introspect import Introspector
    from deltaplan.model.plan import TableDiff, TableFacts
    from deltaplan.planner import build_plan

    fake = FakeWarehouse.of(
        *((live,) if live is not None else ()),
        sizes={live.name: size_bytes} if live is not None and size_bytes else {},
    )
    found = Introspector(fake).table(desired.name)
    live_now = found.table if found else None
    return fake, build_plan(
        [
            TableDiff(
                desired.name,
                diff(desired, live_now, compare_order=check_order),
                TableFacts(
                    desired.name,
                    exists=live_now is not None,
                    properties=live_now.properties if live_now else (),
                    size_bytes=found.size_bytes if found else None,
                ),
                unmanaged(desired, live_now) if live_now else (),
            )
        ],
        target="test",
        tool_version="0.1.0",
        spec_hash="spec",
        state_fingerprint="live",
    )


def run(plan: Plan, fake: FakeWarehouse) -> None:
    """Run every statement in a plan against the fake, in order."""
    for step in plan.steps:
        if step.sql is None:
            raise AssertionError(f"step {step.id} ({step.title}) has no SQL")
        fake.query(step.sql)
