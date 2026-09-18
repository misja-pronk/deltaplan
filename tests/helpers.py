"""Small builders, so tests read as tables rather than as constructor calls."""

from deltaplan.model.table import Constraint, Table
from deltaplan.model.types import Column, Field
from deltaplan.typeparser import parse_type


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
