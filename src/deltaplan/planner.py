"""Changes in, ordered steps out.

Like the differ, this is a pure function: it is handed the live facts it needs
(`TableFacts`) rather than fetching them, so every classification is reproducible
and testable without a workspace.

The planner's whole job is to be explicit about cost. Delta divides schema
changes into ones that only touch the log, ones that need a table feature turned
on first, and ones that rewrite the data — and the difference between them is the
difference between a second and an hour. Prerequisites are inserted as their own
numbered steps rather than happening invisibly.

References:
  column mapping   https://docs.databricks.com/aws/en/delta/column-mapping
  type widening    https://docs.databricks.com/aws/en/delta/type-widening
  ALTER TABLE      https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table
  constraints      https://docs.databricks.com/aws/en/tables/constraints
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import assert_never

from deltaplan.differ import diff as compute_changes
from deltaplan.differ import unmanaged_properties
from deltaplan.model.change import Change
from deltaplan.model.function import Function
from deltaplan.model.plan import Plan, Risk, Step, TableDiff, TableFacts
from deltaplan.model.schema import Schema
from deltaplan.model.table import (
    CLUSTER_AUTO,
    COLUMN_MAPPING_PROPERTY,
    DEFAULTS_FEATURE,
    MANAGED_PROPERTY,
    TYPE_WIDENING_PROPERTY,
    Check,
    ForeignKey,
    PrimaryKey,
    RowFilter,
    Table,
    default_foreign_key_name,
    default_primary_key_name,
)
from deltaplan.model.types import (
    Array,
    Char,
    DataType,
    Decimal,
    Field,
    Map,
    Mask,
    Primitive,
    Struct,
    Varchar,
    as_data_type,
    contains_timestamp_ntz,
    render_type,
    type_kind,
)
from deltaplan.model.view import View
from deltaplan.model.volume import Volume
from deltaplan.sql import privilege_sql, quote_ident, quote_literal, quote_qualified

STREAMING_WARNING = "breaks streaming readers — they must be restarted from scratch"
IRREVERSIBLE_NOTE = "column mapping cannot be turned off again"
PROTOCOL_NOTE = "raises the table's protocol version; older clients lose access"
NOT_NULL_WARNING = "fails unless every existing row already has a value"
NEW_NOT_NULL_WARNING = (
    "a new column is NULL for every existing row, so this fails until they are "
    "backfilled — give the column a `using:` expression to fill them"
)
CHECK_WARNING = "Databricks validates every existing row, which scans the table"

# Integer digits per type, for deciding whether an integer fits in a decimal.
_INTEGER_DIGITS = {"tinyint": 3, "smallint": 5, "int": 10, "bigint": 19}
_INTEGER_CHAIN = ("tinyint", "smallint", "int", "bigint")


def widens(before: object, after: object) -> bool:
    """Is this a type change Delta can apply as metadata, given type widening?

    Deliberately conservative: anything not listed here is planned as a rewrite.
    Over-classifying costs time, under-classifying corrupts a table.

    TODO(verify): confirm the full matrix against a live workspace — in
    particular integer-to-decimal and the nested cases.
    https://docs.databricks.com/aws/en/delta/type-widening
    """
    match (before, after):
        case (Primitive(old), Primitive(new)):
            if old in _INTEGER_CHAIN and new in _INTEGER_CHAIN:
                return _INTEGER_CHAIN.index(new) > _INTEGER_CHAIN.index(old)
            if old == "float" and new == "double":
                return True
            if old in {"tinyint", "smallint", "int"} and new == "double":
                return True
            return old == "date" and new == "timestamp_ntz"
        case (Decimal(old_p, old_s), Decimal(new_p, new_s)):
            if (old_p, old_s) == (new_p, new_s):
                return False
            # The integer part may not shrink, and neither may the scale.
            return new_s >= old_s and (new_p - new_s) >= (old_p - old_s)
        case (Primitive(old), Decimal(new_p, new_s)) if old in _INTEGER_DIGITS:
            return (new_p - new_s) >= _INTEGER_DIGITS[old]
        case _:
            return False


def build_plan(
    diffs: Sequence[TableDiff],
    *,
    target: str,
    tool_version: str,
    spec_hash: str,
    state_fingerprint: str,
    clone: bool = False,
) -> Plan:
    """Expand changes into ordered steps, inserting prerequisites as they arise.

    `clone` adds a `SHALLOW CLONE` of each table before the first step that could
    lose data — a restore point you can query, on top of the Delta version that is
    always recorded.
    """
    planner = _Planner(clone_suffix=state_fingerprint[:8] if clone else None)
    for diff in diffs:
        planner.plan_table(diff)
    planner.finish()
    return Plan(
        tool_version=tool_version,
        target=target,
        spec_hash=spec_hash,
        state_fingerprint=state_fingerprint,
        diffs=tuple(diffs),
        steps=tuple(planner.steps),
    )


#: Steps before which a table may be cloned: the ones that can lose data.
CLONE_BEFORE: frozenset[Risk] = frozenset({"rewrite", "destructive"})

#: Appended to a table's name for its pre-change clone.
BACKUP_SUFFIX = "__deltaplan_backup"


class _Planner:
    """Sequences step ids and remembers which prerequisites are already planned."""

    def __init__(self, *, clone_suffix: str | None = None) -> None:
        self._kind: str = "table"
        self._clone_suffix = clone_suffix
        self._cloned: set[str] = set()
        self._existing: set[str] = set()
        self._schemas_created: set[str] = set()
        self._column_defaults: set[str] = set()
        #: Foreign keys wait until every table exists: a key can only be added
        #: once the table it references — and its primary key — is there.
        self._deferred_keys: list[tuple[int, str, ForeignKey]] = []
        self.steps: list[Step] = []
        self._column_mapping: set[str] = set()
        self._type_widening: set[str] = set()
        self._timestamp_ntz: set[str] = set()
        self._change = -1

    # -- emitting ----------------------------------------------------------
    def emit(
        self,
        table: str,
        title: str,
        risk: Risk,
        *,
        path: str = "",
        sql: str | None = None,
        precheck: str | None = None,
        refusal: str | None = None,
        postcheck: str | None = None,
        failure: str | None = None,
        est_bytes: int | None = None,
        undo_hint: str | None = None,
        warnings: tuple[str, ...] = (),
        note: str | None = None,
    ) -> None:
        if risk in CLONE_BEFORE:
            self._clone_first(table)
        self.steps.append(
            Step(
                id=len(self.steps) + 1,
                table=table,
                title=title,
                risk=risk,
                change=self._change,
                path=path,
                sql=sql,
                precheck=precheck,
                refusal=refusal,
                postcheck=postcheck,
                failure=failure,
                est_bytes=est_bytes,
                undo_hint=undo_hint,
                warnings=warnings,
                note=note,
            )
        )

    def _clone_first(self, table: str) -> None:
        """Clone a live table once, just before the first step that risks it."""
        if (
            self._clone_suffix is None
            or table in self._cloned
            or table not in self._existing
        ):
            return
        self._cloned.add(table)
        backup = backup_name(table, self._clone_suffix)
        self.emit(
            table,
            "CLONE backup",
            "meta",
            sql=(
                f"CREATE OR REPLACE TABLE {quote_qualified(backup)} "
                f"SHALLOW CLONE {quote_qualified(table)}"
            ),
            undo_hint=f"the table as it was is readable at {backup}",
            # TODO(verify): that a shallow clone stays readable after the source
            # is replaced, until VACUUM removes the files it points at.
            # https://docs.databricks.com/aws/en/delta/clone
            note=(
                "a shallow clone copies no data: it points at the table's current "
                "files, so it lasts until a VACUUM removes them"
            ),
        )

    def finish(self) -> None:
        """Steps that had to wait for every table: foreign keys."""
        for change, table, key in self._deferred_keys:
            self._change, self._kind = change, "table"
            name = key.name or default_foreign_key_name(table, key)
            columns = ", ".join(quote_ident(column) for column in key.columns)
            referenced = ", ".join(
                quote_ident(column) for column in key.referenced_columns
            )
            self.emit(
                table,
                f"ADD CONSTRAINT {name} FOREIGN KEY",
                "meta",
                sql=(
                    f"ALTER TABLE {quote_qualified(table)} ADD CONSTRAINT "
                    f"{quote_ident(name)} FOREIGN KEY ({columns}) REFERENCES "
                    f"{quote_qualified(key.references)} ({referenced})"
                ),
                note="planned after every table, so the one it references exists",
            )
        self._deferred_keys.clear()

    # -- prerequisites -----------------------------------------------------
    def need_schema(self, facts: TableFacts) -> None:
        """A table or view in a schema that isn't there yet needs it first.

        Schemas, never catalogs: a catalog comes with storage and ownership
        decisions that belong to whoever runs the metastore. And a schema is never
        dropped — not even in strict mode.
        """
        schema = facts.name.rsplit(".", 1)[0]
        if facts.schema_exists or schema in self._schemas_created:
            return
        self._schemas_created.add(schema)
        self.emit(
            facts.name,
            f"CREATE SCHEMA {schema.split('.')[-1]}",
            "meta",
            sql=f"CREATE SCHEMA IF NOT EXISTS {quote_qualified(schema)}",
            note="the schema doesn't exist yet; deltaplan creates schemas, not catalogs",
        )

    def need_column_mapping(self, facts: TableFacts, path: str) -> None:
        """Renames and drops need name-based column mapping on the table."""
        if facts.name in self._column_mapping:
            return
        self._column_mapping.add(facts.name)
        if (facts.property(COLUMN_MAPPING_PROPERTY) or "none").lower() == "name":
            return
        self.emit(
            facts.name,
            "enable columnMapping",
            "feature",
            path=path,
            sql=(
                f"ALTER TABLE {quote_qualified(facts.name)} SET TBLPROPERTIES (\n"
                "  'delta.columnMapping.mode' = 'name',\n"
                "  'delta.minReaderVersion' = '2',\n"
                "  'delta.minWriterVersion' = '5')"
            ),
            warnings=(STREAMING_WARNING,),
            note=IRREVERSIBLE_NOTE,
        )

    def need_type_widening(self, facts: TableFacts, path: str) -> None:
        if facts.name in self._type_widening:
            return
        self._type_widening.add(facts.name)
        if facts.property_is_true(TYPE_WIDENING_PROPERTY):
            return
        self.emit(
            facts.name,
            "enable typeWidening",
            "feature",
            path=path,
            sql=(
                f"ALTER TABLE {quote_qualified(facts.name)} "
                "SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')"
            ),
            note=PROTOCOL_NOTE,
        )

    def need_timestamp_ntz(self, facts: TableFacts, path: str, new: DataType) -> None:
        """Adding a TIMESTAMP_NTZ column, or widening to one, needs the
        timestampNtz feature first. CREATE TABLE turns it on by itself; ALTER
        TABLE doesn't — verified live (DELTA_FEATURES_REQUIRE_MANUAL_ENABLEMENT).
        https://docs.databricks.com/aws/en/sql/language-manual/data-types/timestamp-ntz-type
        """
        if not facts.exists or not contains_timestamp_ntz(new):
            return
        if facts.name in self._timestamp_ntz or facts.has_feature("timestampNtz"):
            return
        self._timestamp_ntz.add(facts.name)
        self.emit(
            facts.name,
            "enable timestampNtz",
            "feature",
            path=path,
            sql=(
                f"ALTER TABLE {quote_qualified(facts.name)} SET TBLPROPERTIES "
                "('delta.feature.timestampNtz' = 'supported')"
            ),
            note=PROTOCOL_NOTE,
        )

    # -- dispatch ----------------------------------------------------------
    def plan_table(self, table_diff: TableDiff) -> None:
        if table_diff.facts.exists and table_diff.live is not None:
            self._existing.add(table_diff.table)
        self._kind = table_diff.facts.kind
        hooks = (
            table_diff.desired.hooks if isinstance(table_diff.desired, Table) else None
        )
        start = self._change + 1
        changes = table_diff.changes
        if changes and changes[0].kind == "rename_table":
            # The rename goes first, so every step after it — hooks included —
            # finds the table under the name the spec gives it.
            self._change += 1
            self.plan_change(changes[0], table_diff.facts)
            changes = changes[1:]
        # Hooks run only when the table has something to do in this plan — they are
        # for the change, not for every apply.
        if hooks and hooks.before and table_diff.changes:
            self._emit_hook(table_diff.table, "BEFORE hook", hooks.before)
        if _rewrites(table_diff):
            # The table is rebuilt whole rather than patched change by change, so
            # its steps belong to the table rather than to any one change.
            self._change = -1
            self._rewrite(table_diff)
            self._change = start + len(table_diff.changes) - 1
        else:
            for change in changes:
                self._change += 1
                self.plan_change(change, table_diff.facts)
        if hooks and hooks.after and table_diff.changes:
            self._emit_hook(table_diff.table, "AFTER hook", hooks.after)

    def _emit_hook(self, table: str, title: str, sql: str) -> None:
        change, self._change = self._change, -1
        self.emit(
            table,
            title,
            "meta",
            sql=sql.strip().rstrip(";"),
            warnings=("runs your SQL as written — deltaplan can't tell what it does",),
        )
        self._change = change

    @property
    def _object(self) -> str:
        """`TABLE`, `VIEW` or `FUNCTION`, for the statements that say which."""
        return {
            "view": "VIEW",
            "function": "FUNCTION",
            "schema": "SCHEMA",
            "volume": "VOLUME",
        }.get(self._kind, "TABLE")

    def _rewrite(self, table_diff: TableDiff) -> None:
        desired, live = table_diff.desired, table_diff.live
        assert isinstance(desired, Table) and isinstance(live, Table)  # `_rewrites`
        facts = table_diff.facts
        staging = staging_name(table_diff.table)
        projection = build_projection(desired, live)

        generating = [
            c.name
            for c in live.columns
            if c.identity is not None or c.generated is not None
        ]
        if generating:
            self.emit(
                table_diff.table,
                "REWRITE",
                "rewrite",
                sql=None,
                est_bytes=facts.size_bytes,
                note=(
                    f"{', '.join(generating)} can only be identity or generated columns "
                    "from table creation, and a rewrite rebuilds the table from a query "
                    "— they would come back as plain columns. Rewrite it by hand"
                ),
            )
            return

        if facts.unmodelled:
            # A rewrite rebuilds the table from a query, which carries none of
            # these — partitioning would be gone.
            self.emit(
                table_diff.table,
                "REWRITE",
                "rewrite",
                sql=None,
                est_bytes=facts.size_bytes,
                note=(
                    f"this table has {', '.join(facts.unmodelled)}, which deltaplan "
                    "doesn't model and a rewrite would lose. Rewrite it by hand"
                ),
            )
            return

        if live.protected:
            # The staging copy is written with whatever the applying principal can
            # see — possibly unmasked — into a table with no mask or filter on it.
            # That is an exposure deltaplan will not plan.
            self.emit(
                table_diff.table,
                "REWRITE",
                "rewrite",
                sql=None,
                est_bytes=facts.size_bytes,
                note=(
                    "this table has a column mask or row filter, and a rewrite would "
                    "copy its data into a staging table without them. Rewrite it by "
                    "hand, where you control who can read the copy"
                ),
            )
            return

        if not projection.complete:
            columns = ", ".join(projection.problems)
            self.emit(
                table_diff.table,
                "REWRITE",
                "rewrite",
                sql=None,
                est_bytes=facts.size_bytes,
                undo_hint=_restore_hint(facts),
                note=(
                    f"cannot work out how to fill {columns} from the live table. "
                    "Give those columns a `using:` expression, or make this change "
                    "by hand"
                ),
            )
            return

        select = ",\n  ".join(projection.expressions)
        self.emit(
            table_diff.table,
            "STAGE rewritten data",
            "rewrite",
            sql=(
                f"CREATE OR REPLACE TABLE {quote_qualified(staging)} AS\nSELECT\n"
                f"  {select}\nFROM {quote_qualified(table_diff.table)}"
            ),
            est_bytes=facts.size_bytes,
            postcheck=staging_postcheck(table_diff.table, staging, projection),
            failure=(
                "staging lost rows or values: a conversion turned something into "
                f"NULL. {table_diff.table} is untouched; the staged copy is kept at "
                f"{staging} to inspect. Give the column a `using:` expression"
            ),
            note="a full copy is written alongside the table, then dropped again",
        )
        # A rewrite copies the columns the spec lists and nothing else, so one the
        # spec removed goes with it — which makes this step destructive, whatever
        # else it is.
        dropped = [c.path for c in table_diff.changes if c.kind == "drop_column"]
        # Properties the spec doesn't declare — retention settings, a feature
        # someone enabled — go into the replacement as they were.
        carried_properties = unmanaged_properties(desired, live)
        self.emit(
            table_diff.table,
            "REPLACE TABLE",
            "destructive" if dropped else "rewrite",
            sql=replace_table_sql(
                replace(desired, properties=(*desired.properties, *carried_properties)),
                source=staging,
            ),
            est_bytes=facts.size_bytes,
            undo_hint=_restore_hint(facts),
            warnings=(
                (f"drops {', '.join(dropped)} along with the rewrite",) if dropped else ()
            ),
        )
        # A query result has names, types and an order and nothing else, so the
        # rest of the shape is put back with ordinary ALTERs — worked out by the
        # differ rather than by a second hand-rolled list.
        finishing = compute_changes(desired, ctas_result(desired))
        # TODO(verify): whether REPLACE keeps column and table tags. Assuming it
        # doesn't, the tags the spec *doesn't* declare are put back as they were —
        # a rewrite must not diff away what deltaplan doesn't manage.
        carried = _unmanaged_tags(desired, live)
        unreachable = [change for change in finishing if needs_rewrite(change)]
        for change in finishing:
            if change not in unreachable:
                self.plan_change(change, facts)
        # Constraints the spec doesn't declare are put back too: a query result
        # carries none.
        declared_checks = {check.name.casefold() for check in desired.checks()}
        for check in live.checks():
            if check.name.casefold() not in declared_checks:
                self._emit_check(table_diff.table, check)
        for key in live.foreign_keys():
            if not any(key.same_as(declared) for declared in desired.foreign_keys()):
                self._deferred_keys.append((self._change, table_diff.table, key))
        live_key = live.primary_key()
        if desired.primary_key() is None and live_key is not None:
            self._add_constraint(
                Change(table_diff.table, "add_constraint", after=live_key)
            )
        # The same goes for access. Declared grants come back through the diff
        # above; grants to principals the spec doesn't name are put back here.
        declared = desired.grants_map()
        for grant in live.grants:
            if grant.principal not in declared:
                self._emit_grant(table_diff.table, grant.principal, grant.privileges)
        for column, tags in carried:
            if column:
                self._emit_column_tags(table_diff.table, column, tags)
            else:
                self.emit(
                    table_diff.table,
                    "SET TAGS",
                    "meta",
                    sql=set_tags_sql(table_diff.table, tags),
                    note="put back after the rewrite, as the table had them",
                )
        if unreachable:
            # A rewrite builds the new table out of a query, and a query result
            # has no required fields inside a struct. There is no ALTER for it
            # either, so say so rather than planning something that can't work.
            # TODO(verify): whether any runtime can set NOT NULL on a nested
            # field after the fact.
            paths = ", ".join(change.path for change in unreachable)
            self.emit(
                table_diff.table,
                "UNREACHABLE",
                "rewrite",
                sql=None,
                note=(
                    f"a rewrite cannot make {paths} NOT NULL: the new table is "
                    "built from a query, and a query result has no required "
                    "fields inside a struct. Drop `nullable: false` there, or "
                    "rewrite the table by hand"
                ),
            )
        self.emit(
            table_diff.table,
            "DROP staging",
            "meta",
            sql=f"DROP TABLE IF EXISTS {quote_qualified(staging)}",
        )

    def plan_change(self, change: Change, facts: TableFacts) -> None:
        match change.kind:
            case "create_table":
                self._create_table(change, facts)
            case "set_table_comment":
                self._table_comment(change)
            case "set_cluster_by":
                self._cluster_by(change)
            case "set_property":
                self._property(change)
            case "set_tag":
                self._tag(change)
            case "set_column_tag":
                self._column_tag(change)
            case "add_column":
                self._add_column(change, facts)
            case "drop_column":
                self._drop_column(change, facts)
            case "rename_column":
                self._rename_column(change, facts)
            case "change_type":
                self._change_type(change, facts)
            case "set_nullable":
                self._set_nullable(change, facts)
            case "set_comment":
                self._column_comment(change)
            case "reorder_columns":
                self._reorder(change)
            case "add_constraint":
                self._add_constraint(change)
            case "drop_constraint":
                self._drop_constraint(change)
            case "set_mask":
                self._mask(change)
            case "set_default":
                self._default(change, facts)
            case "set_identity" | "set_generated":
                self._creation_only(change)
            case "set_row_filter":
                self._row_filter(change)
            case "grant":
                self._grant(change)
            case "revoke":
                self._revoke(change)
            case "create_function":
                self._function(change, facts, replacing=False)
            case "replace_function":
                self._function(change, facts, replacing=True)
            case "create_view":
                self._create_view(change, facts)
            case "replace_view":
                self._replace_view(change)
            case "claim_table":
                self._claim(change)
            case "rename_table":
                self._rename_table(change)
            case "create_schema":
                self._create_schema(change)
            case "set_schema_comment":
                self._schema_comment(change)
            case "create_volume":
                self._create_volume(change, facts)
            case "set_volume_comment":
                self._securable_comment(change, "VOLUME")
            case "drop_table":
                self._drop_table(change, facts)
            case _:
                assert_never(change.kind)

    # -- table level -------------------------------------------------------
    def need_column_defaults(self, facts: TableFacts, path: str) -> None:
        """A column default needs the allowColumnDefaults table feature.
        https://docs.databricks.com/aws/en/delta/default-columns
        """
        if facts.name in self._column_defaults:
            return
        self._column_defaults.add(facts.name)
        if facts.has_feature("allowColumnDefaults"):
            return
        self.emit(
            facts.name,
            "enable allowColumnDefaults",
            "feature",
            path=path,
            sql=(
                f"ALTER TABLE {quote_qualified(facts.name)} "
                f"SET TBLPROPERTIES ({quote_literal(DEFAULTS_FEATURE)} = 'supported')"
            ),
            note=PROTOCOL_NOTE,
        )

    def _default(self, change: Change, facts: TableFacts) -> None:
        table = quote_qualified(change.table)
        column = quote_ident(change.path)
        default = change.after if isinstance(change.after, str) else None
        if default is None:
            self.emit(
                change.table,
                "DROP DEFAULT",
                "meta",
                path=change.path,
                sql=f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT",
            )
            return
        self.need_column_defaults(facts, change.path)
        self.emit(
            change.table,
            "SET DEFAULT",
            "meta",
            path=change.path,
            sql=f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {default}",
            note="applies to rows written from now on; existing rows keep their values",
        )

    def _creation_only(self, change: Change) -> None:
        """Identity and generated columns exist only from table creation.

        TODO(verify): that neither can be added to, or changed on, an existing
        table with ALTER. https://docs.databricks.com/aws/en/delta/generated-columns
        """
        what = "an identity" if change.kind == "set_identity" else "a generated column"
        self.emit(
            change.table,
            "CHANGE GENERATION",
            "rewrite",
            path=change.path,
            sql=None,
            note=(
                f"{change.path} would need {what} added, changed or removed, and that "
                "is only possible when a table is created. Recreate the table, or "
                "make the spec match the column as it is"
            ),
        )

    def _mask(self, change: Change) -> None:
        mask = change.after
        assert isinstance(mask, Mask)
        table = quote_qualified(change.table)
        column = quote_ident(change.path)
        previous = change.before
        self.emit(
            change.table,
            "SET MASK",
            "meta",
            path=change.path,
            sql=f"ALTER TABLE {table} ALTER COLUMN {column} SET MASK {mask_sql(mask)}",
            precheck=function_missing_sql(mask.function),
            refusal=f"the masking function {mask.function} does not exist",
            warnings=(
                f"readers see what {mask.function} returns for {change.path}, "
                "from now on",
            ),
            undo_hint=(
                f"ALTER TABLE {table} ALTER COLUMN {column} SET MASK {mask_sql(previous)}"
                if isinstance(previous, Mask)
                else f"ALTER TABLE {table} ALTER COLUMN {column} DROP MASK"
            ),
        )

    def _row_filter(self, change: Change) -> None:
        row_filter = change.after
        assert isinstance(row_filter, RowFilter)
        table = quote_qualified(change.table)
        previous = change.before
        self.emit(
            change.table,
            "SET ROW FILTER",
            "meta",
            sql=f"ALTER TABLE {table} SET ROW FILTER {row_filter_sql(row_filter)}",
            precheck=function_missing_sql(row_filter.function),
            refusal=f"the row filter function {row_filter.function} does not exist",
            warnings=(
                f"readers see only the rows {row_filter.function} allows, from now on",
            ),
            undo_hint=(
                f"ALTER TABLE {table} SET ROW FILTER {row_filter_sql(previous)}"
                if isinstance(previous, RowFilter)
                else f"ALTER TABLE {table} DROP ROW FILTER"
            ),
        )

    def _grant(self, change: Change) -> None:
        privileges = change.after if isinstance(change.after, tuple) else ()
        self._emit_grant(change.table, change.path, privileges)

    def _emit_grant(
        self, table: str, principal: str, privileges: tuple[str, ...]
    ) -> None:
        self.emit(
            table,
            f"GRANT to {principal}",
            "meta",
            path=principal,
            sql=grant_sql(table, principal, privileges, self._object),
        )

    def _revoke(self, change: Change) -> None:
        privileges = change.before if isinstance(change.before, tuple) else ()
        self.emit(
            change.table,
            f"REVOKE from {change.path}",
            "meta",
            path=change.path,
            sql=(
                f"REVOKE {', '.join(privilege_sql(p) for p in privileges)} ON "
                f"{self._object} {quote_qualified(change.table)} "
                f"FROM {quote_ident(change.path)}"
            ),
            warnings=(f"takes {', '.join(privileges)} away from {change.path}",),
            undo_hint=grant_sql(change.table, change.path, privileges, self._object),
        )

    def _claim(self, change: Change) -> None:
        self.emit(
            change.table,
            "CLAIM ownership",
            "meta",
            path=change.path,
            sql=(
                f"ALTER {self._object} {quote_qualified(change.table)} SET TBLPROPERTIES "
                f"({quote_literal(MANAGED_PROPERTY)} = 'true')"
            ),
            note=(
                f"a spec now describes this {self._object.lower()}, so deltaplan "
                "manages it — in a strict schema, removing its spec later will drop it"
            ),
        )

    def _create_schema(self, change: Change) -> None:
        """A declared schema, created with its comment; then its tags and
        grants, as for a table. Tables planned into it later find it made."""
        schema = change.after
        assert isinstance(schema, Schema)
        self._schemas_created.add(schema.name)
        sql = f"CREATE SCHEMA IF NOT EXISTS {quote_qualified(schema.name)}"
        if schema.comment is not None:
            sql += f" COMMENT {quote_literal(schema.comment)}"
        self.emit(
            schema.name,
            f"CREATE SCHEMA {schema.short_name}",
            "meta",
            sql=sql,
            note="deltaplan never drops a schema — not even in strict mode",
        )
        change_index, self._change = self._change, -1
        if schema.tags:
            self.emit(
                schema.name,
                "SET TAGS",
                "meta",
                sql=set_tags_sql(schema.name, schema.tags, "SCHEMA"),
            )
        for grant in schema.grants:
            self._emit_grant(schema.name, grant.principal, grant.privileges)
        self._change = change_index

    def _schema_comment(self, change: Change) -> None:
        self._securable_comment(change, "SCHEMA")

    def _securable_comment(self, change: Change, kind: str) -> None:
        """`COMMENT ON SCHEMA|VOLUME … IS …`, with the old comment as undo."""
        comment = change.after if isinstance(change.after, str) else ""
        previous = change.before
        self.emit(
            change.table,
            f"COMMENT ON {kind}",
            "meta",
            sql=f"COMMENT ON {kind} {quote_qualified(change.table)} IS "
            f"{quote_literal(comment)}",
            undo_hint=(
                f"COMMENT ON {kind} {quote_qualified(change.table)} IS "
                f"{quote_literal(previous) if isinstance(previous, str) else 'NULL'}"
            ),
        )

    def _create_volume(self, change: Change, facts: TableFacts) -> None:
        """A managed volume, with its comment; then its tags and grants.
        https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-volume
        """
        volume = change.after
        assert isinstance(volume, Volume)
        self.need_schema(facts)
        sql = f"CREATE VOLUME IF NOT EXISTS {quote_qualified(volume.name)}"
        if volume.comment is not None:
            sql += f" COMMENT {quote_literal(volume.comment)}"
        self.emit(
            volume.name,
            f"CREATE VOLUME {volume.short_name}",
            "meta",
            sql=sql,
            note=(
                "a managed volume; deltaplan never drops one — that would delete "
                "its files"
            ),
        )
        change_index, self._change = self._change, -1
        if volume.tags:
            self.emit(
                volume.name,
                "SET TAGS",
                "meta",
                sql=set_tags_sql(volume.name, volume.tags, "VOLUME"),
            )
        for grant in volume.grants:
            self._emit_grant(volume.name, grant.principal, grant.privileges)
        self._change = change_index

    def _rename_table(self, change: Change) -> None:
        old = change.before
        assert isinstance(old, str)
        # TODO(verify): that RENAME TO takes a fully qualified name in the same
        # schema. https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table
        self.emit(
            change.table,
            "RENAME TABLE",
            "meta",
            sql=(
                f"ALTER TABLE {quote_qualified(old)} "
                f"RENAME TO {quote_qualified(change.table)}"
            ),
            undo_hint=(
                f"ALTER TABLE {quote_qualified(change.table)} "
                f"RENAME TO {quote_qualified(old)}"
            ),
            warnings=(
                f"anything that reads {old} by name — a view, a job, a dashboard — "
                "stops finding it",
            ),
        )

    def _function(self, change: Change, facts: TableFacts, *, replacing: bool) -> None:
        function = change.after
        assert isinstance(function, Function)
        if not replacing:
            self.need_schema(facts)
        previous = change.before
        self.emit(
            function.name,
            f"{'REPLACE' if replacing else 'CREATE'} FUNCTION {function.short_name}",
            "meta",
            sql=create_function_sql(function, replace=replacing),
            undo_hint=(
                create_function_sql(previous, replace=True)
                if isinstance(previous, Function)
                else f"DROP FUNCTION {quote_qualified(function.name)}"
            ),
            warnings=(
                (
                    "anything calling it — a mask, a row filter, a view — sees the new "
                    "definition from the moment it runs",
                )
                if replacing
                else ()
            ),
        )
        if not replacing:
            for grant in function.grants:
                self._emit_grant(function.name, grant.principal, grant.privileges)
            return
        # TODO(verify): whether CREATE OR REPLACE FUNCTION keeps the function's
        # grants. Put them back as they were, as for a view, so the answer doesn't
        # matter; the spec's own grant changes follow as their own steps.
        # https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-sql-function
        assert isinstance(previous, Function)
        change_index = self._change
        self._change = -1
        for grant in previous.grants:
            self._emit_grant(function.name, grant.principal, grant.privileges)
        self._change = change_index

    def _create_view(self, change: Change, facts: TableFacts) -> None:
        view = change.after
        assert isinstance(view, View)
        self.need_schema(facts)
        self.emit(
            view.name,
            f"CREATE VIEW {view.short_name}",
            "meta",
            sql=create_view_sql(view, if_not_exists=True),
            undo_hint=f"DROP VIEW {quote_qualified(view.name)}",
        )
        if view.tags:
            self.emit(
                view.name,
                "SET TAGS",
                "meta",
                sql=set_tags_sql(view.name, view.tags, "VIEW"),
            )
        for grant in view.grants:
            self._emit_grant(view.name, grant.principal, grant.privileges)

    def _replace_view(self, change: Change) -> None:
        view, previous = change.after, change.before
        assert isinstance(view, View) and isinstance(previous, View)
        carried = unmanaged_properties(view, previous)
        self.emit(
            view.name,
            "REPLACE VIEW",
            "meta",
            sql=create_view_sql(replace(view, properties=(*view.properties, *carried))),
            undo_hint=create_view_sql(previous),
            note="readers see the new definition from the moment it runs",
        )
        # TODO(verify): whether CREATE OR REPLACE VIEW keeps the view's tags and
        # grants. Put them back as they were, so the answer doesn't matter; the
        # spec's own changes to them follow as their own steps. These belong to
        # the view rather than the change, so a resume runs them again.
        change_index = self._change
        self._change = -1
        if previous.tags:
            self.emit(
                view.name,
                "SET TAGS",
                "meta",
                sql=set_tags_sql(view.name, previous.tags, "VIEW"),
                note="put back after the replace, as the view had them",
            )
        for grant in previous.grants:
            self._emit_grant(view.name, grant.principal, grant.privileges)
        self._change = change_index

    def _drop_table(self, change: Change, facts: TableFacts) -> None:
        dropped = change.before
        if isinstance(dropped, View):
            self.emit(
                change.table,
                "DROP VIEW",
                "destructive",
                sql=f"DROP VIEW {quote_qualified(change.table)}",
                undo_hint=create_view_sql(dropped),
            )
            return
        self.emit(
            change.table,
            "DROP TABLE",
            "destructive",
            sql=f"DROP TABLE {quote_qualified(change.table)}",
            est_bytes=facts.size_bytes,
            # TODO(verify): UNDROP covers managed tables for a retention window
            # (seven days at the time of writing).
            # https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-undrop-table
            undo_hint=f"UNDROP TABLE {quote_qualified(change.table)}",
        )

    def _create_table(self, change: Change, facts: TableFacts) -> None:
        table = change.after
        assert isinstance(table, Table)
        self.need_schema(facts)
        self.emit(
            table.name,
            f"CREATE TABLE {table.short_name}",
            "meta",
            sql=create_table_sql(table),
            undo_hint=f"DROP TABLE {quote_qualified(table.name)}",
        )
        # Tags and CHECK constraints are not part of CREATE TABLE.
        # TODO(verify): recent runtimes may accept inline CHECK constraints; we
        # do not rely on it. https://docs.databricks.com/aws/en/tables/constraints
        for check in table.checks():
            self._emit_check(table.name, check)
        for key in table.foreign_keys():
            self._deferred_keys.append((self._change, table.name, key))
        if table.tags:
            self.emit(
                table.name,
                "SET TAGS",
                "meta",
                sql=set_tags_sql(table.name, table.tags),
            )
        for column in table.columns:
            if column.tags:
                self._emit_column_tags(table.name, column.name, column.tags)
        for grant in table.grants:
            self._emit_grant(table.name, grant.principal, grant.privileges)

    def _table_comment(self, change: Change) -> None:
        comment = change.after
        literal = quote_literal(comment) if isinstance(comment, str) else "NULL"
        self.emit(
            change.table,
            "COMMENT ON TABLE",
            "meta",
            sql=f"COMMENT ON TABLE {quote_qualified(change.table)} IS {literal}",
        )

    def _cluster_by(self, change: Change) -> None:
        columns = change.after if isinstance(change.after, tuple) else ()
        clause = (
            "AUTO"
            if change.after == CLUSTER_AUTO
            else f"({', '.join(quote_ident(name) for name in columns)})"
            if columns
            else "NONE"
        )
        self.emit(
            change.table,
            "CLUSTER BY",
            "meta",
            sql=f"ALTER TABLE {quote_qualified(change.table)} CLUSTER BY {clause}",
            note="applies to new data; run OPTIMIZE to recluster what is there",
        )

    def _property(self, change: Change) -> None:
        value = change.after if isinstance(change.after, str) else ""
        self.emit(
            change.table,
            "SET TBLPROPERTIES",
            "meta",
            path=change.path,
            sql=(
                f"ALTER {self._object} {quote_qualified(change.table)} SET TBLPROPERTIES "
                f"({quote_literal(change.path)} = {quote_literal(value)})"
            ),
        )

    def _tag(self, change: Change) -> None:
        value = change.after if isinstance(change.after, str) else ""
        self.emit(
            change.table,
            "SET TAGS",
            "meta",
            path=change.path,
            sql=set_tags_sql(change.table, ((change.path, value),), self._object),
        )

    def _column_tag(self, change: Change) -> None:
        key, value = change.after if isinstance(change.after, tuple) else ("", "")
        self._emit_column_tags(change.table, change.path, ((key, value),))

    def _emit_column_tags(
        self, table: str, column: str, tags: tuple[tuple[str, str], ...]
    ) -> None:
        self.emit(
            table,
            "SET COLUMN TAGS",
            "meta",
            path=column,
            sql=column_tags_sql(table, column, tags),
        )

    # -- columns -----------------------------------------------------------
    def _add_column(self, change: Change, facts: TableFacts) -> None:
        column = change.after
        assert isinstance(column, Field)
        # Refused before anything runs: adding it without its generation would
        # leave a plain column where an identity or generated one was asked for.
        if not change.nested and (column.identity is not None or column.generated):
            self._creation_only(
                Change(
                    change.table,
                    "set_identity" if column.identity is not None else "set_generated",
                    change.path,
                    after=column.identity or column.generated,
                )
            )
            return
        self.need_timestamp_ntz(facts, change.path, column.type)
        # A column is always added nullable: on a table with rows, every existing
        # row would violate NOT NULL. The constraint is a separate step.
        definition = (
            f"{column_path_sql(change.path)} {render_type(column.type, upper=True)}"
        )
        if column.comment is not None:
            definition += f" COMMENT {quote_literal(column.comment)}"
        self.emit(
            change.table,
            f"ADD COLUMN {change.path}",
            "meta",
            path=change.path,
            sql=f"ALTER TABLE {quote_qualified(change.table)} ADD COLUMNS ({definition})",
        )
        backfilled = column.using is not None and not change.nested and facts.exists
        if backfilled:
            # `using:` says how to get this column's value from the rest of the
            # row. On a column being added, that fills the rows already there —
            # which is what lets a NOT NULL column be added to a table with data.
            column_sql = quote_ident(column.name)
            self.emit(
                change.table,
                f"BACKFILL {change.path}",
                "rewrite",
                path=change.path,
                sql=(
                    f"UPDATE {quote_qualified(change.table)} "
                    f"SET {column_sql} = {column.using} WHERE {column_sql} IS NULL"
                ),
                est_bytes=facts.size_bytes,
                undo_hint=_restore_hint(facts),
                note="rewrites the files holding rows it fills; safe to repeat",
            )
        if not column.nullable:
            self._emit_set_not_null(
                change,
                facts,
                warnings=() if backfilled else (NEW_NOT_NULL_WARNING,),
            )
        if column.default is not None and not change.nested:
            self._default(
                Change(change.table, "set_default", change.path, after=column.default),
                facts,
            )
        if column.tags and not change.nested:
            self._emit_column_tags(change.table, change.path, column.tags)

    def _drop_column(self, change: Change, facts: TableFacts) -> None:
        self.need_column_mapping(facts, change.path)
        self.emit(
            change.table,
            "DROP COLUMN",
            "destructive",
            path=change.path,
            sql=(
                f"ALTER TABLE {quote_qualified(change.table)} "
                f"DROP COLUMN {column_path_sql(change.path)}"
            ),
            undo_hint=_restore_hint(facts),
        )

    def _rename_column(self, change: Change, facts: TableFacts) -> None:
        self.need_column_mapping(facts, change.path)
        old_name = change.before if isinstance(change.before, str) else ""
        old_path = _replace_leaf(change.path, old_name)
        self.emit(
            change.table,
            "RENAME COLUMN",
            "meta",
            path=change.path,
            sql=(
                f"ALTER TABLE {quote_qualified(change.table)} "
                f"RENAME COLUMN {column_path_sql(old_path)} TO {quote_ident(change.leaf)}"
            ),
        )

    def _change_type(self, change: Change, facts: TableFacts) -> None:
        after = change.after
        if needs_rewrite(change):
            # Only reached when the caller gave no desired/live tables to rewrite
            # towards; otherwise `plan_table` handled the whole table already.
            self._emit_rewrite(
                change,
                facts,
                title="REWRITE",
                note=(
                    "a map key cannot be altered in place"
                    if change.path.endswith(".key")
                    else "not a supported widening, so the data has to be rewritten"
                ),
            )
            return
        self.need_type_widening(facts, change.path)
        widened = as_data_type(after)
        if widened is not None:
            self.need_timestamp_ntz(facts, change.path, widened)
        rendered = render_type(widened, upper=True) if widened is not None else ""
        self.emit(
            change.table,
            "ALTER COLUMN TYPE",
            "meta",
            path=change.path,
            sql=(
                f"ALTER TABLE {quote_qualified(change.table)} "
                f"ALTER COLUMN {column_path_sql(change.path)} TYPE {rendered}"
            ),
        )

    def _set_nullable(self, change: Change, facts: TableFacts) -> None:
        if needs_rewrite(change):
            self._emit_rewrite(
                change,
                facts,
                title="REWRITE",
                note="nullability of a nested field cannot be altered in place",
            )
            return
        if change.after is False:
            self._emit_set_not_null(change, facts, warnings=(NOT_NULL_WARNING,))
            return
        self.emit(
            change.table,
            "DROP NOT NULL",
            "meta",
            path=change.path,
            sql=(
                f"ALTER TABLE {quote_qualified(change.table)} "
                f"ALTER COLUMN {column_path_sql(change.path)} DROP NOT NULL"
            ),
        )

    def _emit_set_not_null(
        self,
        change: Change,
        facts: TableFacts,
        *,
        warnings: tuple[str, ...],
    ) -> None:
        table = quote_qualified(change.table)
        self.emit(
            change.table,
            "SET NOT NULL",
            "meta",
            path=change.path,
            sql=(
                f"ALTER TABLE {table} ALTER COLUMN "
                f"{column_path_sql(change.path)} SET NOT NULL"
            ),
            # Databricks would reject the ALTER anyway; asking first turns a raw
            # SQL error into deltaplan's own warning.
            precheck=(
                f"SELECT count(*) > 0 AS blocked FROM {table} "
                f"WHERE {column_path_sql(change.path)} IS NULL"
            ),
            refusal=f"{change.path} still has NULLs in it",
            warnings=warnings,
            est_bytes=facts.size_bytes,
        )

    def _column_comment(self, change: Change) -> None:
        comment = change.after
        literal = quote_literal(comment) if isinstance(comment, str) else "NULL"
        self.emit(
            change.table,
            "COMMENT ON COLUMN",
            "meta",
            path=change.path,
            sql=(
                f"ALTER TABLE {quote_qualified(change.table)} "
                f"ALTER COLUMN {column_path_sql(change.path)} COMMENT {literal}"
            ),
        )

    def _reorder(self, change: Change) -> None:
        order = change.after if isinstance(change.after, tuple) else ()
        table = quote_qualified(change.table)
        previous: str | None = None
        for name in order:
            position = "FIRST" if previous is None else f"AFTER {quote_ident(previous)}"
            self.emit(
                change.table,
                f"ALTER COLUMN {name} {position}",
                "meta",
                path=name,
                sql=f"ALTER TABLE {table} ALTER COLUMN {quote_ident(name)} {position}",
            )
            previous = name

    # -- constraints -------------------------------------------------------
    def _add_constraint(self, change: Change) -> None:
        constraint = change.after
        if isinstance(constraint, Check):
            self._emit_check(change.table, constraint)
            return
        if isinstance(constraint, ForeignKey):
            self._deferred_keys.append((self._change, change.table, constraint))
            return
        assert isinstance(constraint, PrimaryKey)
        name = constraint.name or f"{change.table.split('.')[-1]}_pk"
        columns = ", ".join(quote_ident(column) for column in constraint.columns)
        self.emit(
            change.table,
            f"ADD CONSTRAINT {name} PRIMARY KEY",
            "meta",
            sql=(
                f"ALTER TABLE {quote_qualified(change.table)} "
                f"ADD CONSTRAINT {quote_ident(name)} PRIMARY KEY ({columns})"
            ),
        )

    def _emit_check(self, table: str, check: Check) -> None:
        self.emit(
            table,
            f"ADD CONSTRAINT {check.name} CHECK",
            "meta",
            sql=(
                f"ALTER TABLE {quote_qualified(table)} "
                f"ADD CONSTRAINT {quote_ident(check.name)} CHECK ({check.expression})"
            ),
            warnings=(CHECK_WARNING,),
        )

    def _drop_constraint(self, change: Change) -> None:
        constraint = change.before
        name = _constraint_name(constraint, change.table)
        self.emit(
            change.table,
            f"DROP CONSTRAINT {name}",
            "meta",
            sql=(
                f"ALTER TABLE {quote_qualified(change.table)} "
                f"DROP CONSTRAINT {quote_ident(name)}"
            ),
            undo_hint="re-add the constraint with ALTER TABLE ... ADD CONSTRAINT",
        )

    # -- rewrites ----------------------------------------------------------
    def _emit_rewrite(
        self,
        change: Change,
        facts: TableFacts,
        *,
        title: str,
        note: str,
    ) -> None:
        self.emit(
            change.table,
            title,
            "rewrite",
            path=change.path,
            sql=None,
            est_bytes=facts.size_bytes,
            undo_hint=_restore_hint(facts),
            note=f"{note}. Rewrites are planned but not generated in v1 (milestone 3)",
        )


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------


def column_path_sql(path: str) -> str:
    """Quote a nested column path segment by segment: ``address.zip``."""
    return ".".join(quote_ident(part) for part in path.split("."))


def set_tags_sql(
    table: str, tags: tuple[tuple[str, str], ...], kind: str = "TABLE"
) -> str:
    """TODO(verify): tag syntax against a live workspace.
    https://docs.databricks.com/aws/en/database-objects/tags
    """
    pairs = ", ".join(
        f"{quote_literal(key)} = {quote_literal(value)}" for key, value in tags
    )
    return f"ALTER {kind} {quote_qualified(table)} SET TAGS ({pairs})"


def create_view_sql(view: View, *, if_not_exists: bool = False) -> str:
    """`CREATE VIEW`, marked managed like every object deltaplan creates.

    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-view
    """
    verb = "CREATE VIEW IF NOT EXISTS" if if_not_exists else "CREATE OR REPLACE VIEW"
    lines = [f"{verb} {quote_qualified(view.name)}"]
    if view.comment is not None:
        lines.append(f"COMMENT {quote_literal(view.comment)}")
    properties = dict(view.properties)
    properties[MANAGED_PROPERTY] = "true"
    rendered = ",\n".join(
        f"  {quote_literal(key)} = {quote_literal(value)}"
        for key, value in sorted(properties.items())
    )
    lines.append(f"TBLPROPERTIES (\n{rendered}\n)")
    lines.append("AS")
    lines.append(view.query.strip().rstrip(";"))
    return "\n".join(lines)


def grant_sql(
    table: str, principal: str, privileges: tuple[str, ...], kind: str = "TABLE"
) -> str:
    """https://docs.databricks.com/aws/en/sql/language-manual/security-grant"""
    return (
        f"GRANT {', '.join(privilege_sql(p) for p in privileges)} ON {kind} "
        f"{quote_qualified(table)} TO {quote_ident(principal)}"
    )


