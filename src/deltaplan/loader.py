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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from deltaplan.bundle import WAREHOUSE_VARIABLE, BundleError, BundleTarget, read_bundle
from deltaplan.model.function import Function, Parameter
from deltaplan.model.table import (
    MAINTAINED_PROPERTIES,
    PREREQUISITE_PROPERTIES,
    Check,
    Constraint,
    ForeignKey,
    Grant,
    Hooks,
    PrimaryKey,
    RowFilter,
    Table,
    is_bookkeeping,
    is_platform_default,
)
from deltaplan.model.types import (
    Array,
    Column,
    DataType,
    Field,
    Identity,
    Map,
    Mask,
    Primitive,
    Struct,
    render_type,
)
from deltaplan.model.view import Relation, View
from deltaplan.sql import FUNCTION_PRIVILEGES, TABLE_PRIVILEGES, privilege_sql
from deltaplan.typeparser import TypeParseError, parse_type

SPEC_SUFFIXES = (".yml", ".yaml", ".sql")
CONFIG_NAMES = ("deltaplan.yml", "deltaplan.yaml")
#: `${name}`, or `${var.name}` — the spelling a bundle uses for the same thing.
VARIABLE = re.compile(r"\$\{(?:var\.)?([A-Za-z_][A-Za-z0-9_]*)\}")

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
    """A named deployment: which workspace, and what the specs are rendered with.

    `profile` names a `~/.databrickscfg` profile, because dev and prod are
    usually different workspaces; without one, `host` (from a bundle) or the
    Databricks SDK's own defaults apply (environment variables, then the DEFAULT
    profile).

    From a bundle, `unresolved` holds the variables that have no value without
    a workspace, with the reason, and `warehouse_lookup` the name of a warehouse
    to find once connected.
    """

    name: str
    variables: tuple[tuple[str, str], ...] = ()
    warehouse_id: str | None = None
    mode: Mode = "additive"
    profile: str | None = None
    host: str | None = None
    unresolved: tuple[tuple[str, str], ...] = ()
    warehouse_lookup: str | None = None

    def variables_map(self) -> dict[str, str]:
        return dict(self.variables)

    def unresolved_map(self) -> dict[str, str]:
        return dict(self.unresolved)


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
    #: The target to use when none is named: the only one, or the one marked
    #: `default: true` here or in the bundle.
    default_target: str | None = None
    bundle: Path | None = None

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
                if substitute(pattern, variables).lower() == schema.lower():
                    return mode
            except KeyError:
                continue  # a pattern using a variable this target doesn't define
        return target.mode


@dataclass(frozen=True, slots=True)
class LoadedSpec:
    """A table or view, and the file it came from.

    `table` because Unity Catalog calls a view a kind of table, and so does the
    rest of deltaplan's vocabulary.
    """

    path: Path
    table: Relation


# ---------------------------------------------------------------------------
# node plumbing
# ---------------------------------------------------------------------------


