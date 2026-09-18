"""YAML in, model out.

This is one of the three modules that touch the outside world (with `introspect`
and `executor`), and the only place that validates. Everything downstream can
assume a well-formed model.

Specs are read through the YAML *node* tree rather than plain `safe_load`, so
every error can point at the file, line and column it came from, and unknown keys
are caught where they were written.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from deltaplan.model.table import (
    MANAGED_PROPERTY,
    Check,
    Constraint,
    PrimaryKey,
    Table,
)
from deltaplan.model.types import (
    Array,
    Column,
    DataType,
    Field,
    Map,
    Primitive,
    Struct,
    render_type,
)
from deltaplan.typeparser import TypeParseError, parse_type

SPEC_SUFFIXES = (".yml", ".yaml")
CONFIG_NAMES = ("deltaplan.yml", "deltaplan.yaml")
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

Mode: TypeAlias = Literal["additive", "strict"]
Severity: TypeAlias = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class Loc:
    """Where in a file something was written."""

    file: Path
    line: int  # 1-based
    column: int  # 1-based

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.column}"


class SpecError(Exception):
    """A spec that doesn't hold up. Always carries a location."""

    def __init__(self, message: str, loc: Loc) -> None:
        self.message = message
        self.loc = loc
        super().__init__(f"{loc}: {message}")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Something `validate` wants to say about a spec that parsed."""

    severity: Severity
    message: str
    where: str

    def __str__(self) -> str:
        return f"{self.where}: {self.severity}: {self.message}"


@dataclass(frozen=True, slots=True)
class Target:
    """A named deployment: the variables a spec is rendered with."""

    name: str
    variables: tuple[tuple[str, str], ...] = ()
    warehouse_id: str | None = None
    mode: Mode = "additive"

    def variables_map(self) -> dict[str, str]:
        return dict(self.variables)


@dataclass(frozen=True, slots=True)
class Project:
    """A `deltaplan.yml` and the specs it points at.

    `history_schema` and the keys of `schema_modes` are kept as written, `${var}`
    and all, and resolved per target — the same project serves every catalog.
    """

    root: Path
    spec_paths: tuple[Path, ...]
    targets: tuple[Target, ...]
    history_schema: str | None = None
    schema_modes: tuple[tuple[str, Mode], ...] = ()

    def target(self, name: str) -> Target:
        for candidate in self.targets:
            if candidate.name == name:
                return candidate
        known = ", ".join(t.name for t in self.targets) or "none defined"
        raise KeyError(f"unknown target {name!r} (known targets: {known})")

    def history_schema_for(self, target: Target) -> str | None:
        if self.history_schema is None:
            return None
        return substitute(self.history_schema, target.variables_map())

    def mode_for(self, target: Target, schema: str) -> Mode:
        """`strict` or `additive` for one `catalog.schema`, under one target.

        A schema listed under `schemas:` gets its own mode; any other gets the
        target's.
        """
        variables = target.variables_map()
        for pattern, mode in self.schema_modes:
            try:
                if substitute(pattern, variables) == schema:
                    return mode
            except KeyError:
                continue  # a pattern using a variable this target doesn't define
        return target.mode


@dataclass(frozen=True, slots=True)
class LoadedSpec:
    """A table, and the file it came from."""

    path: Path
    table: Table


# ---------------------------------------------------------------------------
# node plumbing
# ---------------------------------------------------------------------------


def substitute(text: str, variables: dict[str, str]) -> str:
    """Replace `${name}` from `variables`. An undefined name is a KeyError."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            known = ", ".join(sorted(variables)) or "none defined"
            raise KeyError(f"undefined variable ${{{name}}} (known: {known})")
        return variables[name]

    return VARIABLE.sub(replace, text)


@dataclass(frozen=True, slots=True)
class _Ctx:
    file: Path
    variables: tuple[tuple[str, str], ...] = ()
    #: Leave `${var}` alone. The project file is read before any target is
    #: chosen, so its variables can only be resolved later.
    raw: bool = False

    def loc(self, node: Node) -> Loc:
        mark = node.start_mark
        return Loc(self.file, mark.line + 1, mark.column + 1)

    def variables_map(self) -> dict[str, str]:
        return dict(self.variables)