def create_function_sql(function: Function, *, replace: bool = False) -> str:
    """`CREATE FUNCTION … RETURNS … RETURN body`.
    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-sql-function
    """
    verb = "CREATE OR REPLACE FUNCTION" if replace else "CREATE FUNCTION IF NOT EXISTS"
    parameters = ", ".join(
        f"{quote_ident(p.name)} {render_type(p.type, upper=True)}"
        for p in function.parameters
    )
    lines = [
        f"{verb} {quote_qualified(function.name)}({parameters})",
        f"RETURNS {render_type(function.returns, upper=True)}",
    ]
    if function.comment is not None:
        lines.append(f"COMMENT {quote_literal(function.comment)}")
    lines.append(f"RETURN {function.body.strip().rstrip(';')}")
    return "\n".join(lines)


def column_tags_sql(table: str, column: str, tags: tuple[tuple[str, str], ...]) -> str:
    """TODO(verify): column tag syntax against a live workspace.
    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table-manage-column
    """
    pairs = ", ".join(f"{quote_literal(k)} = {quote_literal(v)}" for k, v in tags)
    return (
        f"ALTER TABLE {quote_qualified(table)} "
        f"ALTER COLUMN {quote_ident(column)} SET TAGS ({pairs})"
    )


def create_table_sql(table: Table) -> str:
    """`CREATE TABLE`, with the managed marker and an inline primary key."""
    lines = [f"  {_column_definition(column)}" for column in table.columns]
    primary_key = table.primary_key()
    if primary_key is not None:
        name = primary_key.name or default_primary_key_name(table)
        columns = ", ".join(quote_ident(column) for column in primary_key.columns)
        lines.append(f"  CONSTRAINT {quote_ident(name)} PRIMARY KEY ({columns})")

    properties = dict(table.properties)
    properties[MANAGED_PROPERTY] = "true"
    if any(column.default is not None for column in table.columns):
        properties[DEFAULTS_FEATURE] = "supported"
    rendered_properties = ",\n".join(
        f"  {quote_literal(key)} = {quote_literal(value)}"
        for key, value in sorted(properties.items())
    )

    sql = [
        f"CREATE TABLE IF NOT EXISTS {quote_qualified(table.name)} (",
        ",\n".join(lines),
        ")",
        "USING DELTA",
    ]
    if clustering := _clustering_clause(table):
        sql.append(clustering)
    if table.comment is not None:
        sql.append(f"COMMENT {quote_literal(table.comment)}")
    sql.append(f"TBLPROPERTIES (\n{rendered_properties}\n)")
    if table.row_filter is not None:
        # TODO(verify): clause placement against a live workspace.
        sql.append(f"WITH ROW FILTER {row_filter_sql(table.row_filter)}")
    return "\n".join(sql)


