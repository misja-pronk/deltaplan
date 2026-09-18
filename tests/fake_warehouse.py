"""An in-memory Unity Catalog that speaks deltaplan's own SQL.

This is how `apply` is tested without a workspace. The fake holds `Table` models,
answers the introspector's `information_schema` and `DESCRIBE` queries by
rendering them into the row shapes the real API returns, and *interprets* the
statements the planner generates by mutating those models.

What it proves: that the planner's SQL says what the planner's changes mean, that
applying a plan closes the diff, and that the executor's own machinery — skip,
resume, failure, gating — behaves. All offline, in milliseconds.

What it cannot prove: that Databricks accepts any of it. The fake implements
*our* reading of the manual. `tests/integration/` is the only thing that settles
what a warehouse really does, and anything the fake doesn't recognise raises
`FakeSqlError` rather than passing quietly — so a new statement shape can't slip
through untested.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from deltaplan.model.table import Check, Constraint, PrimaryKey, Table
from deltaplan.model.types import Array, DataType, Field, Map, Struct
from deltaplan.typeparser import parse_type

Row = dict[str, str | None]
Fields = tuple[Field, ...]


class FakeSqlError(Exception):
    """The fake doesn't know this statement — or the statement is wrong."""


@dataclass
class FakeWarehouse:
    """A `SqlRunner` backed by models rather than a database."""

    tables: dict[str, Table] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    #: Substring -> error message, so a test can make any statement fail.
    failures: dict[str, str] = field(default_factory=dict)
    #: What a `blocked` precheck should answer.
    blocked: bool = False
    statements: list[str] = field(default_factory=list)

    @classmethod
    def of(
        cls,
        *tables: Table,
        sizes: dict[str, int] | None = None,
    ) -> FakeWarehouse:
        fake = cls(sizes=sizes or {})
        for table in tables:
            fake.tables[table.name] = table
            fake.versions.setdefault(table.name, 1)
        return fake

    # -- the SqlRunner protocol -------------------------------------------
    def query(self, statement: str) -> tuple[Row, ...]:
        self.statements.append(statement)
        for fragment, message in self.failures.items():
            if fragment in statement:
                raise FakeSqlError(message)
        return self._dispatch(" ".join(statement.split()), statement)

    @property
    def ddl(self) -> list[str]:
        """Only the statements that changed something."""
        return [
            s for s in self.statements if not s.upper().startswith(("SELECT", "DESCRIBE"))
        ]

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, flat: str, original: str) -> tuple[Row, ...]:
        upper = flat.upper()
        if "AS BLOCKED" in upper:
            return ({"blocked": "true" if self.blocked else "false"},)
        if match := re.fullmatch(r"SELECT (true|false) AS (\w+)", flat, re.IGNORECASE):
            return ({match.group(2): match.group(1).lower()},)
        if upper.startswith("SELECT"):
            return self._information_schema(flat)
        if upper.startswith("DESCRIBE DETAIL"):
            return self._describe_detail(flat)
        if upper.startswith("DESCRIBE HISTORY"):
            return self._describe_history(flat)
        if upper.startswith("DESCRIBE TABLE"):
            return ()
        if upper.startswith("CREATE TABLE") or upper.startswith(
            "CREATE OR REPLACE TABLE"
        ):
            return self._create_table(original)
        if upper.startswith("DROP TABLE"):
            return self._drop_table(flat)
        if upper.startswith("INSERT OVERWRITE") or upper.startswith("INSERT INTO"):
            # The fake models schemas, not rows: moving data is a no-op here, and
            # whether it is *valid* is a question only a warehouse can answer.
            return ()
        if upper.startswith("CREATE SCHEMA") or upper.startswith("DROP SCHEMA"):
            return ()
        if upper.startswith("COMMENT ON TABLE"):
            return self._comment_on_table(flat)
        if upper.startswith("ALTER TABLE"):
            return self._alter_table(flat)
        raise FakeSqlError(f"the fake warehouse does not know this statement: {flat}")

    # -- reads -------------------------------------------------------------
    def _information_schema(self, flat: str) -> tuple[Row, ...]:
        schema = _literal_after(flat, "table_schema = ") or _literal_after(
            flat, "schema_name = "
        )
        if schema is None:
            raise FakeSqlError(f"no schema filter in: {flat}")
        catalog = _unquote(flat.split(".information_schema")[0].split("FROM ")[1])
        tables = [
            table
            for name, table in sorted(self.tables.items())
            if name.startswith(f"{catalog}.{schema}.")
        ]
        if "information_schema.tables" in flat:
            return tuple(
                {
                    "table_name": table.short_name,
                    "comment": table.comment,
                    "table_type": "MANAGED",
                    "data_source_format": "DELTA",
                }
                for table in tables
            )
        if "information_schema.columns" in flat:
            return tuple(
                {
                    "table_name": table.short_name,
                    "column_name": column.name,
                    "ordinal_position": str(position),
                    "full_data_type": _render(column.type),
                    "is_nullable": "YES" if column.nullable else "NO",
                    "comment": column.comment,
                }
                for table in tables
                for position, column in enumerate(table.columns, start=1)
            )
        if "information_schema.table_tags" in flat:
            return tuple(
                {"table_name": table.short_name, "tag_name": key, "tag_value": value}
                for table in tables
                for key, value in table.tags
            )
        if "information_schema.table_constraints" in flat:
            return tuple(
                _constraint_row(table, constraint)
                for table in tables
                for constraint in table.constraints
            )
        if "information_schema.key_column_usage" in flat:
            rows: list[Row] = []
            for table in tables:
                key = table.primary_key()
                if key is None:
                    continue
                for position, column in enumerate(key.columns, start=1):
                    rows.append(
                        {
                            "table_name": table.short_name,
                            "constraint_name": key.name or f"{table.short_name}_pk",
                            "column_name": column,
                            "ordinal_position": str(position),
                        }
                    )
            return tuple(rows)
        raise FakeSqlError(f"unknown information_schema query: {flat}")

    def _describe_detail(self, flat: str) -> tuple[Row, ...]:
        table = self._table(_unquote(flat[len("DESCRIBE DETAIL ") :]))
        return (
            {
                "format": "delta",
                "name": table.name,
                "clusteringColumns": json.dumps(list(table.cluster_by)),
                "sizeInBytes": str(self.sizes.get(table.name, 0)),
                "properties": json.dumps(dict(table.properties)),
            },
        )

    def _describe_history(self, flat: str) -> tuple[Row, ...]:
        name = _unquote(flat[len("DESCRIBE HISTORY ") :].removesuffix(" LIMIT 1"))
        return ({"version": str(self.versions.get(name, 0))},)

    # -- writes ------------------------------------------------------------
    def _create_table(self, statement: str) -> tuple[Row, ...]:
        stripped = statement.strip()
        replacing = stripped.upper().startswith("CREATE OR REPLACE TABLE")
        if re.search(r"\bAS\s+SELECT\b", stripped):
            table = self._parse_ctas(stripped)
        else:
            table = _parse_create_table(stripped)
        if table.name in self.tables and not replacing:
            return ()  # IF NOT EXISTS
        self.tables[table.name] = table
        self.versions[table.name] = self.versions.get(table.name, -1) + 1
        return ()

    def _parse_ctas(self, statement: str) -> Table:
        """`CREATE OR REPLACE TABLE x [clauses] AS SELECT … FROM y`.

        The columns are worked out from the SELECT list, which is the point: if a
        projection produces the wrong type, the table it builds has the wrong type
        and the convergence test says so.
        """
        match = _CTAS.fullmatch(statement)
        if match is None:
            raise FakeSqlError(f"cannot read CREATE … AS SELECT:\n{statement}")
        source = self._table(_unquote(match.group("source")))
        select = match.group("select").strip()
        columns = source.columns if select == "*" else _project(select, source)
        properties = (
            tuple(_pairs(match.group("properties")).items())
            if match.group("properties")
            else ()
        )
        return Table(
            name=_unquote(match.group("name")),
            columns=columns,
            comment=_unliteral(match.group("comment"))
            if match.group("comment")
            else None,
            cluster_by=tuple(_idents(match.group("cluster") or "")),
            properties=properties,
        )

    def _drop_table(self, flat: str) -> tuple[Row, ...]:
        name = _unquote(flat[len("DROP TABLE ") :].removeprefix("IF EXISTS ").strip())
        self.tables.pop(name, None)
        return ()

    def _comment_on_table(self, flat: str) -> tuple[Row, ...]:
        match = re.fullmatch(r"COMMENT ON TABLE (\S+) IS (.+)", flat)
        if match is None:
            raise FakeSqlError(f"cannot read: {flat}")
        name = _unquote(match.group(1))
        comment = None if match.group(2) == "NULL" else _unliteral(match.group(2))
        self._store(replace(self._table(name), comment=comment))
        return ()

    def _alter_table(self, flat: str) -> tuple[Row, ...]:
        match = re.fullmatch(r"ALTER TABLE (\S+) (.+)", flat)
        if match is None:
            raise FakeSqlError(f"cannot read: {flat}")
        table = self._table(_unquote(match.group(1)))
        self._store(_apply_alter(table, match.group(2)))
        return ()

    # -- plumbing ----------------------------------------------------------
    def _table(self, name: str) -> Table:
        if name not in self.tables:
            raise FakeSqlError(f"no such table: {name}")
        return self.tables[name]

    def _store(self, table: Table) -> None:
        self.tables[table.name] = table
        self.versions[table.name] = self.versions.get(table.name, 0) + 1