def _compose(path: Path) -> Node | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SpecError(f"cannot read spec: {error}", Loc(path, 1, 1)) from error
    try:
        return yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark
        loc = Loc(path, mark.line + 1 if mark else 1, mark.column + 1 if mark else 1)
        raise SpecError(error.problem or "invalid YAML", loc) from error
    except yaml.YAMLError as error:
        raise SpecError(f"invalid YAML: {error}", Loc(path, 1, 1)) from error


def _mapping(ctx: _Ctx, node: Node, what: str) -> dict[str, tuple[Node, Loc]]:
    if not isinstance(node, MappingNode):
        raise SpecError(f"{what} must be a mapping", ctx.loc(node))
    items: dict[str, tuple[Node, Loc]] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, ScalarNode):
            raise SpecError("keys must be plain strings", ctx.loc(key_node))
        key = str(key_node.value)
        if key in items:
            raise SpecError(f"duplicate key {key!r}", ctx.loc(key_node))
        items[key] = (value_node, ctx.loc(key_node))
    return items


def _known_keys(
    items: dict[str, tuple[Node, Loc]],
    *,
    allowed: set[str],
    what: str,
) -> None:
    for key, (_, loc) in items.items():
        if key not in allowed:
            options = ", ".join(sorted(allowed))
            raise SpecError(
                f"unknown key {key!r} in {what} (expected one of: {options})", loc
            )


def _require(
    ctx: _Ctx,
    items: dict[str, tuple[Node, Loc]],
    node: Node,
    key: str,
    what: str,
) -> Node:
    if key not in items:
        raise SpecError(f"{what} needs a {key!r} key", ctx.loc(node))
    return items[key][0]


def _sequence(ctx: _Ctx, node: Node, what: str) -> list[Node]:
    if not isinstance(node, SequenceNode):
        raise SpecError(f"{what} must be a list", ctx.loc(node))
    return list(node.value)


def _scalar(ctx: _Ctx, node: Node, what: str) -> ScalarNode:
    if not isinstance(node, ScalarNode):
        raise SpecError(f"{what} must be a single value", ctx.loc(node))
    return node


def _substitute(ctx: _Ctx, text: str, loc: Loc) -> str:
    if ctx.raw:
        return text
    try:
        return substitute(text, ctx.variables_map())
    except KeyError as error:
        message = str(error.args[0])
        if not ctx.variables:
            message = message.replace("none defined", "none defined for this target")
        raise SpecError(message, loc) from error


def _string(ctx: _Ctx, node: Node, what: str) -> str:
    scalar = _scalar(ctx, node, what)
    if scalar.tag != "tag:yaml.org,2002:str":
        raise SpecError(
            f"{what} must be a string — quote it if you mean {scalar.value!r}",
            ctx.loc(node),
        )
    return _substitute(ctx, str(scalar.value), ctx.loc(node))


def _bool(ctx: _Ctx, node: Node, what: str) -> bool:
    scalar = _scalar(ctx, node, what)
    if scalar.tag != "tag:yaml.org,2002:bool":
        raise SpecError(f"{what} must be true or false", ctx.loc(node))
    return str(scalar.value).lower() in {"true", "yes", "on"}


def _string_map(ctx: _Ctx, node: Node, what: str) -> tuple[tuple[str, str], ...]:
    items = _mapping(ctx, node, what)
    return tuple(
        (key, _string(ctx, value_node, f"{what} value for {key!r}"))
        for key, (value_node, _) in items.items()
    )


def _string_list(ctx: _Ctx, node: Node, what: str) -> tuple[str, ...]:
    return tuple(
        _string(ctx, item, f"{what} entry") for item in _sequence(ctx, node, what)
    )


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------

FIELD_KEYS = {"name", "type", "nullable", "comment", "renamed_from", "using"}


