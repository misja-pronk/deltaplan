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
from typing import TypeVar

from deltaplan.model.function import Function, Parameter
from deltaplan.model.schema import Schema
from deltaplan.model.table import (
    FEATURE_FLAG_PREFIX,
    Check,
    Constraint,
    ForeignKey,
    Grant,
    PrimaryKey,
    RowFilter,
    Table,
    default_foreign_key_name,
)
from deltaplan.model.types import (
    Array,
    DataType,
    Field,
    Identity,
    Map,
    Mask,
    Struct,
    contains_timestamp_ntz,
    walk,
)
from deltaplan.model.view import View
from deltaplan.model.volume import Volume
from deltaplan.sql import needs_name_mapping, referenced_columns
from deltaplan.typeparser import parse_type

Row = dict[str, str | None]
Fields = tuple[Field, ...]

NTZ_FEATURE = "delta.feature.timestampNtz"


#: The columns of each information_schema view deltaplan reads, as a live
#: workspace lists them (read 2026-09-18 from `information_schema.columns`).
#: The fake refuses a query naming anything else — an invented column is how
#: the first live run failed, and the fake had happily answered it.
INFORMATION_SCHEMA_COLUMNS: dict[str, frozenset[str]] = {
    view: frozenset(columns.split())
    for view, columns in {
        "tables": "table_catalog table_schema table_name table_type "
        "is_insertable_into commit_action table_owner comment created created_by "
        "last_altered last_altered_by data_source_format storage_sub_directory "
        "storage_path",
        "columns": "table_catalog table_schema table_name column_name "
        "ordinal_position column_default is_nullable full_data_type data_type "
        "character_maximum_length character_octet_length numeric_precision "
        "numeric_precision_radix numeric_scale datetime_precision interval_type "
        "interval_precision maximum_cardinality is_identity identity_generation "
        "identity_start identity_increment identity_maximum identity_minimum "
        "identity_cycle is_generated generation_expression "
        "is_system_time_period_start is_system_time_period_end "
        "system_time_period_timestamp_generation is_updatable partition_index "
        "comment collation_catalog collation_schema collation_name",
        "views": "table_catalog table_schema table_name view_definition "
        "check_option is_updatable is_insertable_into sql_path is_materialized",
        "column_masks": "table_catalog table_schema table_name column_name "
        "mask_name using_columns",
        "row_filters": "table_catalog table_schema table_name filter_name target_columns",
        "table_tags": "catalog_name schema_name table_name tag_name tag_value",
        "column_tags": "catalog_name schema_name table_name column_name tag_name "
        "tag_value",
        "table_privileges": "grantor grantee table_catalog table_schema table_name "
        "privilege_type is_grantable inherited_from",
        "table_constraints": "constraint_catalog constraint_schema constraint_name "
        "table_catalog table_schema table_name constraint_type is_deferrable "
        "initially_deferred enforced",
        "check_constraints": "constraint_catalog constraint_schema constraint_name "
        "check_clause sql_path comment",
        "key_column_usage": "constraint_catalog constraint_schema constraint_name "
        "table_catalog table_schema table_name column_name ordinal_position "
        "position_in_unique_constraint",
        "referential_constraints": "constraint_catalog constraint_schema "
        "constraint_name unique_constraint_catalog unique_constraint_schema "
        "unique_constraint_name match_option update_rule delete_rule",
        "routines": "specific_catalog specific_schema specific_name routine_catalog "
        "routine_schema routine_name routine_owner routine_type data_type "
        "full_data_type character_maximum_length character_octet_length "
        "numeric_precision numeric_precision_radix numeric_scale datetime_precision "
        "interval_type interval_precision maximum_cardinality routine_body "
        "routine_definition external_name external_language parameter_style "
        "is_deterministic sql_data_access is_null_call security_type sql_path "
        "comment created created_by last_altered last_altered_by collation_catalog "
        "collation_schema collation_name",
        "parameters": "specific_catalog specific_schema specific_name "
        "ordinal_position parameter_mode is_result as_locator parameter_name "
        "data_type full_data_type character_maximum_length character_octet_length "
        "numeric_precision numeric_precision_radix numeric_scale datetime_precision "
        "interval_type interval_precision maximum_cardinality parameter_default "
        "comment collation_catalog collation_schema collation_name",
        "routine_privileges": "grantor grantee specific_catalog specific_schema "
        "specific_name routine_catalog routine_schema routine_name privilege_type "
        "is_grantable inherited_from",
        "schemata": "catalog_name schema_name schema_owner comment created "
        "created_by last_altered last_altered_by url custom_max_retention_hours",
        "schema_tags": "catalog_name schema_name tag_name tag_value",
        "volumes": "volume_catalog volume_schema volume_name volume_type volume_owner "
        "comment storage_location created created_by last_altered last_altered_by",
        "volume_tags": "catalog_name schema_name volume_name tag_name tag_value",
        "volume_privileges": "grantor grantee volume_catalog volume_schema volume_name "
        "privilege_type is_grantable inherited_from",
        "schema_privileges": "grantor grantee catalog_name schema_name privilege_type "
        "is_grantable inherited_from",
    }.items()
}


class FakeSqlError(Exception):
    """The fake doesn't know this statement — or the statement is wrong."""