# ---------------------------------------------------------------------------
# statement interpretation
# ---------------------------------------------------------------------------


def _apply_alter(table: Table, clause: str) -> Table:
    if match := re.fullmatch(r"SET TBLPROPERTIES \((.*)\)", clause):
        properties = dict(table.properties) | _pairs(match.group(1))
        return replace(table, properties=tuple(properties.items()))
    if match := re.fullmatch(r"SET TAGS \((.*)\)", clause):
        return replace(
            table, tags=tuple((dict(table.tags) | _pairs(match.group(1))).items())
        )
    if match := re.fullmatch(r"CLUSTER BY \((.*)\)", clause):
        return replace(table, cluster_by=tuple(_idents(match.group(1))))
    if clause == "CLUSTER BY NONE":
        return replace(table, cluster_by=())
    if match := re.fullmatch(r"ADD COLUMNS \((.+)\)", clause):
        return _add_column(table, match.group(1))
    if match := re.fullmatch(r"DROP COLUMN (\S+)", clause):
        path = _unquote(match.group(1))
        return _edit_container(table, path, _drop(_leaf(path)))
    if match := re.fullmatch(r"RENAME COLUMN (\S+) TO (\S+)", clause):
        path, new_name = _unquote(match.group(1)), _unquote(match.group(2))
        return _edit_container(table, path, _rename(_leaf(path), new_name))
    if match := re.fullmatch(r"ALTER COLUMN (\S+) TYPE (.+)", clause):
        return _set_type(table, _unquote(match.group(1)), parse_type(match.group(2)))
    if match := re.fullmatch(r"ALTER COLUMN (\S+) (SET|DROP) NOT NULL", clause):
        path = _unquote(match.group(1))
        nullable = match.group(2) == "DROP"
        return _edit_container(
            table, path, _amend(_leaf(path), lambda f: replace(f, nullable=nullable))
        )
    if match := re.fullmatch(r"ALTER COLUMN (\S+) COMMENT (.+)", clause):
        path = _unquote(match.group(1))
        raw = match.group(2)
        comment = None if raw == "NULL" else _unliteral(raw)
        return _edit_container(
            table, path, _amend(_leaf(path), lambda f: replace(f, comment=comment))
        )
    if match := re.fullmatch(r"ALTER COLUMN (\S+) (FIRST|AFTER (\S+))", clause):
        return _move(table, _unquote(match.group(1)), match.group(3))
    if match := re.fullmatch(r"ADD CONSTRAINT (\S+) PRIMARY KEY \((.*)\)", clause):
        key = PrimaryKey(tuple(_idents(match.group(2))), _unquote(match.group(1)))
        return replace(table, constraints=(*table.constraints, key))
    if match := re.fullmatch(r"ADD CONSTRAINT (\S+) CHECK \((.+)\)", clause):
        check = Check(_unquote(match.group(1)), match.group(2))
        return replace(table, constraints=(*table.constraints, check))
    if match := re.fullmatch(r"DROP CONSTRAINT (\S+)", clause):
        name = _unquote(match.group(1))
        return replace(
            table,
            constraints=tuple(
                c for c in table.constraints if _constraint_name(c, table) != name
            ),
        )
    raise FakeSqlError(f"the fake warehouse does not know this clause: {clause}")