def substitute(
    text: str,
    variables: Mapping[str, str],
    unresolved: Mapping[str, str] | None = None,
) -> str:
    """Replace `${name}` from `variables`. An undefined name is a KeyError —
    saying why, when `unresolved` knows."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return variables[name]
        if unresolved and name in unresolved:
            raise KeyError(
                f"variable ${{{name}}} comes from the bundle but {unresolved[name]}; "
                "set it under this target's vars in deltaplan.yml"
            )
        known = ", ".join(sorted(variables)) or "none defined"
        raise KeyError(f"undefined variable ${{{name}}} (known: {known})")

    return VARIABLE.sub(replace, text)


@dataclass(frozen=True, slots=True)
class _Ctx:
    file: Path
    variables: tuple[tuple[str, str], ...] = ()
    unresolved: tuple[tuple[str, str], ...] = ()
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
        return substitute(text, ctx.variables_map(), dict(ctx.unresolved))
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

FIELD_KEYS = {
    "name",
    "type",
    "nullable",
    "comment",
    "renamed_from",
    "using",
    "tags",
    "mask",
    "identity",
    "generated",
    "default",
}


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
    tags: tuple[tuple[str, str], ...] = ()
    if "tags" in items:
        tags = _string_map(ctx, items["tags"][0], f"tags of {name!r}")
    mask = None
    if "mask" in items:
        mask = _read_mask(ctx, items["mask"][0])
    identity = None
    if "identity" in items:
        identity = _read_identity(ctx, items["identity"][0])
    generated = None
    if "generated" in items:
        generated = _string(ctx, items["generated"][0], f"generated of {name!r}")
    default = None
    if "default" in items:
        default = _string(ctx, items["default"][0], f"default of {name!r}")
    return Field(
        name,
        field_type,
        nullable=nullable,
        comment=comment,
        renamed_from=renamed_from,
        using=using,
        tags=tags,
        mask=mask,
        identity=identity,
        generated=generated,
        default=default,
    )


def _read_identity(ctx: _Ctx, node: Node) -> Identity:
    """`identity: always`, `by_default`, or `{generated: …, start: …, increment: …}`."""

    def kind(value_node: Node) -> bool:
        value = _string(ctx, value_node, "identity").lower().replace(" ", "_")
        if value not in {"always", "by_default"}:
            raise SpecError(
                f"identity is 'always' or 'by_default', not {value!r}",
                ctx.loc(value_node),
            )
        return value == "always"

    if isinstance(node, ScalarNode):
        return Identity(always=kind(node))
    items = _mapping(ctx, node, "an identity")
    _known_keys(items, allowed={"generated", "start", "increment"}, what="an identity")
    always = kind(items["generated"][0]) if "generated" in items else True
    start = _integer(ctx, items["start"][0], "start") if "start" in items else 1
    increment = (
        _integer(ctx, items["increment"][0], "increment") if "increment" in items else 1
    )
    if increment == 0:
        raise SpecError(
            "an identity can't increment by 0", ctx.loc(items["increment"][0])
        )
    return Identity(always, start, increment)


def _integer(ctx: _Ctx, node: Node, what: str) -> int:
    scalar = _scalar(ctx, node, what)
    if scalar.tag != "tag:yaml.org,2002:int":
        raise SpecError(f"{what} must be a whole number", ctx.loc(node))
    return int(str(scalar.value))


def _read_function(ctx: _Ctx, node: Node, what: str) -> str:
    function = _string(ctx, node, what)
    if len(function.split(".")) != 3:
        raise SpecError(
            f"{what} must be a catalog.schema.function name, not {function!r}",
            ctx.loc(node),
        )
    return function


def _read_mask(ctx: _Ctx, node: Node) -> Mask:
    """`mask: cat.sch.fn`, or `{function: …, using_columns: […]}`."""
    if isinstance(node, ScalarNode):
        return Mask(_read_function(ctx, node, "mask"))
    items = _mapping(ctx, node, "a mask")
    _known_keys(items, allowed={"function", "using_columns"}, what="a mask")
    function = _read_function(
        ctx, _require(ctx, items, node, "function", "a mask"), "mask function"
    )
    using: tuple[str, ...] = ()
    if "using_columns" in items:
        using = _string_list(ctx, items["using_columns"][0], "using_columns")
    return Mask(function, using)


def _read_row_filter(ctx: _Ctx, node: Node) -> RowFilter:
    items = _mapping(ctx, node, "a row filter")
    _known_keys(items, allowed={"function", "columns"}, what="a row filter")
    function = _read_function(
        ctx, _require(ctx, items, node, "function", "a row filter"), "row filter function"
    )
    columns = _string_list(
        ctx, _require(ctx, items, node, "columns", "a row filter"), "columns"
    )
    return RowFilter(function, columns)


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
    "grants",
    "row_filter",
    "hooks",
    "renamed_from",
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
            return _read_foreign_key(ctx, value_node)
        case _:
            raise SpecError(
                f"unknown constraint {kind!r} (expected primary_key, check or "
                "foreign_key)",
                key_loc,
            )


def _read_foreign_key(ctx: _Ctx, node: Node) -> ForeignKey:
    items = _mapping(ctx, node, "a foreign key")
    _known_keys(
        items,
        allowed={"columns", "references", "referenced_columns", "name"},
        what="a foreign key",
    )
    columns = _string_list(
        ctx, _require(ctx, items, node, "columns", "a foreign key"), "columns"
    )
    references_node = _require(ctx, items, node, "references", "a foreign key")
    references = _string(ctx, references_node, "references")
    if len(references.split(".")) != 3:
        raise SpecError(
            f"references must be catalog.schema.table, not {references!r}",
            ctx.loc(references_node),
        )
    referenced = _string_list(
        ctx,
        _require(ctx, items, node, "referenced_columns", "a foreign key"),
        "referenced_columns",
    )
    if len(referenced) != len(columns):
        raise SpecError(
            f"a foreign key on {len(columns)} column(s) must reference "
            f"{len(columns)}, not {len(referenced)}",
            ctx.loc(node),
        )
    name = _string(ctx, items["name"][0], "name") if "name" in items else None
    return ForeignKey(columns, references, referenced, name)


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


VIEW_KEYS = {"view", "query", "comment", "tags", "properties", "grants"}


def load_spec(
    path: Path,
    variables: Mapping[str, str] | None = None,
    unresolved: Mapping[str, str] | None = None,
) -> Relation:
    """Read one spec file: a table, or — with a `view:` or `function:` key — one
    of those. A `.sql` file is a CREATE statement, read by `deltaplan.sqlspec`."""
    if path.suffix.lower() == ".sql":
        # Imported here: sqlspec builds on this module's variables and errors.
        from deltaplan.sqlspec import load_sql_spec

        return load_sql_spec(path, variables, unresolved)
    ctx = _Ctx(
        path,
        tuple(sorted((variables or {}).items())),
        tuple(sorted((unresolved or {}).items())),
    )
    node = _compose(path)
    if node is None:
        raise SpecError("spec file is empty", Loc(path, 1, 1))
    items = _mapping(ctx, node, "a spec")
    if "view" in items:
        return _read_view(ctx, node, items)
    if "function" in items:
        return _read_function_spec(ctx, node, items)
    if "table" not in items:
        raise SpecError("a spec needs a 'table', 'view' or 'function' key", ctx.loc(node))
    return _read_table(ctx, node, items)


def load_table(path: Path, variables: dict[str, str] | None = None) -> Table:
    """Read one spec file that must describe a table."""
    spec = load_spec(path, variables)
    if not isinstance(spec, Table):
        kind = "view" if isinstance(spec, View) else "function"
        raise SpecError(f"this spec describes a {kind}, not a table", Loc(path, 1, 1))
    return spec


FUNCTION_KEYS = {"function", "parameters", "returns", "body", "comment", "grants"}


def _read_function_spec(
    ctx: _Ctx, node: Node, items: dict[str, tuple[Node, Loc]]
) -> Function:
    _known_keys(items, allowed=FUNCTION_KEYS, what="a function spec")
    name = _string(ctx, items["function"][0], "function name")
    parameters: list[Parameter] = []
    if "parameters" in items:
        for item in _sequence(ctx, items["parameters"][0], "parameters"):
            entry = _mapping(ctx, item, "a parameter")
            _known_keys(entry, allowed={"name", "type"}, what="a parameter")
            parameter_name = _string(
                ctx, _require(ctx, entry, item, "name", "a parameter"), "parameter name"
            )
            parameter_type = _read_type(
                ctx,
                _require(ctx, entry, item, "type", "a parameter"),
                f"type of {parameter_name!r}",
            )
            parameters.append(Parameter(parameter_name, parameter_type))
    returns = _read_type(
        ctx, _require(ctx, items, node, "returns", "a function spec"), "returns"
    )
    body_node = _require(ctx, items, node, "body", "a function spec")
    body = _string(ctx, body_node, "body")
    if not body.strip():
        raise SpecError("a function's body cannot be empty", ctx.loc(body_node))
    comment = _string(ctx, items["comment"][0], "comment") if "comment" in items else None
    grants: tuple[Grant, ...] = ()
    if "grants" in items:
        grants = _read_grants(ctx, items["grants"][0], allowed=FUNCTION_PRIVILEGES)
    return Function(name, tuple(parameters), returns, body, comment, grants)


def _read_view(ctx: _Ctx, node: Node, items: dict[str, tuple[Node, Loc]]) -> View:
    _known_keys(items, allowed=VIEW_KEYS, what="a view spec")
    name = _string(ctx, items["view"][0], "view name")
    query = _string(ctx, _require(ctx, items, node, "query", "a view spec"), "query")
    if not query.strip():
        raise SpecError("a view's query cannot be empty", ctx.loc(items["query"][0]))
    comment = None
    if "comment" in items:
        comment = _string(ctx, items["comment"][0], "view comment")
    properties: tuple[tuple[str, str], ...] = ()
    if "properties" in items:
        properties = _string_map(ctx, items["properties"][0], "properties")
    tags: tuple[tuple[str, str], ...] = ()
    if "tags" in items:
        tags = _string_map(ctx, items["tags"][0], "tags")
    grants: tuple[Grant, ...] = ()
    if "grants" in items:
        grants = _read_grants(ctx, items["grants"][0])
    return View(name, query, comment, properties, tags, grants)


def _read_table(ctx: _Ctx, node: Node, items: dict[str, tuple[Node, Loc]]) -> Table:
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
    cluster_auto = False
    if "cluster_by" in items:
        cluster_node = items["cluster_by"][0]
        if isinstance(cluster_node, ScalarNode):
            if _string(ctx, cluster_node, "cluster_by").lower() != "auto":
                raise SpecError(
                    "cluster_by is a list of columns, or `auto` for automatic "
                    "liquid clustering",
                    ctx.loc(cluster_node),
                )
            cluster_auto = True
        else:
            cluster_by = _string_list(ctx, cluster_node, "cluster_by")
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
    grants: tuple[Grant, ...] = ()
    if "grants" in items:
        grants = _read_grants(ctx, items["grants"][0])
    row_filter = None
    if "row_filter" in items:
        row_filter = _read_row_filter(ctx, items["row_filter"][0])
    hooks = None
    if "hooks" in items:
        hook_items = _mapping(ctx, items["hooks"][0], "hooks")
        _known_keys(hook_items, allowed={"before", "after"}, what="hooks")
        hooks = Hooks(
            before=_string(ctx, hook_items["before"][0], "before hook")
            if "before" in hook_items
            else None,
            after=_string(ctx, hook_items["after"][0], "after hook")
            if "after" in hook_items
            else None,
        )

    renamed_from = None
    if "renamed_from" in items:
        renamed_from = _string(ctx, items["renamed_from"][0], "renamed_from")
        if "." not in renamed_from:
            # Just the old table name: it was in the same schema.
            renamed_from = f"{name.rsplit('.', 1)[0]}.{renamed_from}"

    return Table(
        name=name,
        columns=columns,
        comment=comment,
        cluster_by=cluster_by,
        cluster_auto=cluster_auto,
        properties=properties,
        tags=tags,
        constraints=constraints,
        grants=grants,
        row_filter=row_filter,
        hooks=hooks,
        renamed_from=renamed_from,
    )


def _read_grants(
    ctx: _Ctx, node: Node, *, allowed: frozenset[str] = TABLE_PRIVILEGES
) -> tuple[Grant, ...]:
    grants: list[Grant] = []
    seen: set[str] = set()
    for item in _sequence(ctx, node, "grants"):
        entry = _mapping(ctx, item, "a grant")
        _known_keys(entry, allowed={"principal", "privileges"}, what="a grant")
        principal = _string(
            ctx, _require(ctx, entry, item, "principal", "a grant"), "principal"
        )
        if principal in seen:
            raise SpecError(
                f"{principal!r} is granted twice — list its privileges once",
                ctx.loc(item),
            )
        seen.add(principal)
        privileges_node = _require(ctx, entry, item, "privileges", "a grant")
        privileges: list[str] = []
        for privilege_node in _sequence(ctx, privileges_node, "privileges"):
            raw = _string(ctx, privilege_node, "privilege")
            try:
                privileges.append(privilege_sql(raw, allowed))
            except ValueError as error:
                raise SpecError(str(error), ctx.loc(privilege_node)) from error
        grants.append(Grant(principal, tuple(privileges)))
    return tuple(grants)


# ---------------------------------------------------------------------------
# project config
# ---------------------------------------------------------------------------

CONFIG_KEYS = {"version", "specs", "targets", "history_schema", "schemas", "bundle"}
TARGET_KEYS = {"vars", "warehouse_id", "mode", "profile", "default"}


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


def load_project(path: Path, environ: Mapping[str, str] | None = None) -> Project:
    """Read a `deltaplan.yml` — and the bundle it names, if any.

    `environ` supplies a bundle's `BUNDLE_VAR_<name>` overrides; it is passed in
    rather than read here so that loading stays a function of its arguments.
    """
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
    target_locs: dict[str, Loc] = {}
    if "targets" in items:
        for name, (target_node, key_loc) in _mapping(
            ctx, items["targets"][0], "targets"
        ).items():
            targets.append(_read_target(ctx, name, target_node))
            target_locs[name] = key_loc

    bundle_path: Path | None = None
    marked = [
        t.name
        for t, default in zip(targets, _defaults(ctx, items), strict=True)
        if default
    ]
    default_target = marked[0] if marked else None
    if "bundle" in items:
        bundle_node = items["bundle"][0]
        bundle_path = root / _string(ctx, bundle_node, "bundle")
        try:
            bundle = read_bundle(bundle_path, environ)
        except BundleError as error:
            raise SpecError(str(error), ctx.loc(bundle_node)) from error
        for target in targets:
            if bundle.target(target.name) is None:
                known = ", ".join(t.name for t in bundle.targets)
                raise SpecError(
                    f"target {target.name!r} isn't in the bundle (its targets: {known})",
                    target_locs[target.name],
                )
        own = {target.name: target for target in targets}
        targets = [_from_bundle(entry, own.get(entry.name)) for entry in bundle.targets]
        default_target = default_target or bundle.default
    if default_target is None and len(targets) == 1:
        default_target = targets[0].name

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
        default_target=default_target,
        bundle=bundle_path,
    )


def _defaults(ctx: _Ctx, items: dict[str, tuple[Node, Loc]]) -> list[bool]:
    """Which of the config's own targets say `default: true`, in order."""
    if "targets" not in items:
        return []
    found: list[bool] = []
    for name, (node, _) in _mapping(ctx, items["targets"][0], "targets").items():
        entry = _mapping(ctx, node, f"target {name!r}")
        found.append("default" in entry and _bool(ctx, entry["default"][0], "default"))
    if sum(found) > 1:
        raise SpecError(
            "only one target can be the default", ctx.loc(items["targets"][0])
        )
    return found