def _column_definition(column: Field) -> str:
    definition = f"{quote_ident(column.name)} {render_type(column.type, upper=True)}"
    if not column.nullable:
        definition += " NOT NULL"
    if column.comment is not None:
        definition += f" COMMENT {quote_literal(column.comment)}"
    if column.identity is not None:
        identity = column.identity
        kind = "ALWAYS" if identity.always else "BY DEFAULT"
        definition += (
            f" GENERATED {kind} AS IDENTITY "
            f"(START WITH {identity.start} INCREMENT BY {identity.increment})"
        )
    if column.generated is not None:
        definition += f" GENERATED ALWAYS AS ({column.generated})"
    if column.default is not None:
        definition += f" DEFAULT {column.default}"
    if column.mask is not None:
        # Inline, so the table never exists without it.
        definition += f" MASK {mask_sql(column.mask)}"
    return definition


def mask_sql(mask: Mask) -> str:
    """`fn [USING COLUMNS (a, b)]`.
    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-column-mask
    """
    rendered = quote_qualified(mask.function)
    if mask.using_columns:
        rendered += (
            f" USING COLUMNS ({', '.join(quote_ident(c) for c in mask.using_columns)})"
        )
    return rendered


def row_filter_sql(row_filter: RowFilter) -> str:
    """`fn ON (a, b)`.
    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-row-filter
    """
    columns = ", ".join(quote_ident(c) for c in row_filter.columns)
    return f"{quote_qualified(row_filter.function)} ON ({columns})"