@dataclass
class FakeWarehouse:
    """A `SqlRunner` backed by models rather than a database."""

    tables: dict[str, Table] = field(default_factory=dict)
    views: dict[str, View] = field(default_factory=dict)
    functions: dict[str, Function] = field(default_factory=dict)
    #: A schema's comment, tags and grants, by `catalog.schema`.
    schema_defs: dict[str, Schema] = field(default_factory=dict)
    volumes: dict[str, Volume] = field(default_factory=dict)
    #: Volumes with a LOCATION: listed, never managed.
    external_volumes: set[str] = field(default_factory=set)
    #: Schemas that exist even with nothing in them. A schema holding a table or
    #: view exists regardless.
    schemas: set[str] = field(default_factory=set)
    sizes: dict[str, int] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    #: Table -> partition columns: partitioning isn't in deltaplan's model, so it
    #: lives here rather than on the Table.
    partitions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: (table, column) -> extra information_schema.columns values, for the column
    #: features deltaplan doesn't model (identity, generated, default).
    column_features: dict[tuple[str, str], Row] = field(default_factory=dict)
    #: Substring -> error message, so a test can make any statement fail.
    failures: dict[str, str] = field(default_factory=dict)
    #: What a `blocked` precheck should answer.
    blocked: bool = False
    #: What an `ok` postcheck should answer — the fake has no rows to count.
    postcheck_ok: bool = True
    statements: list[str] = field(default_factory=list)

    @classmethod
    def of(
        cls,
        *relations: Table | View | Function | Schema | Volume,
        sizes: dict[str, int] | None = None,
    ) -> FakeWarehouse:
        fake = cls(sizes=sizes or {})
        for relation in relations:
            if isinstance(relation, View):
                fake.views[relation.name] = relation
                continue
            if isinstance(relation, Function):
                fake.functions[relation.name] = relation
                continue
            if isinstance(relation, Schema):
                fake.schemas.add(relation.name)
                fake.schema_defs[relation.name] = relation
                continue
            if isinstance(relation, Volume):
                fake.volumes[relation.name] = relation
                continue
            fake.tables[relation.name] = relation
            fake.versions.setdefault(relation.name, 1)
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
            s
            for s in self.statements
            if not s.upper().startswith(("SELECT", "DESCRIBE", "SHOW"))
        ]

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, flat: str, original: str) -> tuple[Row, ...]:
        upper = flat.upper()
        if "AS BLOCKED" in upper:
            return ({"blocked": "true" if self.blocked else "false"},)
        if upper.startswith("SELECT (") and upper.endswith(") AS OK"):
            return ({"ok": "true" if self.postcheck_ok else "false"},)
        if match := re.fullmatch(r"SELECT (true|false) AS (\w+)", flat, re.IGNORECASE):
            return ({match.group(2): match.group(1).lower()},)
        if upper.startswith("SELECT"):
            return self._information_schema(flat)
        if upper.startswith("DESCRIBE DETAIL"):
            return self._describe_detail(flat)
        if upper.startswith("DESCRIBE HISTORY"):
            return self._describe_history(flat)
        if upper.startswith(("CREATE VIEW", "CREATE OR REPLACE VIEW")):
            return self._create_view(original)
        if upper.startswith(("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")):
            return self._create_function(original)
        if upper.startswith("DROP FUNCTION"):
            self.functions.pop(_unquote(flat[len("DROP FUNCTION ") :]), None)
            return ()
        if upper.startswith("DROP VIEW"):
            self.views.pop(_unquote(flat[len("DROP VIEW ") :]), None)
            return ()
        if upper.startswith("ALTER VIEW"):
            return self._alter_view(flat)
        if upper.startswith("SHOW CREATE TABLE"):
            table = self._table(_unquote(flat[len("SHOW CREATE TABLE ") :]))
            return ({"createtab_stmt": _show_create(table)},)
        if upper.startswith("SHOW TBLPROPERTIES"):
            view = self._view(_unquote(flat[len("SHOW TBLPROPERTIES ") :]))
            return tuple({"key": k, "value": v} for k, v in view.properties)
        if upper.startswith(("CREATE TABLE", "CREATE OR REPLACE TABLE")):
            return self._create_table(original)
        if upper.startswith("DROP TABLE"):
            return self._drop_table(flat)
        if upper.startswith(
            ("INSERT OVERWRITE", "INSERT INTO", "UPDATE ", "DELETE FROM ")
        ):
            # The fake models schemas, not rows: moving data is a no-op here, and
            # whether it is *valid* is a question only a warehouse can answer.
            return ()
        if upper.startswith("CREATE SCHEMA"):
            match = re.fullmatch(
                r"CREATE SCHEMA IF NOT EXISTS (\S+)(?: COMMENT ('(?:[^'\\]|\\.)*'))?",
                flat,
            )
            if match is None:
                raise FakeSqlError(f"cannot read: {flat}")
            name = _unquote(match.group(1)).lower()
            if name not in self.schemas and match.group(2):
                self.schema_defs[name] = Schema(name, _unliteral(match.group(2)))
            self.schemas.add(name)
            return ()
        if upper.startswith("CREATE VOLUME"):
            match = re.fullmatch(
                r"CREATE VOLUME IF NOT EXISTS (\S+)(?: COMMENT ('(?:[^'\\]|\\.)*'))?",
                flat,
            )
            if match is None:
                raise FakeSqlError(f"cannot read: {flat}")
            name = _unquote(match.group(1)).lower()
            self._schema_def(name.rsplit(".", 1)[0])
            if name not in self.volumes:
                comment = _unliteral(match.group(2)) if match.group(2) else None
                self.volumes[name] = Volume(name, comment)
            return ()
        if upper.startswith("COMMENT ON VOLUME"):
            match = re.fullmatch(r"COMMENT ON VOLUME (\S+) IS (.+)", flat)
            if match is None:
                raise FakeSqlError(f"cannot read: {flat}")
            volume = self._volume(_unquote(match.group(1)))
            comment = None if match.group(2) == "NULL" else _unliteral(match.group(2))
            self.volumes[volume.name] = replace(volume, comment=comment)
            return ()
        if upper.startswith("ALTER VOLUME"):
            match = re.fullmatch(r"ALTER VOLUME (\S+) SET TAGS \((.*)\)", flat)
            if match is None:
                raise FakeSqlError(f"cannot read: {flat}")
            volume = self._volume(_unquote(match.group(1)))
            tags = dict(volume.tags) | _pairs(match.group(2))
            self.volumes[volume.name] = replace(volume, tags=tuple(sorted(tags.items())))
            return ()
        if upper.startswith("COMMENT ON SCHEMA"):
            match = re.fullmatch(r"COMMENT ON SCHEMA (\S+) IS (.+)", flat)
            if match is None:
                raise FakeSqlError(f"cannot read: {flat}")
            name = _unquote(match.group(1)).lower()
            comment = None if match.group(2) == "NULL" else _unliteral(match.group(2))
            self.schema_defs[name] = replace(self._schema_def(name), comment=comment)
            return ()
        if upper.startswith("ALTER SCHEMA"):
            match = re.fullmatch(r"ALTER SCHEMA (\S+) SET TAGS \((.*)\)", flat)
            if match is None:
                raise FakeSqlError(f"cannot read: {flat}")
            name = _unquote(match.group(1)).lower()
            current = self._schema_def(name)
            tags = dict(current.tags) | _pairs(match.group(2))
            self.schema_defs[name] = replace(current, tags=tuple(sorted(tags.items())))
            return ()
        if upper.startswith("DROP SCHEMA"):
            return ()
        if upper.startswith("COMMENT ON TABLE"):
            return self._comment_on_table(flat)
        if upper.startswith("ALTER TABLE"):
            return self._alter_table(flat)
        if upper.startswith("GRANT ") or upper.startswith("REVOKE "):
            return self._grant(flat)
        raise FakeSqlError(f"the fake warehouse does not know this statement: {flat}")

    # -- reads -------------------------------------------------------------
    def _information_schema(self, flat: str) -> tuple[Row, ...]:
        _check_columns(flat)
        schema = (
            _literal_after(flat, "table_schema = ")
            or _literal_after(flat, "schema_name = ")
            or _literal_after(flat, "constraint_schema = ")
            or _literal_after(flat, "routine_schema = ")
            or _literal_after(flat, "specific_schema = ")
            or _literal_after(flat, "volume_schema = ")
        )
        if schema is None:
            raise FakeSqlError(f"no schema filter in: {flat}")
        catalog = _unquote(flat.split(".information_schema")[0].split("FROM ")[1])
        tables = [
            table
            for name, table in sorted(self.tables.items())
            if name.startswith(f"{catalog}.{schema}.")
        ]
        views = [
            view
            for name, view in sorted(self.views.items())
            if name.startswith(f"{catalog}.{schema}.")
        ]
        functions = [
            function
            for name, function in sorted(self.functions.items())
            if name.startswith(f"{catalog}.{schema}.")
        ]
        governed: list[Table | View] = [*tables, *views]
        if "information_schema.schemata" in flat:
            name = f"{catalog}.{schema}".lower()
            present = name in self.schemas or any(
                other.startswith(f"{name}.")
                for other in [*self.tables, *self.views, *self.functions, *self.volumes]
            )
            if not present:
                return ()
            return ({"schema_name": schema, "comment": self._schema_def(name).comment},)
        in_schema = [
            v
            for n, v in sorted(self.volumes.items())
            if n.startswith(f"{catalog}.{schema}.")
        ]
        if "information_schema.volumes" in flat:
            external = [
                {
                    "volume_name": n.rsplit(".", 1)[1],
                    "volume_type": "EXTERNAL",
                    "comment": None,
                }
                for n in sorted(self.external_volumes)
                if n.startswith(f"{catalog}.{schema}.")
            ]
            return tuple(
                {
                    "volume_name": v.short_name,
                    "volume_type": "MANAGED",
                    "comment": v.comment,
                }
                for v in in_schema
            ) + tuple(external)
        if "information_schema.volume_tags" in flat:
            return tuple(
                {"volume_name": v.short_name, "tag_name": k, "tag_value": value}
                for v in in_schema
                for k, value in v.tags
            )
        if "information_schema.volume_privileges" in flat:
            return tuple(
                {
                    "volume_name": v.short_name,
                    "grantee": grant.principal,
                    "privilege_type": privilege.replace(" ", "_"),
                    "inherited_from": "NONE",
                }
                for v in in_schema
                for grant in v.grants
                for privilege in grant.privileges
            )
        if "information_schema.schema_tags" in flat:
            return tuple(
                {"schema_name": schema, "tag_name": k, "tag_value": v}
                for k, v in self._schema_def(f"{catalog}.{schema}".lower()).tags
            )
        if "information_schema.schema_privileges" in flat:
            # Underscored, as a warehouse answers — verified live.
            return tuple(
                {
                    "grantee": grant.principal,
                    "privilege_type": privilege.replace(" ", "_"),
                    "inherited_from": "NONE",
                }
                for grant in self._schema_def(f"{catalog}.{schema}".lower()).grants
                for privilege in grant.privileges
            )
        if "information_schema.routines" in flat:
            return tuple(
                {
                    "routine_name": function.short_name,
                    "routine_definition": function.body,
                    "full_data_type": _render(function.returns),
                    "comment": function.comment,
                }
                for function in functions
            )
        if "information_schema.parameters" in flat:
            return tuple(
                {
                    "specific_name": function.short_name,
                    "parameter_name": parameter.name,
                    "ordinal_position": str(position),
                    "full_data_type": _render(parameter.type),
                }
                for function in functions
                for position, parameter in enumerate(function.parameters)
            )
        if "information_schema.routine_privileges" in flat:
            return tuple(
                {
                    "routine_name": function.short_name,
                    "grantee": grant.principal,
                    "privilege_type": privilege,
                    "inherited_from": "NONE",
                }
                for function in functions
                for grant in function.grants
                for privilege in grant.privileges
            )
        if "information_schema.tables" in flat:
            return tuple(
                {
                    "table_name": table.short_name,
                    "comment": table.comment,
                    "table_type": "MANAGED",
                    "data_source_format": "DELTA",
                }
                for table in tables
            ) + tuple(
                {
                    "table_name": view.short_name,
                    "comment": view.comment,
                    "table_type": "VIEW",
                    "data_source_format": None,
                }
                for view in views
            )
        if "information_schema.views" in flat:
            return tuple(
                {"table_name": view.short_name, "view_definition": view.query}
                for view in views
            )
        if "information_schema.columns" in flat:
            return tuple(
                {
                    "table_name": table.short_name,
                    "column_name": column.name,
                    "ordinal_position": str(position),
                    # As a warehouse answers: no NOT NULL or comments inside
                    # structs, and nothing about identity, generation or
                    # defaults — those are in SHOW CREATE TABLE. Verified live.
                    "full_data_type": _render(_bare(column.type)),
                    "is_nullable": "YES" if column.nullable else "NO",
                    "comment": column.comment,
                    "is_identity": "NO",
                    "is_generated": "NO",
                    **self.column_features.get((table.name, column.name), {}),
                }
                for table in tables
                for position, column in enumerate(table.columns, start=1)
            )
        if "information_schema.column_tags" in flat:
            return tuple(
                {
                    "table_name": table.short_name,
                    "column_name": column.name,
                    "tag_name": key,
                    "tag_value": value,
                }
                for table in tables
                for column in table.columns
                for key, value in column.tags
            )
        if "information_schema.column_masks" in flat:
            rows: list[Row] = []
            for table in tables:
                for column in table.columns:
                    if column.mask is None:
                        continue
                    rows.append(
                        {
                            "table_name": table.short_name,
                            "column_name": column.name,
                            "mask_name": column.mask.function,
                            "using_columns": ", ".join(column.mask.using_columns) or None,
                        }
                    )
            return tuple(rows)
        if "information_schema.row_filters" in flat:
            filtered: list[Row] = []
            for table in tables:
                if table.row_filter is None:
                    continue
                filtered.append(
                    {
                        "table_name": table.short_name,
                        "filter_name": table.row_filter.function,
                        "target_columns": ", ".join(table.row_filter.columns),
                    }
                )
            return tuple(filtered)
        if "information_schema.table_privileges" in flat:
            return tuple(
                {
                    "table_name": table.short_name,
                    "grantee": grant.principal,
                    "privilege_type": privilege,
                    "inherited_from": "NONE",
                }
                for table in governed
                for grant in table.grants
                for privilege in grant.privileges
            )
        if "information_schema.table_tags" in flat:
            return tuple(
                {"table_name": table.short_name, "tag_name": key, "tag_value": value}
                for table in governed
                for key, value in table.tags
            )
        if "information_schema.table_constraints" in flat:
            # Keys only: Delta keeps CHECKs as table properties, and
            # information_schema doesn't list them — verified live.
            return tuple(
                _constraint_row(table, constraint)
                for table in tables
                for constraint in table.constraints
                if not isinstance(constraint, Check)
            )
        if "information_schema.key_column_usage" in flat:
            rows: list[Row] = []
            for table in tables:
                keyed: list[tuple[str, tuple[str, ...]]] = []
                key = table.primary_key()
                if key is not None:
                    keyed.append((key.name or f"{table.short_name}_pk", key.columns))
                for foreign in table.foreign_keys():
                    keyed.append((_constraint_name(foreign, table), foreign.columns))
                for name, columns in keyed:
                    for position, column in enumerate(columns, start=1):
                        rows.append(
                            {
                                "table_name": table.short_name,
                                "constraint_name": name,
                                "column_name": column,
                                "ordinal_position": str(position),
                            }
                        )
            return tuple(rows)
        if "information_schema.referential_constraints" in flat:
            found: list[Row] = []
            for table in tables:
                for foreign in table.foreign_keys():
                    target = self.tables.get(foreign.references)
                    target_key = target.primary_key() if target else None
                    if target is None or target_key is None:
                        continue
                    ref_catalog, ref_schema, _ = foreign.references.split(".")
                    found.append(
                        {
                            "constraint_name": _constraint_name(foreign, table),
                            "unique_constraint_catalog": ref_catalog,
                            "unique_constraint_schema": ref_schema,
                            "unique_constraint_name": target_key.name
                            or f"{target.short_name}_pk",
                        }
                    )
            return tuple(found)
        raise FakeSqlError(f"unknown information_schema query: {flat}")

    def _describe_detail(self, flat: str) -> tuple[Row, ...]:
        table = self._table(_unquote(flat[len("DESCRIBE DETAIL ") :]))
        return (
            {
                "format": "delta",
                "name": table.name,
                "clusteringColumns": json.dumps(list(table.cluster_by)),
                "clusterByAuto": "true" if table.cluster_auto else "false",
                "partitionColumns": json.dumps(list(self.partitions.get(table.name, ()))),
                "sizeInBytes": str(self.sizes.get(table.name, 0)),
                # As a warehouse answers: table features in their own list, not
                # among the properties.
                "properties": json.dumps(
                    {
                        **{
                            k: v
                            for k, v in table.properties
                            if not k.startswith(FEATURE_FLAG_PREFIX)
                        },
                        # Where Delta keeps a CHECK constraint.
                        **{
                            f"delta.constraints.{c.name}": c.expression
                            for c in table.constraints
                            if isinstance(c, Check)
                        },
                    }
                ),
                "tableFeatures": json.dumps(
                    [
                        k.removeprefix(FEATURE_FLAG_PREFIX)
                        for k, _ in table.properties
                        if k.startswith(FEATURE_FLAG_PREFIX)
                    ]
                ),
            },
        )

    def _describe_history(self, flat: str) -> tuple[Row, ...]:
        name = _unquote(flat[len("DESCRIBE HISTORY ") :].removesuffix(" LIMIT 1"))
        return ({"version": str(self.versions.get(name, 0))},)

    # -- writes ------------------------------------------------------------
    def _create_table(self, statement: str) -> tuple[Row, ...]:
        stripped = statement.strip()
        replacing = stripped.upper().startswith("CREATE OR REPLACE TABLE")
        if match := re.fullmatch(
            r"CREATE OR REPLACE TABLE (\S+) SHALLOW CLONE (\S+)"
            r"(?: TBLPROPERTIES \((.*)\))?",
            stripped,
        ):
            # A clone copies the source's properties; TBLPROPERTIES overrides
            # them — both verified live.
            source = self._table(_unquote(match.group(2)))
            overrides = _pairs(match.group(3)) if match.group(3) else {}
            properties = dict(source.properties) | overrides
            clone = replace(
                source,
                name=_unquote(match.group(1)),
                properties=tuple(properties.items()),
            )
            self.tables[clone.name] = clone
            self.versions[clone.name] = 0
            return ()
        if re.search(r"\bAS\s+SELECT\b", stripped):
            table = self._parse_ctas(stripped)
        else:
            table = _parse_create_table(stripped)
        if table.name in self.tables and not replacing:
            return ()  # IF NOT EXISTS
        if any(contains_timestamp_ntz(c.type) for c in table.columns):
            # CREATE turns the feature on by itself; ALTER doesn't (see below).
            table = replace(
                table,
                properties=(*table.properties, (NTZ_FEATURE, "supported")),
            )
        _enforce_delta_rules(table)
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
            cluster_auto=bool(match.group("auto")),
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
        if rename := re.fullmatch(r"RENAME TO (\S+)", match.group(2)):
            return self._rename_table(table, _unquote(rename.group(1)))
        clause = match.group(2)
        if (
            re.match(r"(ADD COLUMNS|ALTER COLUMN \S+ TYPE) ", clause)
            and "TIMESTAMP_NTZ" in clause.upper()
            and NTZ_FEATURE not in table.properties_map()
        ):
            # As a warehouse does — verified live.
            raise FakeSqlError(
                "[DELTA_FEATURES_REQUIRE_MANUAL_ENABLEMENT] Your table schema "
                "requires manually enablement of the following table feature(s): "
                "timestampNtz"
            )
        _refuse_dependent_change(table, clause)
        altered = _apply_alter(table, clause)
        _enforce_delta_rules(altered)
        self._store(altered)
        return ()

    def _rename_table(self, table: Table, new_name: str) -> tuple[Row, ...]:
        """A rename moves the table — and what the fake keeps beside it."""
        if table.name.rsplit(".", 1)[0] != new_name.rsplit(".", 1)[0]:
            raise FakeSqlError(f"a rename stays in its schema: {new_name}")
        if new_name in self.tables or new_name in self.views:
            raise FakeSqlError(f"{new_name} already exists")
        del self.tables[table.name]
        _move_key(self.sizes, table.name, new_name)
        _move_key(self.versions, table.name, new_name)
        _move_key(self.partitions, table.name, new_name)
        for (owner, column), row in list(self.column_features.items()):
            if owner == table.name:
                self.column_features[(new_name, column)] = row
                del self.column_features[(owner, column)]
        self._store(replace(table, name=new_name))
        return ()

    def _grant(self, flat: str) -> tuple[Row, ...]:
        match = re.fullmatch(
            r"(GRANT|REVOKE) (.+) ON (TABLE|VIEW|FUNCTION|SCHEMA|VOLUME) (\S+) "
            r"(?:TO|FROM) (\S+)",
            flat,
        )
        if match is None:
            raise FakeSqlError(f"cannot read: {flat}")
        verb, privileges, kind, name, principal = match.groups()
        target = _unquote(name).lower()
        table: Table | View | Function | Schema | Volume
        if kind == "SCHEMA":
            table = self._schema_def(target)
        elif kind == "VOLUME":
            table = self._volume(target)
        elif target in self.functions:
            table = self.functions[target]
        elif target in self.views:
            table = self.views[target]
        else:
            table = self._table(target)
        who = _unquote(principal)
        held = dict(table.grants_map())
        changed = {p.strip() for p in privileges.split(",")}
        current = set(held.get(who, ()))
        current = current | changed if verb == "GRANT" else current - changed
        if current:
            held[who] = tuple(current)
        else:
            held.pop(who, None)
        updated = replace(
            table, grants=tuple(Grant(p, tuple(v)) for p, v in held.items())
        )
        if isinstance(updated, Schema):
            self.schema_defs[updated.name] = updated
        elif isinstance(updated, Volume):
            self.volumes[updated.name] = updated
        elif isinstance(updated, Function):
            self.functions[updated.name] = updated
        elif isinstance(updated, View):
            self.views[updated.name] = updated
        else:
            self._store(updated)
        return ()

    def _volume(self, name: str) -> Volume:
        name = name.lower()
        if name not in self.volumes:
            raise FakeSqlError(f"no such volume: {name}")
        return self.volumes[name]

    def _schema_def(self, name: str) -> Schema:
        if name not in self.schemas and not any(
            other.startswith(f"{name}.")
            for other in [*self.tables, *self.views, *self.functions, *self.volumes]
        ):
            raise FakeSqlError(f"no such schema: {name}")
        return self.schema_defs.get(name, Schema(name))

    def _create_function(self, statement: str) -> tuple[Row, ...]:
        match = _FUNCTION.fullmatch(statement.strip())
        if match is None:
            raise FakeSqlError(f"cannot read CREATE FUNCTION:\n{statement}")
        name = _unquote(match.group("name"))
        if match.group("verb").endswith("IF NOT EXISTS") and name in self.functions:
            return ()
        existing = self.functions.get(name)
        parameters = tuple(
            Parameter(
                _unquote(entry.split(" ", 1)[0]), parse_type(entry.split(" ", 1)[1])
            )
            for entry in _split_args(match.group("parameters"))
            if entry.strip()
        )
        self.functions[name] = Function(
            name=name,
            parameters=parameters,
            returns=parse_type(match.group("returns")),
            body=match.group("body"),
            comment=_unliteral(match.group("comment"))
            if match.group("comment")
            else None,
            # A replace keeps the grants in the fake; the planner puts them back
            # anyway, so either behaviour converges.
            grants=existing.grants if existing else (),
        )
        return ()

    def _create_view(self, statement: str) -> tuple[Row, ...]:
        match = _VIEW.fullmatch(statement.strip())
        if match is None:
            raise FakeSqlError(f"cannot read CREATE VIEW:\n{statement}")
        name = _unquote(match.group("name"))
        if name in self.tables:
            raise FakeSqlError(f"{name} is a table")
        if match.group("verb").endswith("IF NOT EXISTS") and name in self.views:
            return ()
        existing = self.views.get(name)
        self.views[name] = View(
            name=name,
            query=match.group("query"),
            comment=_unliteral(match.group("comment"))
            if match.group("comment")
            else None,
            properties=tuple(_pairs(match.group("properties")).items()),
            # A replace keeps the view's tags and grants in the fake; the planner
            # puts them back anyway, so either behaviour converges.
            tags=existing.tags if existing else (),
            grants=existing.grants if existing else (),
        )
        return ()

    def _alter_view(self, flat: str) -> tuple[Row, ...]:
        match = re.fullmatch(r"ALTER VIEW (\S+) SET (TBLPROPERTIES|TAGS) \((.*)\)", flat)
        if match is None:
            raise FakeSqlError(f"the fake warehouse does not know this: {flat}")
        view = self._view(_unquote(match.group(1)))
        added = _pairs(match.group(3))
        if match.group(2) == "TAGS":
            view = replace(view, tags=tuple((dict(view.tags) | added).items()))
        else:
            view = replace(
                view, properties=tuple((dict(view.properties) | added).items())
            )
        self.views[view.name] = view
        return ()

    def _view(self, name: str) -> View:
        name = name.lower()
        if name not in self.views:
            raise FakeSqlError(f"no such view: {name}")
        return self.views[name]

    # -- plumbing ----------------------------------------------------------
    def _table(self, name: str) -> Table:
        name = name.lower()
        if name not in self.tables:
            raise FakeSqlError(f"no such table: {name}")
        return self.tables[name]

    def _store(self, table: Table) -> None:
        self.tables[table.name] = table
        self.versions[table.name] = self.versions.get(table.name, 0) + 1