def _from_bundle(entry: BundleTarget, own: Target | None) -> Target:
    """A bundle target, with what deltaplan.yml says about it on top.

    deltaplan.yml wins where both speak: its `vars` override the bundle's
    variables, its `profile` the bundle's workspace. `warehouse_id` falls back to
    the bundle's variable of that name.
    """
    variables = dict(entry.variables)
    if own is not None:
        variables |= own.variables_map()
    unresolved = {k: v for k, v in entry.unresolved if k not in variables}
    warehouse_id = own.warehouse_id if own else None
    lookup = None
    if warehouse_id is None:
        warehouse_id = variables.get(WAREHOUSE_VARIABLE)
        lookup = entry.warehouse_lookup if warehouse_id is None else None
    return Target(
        name=entry.name,
        variables=tuple(sorted(variables.items())),
        warehouse_id=warehouse_id,
        mode=own.mode if own else "additive",
        profile=(own.profile if own else None) or entry.profile,
        host=entry.host,
        unresolved=tuple(sorted(unresolved.items())),
        warehouse_lookup=lookup,
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
    profile = None
    if "profile" in items:
        profile = _string(ctx, items["profile"][0], "profile")
    return Target(name, variables, warehouse_id, mode, profile)


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
    unresolved = target.unresolved_map()
    return tuple(
        LoadedSpec(path, load_spec(path, variables, unresolved))
        for path in spec_files(project)
    )


# ---------------------------------------------------------------------------
# linting
# ---------------------------------------------------------------------------

# Liquid clustering takes at most four columns.
# TODO(verify): confirm against a live workspace; the limit has moved before.
# https://docs.databricks.com/aws/en/delta/clustering
MAX_CLUSTER_COLUMNS = 4


def validate_spec(spec: Relation, where: str) -> tuple[Diagnostic, ...]:
    """Lint a table, a view or a function."""
    if isinstance(spec, View):
        return validate_view(spec, where)
    if isinstance(spec, Function):
        return validate_function(spec, where)
    return validate_table(spec, where)


def validate_function(function: Function, where: str) -> tuple[Diagnostic, ...]:
    found: list[Diagnostic] = []
    if len(function.parts) != 3:
        found.append(
            Diagnostic(
                "error",
                f"function name {function.name!r} must be catalog.schema.function",
                where,
            )
        )
    names = [p.name.casefold() for p in function.parameters]
    for name in sorted({n for n in names if names.count(n) > 1}):
        found.append(Diagnostic("error", f"parameter {name!r} is declared twice", where))
    return tuple(found)


def validate_view(view: View, where: str) -> tuple[Diagnostic, ...]:
    found: list[Diagnostic] = []
    if len(view.parts) != 3:
        found.append(
            Diagnostic(
                "error",
                f"view name {view.name!r} must be catalog.schema.view "
                "(three parts, after variable substitution)",
                where,
            )
        )
    if VARIABLE.search(view.query):
        found.append(
            Diagnostic("error", "the query still contains an unsubstituted ${…}", where)
        )
    return tuple(found)


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
    for key, _ in table.properties:
        if key in MAINTAINED_PROPERTIES:
            error(f"property {key!r} is maintained by Delta itself; don't declare it")
    if table.renamed_from is not None:
        old = table.renamed_from.split(".")
        if table.renamed_from == table.name:
            error("renamed_from names the table itself")
        elif len(old) != 3:
            error(
                f"renamed_from {table.renamed_from!r} must be the old table's name, "
                "alone or as catalog.schema.table"
            )
        elif old[:2] != table.name.split(".")[:2]:
            # TODO(verify): Unity Catalog renames a table only within its schema.
            # https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table
            error(
                "renamed_from must be in the same schema: a rename can't move a "
                "table to another schema or catalog"
            )

    # Delta column names ignore case, so `id` and `ID` are the same column.
    seen: set[str] = set()
    for column in table.columns:
        if column.name.casefold() in seen:
            error(f"duplicate column {column.name!r} (column names ignore case)")
        seen.add(column.name.casefold())

    for column in table.columns:
        _lint_field(column, column.name, table, error, warn)

    for name in table.cluster_by:
        if name.casefold() not in seen:
            error(f"cluster_by column {name!r} is not in the spec")
    if table.row_filter is not None:
        for name in table.row_filter.columns:
            if name.casefold() not in seen:
                error(f"row filter column {name!r} is not in the spec")
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

    for key in table.foreign_keys():
        for name in key.columns:
            if table.column(name) is None:
                error(f"foreign key column {name!r} is not in the spec")

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
    if field.tags and "." in path:
        error(f"{path}: tags go on columns, not on fields inside them")
    if field.mask is not None and "." in path:
        error(f"{path}: masks go on columns, not on fields inside them")
    generation = [
        what
        for what, value in (
            ("identity", field.identity),
            ("generated", field.generated),
            ("default", field.default),
        )
        if value is not None
    ]
    if generation and "." in path:
        error(f"{path}: {generation[0]} goes on columns, not on fields inside them")
    if len(generation) > 1:
        error(f"{path}: a column takes one of identity, generated and default, not both")
    if field.identity is not None and render_type(field.type) != "bigint":
        # https://docs.databricks.com/aws/en/delta/generated-columns
        error(f"{path}: an identity column must be bigint")
    if field.mask is not None:
        for name in field.mask.using_columns:
            if table.column(name) is None:
                error(f"{path}: mask uses column {name!r}, which is not in the spec")

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
                if member.name.casefold() in names:
                    error(f"{path}: duplicate field {member.name!r} (names ignore case)")
                names.add(member.name.casefold())
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


def dump_spec(table: Relation, *, catalog_variable: str | None = None) -> str:
    """Render a table or view as a spec file, the way `import` writes it.

    Types are written in the string notation, which carries nested comments and
    nullability, so the result round-trips through `load_table` unchanged.
    `catalog_variable` puts the catalog back behind a `${var}`, so one imported
    spec serves every target.
    """
    name = table.name
    if catalog_variable:
        _, _, rest = name.partition(".")
        name = f"${{{catalog_variable}}}.{rest}"
    if isinstance(table, View):
        return _dump_view(table, name)
    if isinstance(table, Function):
        return _dump_function(table, name)

    document: dict[str, object] = {"table": name}
    if table.comment is not None:
        document["comment"] = table.comment
    if table.cluster_auto:
        # The live keys are Databricks' choice; the spec only asks for AUTO.
        document["cluster_by"] = "auto"
    elif table.cluster_by:
        document["cluster_by"] = list(table.cluster_by)
    if table.tags:
        document["tags"] = dict(table.tags)
    # Delta's own bookkeeping and deltaplan's marker are not intent; a spec that
    # declared them would fight Delta for them.
    properties = {
        key: value
        for key, value in table.properties
        if (not is_bookkeeping(key) or key in PREREQUISITE_PROPERTIES)
        and not is_platform_default(key, value)
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
    if table.row_filter is not None:
        document["row_filter"] = {
            "function": table.row_filter.function,
            "columns": list(table.row_filter.columns),
        }
    if table.grants:
        document["grants"] = [
            {"principal": grant.principal, "privileges": list(grant.privileges)}
            for grant in table.grants
        ]

    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=100)


def _dump_function(function: Function, name: str) -> str:
    document: dict[str, object] = {"function": name}
    if function.comment is not None:
        document["comment"] = function.comment
    if function.parameters:
        document["parameters"] = [
            {"name": p.name, "type": render_type(p.type)} for p in function.parameters
        ]
    document["returns"] = render_type(function.returns)
    if function.grants:
        document["grants"] = [
            {"principal": grant.principal, "privileges": list(grant.privileges)}
            for grant in function.grants
        ]
    document["body"] = _LiteralText(function.body.strip() + "\n")
    return yaml.dump(
        document, Dumper=_SpecDumper, sort_keys=False, default_flow_style=False, width=100
    )


def _dump_view(view: View, name: str) -> str:
    """A view spec. The query is written as the catalog holds it, catalog names
    and all — rewriting names inside SQL is not something to do by text search.
    """
    document: dict[str, object] = {"view": name}
    if view.comment is not None:
        document["comment"] = view.comment
    properties = {k: v for k, v in view.properties if not is_bookkeeping(k)}
    if properties:
        document["properties"] = properties
    if view.tags:
        document["tags"] = dict(view.tags)
    if view.grants:
        document["grants"] = [
            {"principal": grant.principal, "privileges": list(grant.privileges)}
            for grant in view.grants
        ]
    document["query"] = _LiteralText(view.query.strip() + "\n")
    return yaml.dump(
        document, Dumper=_SpecDumper, sort_keys=False, default_flow_style=False, width=100
    )


class _LiteralText(str):
    """Written as a `|` block, so a query reads like SQL rather than one long line."""


class _SpecDumper(yaml.SafeDumper):
    pass


_SpecDumper.add_representer(
    _LiteralText,
    lambda dumper, text: dumper.represent_scalar(
        "tag:yaml.org,2002:str", str(text), style="|"
    ),
)


def _column_document(column: Field) -> dict[str, object]:
    rendered: dict[str, object] = {"name": column.name, "type": render_type(column.type)}
    if not column.nullable:
        rendered["nullable"] = False
    if column.comment is not None:
        rendered["comment"] = column.comment
    if column.tags:
        rendered["tags"] = dict(column.tags)
    if column.identity is not None:
        identity = column.identity
        default_identity = identity.start == 1 and identity.increment == 1
        rendered["identity"] = (
            ("always" if identity.always else "by_default")
            if default_identity
            else {
                "generated": "always" if identity.always else "by_default",
                "start": identity.start,
                "increment": identity.increment,
            }
        )
    if column.generated is not None:
        rendered["generated"] = column.generated
    if column.default is not None:
        rendered["default"] = column.default
    if column.mask is not None:
        rendered["mask"] = (
            {
                "function": column.mask.function,
                "using_columns": list(column.mask.using_columns),
            }
            if column.mask.using_columns
            else column.mask.function
        )
    return rendered


def _constraint_document(constraint: Constraint) -> dict[str, object] | None:
    if isinstance(constraint, PrimaryKey):
        body: dict[str, object] = {"columns": list(constraint.columns)}
        if constraint.name:
            body["name"] = constraint.name
        return {"primary_key": body}
    if isinstance(constraint, Check):
        return {"check": {"name": constraint.name, "expression": constraint.expression}}
    body: dict[str, object] = {
        "columns": list(constraint.columns),
        "references": constraint.references,
        "referenced_columns": list(constraint.referenced_columns),
    }
    if constraint.name:
        body["name"] = constraint.name
    return {"foreign_key": body}