def function_missing_sql(function: str) -> str | None:
    """A precheck that refuses a mask or filter whose function isn't there."""
    parts = function.split(".")
    if len(parts) != 3:
        return None
    catalog, schema, name = parts
    return (
        f"SELECT count(*) = 0 AS blocked FROM {quote_ident(catalog)}.information_schema"
        f".routines WHERE routine_schema = {quote_literal(schema)} "
        f"AND routine_name = {quote_literal(name)}"
    )


def _replace_leaf(path: str, leaf: str) -> str:
    parent, _, _ = path.rpartition(".")
    return f"{parent}.{leaf}" if parent else leaf


def _constraint_name(constraint: object, table: str) -> str:
    if isinstance(constraint, Check):
        return constraint.name
    if isinstance(constraint, PrimaryKey):
        return constraint.name or f"{table.split('.')[-1]}_pk"
    if isinstance(constraint, ForeignKey):
        return constraint.name or default_foreign_key_name(table, constraint)
    return "unknown"


def _restore_hint(facts: TableFacts) -> str | None:
    if facts.delta_version is None:
        return None
    return (
        f"RESTORE TABLE {quote_qualified(facts.name)} "
        f"TO VERSION AS OF {facts.delta_version}"
    )


# ---------------------------------------------------------------------------
# rewrites
# ---------------------------------------------------------------------------