# ---------------------------------------------------------------------------
# statement interpretation
# ---------------------------------------------------------------------------


_V = TypeVar("_V")


_IDENTIFIER = re.compile(r"\b(?:\w+\.)?([a-z_][a-z0-9_]*)\b")
_SQL_WORDS = frozenset(
    [
        "select",
        "from",
        "where",
        "and",
        "or",
        "order",
        "by",
        "on",
        "left",
        "join",
        "as",
        "asc",
        "desc",
        "in",
        "is",
        "not",
        "null",
        "true",
        "false",
    ]
)


def _check_columns(flat: str) -> None:
    """Every column an information_schema query names must exist in a view it
    reads. Literals are skipped; so are SQL keywords and the views' own names."""
    views = re.findall(r"information_schema\.(\w+)", flat)
    unknown_views = [v for v in views if v not in INFORMATION_SCHEMA_COLUMNS]
    if unknown_views:
        raise FakeSqlError(f"no such information_schema view: {unknown_views}")
    allowed = frozenset().union(*(INFORMATION_SCHEMA_COLUMNS[v] for v in views))
    code = re.sub(r"'(?:[^'\\]|\\.|'')*'|`[^`]*`", " ", flat)
    code = re.sub(r"\S*information_schema\.\w+( \w+)?", " ", code)
    for match in _IDENTIFIER.finditer(code.lower()):
        word = match.group(1)
        if word not in _SQL_WORDS and word not in allowed:
            raise FakeSqlError(f"information_schema has no column {word!r}: {flat}")


