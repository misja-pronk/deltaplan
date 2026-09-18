"""Live state, read from Unity Catalog.

One of the three modules that touch the outside world. Everything it returns is
the same model the loader produces, so the differ cannot tell which side came
from a file and which from a warehouse.

Reads are done with `information_schema` (one query per schema, not per table)
plus `DESCRIBE DETAIL` for the things it doesn't carry — properties, clustering
columns and size.

References:
  information_schema  https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-information-schema
  DESCRIBE DETAIL     https://docs.databricks.com/aws/en/delta/table-details
  Statement Execution https://docs.databricks.com/api/workspace/statementexecution
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol, TypeVar

from deltaplan.ddl import DdlError, read_columns
from deltaplan.model.function import Function, Parameter
from deltaplan.model.schema import Schema
from deltaplan.model.table import (
    CHECK_PROPERTY_PREFIX,
    Check,
    Constraint,
    ForeignKey,
    Grant,
    PrimaryKey,
    RowFilter,
    Table,
)
from deltaplan.model.types import Column, DataType, Field, Identity, Mask, Primitive
from deltaplan.model.view import Relation, View
from deltaplan.sql import (
    normalise_expression,
    normalise_privilege,
    quote_literal,
    quote_qualified,
)
from deltaplan.typeparser import TypeParseError, parse_type

if TYPE_CHECKING:  # the SDK is only needed to talk to a workspace
    from databricks.sdk import WorkspaceClient

Row = dict[str, str | None]


#: `table_type`s that are neither tables deltaplan can alter nor views it can
#: define. TODO(verify): the exact strings against a live workspace.
_NOT_TABLES = frozenset({"MATERIALIZED_VIEW", "STREAMING_TABLE"})


T = TypeVar("T")


class IntrospectionError(Exception):
    """A query failed, or came back in a shape we don't understand."""


class SqlRunner(Protocol):
    """Anything that can run a read-only statement and return rows.

    Introspection is written against this rather than the SDK so the mapping
    logic — which is where the bugs live — can be tested without a workspace.
    """

    def query(self, statement: str) -> tuple[Row, ...]: ...


@dataclass(frozen=True, slots=True)
class LiveTable:
    """A live table, plus the facts the planner needs about it.

    `unmodelled` describes what the table has that deltaplan's model doesn't
    cover — partitioning, identity and generated columns, column defaults. It is
    reported, never diffed; and because a rewrite rebuilds a table from a query
    that carries none of it, a table with any is never rewritten.
    """

    table: Table
    size_bytes: int | None = None
    data_format: str = "DELTA"
    unmodelled: tuple[str, ...] = ()
    #: Delta table features, from DESCRIBE DETAIL's `tableFeatures` — verified
    #: live to be where they are listed; its `properties` leave them out.
    features: tuple[str, ...] = ()
    #: Whether SHOW CREATE TABLE was read. A light read — for a table no spec
    #: describes — leaves it out: identity, generation, defaults and nested
    #: NOT NULL are then missing. `Introspector.complete` adds them.
    definition_read: bool = True


