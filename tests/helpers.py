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