def _enforce_delta_rules(table: Table) -> None:
    """What Delta refuses in a table's shape — each seen live (2026-09-19)."""
    names = [
        name
        for column in table.columns
        for name in (column.name, *(f.name for _, f in walk(column.type, column.name)))
    ]
    mapped = table.properties_map().get("delta.columnMapping.mode") == "name"
    if not mapped and any(needs_name_mapping(name) for name in names):
        raise FakeSqlError(
            "[DELTA_INVALID_CHARACTERS_IN_COLUMN_NAMES] Found invalid character(s) "
            "in the column names of your schema."
        )
    for column in table.columns:
        if _not_null_inside_collection(column.type, inside=False):
            raise FakeSqlError(
                f"[DELTA_NESTED_NOT_NULL_CONSTRAINT] {column.name} contains a NOT NULL "
                "constraint inside an array or map."
            )


def _not_null_inside_collection(data_type: DataType, *, inside: bool) -> bool:
    match data_type:
        case Struct(fields=fields):
            return any(
                (inside and not f.nullable)
                or _not_null_inside_collection(f.type, inside=inside)
                for f in fields
            )
        case Array(element=element):
            return _not_null_inside_collection(element, inside=True)
        case Map(key=key, value=value):
            return _not_null_inside_collection(
                key, inside=True
            ) or _not_null_inside_collection(value, inside=True)
        case _:
            return False