def _read_type(ctx: _Ctx, node: Node, what: str) -> DataType:
    """A type is either a Databricks type string or the nested YAML form."""
    if isinstance(node, ScalarNode):
        text = _string(ctx, node, what)
        try:
            return parse_type(text)
        except TypeParseError as error:
            raise SpecError(str(error), ctx.loc(node)) from error

    items = _mapping(ctx, node, what)
    _known_keys(items, allowed={"struct", "array", "map"}, what=what)
    if len(items) != 1:
        raise SpecError(
            f"{what} must name exactly one of struct, array or map", ctx.loc(node)
        )
    kind, (value_node, _) = next(iter(items.items()))
    match kind:
        case "struct":
            fields = tuple(
                _read_field(ctx, item)
                for item in _sequence(ctx, value_node, "struct fields")
            )
            return Struct(fields)
        case "array":
            return _read_array(ctx, value_node)
        case _:
            return _read_map(ctx, value_node)


def _read_array(ctx: _Ctx, node: Node) -> Array:
    if isinstance(node, ScalarNode):
        return Array(_read_type(ctx, node, "array element"))
    items = _mapping(ctx, node, "array")
    _known_keys(items, allowed={"element", "contains_null"}, what="array")
    element_node = _require(ctx, items, node, "element", "an array")
    contains_null = True
    if "contains_null" in items:
        contains_null = _bool(ctx, items["contains_null"][0], "contains_null")
    return Array(_read_type(ctx, element_node, "array element"), contains_null)


def _read_map(ctx: _Ctx, node: Node) -> Map:
    items = _mapping(ctx, node, "map")
    _known_keys(items, allowed={"key", "value"}, what="map")
    key_node = _require(ctx, items, node, "key", "a map")
    value_node = _require(ctx, items, node, "value", "a map")
    return Map(
        _read_type(ctx, key_node, "map key"),
        _read_type(ctx, value_node, "map value"),
    )


def _read_field(ctx: _Ctx, node: Node) -> Field:
    items = _mapping(ctx, node, "a field")
    _known_keys(items, allowed=FIELD_KEYS, what="a field")
    name = _string(ctx, _require(ctx, items, node, "name", "a field"), "field name")
    field_type = _read_type(
        ctx, _require(ctx, items, node, "type", f"field {name!r}"), f"type of {name!r}"
    )
    nullable = True
    if "nullable" in items:
        nullable = _bool(ctx, items["nullable"][0], f"nullable of {name!r}")
    comment = None
    if "comment" in items:
        comment = _string(ctx, items["comment"][0], f"comment of {name!r}")
    renamed_from = None
    if "renamed_from" in items:
        renamed_from = _string(ctx, items["renamed_from"][0], f"renamed_from of {name!r}")
    using = None
    if "using" in items:
        using = _string(ctx, items["using"][0], f"using of {name!r}")
    return Field(
        name,
        field_type,
        nullable=nullable,
        comment=comment,
        renamed_from=renamed_from,
        using=using,
    )


# ---------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------

TABLE_KEYS = {
    "table",
    "comment",
    "cluster_by",
    "tags",
    "properties",
    "columns",
    "constraints",
}


def _read_constraint(ctx: _Ctx, node: Node) -> Constraint:
    items = _mapping(ctx, node, "a constraint")
    if len(items) != 1:
        raise SpecError("a constraint names exactly one kind", ctx.loc(node))
    kind, (value_node, key_loc) = next(iter(items.items()))
    match kind:
        case "primary_key":
            return _read_primary_key(ctx, value_node)
        case "check":
            check_items = _mapping(ctx, value_node, "a check constraint")
            _known_keys(check_items, allowed={"name", "expression"}, what="a check")
            name_node = _require(ctx, check_items, value_node, "name", "a check")
            expr_node = _require(ctx, check_items, value_node, "expression", "a check")
            return Check(
                _string(ctx, name_node, "check name"),
                _string(ctx, expr_node, "check expression"),
            )
        case "foreign_key":
            raise SpecError(
                "foreign keys are not modelled yet — deltaplan v1 handles "
                "primary_key and check constraints",
                key_loc,
            )
        case _:
            raise SpecError(
                f"unknown constraint {kind!r} (expected primary_key or check)", key_loc
            )