#: Appended to a table's name for the table a rewrite stages its data in.
STAGING_SUFFIX = "__deltaplan_rewrite"


def needs_rewrite(change: Change) -> bool:
    """Can this change only be made by rewriting the data?

    The one place that decides. `plan_table` asks it up front, because a table
    that needs a rewrite is rebuilt whole rather than patched change by change.
    """
    match change.kind:
        case "change_type":
            # A map key can't be altered in place whatever the types involved.
            return change.path.endswith(".key") or not widens(change.before, change.after)
        case "set_nullable":
            # TODO(verify): no runtime we know of can alter a nested field's
            # nullability in place.
            return change.nested
        case _:
            return False


def _rewrites(table_diff: TableDiff) -> bool:
    """Is this a table to rebuild rather than patch?

    It takes both sides to rewrite: the shape to build, and the table to read the
    data out of. Without them the planner falls back to classifying the change and
    saying it can't generate the SQL.
    """
    return (
        isinstance(table_diff.desired, Table)
        and isinstance(table_diff.live, Table)
        and any(needs_rewrite(change) for change in table_diff.changes)
    )


def backup_name(table: str, suffix: str) -> str:
    parts = table.split(".")
    return ".".join([*parts[:-1], f"{parts[-1]}{BACKUP_SUFFIX}_{suffix}"])