def _refuse_dependent_change(table: Table, clause: str) -> None:
    """Delta won't change the type of, rename or drop a column that a CHECK or a
    generated column uses — seen live (2026-09-19)."""
    match = re.match(
        r"(?:ALTER COLUMN (\S+) TYPE|RENAME COLUMN (\S+) TO|DROP COLUMNS? \(?(\S+?)\)?$)",
        clause,
    )
    if match is None:
        return
    column = _unquote(next(g for g in match.groups() if g).split(".")[0]).casefold()
    for check in table.checks():
        if column in referenced_columns(check.expression):
            raise FakeSqlError(
                f"[DELTA_CONSTRAINT_DEPENDENT_COLUMN_CHANGE] Cannot alter column "
                f"{column}: the check constraint {check.name} uses it"
            )
    for other in table.columns:
        if other.generated and column in referenced_columns(other.generated):
            raise FakeSqlError(
                f"[DELTA_GENERATED_COLUMNS_DEPENDENT_COLUMN_CHANGE] Cannot alter column "
                f"{column}: the generated column {other.name} uses it"
            )


def _move_key(store: dict[str, _V], old: str, new: str) -> None:
    if old in store:
        store[new] = store.pop(old)


def _apply_alter(table: Table, clause: str) -> Table:
    if match := re.fullmatch(r"SET TBLPROPERTIES \((.*)\)", clause):
        properties = dict(table.properties) | _pairs(match.group(1))
        return replace(table, properties=tuple(properties.items()))
    if match := re.fullmatch(r"SET TAGS \((.*)\)", clause):
        return replace(
            table, tags=tuple((dict(table.tags) | _pairs(match.group(1))).items())
        )
    if match := re.fullmatch(r"CLUSTER BY \((.*)\)", clause):
        # Naming keys turns AUTO off — verified live.
        return replace(
            table, cluster_by=tuple(_idents(match.group(1))), cluster_auto=False
        )
    if clause == "CLUSTER BY NONE":
        return replace(table, cluster_by=(), cluster_auto=False)
    if clause == "CLUSTER BY AUTO":
        # The keys stay what they were until Databricks picks its own.
        return replace(table, cluster_auto=True)
    if match := re.fullmatch(r"ADD COLUMNS \((.+)\)", clause):
        return _add_column(table, match.group(1))
    if match := re.fullmatch(r"DROP COLUMN (\S+)", clause):
        path = _unquote(match.group(1))
        return _edit_container(table, path, _drop(_leaf(path)))
    if match := re.fullmatch(r"RENAME COLUMN (\S+) TO (\S+)", clause):
        path, new_name = _unquote(match.group(1)), _unquote(match.group(2))
        return _edit_container(table, path, _rename(_leaf(path), new_name))
    if match := re.fullmatch(r"ALTER COLUMN (\S+) SET DEFAULT (.+)", clause):
        path, default = _unquote(match.group(1)), match.group(2)
        return _edit_container(
            table, path, _amend(_leaf(path), lambda f: replace(f, default=default))
        )
    if match := re.fullmatch(r"ALTER COLUMN (\S+) DROP DEFAULT", clause):
        path = _unquote(match.group(1))
        return _edit_container(
            table, path, _amend(_leaf(path), lambda f: replace(f, default=None))
        )
    if match := re.fullmatch(
        r"ALTER COLUMN (\S+) SET MASK (\S+)(?: USING COLUMNS \((.*)\))?", clause
    ):
        path = _unquote(match.group(1))
        mask = Mask(_unquote(match.group(2)), tuple(_idents(match.group(3) or "")))
        return _edit_container(
            table, path, _amend(_leaf(path), lambda f: replace(f, mask=mask))
        )
    if match := re.fullmatch(r"SET ROW FILTER (\S+) ON \((.*)\)", clause):
        row_filter = RowFilter(_unquote(match.group(1)), tuple(_idents(match.group(2))))
        return replace(table, row_filter=row_filter)
    if match := re.fullmatch(r"ALTER COLUMN (\S+) SET TAGS \((.*)\)", clause):
        path = _unquote(match.group(1))
        added = _pairs(match.group(2))
        return _edit_container(
            table,
            path,
            _amend(
                _leaf(path),
                lambda f: replace(f, tags=tuple((dict(f.tags) | added).items())),
            ),
        )
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
    if match := re.fullmatch(
        r"ADD CONSTRAINT (\S+) FOREIGN KEY \((.*?)\) REFERENCES (\S+) \((.*)\)", clause
    ):
        key = ForeignKey(
            tuple(_idents(match.group(2))),
            _unquote(match.group(3)),
            tuple(_idents(match.group(4))),
            _unquote(match.group(1)),
        )
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
    name, rest = _split_name(definition)
    match = re.fullmatch(r"(.+?)(?: COMMENT (.+))?", rest)
    if match is None:
        raise FakeSqlError(f"cannot read column definition: {definition}")
    path = _unquote(name)
    added = Field(
        _leaf(path),
        parse_type(match.group(1)),
        comment=_unliteral(match.group(2)) if match.group(2) else None,
    )
    return _edit_container(table, path, lambda fields: (*fields, added))


