"""Tables and their constraints.

A `Table` is the desired *or* the live state — the differ takes one of each and
never needs to know which came from where.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
#: The table feature column defaults need. A `delta.feature.*` flag, so it is
#: bookkeeping as far as reporting and import are concerned.
DEFAULTS_FEATURE = "delta.feature.allowColumnDefaults"
PREREQUISITE_PROPERTIES = frozenset({COLUMN_MAPPING_PROPERTY, TYPE_WIDENING_PROPERTY})

#: Properties Delta maintains itself. Declaring one in a spec would have deltaplan
#: fight Delta for it — setting `maxColumnId` back after columns were added
#: corrupts column mapping — so a spec may not, and `import` never writes them.
MAINTAINED_PROPERTIES = frozenset(
    {
        "delta.columnMapping.maxColumnId",
        "delta.minReaderVersion",
        "delta.minWriterVersion",
    }
)

#: Delta keeps each CHECK constraint as a table property under this prefix,
#: named after the constraint. deltaplan models them as constraints instead.
CHECK_PROPERTY_PREFIX = "delta.constraints."

#: How a `set_cluster_by` change says "automatic liquid clustering".
CLUSTER_AUTO = "auto"

#: `delta.feature.<name>` records that a table feature is supported. Delta sets it
#: when the feature is enabled another way; it isn't anyone's intent to report.
FEATURE_FLAG_PREFIX = "delta.feature."


#: Properties Unity Catalog and Delta set for their own use — table ids, the
#: hidden columns row tracking keeps, internal format markers. Seen on every new
#: table in a live workspace (2026-09-18); never anyone's intent, and replayed
#: onto another table they would be wrong.
INTERNAL_PROPERTY_PREFIXES = ("io.unitycatalog.", "delta.rowTracking.materialized")

#: What a new table gets without asking, on a current workspace (observed
#: 2026-09-18). Not reported as unmanaged and not written by `import` while they
#: hold these values — they are the platform's, not the table's. A table where
#: someone changed one still shows it. A spec may declare them like any other.
PLATFORM_DEFAULTS: dict[str, str] = {
    "delta.enableDeletionVectors": "true",
    "delta.enableRowTracking": "true",
    "delta.checkpointPolicy": "v2",
    "delta.checkpoint.writeStatsAsJson": "false",
    "delta.checkpoint.writeStatsAsStruct": "true",
    "delta.parquet.compression.codec": "zstd",
    "delta.parquet.format.version": "2.12.0",
}


def is_platform_default(key: str, value: str) -> bool:
    return PLATFORM_DEFAULTS.get(key) == value


def is_bookkeeping(key: str) -> bool:
    """Is this property Delta's or deltaplan's own, rather than anyone's intent?"""
    return (
        key in MAINTAINED_PROPERTIES
        or key.startswith(INTERNAL_PROPERTY_PREFIXES)
        or key.endswith(".internal")
        or key.startswith(FEATURE_FLAG_PREFIX)
        or key == MANAGED_PROPERTY
        or key in PREREQUISITE_PROPERTIES
        or key.startswith(CHECK_PROPERTY_PREFIX)
    )


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


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """An informational foreign key onto another table's primary key.

    Like a primary key in Unity Catalog, it is declared rather than enforced.
    https://docs.databricks.com/aws/en/tables/constraints
    """

    columns: tuple[str, ...]
    references: str
    referenced_columns: tuple[str, ...]
    name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", self.references.lower())

    def same_as(self, other: ForeignKey) -> bool:
        """The same relationship, whatever it is called."""
        return (
            tuple(c.casefold() for c in self.columns)
            == tuple(c.casefold() for c in other.columns)
            and self.references == other.references
            and tuple(c.casefold() for c in self.referenced_columns)
            == tuple(c.casefold() for c in other.referenced_columns)
        )


Constraint: TypeAlias = PrimaryKey | Check | ForeignKey


@dataclass(frozen=True, slots=True)
class RowFilter:
    """A row filter: a SQL function over some columns that hides rows.

    https://docs.databricks.com/aws/en/tables/row-and-column-filters
    """

    function: str
    columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "function", self.function.lower())


@dataclass(frozen=True, slots=True)
class Hooks:
    """SQL to run around a table's changes — only when it has some in the plan.

    The design's "simple pre/post SQL hooks": the escape hatch for what a spec
    can't say, like a backfill that isn't one column's expression. deltaplan runs
    them as written and can't tell what they do, so the plan says so.
    """

    before: str | None = None
    after: str | None = None


@dataclass(frozen=True, slots=True)
class Grant:
    """What one principal may do with a table.

    A spec that names a principal manages that principal's privileges exactly;
    principals it doesn't name are someone else's business.
    """

    principal: str
    privileges: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "privileges", tuple(sorted(set(self.privileges))))


