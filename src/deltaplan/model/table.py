"""Tables and their constraints.

A `Table` is the desired *or* the live state — the differ takes one of each and
never needs to know which came from where.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from deltaplan.model.types import Array, Column, DataType, Field, Map, Struct

#: Set on every table deltaplan creates. Only tables carrying it can ever become
#: drop candidates; everything else is reported as unmanaged and left alone.
MANAGED_PROPERTY = "deltaplan.managed"

#: Delta table features deltaplan turns on itself, as prerequisites for a change
#: that needs them. A spec doesn't list them, and reporting them as unmanaged
#: would be reporting our own work back at the user.
#: https://docs.databricks.com/aws/en/delta/column-mapping
#: https://docs.databricks.com/aws/en/delta/type-widening
COLUMN_MAPPING_PROPERTY = "delta.columnMapping.mode"
TYPE_WIDENING_PROPERTY = "delta.enableTypeWidening"
PREREQUISITE_PROPERTIES = frozenset({COLUMN_MAPPING_PROPERTY, TYPE_WIDENING_PROPERTY})


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


def type_at(table: Table, path: str) -> DataType | None:
    """The type at a nested path, or None if nothing lives there.

    Paths are Databricks' own: `amount`, `address.zip`, `lines.element.sku`,
    `by_code.key` / `by_code.value`.
    """
    parts = path.split(".")
    column = table.column(parts[0])
    if column is None:
        return None
    current: DataType = column.type
    for part in parts[1:]:
        match current:
            case Struct():
                member = current.field(part)
                if member is None:
                    return None
                current = member.type
            case Array(element, _) if part == "element":
                current = element
            case Map(key, _) if part == "key":
                current = key
            case Map(_, value) if part == "value":
                current = value
            case _:
                return None
    return current


def field_at(table: Table, path: str) -> Field | None:
    """The field at a nested path — a column, or a struct member inside one.

    Array elements and map keys/values are types, not fields, so they have no
    name, comment or nullability of their own and return None here.
    """
    parts = path.split(".")
    column = table.column(parts[0])
    if column is None or len(parts) == 1:
        return column
    parent = type_at(table, ".".join(parts[:-1]))
    return parent.field(parts[-1]) if isinstance(parent, Struct) else None
