"""Views: a name, a query, and what governs them.

In Unity Catalog a view is a kind of table — it has the same three-part name,
lives in `information_schema.tables`, and takes the same tags, grants and
properties. What it doesn't have is a schema of its own: its columns are
whatever its query returns, so a view spec declares the query, not the columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

from deltaplan.model.function import Function
from deltaplan.model.schema import Schema
from deltaplan.model.table import Grant, Securable, Table, sort_governance
from deltaplan.model.volume import Volume


@dataclass(frozen=True, slots=True)
class View(Securable):
    """A view."""

    name: str
    query: str
    comment: str | None = None
    properties: tuple[tuple[str, str], ...] = ()
    tags: tuple[tuple[str, str], ...] = ()
    grants: tuple[Grant, ...] = ()
    #: What the spec says must not be there: `tags: {pii: null}`. Spec-only —
    #: a live object never has any — so they take no part in comparing.
    removed_properties: tuple[str, ...] = field(default=(), compare=False)
    removed_tags: tuple[str, ...] = field(default=(), compare=False)
    #: Who owns it in Unity Catalog. Only a spec that names an owner has it
    #: enforced; a live object always has one, so it takes no part in comparing.
    owner: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        sort_governance(self)
        # Surrounding whitespace isn't part of a query, whichever way it arrived:
        # a YAML block adds a newline, the catalog may not.
        object.__setattr__(self, "query", self.query.strip())


#: Anything a spec can describe.
Relation: TypeAlias = Table | View | Function | Schema | Volume


def normalise_query(query: str) -> str:
    """A view's query, as compared across a diff.

    Whitespace runs collapse and a trailing semicolon goes; beyond that the
    comparison is textual. Unity Catalog stores `view_definition` as written —
    verified live by `test_a_view_reads_back_as_its_spec`; if that ever changes,
    every plan shows a change that isn't one.
    """
    return " ".join(query.split()).rstrip(";").strip()