def _add_column(table: Table, definition: str) -> Table:
    match = re.fullmatch(r"(\S+) (.+?)(?: COMMENT (.+))?", definition)
    if match is None:
        raise FakeSqlError(f"cannot read column definition: {definition}")
    path = _unquote(match.group(1))
    added = Field(
        _leaf(path),
        parse_type(match.group(2)),
        comment=_unliteral(match.group(3)) if match.group(3) else None,
    )
    return _edit_container(table, path, lambda fields: (*fields, added))


def _move(table: Table, name: str, after: str | None) -> Table:
    column = table.column(name)
    if column is None:
        raise FakeSqlError(f"no such column: {name}")
    rest = [c for c in table.columns if c.name != name]
    if after is None:
        return replace(table, columns=(column, *rest))
    target = _unquote(after)
    index = next((i for i, c in enumerate(rest) if c.name == target), None)
    if index is None:
        raise FakeSqlError(f"no such column: {target}")
    return replace(table, columns=(*rest[: index + 1], column, *rest[index + 1 :]))


# -- field-tuple edits ------------------------------------------------------


def _drop(leaf: str) -> Callable[[Fields], Fields]:
    return lambda fields: tuple(f for f in fields if f.name != leaf)


def _rename(leaf: str, new_name: str) -> Callable[[Fields], Fields]:
    return lambda fields: tuple(
        replace(f, name=new_name) if f.name == leaf else f for f in fields
    )