def _split_name(text: str) -> tuple[str, str]:
    """A leading column name or dotted path — backticked parts may hold spaces —
    and what follows it."""
    index = 0
    while True:
        if text.startswith("`", index):
            index += 1
            while index < len(text):
                if text[index] == "`" and text.startswith("``", index):
                    index += 2
                elif text[index] == "`":
                    index += 1
                    break
                else:
                    index += 1
        else:
            match = re.match(r"[^\s.`]+", text[index:])
            if match is None:
                raise FakeSqlError(f"cannot read a name in: {text}")
            index += match.end()
        if not text.startswith(".", index):
            return text[:index], text[index:].lstrip()
        index += 1


def _move(table: Table, name: str, after: str | None) -> Table:
    column = table.column(name)
    if column is None:
        raise FakeSqlError(f"no such column: {name}")
    rest = [c for c in table.columns if not _same(c.name, name)]
    if after is None:
        return replace(table, columns=(column, *rest))
    target = _unquote(after)
    index = next((i for i, c in enumerate(rest) if _same(c.name, target)), None)
    if index is None:
        raise FakeSqlError(f"no such column: {target}")
    return replace(table, columns=(*rest[: index + 1], column, *rest[index + 1 :]))


# -- field-tuple edits ------------------------------------------------------


def _drop(leaf: str) -> Callable[[Fields], Fields]:
    return lambda fields: tuple(f for f in fields if not _same(f.name, leaf))