def staging_name(table: str) -> str:
    parts = table.split(".")
    return ".".join([*parts[:-1], f"{parts[-1]}{STAGING_SUFFIX}"])


@dataclass(frozen=True, slots=True)
class Projection:
    """How to read each desired column out of the live table.

    `problems` names the columns deltaplan couldn't work out an expression for.
    A projection with problems is not used: the plan says what is missing and
    asks for a `using:` expression instead of generating something wrong.
    """

    expressions: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    #: (new column, live column) for every value that is *converted* rather than
    #: copied — the ones a failed conversion could quietly turn into NULL.
    conversions: tuple[tuple[str, str], ...] = ()

    @property
    def complete(self) -> bool:
        return not self.problems


def build_projection(desired: Table, live: Table) -> Projection:
    """The SELECT list that turns the live table into the desired one."""
    expressions: list[str] = []
    problems: list[str] = []
    conversions: list[tuple[str, str]] = []
    for column in desired.columns:
        source = _live_counterpart(column, live)
        expression = (
            column.using
            if column.using is not None
            else _value_expression(column, source)
        )
        if expression is None:
            problems.append(column.name)
            continue
        expressions.append(f"{expression} AS {quote_ident(column.name)}")
        if column.using is None and source is not None and source.type != column.type:
            conversions.append((column.name, source.name))
    return Projection(tuple(expressions), tuple(problems), tuple(conversions))


