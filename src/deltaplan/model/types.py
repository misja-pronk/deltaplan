"""The type tree.

`DataType` is a closed union of frozen values. Consumers match on it exhaustively;
`render_type()` turns it back into a Databricks type string.

Reference: https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-datatypes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

from deltaplan.sql import maybe_quote_ident, quote_literal

# Spellings Databricks accepts for the same type, normalised to the left-hand name.
# https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-datatypes
ALIASES: dict[str, str] = {
    "integer": "int",
    "long": "bigint",
    "short": "smallint",
    "byte": "tinyint",
    "real": "float",
    "dec": "decimal",
    "numeric": "decimal",
    "bool": "boolean",
}

# Names we recognise. Unknown names still parse — Databricks keeps adding types, and
# refusing to model a live table because of one is worse than passing the name
# through — but `validate` warns about them.
KNOWN_PRIMITIVES: frozenset[str] = frozenset(
    {
        "boolean",
        "tinyint",
        "smallint",
        "int",
        "bigint",
        "float",
        "double",
        "string",
        "binary",
        "date",
        "timestamp",
        "timestamp_ntz",
        "variant",
        "void",
    }
)


def normalise_name(name: str) -> str:
    """Lower-case a type name and resolve its aliases (`long` -> `bigint`)."""
    lowered = name.lower()
    return ALIASES.get(lowered, lowered)


@dataclass(frozen=True, slots=True)
class Primitive:
    """A type with no parameters: `string`, `bigint`, `timestamp`, ..."""

    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalise_name(self.name))

    @property
    def known(self) -> bool:
        return self.name in KNOWN_PRIMITIVES


@dataclass(frozen=True, slots=True)
class Decimal:
    """`decimal(p, s)`. Databricks defaults to (10, 0) when unparameterised."""

    precision: int = 10
    scale: int = 0


@dataclass(frozen=True, slots=True)
class Char:
    """`char(n)` — fixed length, space padded."""

    length: int


@dataclass(frozen=True, slots=True)
class Varchar:
    """`varchar(n)` — string with a length limit."""

    length: int


@dataclass(frozen=True, slots=True)
class Array:
    """`array<element>`.

    `contains_null` is not expressible in a type string; it comes from live schema
    metadata and is carried so introspection is lossless.
    """

    element: DataType
    contains_null: bool = True


@dataclass(frozen=True, slots=True)
class Map:
    """`map<key, value>`."""

    key: DataType
    value: DataType


@dataclass(frozen=True, slots=True)
class Struct:
    """`struct<name: type [not null] [comment '...'], ...>`."""

    fields: tuple[Field, ...] = ()

    def field(self, name: str) -> Field | None:
        """Look a field up the way Spark resolves one: ignoring case."""
        wanted = name.casefold()
        for candidate in self.fields:
            if candidate.name.casefold() == wanted:
                return candidate
        return None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)


@dataclass(frozen=True, slots=True)
class Field:
    """A named member of a struct — and, at the top level, a column.

    `renamed_from` and `using` are planning *hints*, not state: they say how the
    live table got here, and how to get the new value out of the old one when a
    rewrite is needed. Neither takes part in equality, so a spec that still
    carries a spent hint compares equal to the table it describes.
    """

    name: str
    type: DataType
    nullable: bool = True
    comment: str | None = None
    renamed_from: str | None = field(default=None, compare=False)
    #: A SQL expression over the *live* table, used only when this table has to
    #: be rewritten and deltaplan can't work out the conversion itself.
    using: str | None = field(default=None, compare=False)
    #: Unity Catalog tags on a column. Top-level columns only; stored sorted, as
    #: the unordered map they are.
    tags: tuple[tuple[str, str], ...] = ()
    #: Tags the spec says must not be on the column: `tags: {pii: null}`.
    removed_tags: tuple[str, ...] = field(default=(), compare=False)
    #: A column mask. Top-level columns only.
    mask: Mask | None = None
    #: How the column gets a value it wasn't given. At most one of the three;
    #: top-level columns only. Identity and generated columns exist only from
    #: table creation; a default can be set and changed at any time.
    identity: Identity | None = None
    generated: str | None = None
    default: str | None = None

    def __post_init__(self) -> None:
        if self.tags:
            object.__setattr__(self, "tags", tuple(sorted(self.tags)))


@dataclass(frozen=True, slots=True)
class Mask:
    """A column mask: a SQL function that decides what each reader sees.

    https://docs.databricks.com/aws/en/tables/row-and-column-filters
    """

    function: str
    using_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Unity Catalog stores function names in lower case, like every name.
        object.__setattr__(self, "function", self.function.lower())


@dataclass(frozen=True, slots=True)
class Identity:
    """`GENERATED { ALWAYS | BY DEFAULT } AS IDENTITY (START WITH … INCREMENT BY …)`.

    Only on a BIGINT column, and only when the table is created.
    https://docs.databricks.com/aws/en/delta/generated-columns
    """

    always: bool = True
    start: int = 1
    increment: int = 1


#: A column is a top-level field.
Column: TypeAlias = Field

DataType: TypeAlias = Primitive | Decimal | Char | Varchar | Array | Map | Struct


def render_type(data_type: DataType, *, upper: bool = False) -> str:
    """Render a type back to a Databricks type string.

    `upper` uppercases type *keywords* only — field names and comments are left
    alone, so `STRUCT<zip: STRING COMMENT 'Postal code'>` stays readable.
    """

    def kw(word: str) -> str:
        return word.upper() if upper else word

    match data_type:
        case Primitive(name):
            return kw(name)
        case Decimal(precision, scale):
            return f"{kw('decimal')}({precision},{scale})"
        case Char(length):
            return f"{kw('char')}({length})"
        case Varchar(length):
            return f"{kw('varchar')}({length})"
        case Array(element, _):
            return f"{kw('array')}<{render_type(element, upper=upper)}>"
        case Map(key, value):
            rendered_key = render_type(key, upper=upper)
            rendered_value = render_type(value, upper=upper)
            return f"{kw('map')}<{rendered_key},{rendered_value}>"
        case Struct(fields):
            return f"{kw('struct')}<{','.join(_render_field(f, upper) for f in fields)}>"


def _render_field(field_: Field, upper: bool) -> str:
    def kw(word: str) -> str:
        return word.upper() if upper else word

    rendered = f"{maybe_quote_ident(field_.name)}:{render_type(field_.type, upper=upper)}"
    if not field_.nullable:
        rendered += f" {kw('not null')}"
    if field_.comment is not None:
        rendered += f" {kw('comment')} {quote_literal(field_.comment)}"
    return rendered


def as_data_type(value: object) -> DataType | None:
    """Narrow a `ChangeValue` (or anything else) to a type, or None.

    Changes carry whatever the change is about, so consumers that only make sense
    for a type — rendering it, widening it — ask for one here.
    """
    if isinstance(value, Primitive | Decimal | Char | Varchar | Array | Map | Struct):
        return value
    return None


def type_kind(data_type: DataType) -> str:
    """The shape of a type, ignoring its parameters: `struct`, `array`, `decimal`, ...

    Two types with different kinds can never be altered into one another — that is a
    rewrite, not a widening.
    """
    match data_type:
        case Primitive(name):
            return name
        case Decimal():
            return "decimal"
        case Char():
            return "char"
        case Varchar():
            return "varchar"
        case Array():
            return "array"
        case Map():
            return "map"
        case Struct():
            return "struct"


def walk(data_type: DataType, path: str = "") -> list[tuple[str, Field]]:
    """Every nested field in a type, as (path, field) pairs.

    Paths use Databricks' own nested syntax, which is what `ALTER TABLE` expects:
    `a.b` inside a struct, `a.element.b` inside an array, `m.key` / `m.value`
    inside a map.
    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table-manage-column
    """
    found: list[tuple[str, Field]] = []
    match data_type:
        case Struct(fields):
            for member in fields:
                child_path = f"{path}.{member.name}" if path else member.name
                found.append((child_path, member))
                found.extend(walk(member.type, child_path))
        case Array(element, _):
            found.extend(walk(element, f"{path}.element" if path else "element"))
        case Map(key, value):
            found.extend(walk(key, f"{path}.key" if path else "key"))
            found.extend(walk(value, f"{path}.value" if path else "value"))
        case _:
            pass
    return found


def contains_timestamp_ntz(data_type: DataType) -> bool:
    """Whether a type is, or nests, TIMESTAMP_NTZ — which a table must have the
    timestampNtz feature to hold."""
    match data_type:
        case Primitive(name=name):
            return name == "timestamp_ntz"
        case Array(element=element):
            return contains_timestamp_ntz(element)
        case Map(key=key, value=value):
            return contains_timestamp_ntz(key) or contains_timestamp_ntz(value)
        case Struct(fields=fields):
            return any(contains_timestamp_ntz(f.type) for f in fields)
        case _:
            return False


#: What a seed may write into: types with a literal spelling everyone agrees on.
_QUOTED = ("string", "date", "timestamp", "timestamp_ntz", "interval")
_NUMERIC = (
    "tinyint",
    "smallint",
    "int",
    "bigint",
    "float",
    "double",
)


def seed_literal(value: str | None, data_type: DataType) -> str:
    """One seed value, written as a literal of its column's type.

    Raises `ValueError` when the text isn't one — `abc` where a `bigint` is
    declared — so a caller can say where in the file it was.

    Nothing here interpolates the value unescaped: strings and temporal values
    go through `quote_literal`, and a number has to parse as one before it is
    written bare.
    """
    if value is None:
        return "NULL"
    match data_type:
        case Primitive(name) if name in _NUMERIC:
            return _number(value, name)
        case Decimal():
            return _number(value, "decimal")
        case Primitive("boolean"):
            if value.strip().lower() in {"true", "1", "yes"}:
                return "TRUE"
            if value.strip().lower() in {"false", "0", "no"}:
                return "FALSE"
            raise ValueError(f"{value!r} is not a boolean")
        case Primitive("date"):
            return f"DATE {quote_literal(value)}"
        case Primitive("timestamp" | "timestamp_ntz"):
            return f"TIMESTAMP {quote_literal(value)}"
        case Primitive(name) if name in _QUOTED:
            return quote_literal(value)
        case Char() | Varchar():
            return quote_literal(value)
        case _:
            raise ValueError(f"a seed can't write a {render_type(data_type)} value")


def _number(value: str, kind: str) -> str:
    text = value.strip()
    try:
        float(text)
    except ValueError:
        raise ValueError(f"{value!r} is not a {kind}") from None
    return text