def _amend(leaf: str, change: Callable[[Field], Field]) -> Callable[[Fields], Fields]:
    return lambda fields: tuple(change(f) if f.name == leaf else f for f in fields)


def _edit_container(table: Table, path: str, edit: Callable[[Fields], Fields]) -> Table:
    """Apply `edit` to the field tuple that directly holds `path`'s last segment."""
    parts = path.split(".")
    if len(parts) == 1:
        return replace(table, columns=edit(table.columns))
    column = table.column(parts[0])
    if column is None:
        raise FakeSqlError(f"no such column: {parts[0]}")
    return _replace_column(
        table, replace(column, type=_edit_type(column.type, parts[1:-1], edit))
    )


def _edit_type(
    data_type: DataType, parts: list[str], edit: Callable[[Fields], Fields]
) -> DataType:
    if not parts:
        if not isinstance(data_type, Struct):
            raise FakeSqlError(f"cannot add or drop fields in {_render(data_type)}")
        return Struct(edit(data_type.fields))
    head, rest = parts[0], parts[1:]
    match data_type:
        case Struct(fields):
            member = data_type.field(head)
            if member is None:
                raise FakeSqlError(f"no such field: {head}")
            return Struct(
                tuple(
                    replace(f, type=_edit_type(f.type, rest, edit))
                    if f.name == head
                    else f
                    for f in fields
                )
            )
        case Array(element, contains_null) if head == "element":
            return Array(_edit_type(element, rest, edit), contains_null)
        case Map(key, value) if head == "key":
            return Map(_edit_type(key, rest, edit), value)
        case Map(key, value) if head == "value":
            return Map(key, _edit_type(value, rest, edit))
        case _:
            raise FakeSqlError(f"cannot descend into {_render(data_type)} at {head}")


def _set_type(table: Table, path: str, new_type: DataType) -> Table:
    parts = path.split(".")
    column = table.column(parts[0])
    if column is None:
        raise FakeSqlError(f"no such column: {parts[0]}")
    return _replace_column(
        table, replace(column, type=_replace_type(column.type, parts[1:], new_type))
    )


def _replace_type(data_type: DataType, parts: list[str], new_type: DataType) -> DataType:
    if not parts:
        return new_type
    head, rest = parts[0], parts[1:]
    match data_type:
        case Struct(fields):
            if data_type.field(head) is None:
                raise FakeSqlError(f"no such field: {head}")
            return Struct(
                tuple(
                    replace(f, type=_replace_type(f.type, rest, new_type))
                    if f.name == head
                    else f
                    for f in fields
                )
            )
        case Array(element, contains_null) if head == "element":
            return Array(_replace_type(element, rest, new_type), contains_null)
        case Map(key, value) if head == "key":
            return Map(_replace_type(key, rest, new_type), value)
        case Map(key, value) if head == "value":
            return Map(key, _replace_type(value, rest, new_type))
        case _:
            raise FakeSqlError(f"cannot descend into {_render(data_type)} at {head}")