def _read_primary_key(ctx: _Ctx, node: Node) -> PrimaryKey:
    if isinstance(node, SequenceNode):
        return PrimaryKey(_string_list(ctx, node, "primary_key"))
    items = _mapping(ctx, node, "a primary key")
    _known_keys(items, allowed={"columns", "name"}, what="a primary key")
    columns_node = _require(ctx, items, node, "columns", "a primary key")
    name = None
    if "name" in items:
        name = _string(ctx, items["name"][0], "primary key name")
    return PrimaryKey(_string_list(ctx, columns_node, "primary_key columns"), name)


def load_table(path: Path, variables: dict[str, str] | None = None) -> Table:
    """Read one spec file into a `Table`."""
    ctx = _Ctx(path, tuple(sorted((variables or {}).items())))
    node = _compose(path)
    if node is None:
        raise SpecError("spec file is empty", Loc(path, 1, 1))
    items = _mapping(ctx, node, "a spec")
    _known_keys(items, allowed=TABLE_KEYS, what="a spec")

    name = _string(ctx, _require(ctx, items, node, "table", "a spec"), "table name")
    columns_node = _require(ctx, items, node, "columns", "a spec")
    columns: tuple[Column, ...] = tuple(
        _read_field(ctx, item) for item in _sequence(ctx, columns_node, "columns")
    )
    if not columns:
        raise SpecError("a spec needs at least one column", ctx.loc(columns_node))

    comment = None
    if "comment" in items:
        comment = _string(ctx, items["comment"][0], "table comment")
    cluster_by: tuple[str, ...] = ()
    if "cluster_by" in items:
        cluster_by = _string_list(ctx, items["cluster_by"][0], "cluster_by")
    properties: tuple[tuple[str, str], ...] = ()
    if "properties" in items:
        properties = _string_map(ctx, items["properties"][0], "properties")
    tags: tuple[tuple[str, str], ...] = ()
    if "tags" in items:
        tags = _string_map(ctx, items["tags"][0], "tags")
    constraints: tuple[Constraint, ...] = ()
    if "constraints" in items:
        constraints = tuple(
            _read_constraint(ctx, item)
            for item in _sequence(ctx, items["constraints"][0], "constraints")
        )

    return Table(
        name=name,
        columns=columns,
        comment=comment,
        cluster_by=cluster_by,
        properties=properties,
        tags=tags,
        constraints=constraints,
    )


# ---------------------------------------------------------------------------
# project config
# ---------------------------------------------------------------------------

CONFIG_KEYS = {"version", "specs", "targets", "history_schema", "schemas"}
TARGET_KEYS = {"vars", "warehouse_id", "mode"}


def find_project_file(start: Path) -> Path:
    """Walk up from `start` looking for a `deltaplan.yml`."""
    for directory in [start, *start.parents]:
        for name in CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(
        f"no {CONFIG_NAMES[0]} found in {start} or any parent directory"
    )


def load_project(path: Path) -> Project:
    """Read a `deltaplan.yml`."""
    ctx = _Ctx(path, raw=True)
    node = _compose(path)
    if node is None:
        raise SpecError("config file is empty", Loc(path, 1, 1))
    items = _mapping(ctx, node, "the config")
    _known_keys(items, allowed=CONFIG_KEYS, what="the config")

    root = path.parent
    spec_paths: tuple[Path, ...] = (root / "tables",)
    if "specs" in items:
        spec_paths = tuple(
            root / entry for entry in _string_list(ctx, items["specs"][0], "specs")
        )
    history_schema = None
    if "history_schema" in items:
        history_schema = _string(ctx, items["history_schema"][0], "history_schema")

    targets: list[Target] = []
    if "targets" in items:
        for name, (target_node, _) in _mapping(
            ctx, items["targets"][0], "targets"
        ).items():
            targets.append(_read_target(ctx, name, target_node))

    schema_modes: list[tuple[str, Mode]] = []
    if "schemas" in items:
        for schema, (mode_node, key_loc) in _mapping(
            ctx, items["schemas"][0], "schemas"
        ).items():
            if len(schema.split(".")) != 2:
                raise SpecError(
                    f"schemas are keyed catalog.schema, e.g. ${{catalog}}.sales — "
                    f"not {schema!r}",
                    key_loc,
                )
            schema_modes.append((schema, _read_mode(ctx, mode_node)))

    return Project(
        root=root,
        spec_paths=spec_paths,
        targets=tuple(targets),
        history_schema=history_schema,
        schema_modes=tuple(schema_modes),
    )