def _rename(leaf: str, new_name: str) -> Callable[[Fields], Fields]:
    return lambda fields: tuple(
        replace(f, name=new_name) if _same(f.name, leaf) else f for f in fields
    )


def _amend(leaf: str, change: Callable[[Field], Field]) -> Callable[[Fields], Fields]:
    return lambda fields: tuple(change(f) if _same(f.name, leaf) else f for f in fields)


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
                    if _same(f.name, head)
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
                    if _same(f.name, head)
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
        columns=tuple(column if _same(c.name, column.name) else c for c in table.columns),
    )


# -- little parsers ---------------------------------------------------------


def _bare(data_type: DataType) -> DataType:
    """A type as information_schema.columns shows it: nested fields without
    NOT NULL or comments."""
    match data_type:
        case Struct(fields=fields):
            return Struct(tuple(Field(f.name, _bare(f.type)) for f in fields))
        case Array(element=element, contains_null=contains_null):
            return Array(_bare(element), contains_null)
        case Map(key=key, value=value):
            return Map(_bare(key), _bare(value))
        case _:
            return data_type


def _show_create(table: Table) -> str:
    """SHOW CREATE TABLE, shaped like a warehouse's (compare tests/unit/test_ddl.py,
    which holds the real thing): COLLATE UTF8_BINARY after every string,
    `( expr )` around a generation, identity with its START and INCREMENT, masks
    and the row filter in place, CHECKs among the properties."""
    lines = [f"  {_ddl_column(column)}" for column in table.columns]
    for constraint in table.constraints:
        if isinstance(constraint, PrimaryKey):
            columns = ", ".join(f"`{c}`" for c in constraint.columns)
            lines.append(
                f"  CONSTRAINT `{_constraint_name(constraint, table)}` "
                f"PRIMARY KEY ({columns})"
            )
        elif isinstance(constraint, ForeignKey):
            columns = ", ".join(f"`{c}`" for c in constraint.columns)
            referenced = ", ".join(f"`{c}`" for c in constraint.referenced_columns)
            lines.append(
                f"  CONSTRAINT `{_constraint_name(constraint, table)}` FOREIGN KEY "
                f"({columns}) REFERENCES {constraint.references} ({referenced})"
            )
    statement = [f"CREATE TABLE {table.name} (", ",\n".join(lines) + ")", "USING delta"]
    if table.row_filter is not None:
        on = ", ".join(table.row_filter.columns)
        statement.append(f"WITH ROW FILTER {table.row_filter.function} ON ({on})")
    if table.comment is not None:
        statement.append(f"COMMENT {_ddl_literal(table.comment)}")
    if table.cluster_auto:
        statement.append("CLUSTER BY AUTO")
    elif table.cluster_by:
        statement.append(f"CLUSTER BY ({', '.join(table.cluster_by)})")
    properties = dict(table.properties) | {
        f"delta.constraints.{c.name}": c.expression
        for c in table.constraints
        if isinstance(c, Check)
    }
    if properties:
        entries = ",\n".join(
            f"  {_ddl_literal(k)} = {_ddl_literal(v)}"
            for k, v in sorted(properties.items())
        )
        statement.append(f"TBLPROPERTIES (\n{entries})")
    return "\n".join(statement)


def _ddl_column(column: Field) -> str:
    text = f"{column.name} {_ddl_type(column.type)}"
    if not column.nullable:
        text += " NOT NULL"
    if column.generated is not None:
        text += f" GENERATED ALWAYS AS ( {column.generated} )"
    if column.identity is not None:
        how = "ALWAYS" if column.identity.always else "BY DEFAULT"
        text += (
            f" GENERATED {how} AS IDENTITY (START WITH {column.identity.start} "
            f"INCREMENT BY {column.identity.increment})"
        )
    if column.default is not None:
        text += f" DEFAULT {column.default}"
    if column.mask is not None:
        text += f" MASK {column.mask.function}"
        if column.mask.using_columns:
            text += f" USING COLUMNS({', '.join(column.mask.using_columns)})"
    if column.comment is not None:
        text += f" COMMENT {_ddl_literal(column.comment)}"
    return text