def staging_postcheck(table: str, staging: str, projection: Projection) -> str:
    """Nothing lost in staging: every row is there, no converted value went NULL.

    A CAST that can't convert a value errors under ANSI mode and quietly yields
    NULL without it. Counting NULLs before and after catches the second case
    before the original table is touched. It sees top-level values only — a
    field lost inside a rebuilt struct doesn't make the struct NULL.
    TODO(verify): that SQL warehouses run with ANSI mode on by default.
    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-ansi-compliance
    """
    source, copy = quote_qualified(table), quote_qualified(staging)
    conditions = [f"(SELECT count(*) FROM {copy}) = (SELECT count(*) FROM {source})"]
    for new, old in projection.conversions:
        conditions.append(
            f"(SELECT count_if({quote_ident(new)} IS NULL) FROM {copy}) <= "
            f"(SELECT count_if({quote_ident(old)} IS NULL) FROM {source})"
        )
    return "SELECT (\n  " + "\n  AND ".join(conditions) + "\n) AS ok"


def _live_counterpart(column: Field, live: Table) -> Field | None:
    """The live column this one comes from, following a declared rename."""
    if column.renamed_from is not None and live.column(column.name) is None:
        renamed = live.column(column.renamed_from)
        if renamed is not None:
            return renamed
    return live.column(column.name)