def _read_mode(ctx: _Ctx, node: Node) -> Mode:
    raw = _string(ctx, node, "mode")
    if raw not in {"additive", "strict"}:
        raise SpecError(
            f"mode must be 'additive' or 'strict', not {raw!r}", ctx.loc(node)
        )
    return "additive" if raw == "additive" else "strict"


def _read_target(ctx: _Ctx, name: str, node: Node) -> Target:
    items = _mapping(ctx, node, f"target {name!r}")
    _known_keys(items, allowed=TARGET_KEYS, what=f"target {name!r}")
    variables: tuple[tuple[str, str], ...] = ()
    if "vars" in items:
        variables = _string_map(ctx, items["vars"][0], f"vars of target {name!r}")
    warehouse_id = None
    if "warehouse_id" in items:
        warehouse_id = _string(ctx, items["warehouse_id"][0], "warehouse_id")
    mode: Mode = "additive"
    if "mode" in items:
        mode = _read_mode(ctx, items["mode"][0])
    return Target(name, variables, warehouse_id, mode)


def spec_files(project: Project) -> tuple[Path, ...]:
    """Every spec file a project points at, in a stable order."""
    found: list[Path] = []
    for entry in project.spec_paths:
        if entry.is_dir():
            for suffix in SPEC_SUFFIXES:
                found.extend(sorted(entry.rglob(f"*{suffix}")))
        elif entry.is_file():
            found.append(entry)
        else:
            raise FileNotFoundError(f"spec path does not exist: {entry}")
    return tuple(sorted(set(found)))


def load_specs(project: Project, target: Target) -> tuple[LoadedSpec, ...]:
    """Load every spec in a project, rendered for one target."""
    variables = target.variables_map()
    return tuple(
        LoadedSpec(path, load_table(path, variables)) for path in spec_files(project)
    )


# ---------------------------------------------------------------------------
# linting
# ---------------------------------------------------------------------------

# Liquid clustering takes at most four columns.
# TODO(verify): confirm against a live workspace; the limit has moved before.
# https://docs.databricks.com/aws/en/delta/clustering
MAX_CLUSTER_COLUMNS = 4


def validate_table(table: Table, where: str) -> tuple[Diagnostic, ...]:
    """Lint a spec that already parsed. Offline: no workspace, no network."""
    found: list[Diagnostic] = []

    def error(message: str) -> None:
        found.append(Diagnostic("error", message, where))

    def warn(message: str) -> None:
        found.append(Diagnostic("warning", message, where))

    if len(table.parts) != 3:
        error(
            f"table name {table.name!r} must be catalog.schema.table "
            "(three parts, after variable substitution)"
        )

    seen: set[str] = set()
    for column in table.columns:
        if column.name in seen:
            error(f"duplicate column {column.name!r}")
        seen.add(column.name)

    for column in table.columns:
        _lint_field(column, column.name, table, error, warn)

    for name in table.cluster_by:
        if name not in seen:
            error(f"cluster_by column {name!r} is not in the spec")
    if len(table.cluster_by) > MAX_CLUSTER_COLUMNS:
        warn(
            f"{len(table.cluster_by)} clustering columns; Databricks takes at most "
            f"{MAX_CLUSTER_COLUMNS}"
        )

    primary_key = table.primary_key()
    if primary_key is not None:
        for name in primary_key.columns:
            column = table.column(name)
            if column is None:
                error(f"primary key column {name!r} is not in the spec")
            elif column.nullable:
                # https://docs.databricks.com/aws/en/tables/constraints
                error(f"primary key column {name!r} must be declared nullable: false")
    if len([c for c in table.constraints if isinstance(c, PrimaryKey)]) > 1:
        error("a table can have at most one primary key")

    check_names = [check.name for check in table.checks()]
    for name in check_names:
        if check_names.count(name) > 1:
            error(f"duplicate check constraint {name!r}")

    return tuple(found)