@dataclass(frozen=True, slots=True)
class LiveSchema:
    """Everything deltaplan can see in one schema."""

    catalog: str
    schema: str
    tables: tuple[LiveTable, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    views: tuple[View, ...] = ()
    #: False when the schema itself isn't there yet — a fresh target.
    exists: bool = True
    functions: tuple[Function, ...] = ()
    #: The schema itself — its comment, tags and direct grants. None when the
    #: schema doesn't exist.
    definition: Schema | None = None

    def get(self, name: str) -> LiveTable | None:
        for live in self.tables:
            if live.table.name == name:
                return live
        return None

    def get_view(self, name: str) -> View | None:
        for view in self.views:
            if view.name == name:
                return view
        return None

    def get_function(self, name: str) -> Function | None:
        for function in self.functions:
            if function.name == name:
                return function
        return None

    def relation(self, name: str) -> Relation | None:
        if len(name.split(".")) == 2:
            return self.definition if name == f"{self.catalog}.{self.schema}" else None
        return self._object(name)

    def _object(self, name: str) -> Relation | None:
        """A table, a view or a function, whichever lives under that name."""
        live = self.get(name)
        if live is not None:
            return live.table
        return self.get_view(name) or self.get_function(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(live.table.name for live in self.tables) + tuple(
            view.name for view in self.views
        )


@dataclass(slots=True)
class Introspector:
    """Reads live state through a `SqlRunner`."""

    runner: SqlRunner
    #: How many per-table queries (DESCRIBE DETAIL, SHOW CREATE TABLE, SHOW
    #: TBLPROPERTIES) run at once. One per table, each about a second on a
    #: warehouse: sequentially, a 300-table schema took minutes.
    parallel: int = 8
    #: Within one read only: the same key usage is asked for by several
    #: constraints. Kept across reads, a second read returned the first one's
    #: state — found live, where apply's staleness check could then never see a
    #: change. Cleared at the start of every read.
    _detail_cache: dict[str, Row] = field(default_factory=dict)
    _keys_cache: dict[str, dict[str, tuple[str, list[str]]]] = field(default_factory=dict)

    # -- public ------------------------------------------------------------
    def schema(
        self, catalog: str, schema: str, *, full: Collection[str] | None = None
    ) -> LiveSchema:
        """Every Delta table in one schema, as the model — read fresh.

        `full` names the tables to read completely (`catalog.schema.table`);
        the rest get a light read, without SHOW CREATE TABLE — enough to list
        them and tell whether they are deltaplan's. None reads all of them fully.
        """
        self._detail_cache.clear()
        self._keys_cache.clear()
        schema_row = self._schema_row(catalog, schema)
        if schema_row is None:
            return LiveSchema(catalog, schema, exists=False)
        definition = self._schema_definition(catalog, schema, schema_row)
        comments, formats = self._table_rows(catalog, schema)
        columns, column_features = self._column_rows(catalog, schema)
        column_tags = self._column_tag_rows(catalog, schema)
        masks = self._mask_rows(catalog, schema)
        for table_name, table_columns in columns.items():
            columns[table_name] = [
                replace(
                    c,
                    tags=tuple(sorted(column_tags.get((table_name, c.name), {}).items())),
                    mask=masks.get((table_name, c.name)),
                )
                for c in table_columns
            ]
        row_filters = self._row_filter_rows(catalog, schema)
        tags = self._tag_rows(catalog, schema)
        constraints = self._constraint_rows(catalog, schema)
        grants = self._grant_rows(catalog, schema)

        tables: list[LiveTable] = []
        views: list[View] = []
        skipped: list[tuple[str, str]] = []
        definitions = (
            self._view_rows(catalog, schema) if "VIEW" in formats.values() else {}
        )

        def qualified(name: str) -> str:
            return f"{catalog}.{schema}.{name}"

        wanted = {name.lower() for name in full} if full is not None else None
        delta = [n for n, kind in sorted(formats.items()) if kind == "DELTA"]
        details = self._each(delta, lambda n: self._describe_detail(qualified(n)))
        statements = self._each(
            [n for n in delta if wanted is None or qualified(n).lower() in wanted],
            lambda n: self._show_create(qualified(n)),
        )
        view_properties = self._each(
            [n for n, kind in sorted(formats.items()) if kind == "VIEW"],
            lambda n: self._view_properties(qualified(n)),
        )
        for name, table_type in sorted(formats.items()):
            full_name = f"{catalog}.{schema}.{name}"
            if table_type == "VIEW":
                views.append(
                    View(
                        name=full_name,
                        query=definitions.get(name, ""),
                        comment=comments.get(name),
                        properties=view_properties[name],
                        tags=tuple(sorted(tags.get(name, {}).items())),
                        grants=tuple(
                            Grant(principal, tuple(privileges))
                            for principal, privileges in grants.get(name, {}).items()
                        ),
                    )
                )
                continue
            if table_type != "DELTA":
                # Other formats and other kinds of object are out of scope by
                # design, and never touched.
                skipped.append((full_name, table_type.lower().replace("_", " ")))
                continue
            detail = details[name]
            properties = _json_map(detail.get("properties"))
            read = name in statements
            table_columns, definition_notes = (
                _with_definition(columns.get(name, []), statements[name])
                if read
                else (columns.get(name, []), [])
            )
            tables.append(
                LiveTable(
                    table=Table(
                        name=full_name,
                        columns=tuple(table_columns),
                        comment=comments.get(name),
                        cluster_by=_json_list(detail.get("clusteringColumns")),
                        # A top-level DESCRIBE DETAIL field — verified live.
                        cluster_auto=(detail.get("clusterByAuto") or "").lower()
                        == "true",
                        properties=_pairs(
                            {
                                k: v
                                for k, v in properties.items()
                                if not k.startswith(CHECK_PROPERTY_PREFIX)
                            }
                        ),
                        tags=tuple(sorted(tags.get(name, {}).items())),
                        constraints=(
                            *constraints.get(name, ()),
                            *_checks(properties),
                        ),
                        grants=tuple(
                            Grant(principal, tuple(privileges))
                            for principal, privileges in grants.get(name, {}).items()
                        ),
                        row_filter=row_filters.get(name),
                    ),
                    size_bytes=_as_int(detail.get("sizeInBytes")),
                    data_format=table_type,
                    unmodelled=_unmodelled(
                        detail, [*column_features.get(name, []), *definition_notes]
                    ),
                    features=_json_list(detail.get("tableFeatures")),
                    definition_read=read,
                )
            )
        return LiveSchema(
            catalog,
            schema,
            tuple(tables),
            tuple(skipped),
            tuple(views),
            functions=self._functions(catalog, schema, grants),
            definition=definition,
        )

    def table(self, name: str) -> LiveTable | None:
        """One table by its full `catalog.schema.table` name."""
        catalog, schema, short = _split(name)
        return self.schema(catalog, schema).get(f"{catalog}.{schema}.{short}")

    def tables(self, names: Sequence[str]) -> dict[str, Relation | None]:
        """Look up several tables or views — one schema scan per schema, not per
        name, reading only the named tables in full."""
        found: dict[str, Relation | None] = {}
        scanned: dict[tuple[str, str], LiveSchema] = {}
        for name in names:
            parts = name.split(".")
            key = (parts[0], parts[1])
            if key not in scanned:
                wanted = [n for n in names if tuple(n.split(".")[:2]) == key]
                scanned[key] = self.schema(*key, full=wanted)
            found[name] = scanned[key].relation(name)
        return found

    def complete(self, live: LiveTable) -> LiveTable:
        """A lightly read table, with what SHOW CREATE TABLE adds."""
        if live.definition_read:
            return live
        columns, notes = _with_definition(
            list(live.table.columns), self._show_create(live.table.name)
        )
        return replace(
            live,
            table=replace(live.table, columns=tuple(columns)),
            unmodelled=(*live.unmodelled, *notes),
            definition_read=True,
        )

    def latest_version(self, name: str) -> int | None:
        """The table's current Delta version — the restore point for a plan."""
        rows = self.runner.query(f"DESCRIBE HISTORY {quote_qualified(name)} LIMIT 1")
        return _as_int(rows[0].get("version")) if rows else None

    # -- queries -----------------------------------------------------------
    def _table_rows(
        self, catalog: str, schema: str
    ) -> tuple[dict[str, str | None], dict[str, str]]:
        rows = self.runner.query(
            "SELECT table_name, comment, table_type, data_source_format "
            f"FROM {_information_schema(catalog)}.tables "
            f"WHERE table_schema = {quote_literal(schema)}"
        )
        comments: dict[str, str | None] = {}
        formats: dict[str, str] = {}
        for row in rows:
            name = row.get("table_name")
            if name is None:
                continue
            comments[name] = row.get("comment")
            table_type = (row.get("table_type") or "").upper()
            data_format = (row.get("data_source_format") or "").upper()
            if table_type == "VIEW":
                formats[name] = "VIEW"
            elif table_type in _NOT_TABLES:
                # A materialized view or streaming table reports its storage as
                # DELTA, but deltaplan can't ALTER it like a table — or define it.
                formats[name] = table_type
            else:
                formats[name] = data_format or "UNKNOWN"
        return comments, formats

    def _column_rows(
        self, catalog: str, schema: str
    ) -> tuple[dict[str, list[Column]], dict[str, list[str]]]:
        """Each table's columns, with how they get values they weren't given.

        The second result is kept for column features deltaplan doesn't model.
        Verified live: this view reports no identity, generation or default,
        and loses NOT NULL and comments inside structs — `_with_definitions`
        completes the columns from SHOW CREATE TABLE. They're still read here,
        for a workspace that does report them.
        https://docs.databricks.com/aws/en/sql/language-manual/information-schema/columns
        """
        rows = self.runner.query(
            "SELECT table_name, column_name, ordinal_position, full_data_type, "
            "is_nullable, comment, column_default, is_identity, identity_generation, "
            "identity_start, identity_increment, is_generated, generation_expression "
            f"FROM {_information_schema(catalog)}.columns "
            f"WHERE table_schema = {quote_literal(schema)} "
            "ORDER BY table_name, ordinal_position"
        )
        columns: dict[str, list[Column]] = {}
        features: dict[str, list[str]] = {}
        for row in rows:
            table_name = row.get("table_name")
            column_name = row.get("column_name")
            if table_name is None or column_name is None:
                continue
            identity = None
            if (row.get("is_identity") or "NO").upper() == "YES":
                identity = Identity(
                    always=(row.get("identity_generation") or "ALWAYS").upper()
                    == "ALWAYS",
                    start=_as_int(row.get("identity_start")) or 1,
                    increment=_as_int(row.get("identity_increment")) or 1,
                )
            generated = row.get("generation_expression") or None
            columns.setdefault(table_name, []).append(
                Field(
                    column_name,
                    _parse_live_type(row.get("full_data_type"), table_name, column_name),
                    nullable=(row.get("is_nullable") or "YES").upper() != "NO",
                    comment=row.get("comment"),
                    identity=identity,
                    generated=generated if identity is None else None,
                    default=row.get("column_default"),
                )
            )
        return columns, features

    def _tag_rows(self, catalog: str, schema: str) -> dict[str, dict[str, str]]:
        # TODO(verify): table_tags column names against a live workspace.
        # https://docs.databricks.com/aws/en/database-objects/tags
        rows = self.runner.query(
            "SELECT table_name, tag_name, tag_value "
            f"FROM {_information_schema(catalog)}.table_tags "
            f"WHERE schema_name = {quote_literal(schema)}"
        )
        tags: dict[str, dict[str, str]] = {}
        for row in rows:
            table_name = row.get("table_name")
            tag = row.get("tag_name")
            if table_name is None or tag is None:
                continue
            tags.setdefault(table_name, {})[tag] = row.get("tag_value") or ""
        return tags

    def _column_tag_rows(
        self, catalog: str, schema: str
    ) -> dict[tuple[str, str], dict[str, str]]:
        # TODO(verify): column_tags column names against a live workspace.
        # https://docs.databricks.com/aws/en/sql/language-manual/information-schema/column_tags
        rows = self.runner.query(
            "SELECT table_name, column_name, tag_name, tag_value "
            f"FROM {_information_schema(catalog)}.column_tags "
            f"WHERE schema_name = {quote_literal(schema)}"
        )
        tags: dict[tuple[str, str], dict[str, str]] = {}
        for row in rows:
            table_name, column, tag = (
                row.get("table_name"),
                row.get("column_name"),
                row.get("tag_name"),
            )
            if table_name is None or column is None or tag is None:
                continue
            tags.setdefault((table_name, column), {})[tag] = row.get("tag_value") or ""
        return tags

    def _functions(
        self, catalog: str, schema: str, table_grants: dict[str, dict[str, list[str]]]
    ) -> tuple[Function, ...]:
        """The SQL functions in a schema. Python UDFs aren't modelled and are skipped.

        TODO(verify): routines, parameters and routine_privileges column names,
        and that `specific_name` is the routine's name (no overloading in UC).
        https://docs.databricks.com/aws/en/sql/language-manual/information-schema/routines
        """
        del table_grants
        rows = self.runner.query(
            "SELECT routine_name, routine_definition, full_data_type, comment "
            f"FROM {_information_schema(catalog)}.routines "
            f"WHERE routine_schema = {quote_literal(schema)} "
            "AND routine_type = 'FUNCTION' AND routine_body = 'SQL'"
        )
        if not rows:
            return ()
        parameter_rows = self.runner.query(
            "SELECT specific_name, parameter_name, ordinal_position, full_data_type "
            f"FROM {_information_schema(catalog)}.parameters "
            f"WHERE specific_schema = {quote_literal(schema)} "
            "ORDER BY specific_name, ordinal_position"
        )
        parameters: dict[str, list[Parameter]] = {}
        for row in parameter_rows:
            owner, name = row.get("specific_name"), row.get("parameter_name")
            if owner is None or name is None:
                continue
            parameters.setdefault(owner, []).append(
                Parameter(name, _parse_live_type(row.get("full_data_type"), owner, name))
            )
        privilege_rows = self.runner.query(
            "SELECT routine_name, grantee, privilege_type, inherited_from "
            f"FROM {_information_schema(catalog)}.routine_privileges "
            f"WHERE routine_schema = {quote_literal(schema)}"
        )
        grants: dict[str, dict[str, list[str]]] = {}
        for row in privilege_rows:
            routine, grantee, privilege = (
                row.get("routine_name"),
                row.get("grantee"),
                row.get("privilege_type"),
            )
            if routine is None or grantee is None or privilege is None:
                continue
            if (row.get("inherited_from") or "NONE").upper() != "NONE":
                continue
            grants.setdefault(routine, {}).setdefault(grantee, []).append(
                normalise_privilege(privilege)
            )

        found: list[Function] = []
        for row in rows:
            name = row.get("routine_name")
            if name is None:
                continue
            found.append(
                Function(
                    name=f"{catalog}.{schema}.{name}",
                    parameters=tuple(parameters.get(name, ())),
                    returns=_parse_live_type(row.get("full_data_type"), name, "RETURNS"),
                    body=row.get("routine_definition") or "",
                    comment=row.get("comment"),
                    grants=tuple(
                        Grant(principal, tuple(privileges))
                        for principal, privileges in grants.get(name, {}).items()
                    ),
                )
            )
        return tuple(found)

    def _schema_row(self, catalog: str, schema: str) -> Row | None:
        """The schema's row, or None when it doesn't exist.

        TODO(verify): that a missing *catalog* fails this query, rather than
        returning nothing — deltaplan creates schemas, never catalogs.
        https://docs.databricks.com/aws/en/sql/language-manual/information-schema/schemata
        """
        rows = self.runner.query(
            f"SELECT schema_name, comment FROM {_information_schema(catalog)}.schemata "
            f"WHERE schema_name = {quote_literal(schema.lower())}"
        )
        return rows[0] if rows else None

    def _schema_definition(self, catalog: str, schema: str, row: Row) -> Schema:
        """The schema's comment, tags and direct grants — the same views and rules
        as for tables, verified live (2026-09-18): privileges come back
        underscored (`CREATE_FUNCTION`), and inherited ones say where from."""
        literal = quote_literal(schema.lower())
        tags = {
            str(r["tag_name"]): str(r.get("tag_value") or "")
            for r in self.runner.query(
                "SELECT tag_name, tag_value "
                f"FROM {_information_schema(catalog)}.schema_tags "
                f"WHERE schema_name = {literal}"
            )
            if r.get("tag_name")
        }
        held: dict[str, list[str]] = {}
        for r in self.runner.query(
            "SELECT grantee, privilege_type, inherited_from "
            f"FROM {_information_schema(catalog)}.schema_privileges "
            f"WHERE schema_name = {literal}"
        ):
            grantee, privilege = r.get("grantee"), r.get("privilege_type")
            if grantee is None or privilege is None:
                continue
            if (r.get("inherited_from") or "NONE").upper() != "NONE":
                continue
            held.setdefault(grantee, []).append(normalise_privilege(privilege))
        return Schema(
            f"{catalog}.{schema}",
            comment=row.get("comment"),
            tags=tuple(sorted(tags.items())),
            grants=tuple(Grant(p, tuple(v)) for p, v in held.items()),
        )

    def _view_rows(self, catalog: str, schema: str) -> dict[str, str]:
        """Each view's definition. TODO(verify): that `view_definition` is the
        query as written — see `normalise_query`.
        https://docs.databricks.com/aws/en/sql/language-manual/information-schema/views
        """
        rows = self.runner.query(
            "SELECT table_name, view_definition "
            f"FROM {_information_schema(catalog)}.views "
            f"WHERE table_schema = {quote_literal(schema)}"
        )
        return {
            str(row["table_name"]): row.get("view_definition") or ""
            for row in rows
            if row.get("table_name")
        }

    def _view_properties(self, name: str) -> tuple[tuple[str, str], ...]:
        """A view's properties — DESCRIBE DETAIL is for tables only.
        TODO(verify): SHOW TBLPROPERTIES column names against a live workspace.
        """
        rows = self.runner.query(f"SHOW TBLPROPERTIES {quote_qualified(name)}")
        return _pairs(
            {str(row["key"]): row.get("value") or "" for row in rows if row.get("key")}
        )

    def _mask_rows(self, catalog: str, schema: str) -> dict[tuple[str, str], Mask]:
        # Verified live: mask_name is the function's full name, unquoted, and
        # using_columns a comma-separated list ("region" / "region, id").
        # https://docs.databricks.com/aws/en/sql/language-manual/information-schema/column_masks
        rows = self.runner.query(
            "SELECT table_name, column_name, mask_name, using_columns "
            f"FROM {_information_schema(catalog)}.column_masks "
            f"WHERE table_schema = {quote_literal(schema)}"
        )
        masks: dict[tuple[str, str], Mask] = {}
        for row in rows:
            table_name, column = row.get("table_name"), row.get("column_name")
            function = row.get("mask_name")
            if table_name is None or column is None or function is None:
                continue
            masks[(table_name, column)] = Mask(
                function, _name_list(row.get("using_columns"))
            )
        return masks

    def _row_filter_rows(self, catalog: str, schema: str) -> dict[str, RowFilter]:
        # Verified live: filter_name is the function's full name, unquoted, and
        # target_columns a comma-separated list.
        # https://docs.databricks.com/aws/en/sql/language-manual/information-schema/row_filters
        rows = self.runner.query(
            "SELECT table_name, filter_name, target_columns "
            f"FROM {_information_schema(catalog)}.row_filters "
            f"WHERE table_schema = {quote_literal(schema)}"
        )
        filters: dict[str, RowFilter] = {}
        for row in rows:
            table_name = row.get("table_name")
            function = row.get("filter_name")
            if table_name is None or function is None:
                continue
            filters[table_name] = RowFilter(
                function, _name_list(row.get("target_columns"))
            )
        return filters

    def _grant_rows(self, catalog: str, schema: str) -> dict[str, dict[str, list[str]]]:
        """Privileges granted on each table directly — not inherited from above.

        A grant on the schema or catalog shows up here too, marked with where it
        came from. Those aren't the table's to manage, so they are skipped.
        TODO(verify): `inherited_from` values against a live workspace.
        https://docs.databricks.com/aws/en/sql/language-manual/information-schema/table_privileges
        """
        rows = self.runner.query(
            "SELECT table_name, grantee, privilege_type, inherited_from "
            f"FROM {_information_schema(catalog)}.table_privileges "
            f"WHERE table_schema = {quote_literal(schema)}"
        )
        grants: dict[str, dict[str, list[str]]] = {}
        for row in rows:
            table_name, grantee, privilege = (
                row.get("table_name"),
                row.get("grantee"),
                row.get("privilege_type"),
            )
            if table_name is None or grantee is None or privilege is None:
                continue
            if (row.get("inherited_from") or "NONE").upper() != "NONE":
                continue
            grants.setdefault(table_name, {}).setdefault(grantee, []).append(
                normalise_privilege(privilege)
            )
        return grants

    def _constraint_rows(self, catalog: str, schema: str) -> dict[str, list[Constraint]]:
        """Primary and foreign keys. CHECK constraints aren't here: Delta keeps
        them as `delta.constraints.<name>` table properties, and neither
        table_constraints nor check_constraints lists them — verified live. They
        are read from DESCRIBE DETAIL instead (`_checks`)."""
        rows = self.runner.query(
            "SELECT table_name, constraint_name, constraint_type "
            f"FROM {_information_schema(catalog)}.table_constraints "
            f"WHERE table_schema = {quote_literal(schema)}"
        )
        keys = self._key_usage(catalog, schema)
        references = (
            self._references(catalog, schema)
            if any(
                (r.get("constraint_type") or "").upper() == "FOREIGN KEY" for r in rows
            )
            else {}
        )

        constraints: dict[str, list[Constraint]] = {}
        for row in rows:
            table_name = row.get("table_name")
            name = row.get("constraint_name")
            kind = (row.get("constraint_type") or "").upper()
            if table_name is None or name is None:
                continue
            if kind == "PRIMARY KEY":
                constraints.setdefault(table_name, []).append(
                    PrimaryKey(tuple(keys.get(name, ("", []))[1]), name)
                )
            elif kind == "FOREIGN KEY" and name in references:
                referenced_table, referenced_columns = references[name]
                constraints.setdefault(table_name, []).append(
                    ForeignKey(
                        tuple(keys.get(name, ("", []))[1]),
                        referenced_table,
                        referenced_columns,
                        name,
                    )
                )
        return constraints

    def _key_usage(self, catalog: str, schema: str) -> dict[str, tuple[str, list[str]]]:
        """constraint name -> (table, columns in order), for one schema. Cached,
        because a foreign key sends us looking in the schema it references."""
        cache_key = f"{catalog}.{schema}".lower()
        if cache_key in self._keys_cache:
            return self._keys_cache[cache_key]
        rows = self.runner.query(
            "SELECT table_name, constraint_name, column_name "
            f"FROM {_information_schema(catalog)}.key_column_usage "
            f"WHERE table_schema = {quote_literal(schema)} "
            "ORDER BY table_name, constraint_name, ordinal_position"
        )
        found: dict[str, tuple[str, list[str]]] = {}
        for row in rows:
            name, table_name, column = (
                row.get("constraint_name"),
                row.get("table_name"),
                row.get("column_name"),
            )
            if name is None or table_name is None or column is None:
                continue
            found.setdefault(name, (table_name, []))[1].append(column)
        self._keys_cache[cache_key] = found
        return found

    def _references(
        self, catalog: str, schema: str
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        """Foreign key name -> (referenced table, referenced columns).

        TODO(verify): referential_constraints column names against a live
        workspace, and that the referenced key's columns are found in its own
        schema's key_column_usage.
        https://docs.databricks.com/aws/en/sql/language-manual/information-schema/referential_constraints
        """
        rows = self.runner.query(
            "SELECT constraint_name, unique_constraint_catalog, "
            "unique_constraint_schema, unique_constraint_name "
            f"FROM {_information_schema(catalog)}.referential_constraints "
            f"WHERE constraint_schema = {quote_literal(schema)}"
        )
        found: dict[str, tuple[str, tuple[str, ...]]] = {}
        for row in rows:
            name = row.get("constraint_name")
            ref_catalog = row.get("unique_constraint_catalog")
            ref_schema = row.get("unique_constraint_schema")
            ref_name = row.get("unique_constraint_name")
            if not (name and ref_catalog and ref_schema and ref_name):
                continue
            usage = self._key_usage(ref_catalog, ref_schema).get(ref_name)
            if usage is None:
                continue
            ref_table, ref_columns = usage
            found[name] = (f"{ref_catalog}.{ref_schema}.{ref_table}", tuple(ref_columns))
        return found

    def _show_create(self, name: str) -> str | None:
        rows = self.runner.query(f"SHOW CREATE TABLE {quote_qualified(name)}")
        return rows[0].get("createtab_stmt") if rows else None

    def _each(self, items: Sequence[str], read: Callable[[str], T]) -> dict[str, T]:
        """`read` for every item, `parallel` at a time; the results by item.

        TODO(verify): that one WorkspaceClient takes concurrent statement calls —
        the live suite runs this way.
        """
        if self.parallel <= 1 or len(items) <= 1:
            return {item: read(item) for item in items}
        with ThreadPoolExecutor(max_workers=self.parallel) as pool:
            futures = {item: pool.submit(read, item) for item in items}
            return {item: future.result() for item, future in futures.items()}

    def _describe_detail(self, name: str) -> Row:
        if name in self._detail_cache:
            return self._detail_cache[name]
        rows = self.runner.query(f"DESCRIBE DETAIL {quote_qualified(name)}")
        detail = rows[0] if rows else {}
        self._detail_cache[name] = detail
        return detail


# ---------------------------------------------------------------------------
# the real runner
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WarehouseRunner:
    """Runs statements on a SQL warehouse via the Statement Execution API."""

    client: WorkspaceClient
    warehouse_id: str
    wait_timeout: str = "30s"
    poll_seconds: float = 1.0
    timeout_seconds: float = 300.0

    def query(self, statement: str) -> tuple[Row, ...]:
        from databricks.sdk.service.sql import (
            Disposition,
            ExecuteStatementRequestOnWaitTimeout,
            Format,
            StatementState,
        )

        api = self.client.statement_execution
        response = api.execute_statement(
            statement=statement,
            warehouse_id=self.warehouse_id,
            wait_timeout=self.wait_timeout,
            on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
            disposition=Disposition.INLINE,
            format=Format.JSON_ARRAY,
        )

        statement_id = response.statement_id
        if statement_id is None:
            raise IntrospectionError(f"no statement id came back for: {statement}")

        deadline = time.monotonic() + self.timeout_seconds
        while response.status and response.status.state in {
            StatementState.PENDING,
            StatementState.RUNNING,
        }:
            if time.monotonic() > deadline:
                raise IntrospectionError(
                    f"statement timed out after {self.timeout_seconds:.0f}s: {statement}"
                )
            time.sleep(self.poll_seconds)
            response = api.get_statement(statement_id)

        state = response.status.state if response.status else None
        if state is not StatementState.SUCCEEDED:
            message = (
                response.status.error.message
                if response.status and response.status.error
                else "no error message"
            )
            raise IntrospectionError(f"{state}: {message}\n  {statement}")

        names = [
            column.name or f"col{index}"
            for index, column in enumerate(
                (response.manifest.schema.columns or [])
                if response.manifest and response.manifest.schema
                else []
            )
        ]
        rows: list[Row] = []
        result = response.result
        while result is not None:
            for values in result.data_array or []:
                rows.append(dict(zip(names, values, strict=False)))
            if result.next_chunk_index is None:
                break
            result = api.get_statement_result_chunk_n(
                statement_id, result.next_chunk_index
            )
        return tuple(rows)


def warehouse_runner(warehouse_id: str) -> WarehouseRunner:
    """A runner using the SDK's unified auth — profiles, OAuth, env vars."""
    from databricks.sdk import WorkspaceClient

    return WarehouseRunner(WorkspaceClient(), warehouse_id)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _information_schema(catalog: str) -> str:
    return f"{quote_qualified(catalog)}.information_schema"


def _split(name: str) -> tuple[str, str, str]:
    parts = name.split(".")
    if len(parts) != 3:
        raise IntrospectionError(f"expected catalog.schema.table, got {name!r}")
    return parts[0], parts[1], parts[2]


def _parse_live_type(text: str | None, table: str, column: str) -> DataType:
    if not text:
        raise IntrospectionError(f"{table}.{column} has no type in information_schema")
    try:
        return parse_type(text)
    except TypeParseError as error:
        # Better to model the column as an opaque type than to refuse the table:
        # an unparseable type is something deltaplan doesn't manage anyway.
        del error
        return Primitive(text.lower())


def _json_map(value: str | None) -> dict[str, str]:
    """`DESCRIBE DETAIL` returns maps as JSON when the format is JSON_ARRAY."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def _json_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in parsed) if isinstance(parsed, list) else ()


def _unmodelled(detail: Row, column_features: list[str]) -> tuple[str, ...]:
    found: list[str] = []
    partitions = _json_list(detail.get("partitionColumns"))
    if partitions:
        found.append(f"partitioned by ({', '.join(partitions)})")
    found.extend(column_features)
    return tuple(found)


def _with_definition(
    columns: list[Column], statement: str | None
) -> tuple[list[Column], list[str]]:
    """Columns completed from `SHOW CREATE TABLE`: identity, generation,
    default and the full nested type live there and nowhere else — see
    `deltaplan.ddl`. Returns the columns and anything worth reporting."""
    if not statement:
        return columns, ["a definition SHOW CREATE TABLE didn't return"]
    try:
        definitions = {k.casefold(): v for k, v in read_columns(statement).items()}
    except DdlError as error:
        return columns, [f"a definition deltaplan couldn't read ({error})"]
    completed: list[Column] = []
    notes: list[str] = []
    for column in columns:
        found = definitions.get(column.name.casefold())
        if found is None:
            completed.append(column)
            continue
        if found.collation:
            notes.append(f"collation {found.collation} on {column.name}")
        completed.append(
            replace(
                column,
                type=found.type if found.type is not None else column.type,
                identity=found.identity or column.identity,
                generated=found.generated or column.generated,
                default=found.default or column.default,
            )
        )
    return completed, notes


def _checks(properties: dict[str, str]) -> tuple[Check, ...]:
    """CHECK constraints, from the `delta.constraints.<name>` properties Delta
    keeps them in. The value is the expression as written."""
    return tuple(
        Check(key.removeprefix(CHECK_PROPERTY_PREFIX), normalise_expression(value))
        for key, value in sorted(properties.items())
        if key.startswith(CHECK_PROPERTY_PREFIX)
    )


def _name_list(value: str | None) -> tuple[str, ...]:
    """Column names, whether the catalog returns a JSON array or a plain list.

    Decided by the shape of the text, not by whether parsing found anything: an
    empty array is an empty list, not a column called `[]`.
    """
    if not value:
        return ()
    text = value.strip()
    if text.startswith("["):
        return _json_list(text)
    return tuple(name.strip() for name in text.split(",") if name.strip())


def _pairs(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(values.items()))


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