def _ddl_type(data_type: DataType) -> str:
    from deltaplan.model.types import render_type

    match data_type:
        case Struct(fields=fields):
            inner = ", ".join(
                f"{f.name}: {_ddl_type(f.type)}"
                + ("" if f.nullable else " NOT NULL")
                + (f" COMMENT {_ddl_literal(f.comment)}" if f.comment is not None else "")
                for f in fields
            )
            return f"STRUCT<{inner}>"
        case Array(element=element):
            return f"ARRAY<{_ddl_type(element)}>"
        case Map(key=key, value=value):
            return f"MAP<{_ddl_type(key)}, {_ddl_type(value)}>"
        case _:
            rendered = render_type(data_type, upper=True)
            if rendered == "STRING" or rendered.startswith(("VARCHAR", "CHAR")):
                return f"{rendered} COLLATE UTF8_BINARY"
            return rendered


def _ddl_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _same(a: str, b: str) -> bool:
    """Column and field names ignore case in Delta, so they do here."""
    return a.casefold() == b.casefold()


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
    # As Databricks reads it: backslash escapes. (A doubled quote is two
    # literals to Databricks; deltaplan no longer writes one.)
    return re.sub(r"\\(.)", r"\1", stripped[1:-1])


def _idents(text: str) -> Iterable[str]:
    return [_unquote(part) for part in text.split(",") if part.strip()]


def _pairs(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in re.findall(r"('(?:[^'\\]|\\.|'')*')\s*=\s*('(?:[^'\\]|\\.|'')*')", text):
        found[_unliteral(part[0])] = _unliteral(part[1])
    if not found:
        raise FakeSqlError(f"no key = value pairs in: {text}")
    return found


def _constraint_name(constraint: Constraint, table: Table) -> str:
    if isinstance(constraint, Check):
        return constraint.name
    if isinstance(constraint, ForeignKey):
        return constraint.name or default_foreign_key_name(table.name, constraint)
    return constraint.name or f"{table.short_name}_pk"


def _constraint_row(table: Table, constraint: Constraint) -> Row:
    return {
        "table_name": table.short_name,
        "constraint_name": _constraint_name(constraint, table),
        "constraint_type": (
            "FOREIGN KEY" if isinstance(constraint, ForeignKey) else "PRIMARY KEY"
        ),
    }


def _literal_after(text: str, marker: str) -> str | None:
    index = text.find(marker)
    if index < 0:
        return None
    rest = text[index + len(marker) :]
    match = re.match(r"'(?:[^'\\]|\\.|'')*'", rest)
    return _unliteral(match.group(0)) if match else None


_VIEW = re.compile(
    r"(?P<verb>CREATE VIEW IF NOT EXISTS|CREATE OR REPLACE VIEW) (?P<name>\S+)"
    r"(?:\nCOMMENT (?P<comment>'(?:[^'\\]|\\.|'')*'))?"
    r"\nTBLPROPERTIES \((?P<properties>.*?)\n\)"
    r"\nAS\n(?P<query>.*)",
    re.DOTALL,
)

_FUNCTION = re.compile(
    r"(?P<verb>CREATE FUNCTION IF NOT EXISTS|CREATE OR REPLACE FUNCTION) "
    r"(?P<name>[^(\s]+)\((?P<parameters>.*?)\)"
    r"\nRETURNS (?P<returns>[^\n]+)"
    r"(?:\nCOMMENT (?P<comment>'(?:[^'\\]|\\.|'')*'))?"
    r"\nRETURN (?P<body>.*)",
    re.DOTALL,
)

_CTAS = re.compile(
    r"CREATE OR REPLACE TABLE (?P<name>\S+)"
    r"(?:\nCLUSTER BY (?:\((?P<cluster>[^)]*)\)|(?P<auto>AUTO)))?"
    r"(?:\nCOMMENT (?P<comment>'(?:[^'\\]|\\.|'')*'))?"
    r"(?:\nTBLPROPERTIES \((?P<properties>.*?)\n\))?"
    r"\s+AS\s+SELECT\s+(?P<select>.*?)\s+FROM (?P<source>\S+)",
    re.DOTALL,
)

_CREATE = re.compile(
    r"CREATE (?:TABLE IF NOT EXISTS|OR REPLACE TABLE) (?P<name>\S+) "
    r"\((?P<body>.*?)\n\)\nUSING DELTA"
    r"(?:\nCLUSTER BY (?:\((?P<cluster>[^)]*)\)|(?P<auto>AUTO)))?"
    r"(?:\nCOMMENT (?P<comment>'(?:[^'\\]|\\.|'')*'))?"
    r"(?:\nTBLPROPERTIES \((?P<properties>.*?)\n\))?"
    r"(?:\nWITH ROW FILTER (?P<filter>\S+) ON \((?P<filter_columns>[^)]*)\))?",
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
        cluster_auto=bool(match.group("auto")),
        properties=tuple(_pairs(match.group("properties") or "''=''").items())
        if match.group("properties")
        else (),
        constraints=tuple(constraints),
        row_filter=(
            RowFilter(
                _unquote(match.group("filter")),
                tuple(_idents(match.group("filter_columns"))),
            )
            if match.group("filter")
            else None
        ),
    )


def _parse_column_definition(entry: str) -> Field:
    """Read `` `a` BIGINT NOT NULL `` with the real type parser.

    A column definition is a struct field with a space where the colon goes, so
    the parser that is already trusted elsewhere does the work.
    """
    mask = None
    if found := re.search(r" MASK (\S+)(?: USING COLUMNS \(([^)]*)\))?$", entry):
        mask = Mask(_unquote(found.group(1)), tuple(_idents(found.group(2) or "")))
        entry = entry[: found.start()]
    identity = None
    if found := re.search(
        r" GENERATED (ALWAYS|BY DEFAULT) AS IDENTITY \(START WITH (-?\d+) "
        r"INCREMENT BY (-?\d+)\)",
        entry,
    ):
        identity = Identity(
            found.group(1) == "ALWAYS", int(found.group(2)), int(found.group(3))
        )
        entry = entry[: found.start()] + entry[found.end() :]
    generated = None
    if found := re.search(r" GENERATED ALWAYS AS \((.*)\)$", entry):
        generated = found.group(1)
        entry = entry[: found.start()]
    default = None
    if found := re.search(r" DEFAULT (.+)$", entry):
        default = found.group(1)
        entry = entry[: found.start()]
    name, rest = _split_name(entry)
    parsed = parse_type(f"struct<{name}:{rest}>")
    if not isinstance(parsed, Struct) or len(parsed.fields) != 1:
        raise FakeSqlError(f"cannot read column definition: {entry}")
    return replace(
        parsed.fields[0],
        mask=mask,
        identity=identity,
        generated=generated,
        default=default,
    )


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