def _lint_field(
    field: Field,
    path: str,
    table: Table,
    error: Callable[[str], None],
    warn: Callable[[str], None],
) -> None:
    if field.using is not None and "." in path:
        error(
            f"{path}: `using` applies to whole columns only — put the expression on "
            f"{path.split('.')[0]!r} and build the value there"
        )

    if field.renamed_from is not None:
        if field.renamed_from == field.name:
            error(f"{path}: renamed_from is the same as the column name")
        elif "." not in path and table.column(field.renamed_from) is not None:
            error(
                f"{path}: renamed_from {field.renamed_from!r} is itself a column in "
                "this spec — one of the two is wrong"
            )

    match field.type:
        case Primitive() as primitive if not primitive.known:
            warn(f"{path}: unknown type {primitive.name!r} — is it spelled right?")
        case Struct(fields):
            names: set[str] = set()
            for member in fields:
                if member.name in names:
                    error(f"{path}: duplicate field {member.name!r}")
                names.add(member.name)
                _lint_field(member, f"{path}.{member.name}", table, error, warn)
        case Array(element, _):
            _lint_field(Field("element", element), f"{path}.element", table, error, warn)
        case Map(key, value):
            _lint_field(Field("key", key), f"{path}.key", table, error, warn)
            _lint_field(Field("value", value), f"{path}.value", table, error, warn)
        case _:
            pass


# ---------------------------------------------------------------------------
# writing specs back out (the inverse of `load_table`, kept next to it)
# ---------------------------------------------------------------------------


def dump_spec(table: Table, *, catalog_variable: str | None = None) -> str:
    """Render a table as a spec file, the way `import` writes it.

    Types are written in the string notation, which carries nested comments and
    nullability, so the result round-trips through `load_table` unchanged.
    `catalog_variable` puts the catalog back behind a `${var}`, so one imported
    spec serves every target.
    """
    name = table.name
    if catalog_variable:
        _, _, rest = name.partition(".")
        name = f"${{{catalog_variable}}}.{rest}"

    document: dict[str, object] = {"table": name}
    if table.comment is not None:
        document["comment"] = table.comment
    if table.cluster_by:
        document["cluster_by"] = list(table.cluster_by)
    if table.tags:
        document["tags"] = dict(table.tags)
    properties = {
        key: value for key, value in table.properties if key != MANAGED_PROPERTY
    }
    if properties:
        document["properties"] = properties
    document["columns"] = [_column_document(column) for column in table.columns]
    constraints = [
        constraint
        for constraint in (_constraint_document(c) for c in table.constraints)
        if constraint is not None
    ]
    if constraints:
        document["constraints"] = constraints

    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100)


def _column_document(column: Field) -> dict[str, object]:
    rendered: dict[str, object] = {"name": column.name, "type": render_type(column.type)}
    if not column.nullable:
        rendered["nullable"] = False
    if column.comment is not None:
        rendered["comment"] = column.comment
    return rendered


def _constraint_document(constraint: Constraint) -> dict[str, object] | None:
    if isinstance(constraint, PrimaryKey):
        body: dict[str, object] = {"columns": list(constraint.columns)}
        if constraint.name:
            body["name"] = constraint.name
        return {"primary_key": body}
    if isinstance(constraint, Check):
        return {"check": {"name": constraint.name, "expression": constraint.expression}}
    return None  # pragma: no cover - the union has no third member