def _replace_column(table: Table, column: Field) -> Table:
    return replace(
        table,
        columns=tuple(column if c.name == column.name else c for c in table.columns),
    )


# -- little parsers ---------------------------------------------------------


def _render(data_type: DataType) -> str:
    from deltaplan.model.types import render_type

    return render_type(data_type)


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _unquote(text: str) -> str:
    """`` `a`.`b c` `` -> `a.b c`, undoing `quote_qualified`."""
    parts: list[str] = []
    for raw in re.findall(r"`(?:[^`]|``)*`|[^.`]+", text.strip()):
        if raw.startswith("`"):
            parts.append(raw[1:-1].replace("``", "`"))
        else:
            parts.append(raw)
    return ".".join(part for part in parts if part)


def _unliteral(text: str) -> str:
    stripped = text.strip()
    if not (stripped.startswith("'") and stripped.endswith("'")):
        raise FakeSqlError(f"not a string literal: {text}")
    return stripped[1:-1].replace("''", "'")


def _idents(text: str) -> Iterable[str]:
    return [_unquote(part) for part in text.split(",") if part.strip()]


def _pairs(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in re.findall(r"('(?:[^']|'')*')\s*=\s*('(?:[^']|'')*')", text):
        found[_unliteral(part[0])] = _unliteral(part[1])
    if not found:
        raise FakeSqlError(f"no key = value pairs in: {text}")
    return found


def _constraint_name(constraint: Constraint, table: Table) -> str:
    if isinstance(constraint, Check):
        return constraint.name
    return constraint.name or f"{table.short_name}_pk"


def _constraint_row(table: Table, constraint: Constraint) -> Row:
    if isinstance(constraint, Check):
        return {
            "table_name": table.short_name,
            "constraint_name": constraint.name,
            "constraint_type": "CHECK",
            "check_clause": f"({constraint.expression})",
        }
    return {
        "table_name": table.short_name,
        "constraint_name": _constraint_name(constraint, table),
        "constraint_type": "PRIMARY KEY",
        "check_clause": None,
    }


def _literal_after(text: str, marker: str) -> str | None:
    index = text.find(marker)
    if index < 0:
        return None
    rest = text[index + len(marker) :]
    match = re.match(r"'(?:[^']|'')*'", rest)
    return _unliteral(match.group(0)) if match else None


_CTAS = re.compile(
    r"CREATE OR REPLACE TABLE (?P<name>\S+)"
    r"(?:\nCLUSTER BY \((?P<cluster>[^)]*)\))?"
    r"(?:\nCOMMENT (?P<comment>'(?:[^']|'')*'))?"
    r"(?:\nTBLPROPERTIES \((?P<properties>.*?)\n\))?"
    r"\s+AS\s+SELECT\s+(?P<select>.*?)\s+FROM (?P<source>\S+)",
    re.DOTALL,
)

_CREATE = re.compile(
    r"CREATE (?:TABLE IF NOT EXISTS|OR REPLACE TABLE) (?P<name>\S+) "
    r"\((?P<body>.*?)\n\)\nUSING DELTA"
    r"(?:\nCLUSTER BY \((?P<cluster>[^)]*)\))?"
    r"(?:\nCOMMENT (?P<comment>'(?:[^']|'')*'))?"
    r"(?:\nTBLPROPERTIES \((?P<properties>.*?)\n\))?",
    re.DOTALL,
)


def _parse_create_table(statement: str) -> Table:
    match = _CREATE.fullmatch(statement.strip())
    if match is None:
        raise FakeSqlError(f"cannot read CREATE TABLE:\n{statement}")
    columns: list[Field] = []
    constraints: list[Constraint] = []
    for line in match.group("body").split("\n"):
        entry = line.strip().rstrip(",")
        if not entry:
            continue
        if entry.upper().startswith("CONSTRAINT "):
            constraints.append(_parse_inline_constraint(entry))
            continue
        columns.append(_parse_column_definition(entry))
    return Table(
        name=_unquote(match.group("name")),
        columns=tuple(columns),
        comment=_unliteral(match.group("comment")) if match.group("comment") else None,
        cluster_by=tuple(_idents(match.group("cluster") or "")),
        properties=tuple(_pairs(match.group("properties") or "''=''").items())
        if match.group("properties")
        else (),
        constraints=tuple(constraints),
    )


def _parse_column_definition(entry: str) -> Field:
    """Read `` `a` BIGINT NOT NULL `` with the real type parser.

    A column definition is a struct field with a space where the colon goes, so
    the parser that is already trusted elsewhere does the work.
    """
    name, _, rest = entry.partition(" ")
    parsed = parse_type(f"struct<{name}:{rest}>")
    if not isinstance(parsed, Struct) or len(parsed.fields) != 1:
        raise FakeSqlError(f"cannot read column definition: {entry}")
    return parsed.fields[0]


def _parse_inline_constraint(entry: str) -> Constraint:
    if match := re.fullmatch(r"CONSTRAINT (\S+) PRIMARY KEY \((.*)\)", entry):
        return PrimaryKey(tuple(_idents(match.group(2))), _unquote(match.group(1)))
    if match := re.fullmatch(r"CONSTRAINT (\S+) CHECK \((.+)\)", entry):
        return Check(_unquote(match.group(1)), match.group(2))
    raise FakeSqlError(f"cannot read constraint: {entry}")


# ---------------------------------------------------------------------------
# typing a projection
# ---------------------------------------------------------------------------
#
# Enough of an expression typer for the shapes the planner generates. Anything
# else — a hand-written `using:` expression, say — raises, because guessing its
# type would make the convergence test lie.


def _project(select: str, source: Table) -> Fields:
    fields: list[Field] = []
    for item in _split_args(select):
        expression, _, alias = item.strip().rpartition(" AS ")
        if not expression:
            raise FakeSqlError(f"projection item has no alias: {item}")
        fields.append(Field(_unquote(alias), _infer(expression.strip(), source, {})))
    return tuple(fields)


def _infer(expression: str, source: Table, env: dict[str, DataType]) -> DataType:
    text = expression.strip()
    if match := re.fullmatch(r"CAST\((.*) AS ([A-Za-z0-9_<>(), ]+)\)", text, re.DOTALL):
        return parse_type(match.group(2))
    if text.startswith("named_struct(") and text.endswith(")"):
        arguments = _split_args(text[len("named_struct(") : -1])
        if len(arguments) % 2:
            raise FakeSqlError(f"named_struct takes pairs: {text}")
        pairs = zip(arguments[::2], arguments[1::2], strict=True)
        return Struct(
            tuple(
                Field(_unliteral(name), _infer(value, source, env))
                for name, value in pairs
            )
        )
    if text.startswith("transform(") and text.endswith(")"):
        collection, body = _split_args(text[len("transform(") : -1])
        element = _infer(collection, source, env)
        if not isinstance(element, Array):
            raise FakeSqlError(f"transform over something that isn't an array: {text}")
        variable, _, inner = body.partition(" -> ")
        bound = {**env, variable.strip(): element.element}
        return Array(_infer(inner, source, bound))
    return _reference_type(text, source, env)


def _reference_type(text: str, source: Table, env: dict[str, DataType]) -> DataType:
    parts = _unquote(text).split(".")
    head, rest = parts[0], parts[1:]
    if head in env:
        current: DataType = env[head]
    else:
        column = source.column(head)
        if column is None:
            raise FakeSqlError(f"no such column: {head} (in {text!r})")
        current = column.type
    for part in rest:
        match current:
            case Struct():
                member = current.field(part)
                if member is None:
                    raise FakeSqlError(f"no such field: {part} (in {text!r})")
                current = member.type
            case _:
                raise FakeSqlError(f"cannot read {part} out of {_render(current)}")
    return current


def _split_args(text: str) -> list[str]:
    """Split on commas that aren't inside brackets, quotes or backticks."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    previous = ""
    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            previous = char
            continue
        if char in "'`":
            quote = char
            current.append(char)
        elif char in "(<":
            depth += 1
            current.append(char)
        elif char == ">" and previous == "-":
            # The arrow of a lambda, not the end of a type.
            current.append(char)
        elif char in ")>":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        previous = char
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]