def _value_expression(column: Field, source: Field | None) -> str | None:
    if source is None:
        # A column that isn't there yet starts out empty.
        return f"CAST(NULL AS {render_type(column.type, upper=True)})"
    return _convert(column.type, source.type, quote_ident(source.name))


def _convert(desired: DataType, live: DataType, reference: str) -> str | None:
    """An expression converting `reference` from one type to the other.

    Returns None when deltaplan has no honest answer — a struct becoming an
    array, a map whose shape moved — rather than emitting a cast that would
    either fail or, worse, silently line fields up by position.
    """
    if desired == live:
        return reference
    if _is_scalar(desired) and _is_scalar(live):
        # Any scalar to any other scalar is a cast. Whether it is a *sensible*
        # cast is Databricks' call — and `using:` is there for when it isn't.
        return f"CAST({reference} AS {render_type(desired, upper=True)})"
    if type_kind(desired) != type_kind(live):
        # A struct becoming an array, or a scalar becoming a struct: there is no
        # conversion to guess at.
        return None

    match (desired, live):
        case (Struct(), Struct()):
            return _struct_expression(desired, live, reference)
        case (Array(desired_element, _), Array(live_element, _)):
            if desired_element == live_element:
                return reference
            inner = _convert(desired_element, live_element, _LAMBDA_VARIABLE)
            if inner is None:
                return None
            # https://docs.databricks.com/aws/en/sql/language-manual/functions/transform
            return f"transform({reference}, {_LAMBDA_VARIABLE} -> {inner})"
        case _:
            # Maps: transform_keys / transform_values would do it, but a map whose
            # key type moved needs a decision about collisions that only you can
            # make.
            return None


_LAMBDA_VARIABLE = "dp_item"


def _is_scalar(data_type: DataType) -> bool:
    return isinstance(data_type, Primitive | Decimal | Char | Varchar)


def _struct_expression(desired: Struct, live: Struct, reference: str) -> str | None:
    """`named_struct(...)`, built by name — never by position."""
    parts: list[str] = []
    for member in desired.fields:
        source = _live_member(member, live)
        child = f"{reference}.{quote_ident(source.name)}" if source else None
        expression = (
            _convert(member.type, source.type, child)
            if source is not None and child is not None
            else f"CAST(NULL AS {render_type(member.type, upper=True)})"
        )
        if expression is None:
            return None
        parts.append(f"{quote_literal(member.name)}, {expression}")
    return f"named_struct({', '.join(parts)})"


def _live_member(member: Field, live: Struct) -> Field | None:
    if member.renamed_from is not None and live.field(member.name) is None:
        renamed = live.field(member.renamed_from)
        if renamed is not None:
            return renamed
    return live.field(member.name)


def replace_table_sql(
    table: Table,
    *,
    source: str | None = None,
) -> str:
    """`CREATE OR REPLACE TABLE`, either with an explicit schema or from a query.

    Replacing rather than dropping and recreating is what keeps the table's
    identity and its Delta history — which is what makes the recorded restore
    point mean anything.
    TODO(verify): that REPLACE preserves history far enough back to RESTORE.
    """
    if source is None:
        return create_table_sql(table).replace(
            "CREATE TABLE IF NOT EXISTS", "CREATE OR REPLACE TABLE", 1
        )
    clauses = [f"CREATE OR REPLACE TABLE {quote_qualified(table.name)}"]
    if clustering := _clustering_clause(table):
        clauses.append(clustering)
    if table.comment is not None:
        clauses.append(f"COMMENT {quote_literal(table.comment)}")
    properties = dict(table.properties)
    properties[MANAGED_PROPERTY] = "true"
    rendered = ",\n".join(
        f"  {quote_literal(key)} = {quote_literal(value)}"
        for key, value in sorted(properties.items())
    )
    clauses.append(f"TBLPROPERTIES (\n{rendered}\n)")
    clauses.append(f"AS SELECT * FROM {quote_qualified(source)}")
    return "\n".join(clauses)


def _unmanaged_tags(
    desired: Table, live: Table
) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    """Live tags the spec doesn't declare, as (column or "", tags) pairs."""
    carried: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    declared_table = desired.tags_map()
    table_tags = tuple((k, v) for k, v in live.tags if k not in declared_table)
    if table_tags:
        carried.append(("", table_tags))
    for live_column in live.columns:
        column = desired.column(live_column.name) or next(
            (c for c in desired.columns if c.renamed_from == live_column.name), None
        )
        if column is None:
            continue  # the column is going; its tags go with it
        declared = dict(column.tags)
        extra = tuple((k, v) for k, v in live_column.tags if k not in declared)
        if extra:
            carried.append((column.name, extra))
    return carried


def ctas_result(desired: Table) -> Table:
    """What `CREATE OR REPLACE TABLE … AS SELECT` leaves behind.

    A query result has names, types and an order; it has no nullability,
    comments, tags or constraints. Diffing this against the desired table is how
    the planner works out which ordinary `ALTER`s finish the job — reusing the
    differ rather than hand-rolling a second list of them.
    """
    return Table(
        name=desired.name,
        columns=tuple(Field(c.name, _bare(c.type)) for c in desired.columns),
        comment=desired.comment,
        cluster_by=desired.cluster_by,
        cluster_auto=desired.cluster_auto,
        properties=(*desired.properties, (MANAGED_PROPERTY, "true")),
    )


def _clustering_clause(table: Table) -> str | None:
    """`CLUSTER BY AUTO`, `CLUSTER BY (keys)`, or nothing.

    TODO(verify): AUTO needs predictive optimization; on a workspace without it
    the statement's behaviour is unverified (it was on where this was tested).
    https://docs.databricks.com/aws/en/delta/clustering#automatic-liquid-clustering
    """
    if table.cluster_auto:
        return "CLUSTER BY AUTO"
    if table.cluster_by:
        return f"CLUSTER BY ({', '.join(quote_ident(name) for name in table.cluster_by)})"
    return None


def _bare(data_type: DataType) -> DataType:
    """The same type with every nested comment and NOT NULL stripped.

    `named_struct` builds a struct out of values; it carries no field comments and
    marks nothing as required. Saying so here is what makes the planner emit the
    `ALTER`s that put them back, instead of quietly dropping them.
    """
    match data_type:
        case Struct(fields):
            return Struct(tuple(Field(f.name, _bare(f.type)) for f in fields))
        case Array(element, contains_null):
            return Array(_bare(element), contains_null)
        case Map(key, value):
            return Map(_bare(key), _bare(value))
        case _:
            return data_type