class Securable:
    """What tables and views share: a three-part name, and what governs them.

    A mixin rather than a base dataclass, so both stay frozen and slotted.
    """

    __slots__ = ()

    name: str
    properties: tuple[tuple[str, str], ...]
    tags: tuple[tuple[str, str], ...]
    grants: tuple[Grant, ...]
    removed_properties: tuple[str, ...]
    removed_tags: tuple[str, ...]
    owner: str | None

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.name.split("."))

    @property
    def short_name(self) -> str:
        return self.parts[-1]

    @property
    def schema(self) -> str:
        """The `catalog.schema` this lives in."""
        return ".".join(self.parts[:-1])

    def properties_map(self) -> dict[str, str]:
        return dict(self.properties)

    def tags_map(self) -> dict[str, str]:
        return dict(self.tags)

    def grants_map(self) -> dict[str, tuple[str, ...]]:
        return {grant.principal: grant.privileges for grant in self.grants}

    @property
    def managed(self) -> bool:
        """True when deltaplan created this and may therefore drop it."""
        return self.properties_map().get(MANAGED_PROPERTY, "").lower() == "true"


def sort_governance(obj: Securable) -> None:
    """Normalise what the catalog normalises, so equality means what it says.

    * The name in lower case: Unity Catalog stores catalog, schema, table and
      view names that way, whatever case they were written in. Comparing them any
      other way would make `Orders` in a spec a different table from the live
      `orders` — which, in a strict schema, plans dropping the real one.
      https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-names
    * Unordered maps sorted, so equality ignores the order they came in.
    """
    object.__setattr__(obj, "name", obj.name.lower())
    object.__setattr__(obj, "properties", tuple(sorted(obj.properties)))
    object.__setattr__(obj, "tags", tuple(sorted(obj.tags)))
    object.__setattr__(
        obj, "grants", tuple(sorted(obj.grants, key=lambda g: g.principal))
    )


@dataclass(frozen=True, slots=True)
class Table(Securable):
    """A Delta table in Unity Catalog.

    `properties` and `tags` are unordered maps, so they are stored sorted and
    compare regardless of the order they were written in. `cluster_by` and
    `columns` keep their order, because theirs is meaningful.
    """

    name: str
    columns: tuple[Column, ...]
    comment: str | None = None
    cluster_by: tuple[str, ...] = ()
    #: Automatic liquid clustering: Databricks picks the keys and may change
    #: them, so `cluster_by` is then what it chose — never compared.
    #: https://docs.databricks.com/aws/en/delta/clustering#automatic-liquid-clustering
    cluster_auto: bool = False
    properties: tuple[tuple[str, str], ...] = ()
    tags: tuple[tuple[str, str], ...] = ()
    constraints: tuple[Constraint, ...] = ()
    grants: tuple[Grant, ...] = ()
    row_filter: RowFilter | None = None
    #: Not state: nothing in the catalog records them, so they take no part in
    #: comparing a spec with a live table.
    hooks: Hooks | None = field(default=None, compare=False)
    #: The table's previous full name, while a rename is still to be applied.
    renamed_from: str | None = field(default=None, compare=False)
    #: What the spec says must not be there: `tags: {pii: null}`. Spec-only —
    #: a live object never has any — so they take no part in comparing.
    removed_properties: tuple[str, ...] = field(default=(), compare=False)
    removed_tags: tuple[str, ...] = field(default=(), compare=False)
    #: Who owns it in Unity Catalog. Only a spec that names an owner has it
    #: enforced; a live object always has one, so it takes no part in comparing.
    owner: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        sort_governance(self)
        if self.renamed_from is not None:
            object.__setattr__(self, "renamed_from", self.renamed_from.lower())

    # -- lookups -----------------------------------------------------------
    def column(self, name: str) -> Column | None:
        """Look a column up the way Delta resolves one: ignoring case.

        Delta keeps the case a column was written in but won't hold two names
        that differ only by it, so a case-insensitive match is never ambiguous.
        """
        wanted = name.casefold()
        for candidate in self.columns:
            if candidate.name.casefold() == wanted:
                return candidate
        return None

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def protected(self) -> bool:
        """Does anything here decide what a reader may see?"""
        return self.row_filter is not None or any(c.mask for c in self.columns)

    def primary_key(self) -> PrimaryKey | None:
        for constraint in self.constraints:
            if isinstance(constraint, PrimaryKey):
                return constraint
        return None

    def checks(self) -> tuple[Check, ...]:
        return tuple(c for c in self.constraints if isinstance(c, Check))

    def foreign_keys(self) -> tuple[ForeignKey, ...]:
        return tuple(c for c in self.constraints if isinstance(c, ForeignKey))


def default_foreign_key_name(table: str, key: ForeignKey) -> str:
    """Databricks requires a name; this is ours: `orders_customer_id_fk`."""
    return f"{table.split('.')[-1]}_{'_'.join(key.columns)}_fk"


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
