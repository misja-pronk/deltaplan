"""Views: a name, a query, and what governs them.

In Unity Catalog a view is a kind of table — it has the same three-part name,
lives in `information_schema.tables`, and takes the same tags, grants and
properties. What it doesn't have is a schema of its own: its columns are
whatever its query returns, so a view spec declares the query, not the columns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from deltaplan.model.table import Grant, Securable, Table, sort_governance


@dataclass(frozen=True, slots=True)
class View(Securable):
    """A view."""

    name: str
    query: str
    comment: str | None = None
    properties: tuple[tuple[str, str], ...] = ()
    tags: tuple[tuple[str, str], ...] = ()
    grants: tuple[Grant, ...] = ()

    def __post_init__(self) -> None:
        sort_governance(self)
        # Surrounding whitespace isn't part of a query, whichever way it arrived:
        # a YAML block adds a newline, the catalog may not.
        object.__setattr__(self, "query", self.query.strip())


#: Anything a spec can describe.
Relation: TypeAlias = Table | View


def normalise_query(query: str) -> str:
    """A view's query, as compared across a diff.

    Whitespace runs collapse and a trailing semicolon goes; beyond that the
    comparison is textual. TODO(verify): whether Unity Catalog stores
    `view_definition` exactly as written — if it rewrites it, every plan would
    show a change that isn't one.
    """
    return " ".join(query.split()).rstrip(";").strip()
