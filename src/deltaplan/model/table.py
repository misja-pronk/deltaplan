"""Tables and their constraints.

A `Table` is the desired *or* the live state — the differ takes one of each and
never needs to know which came from where.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from deltaplan.model.types import Column

#: Set on every table deltaplan creates. Only tables carrying it can ever become
#: drop candidates; everything else is reported as unmanaged and left alone.
MANAGED_PROPERTY = "deltaplan.managed"


@dataclass(frozen=True, slots=True)
class PrimaryKey:
    """An informational primary key.

    Unity Catalog primary keys are declarative (`RELY`/`NORELY`) rather than
    enforced, and their columns must be `NOT NULL`.
    https://docs.databricks.com/aws/en/tables/constraints
    """

    columns: tuple[str, ...]
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Check:
    """An enforced `CHECK` constraint."""

    name: str
    expression: str


Constraint: TypeAlias = PrimaryKey | Check


@dataclass(frozen=True, slots=True)
class Table:
    """A Delta table in Unity Catalog.

    `properties` and `tags` are unordered maps, so they are stored sorted and
    compare regardless of the order they were written in. `cluster_by` and
    `columns` keep their order, because theirs is meaningful.
    """

    name: str
    columns: tuple[Column, ...]
    comment: str | None = None
    cluster_by: tuple[str, ...] = ()
    properties: tuple[tuple[str, str], ...] = ()
    tags: tuple[tuple[str, str], ...] = ()
    constraints: tuple[Constraint, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "properties", tuple(sorted(self.properties)))
        object.__setattr__(self, "tags", tuple(sorted(self.tags)))

    # -- names -------------------------------------------------------------
    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.name.split("."))

    @property
    def short_name(self) -> str:
        return self.parts[-1]

    @property
    def schema(self) -> str:
        """The `catalog.schema` this table lives in."""
        return ".".join(self.parts[:-1])

    # -- lookups -----------------------------------------------------------
    def column(self, name: str) -> Column | None:
        for candidate in self.columns:
            if candidate.name == name:
                return candidate
        return None

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def properties_map(self) -> dict[str, str]:
        return dict(self.properties)

    def tags_map(self) -> dict[str, str]:
        return dict(self.tags)

    @property
    def managed(self) -> bool:
        """True when deltaplan created this table and may therefore drop it."""
        return self.properties_map().get(MANAGED_PROPERTY, "").lower() == "true"

    def primary_key(self) -> PrimaryKey | None:
        for constraint in self.constraints:
            if isinstance(constraint, PrimaryKey):
                return constraint
        return None

    def checks(self) -> tuple[Check, ...]:
        return tuple(c for c in self.constraints if isinstance(c, Check))


def default_primary_key_name(table: Table) -> str:
    """Databricks requires every constraint to be named; this is our default."""
    return f"{table.short_name}_pk"
