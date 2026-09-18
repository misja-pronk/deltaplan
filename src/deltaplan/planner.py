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
from dataclasses import dataclass

from deltaplan.differ import diff as compute_changes
from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, Risk, Step, TableDiff, TableFacts
from deltaplan.model.table import (
    COLUMN_MAPPING_PROPERTY,
    MANAGED_PROPERTY,
    TYPE_WIDENING_PROPERTY,
    Check,
    PrimaryKey,
    Table,
    default_primary_key_name,
)
from deltaplan.model.types import (
    Array,
    Char,
    DataType,
    Decimal,
    Field,
    Map,
    Primitive,
    Struct,
    Varchar,
    as_data_type,
    render_type,
    type_kind,
)
from deltaplan.sql import quote_ident, quote_literal, quote_qualified

STREAMING_WARNING = "breaks streaming readers — they must be restarted from scratch"
IRREVERSIBLE_NOTE = "column mapping cannot be turned off again"
PROTOCOL_NOTE = "raises the table's protocol version; older clients lose access"
NOT_NULL_WARNING = "fails unless every existing row already has a value"
NEW_NOT_NULL_WARNING = (
    "a new column is NULL for every existing row, so this fails until they are backfilled"
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
) -> Plan:
    """Expand changes into ordered steps, inserting prerequisites as they arise."""
    planner = _Planner()
    for diff in diffs:
        planner.plan_table(diff)
    return Plan(
        tool_version=tool_version,
        target=target,
        spec_hash=spec_hash,
        state_fingerprint=state_fingerprint,
        diffs=tuple(diffs),
        steps=tuple(planner.steps),
    )


class _Planner:
    """Sequences step ids and remembers which prerequisites are already planned."""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self._column_mapping: set[str] = set()
        self._type_widening: set[str] = set()
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
        postcheck: str | None = None,
        est_bytes: int | None = None,
        undo_hint: str | None = None,
        warnings: tuple[str, ...] = (),
        note: str | None = None,
    ) -> None:
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
                postcheck=postcheck,
                est_bytes=est_bytes,
                undo_hint=undo_hint,
                warnings=warnings,
                note=note,
            )
        )

    # -- prerequisites -----------------------------------------------------
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

    # -- dispatch ----------------------------------------------------------
    def plan_table(self, table_diff: TableDiff) -> None:
        start = self._change + 1
        if _rewrites(table_diff):
            # The table is rebuilt whole rather than patched change by change, so
            # its steps belong to the table rather than to any one change.
            self._change = -1
            self._rewrite(table_diff)
            self._change = start + len(table_diff.changes) - 1
            return
        for change in table_diff.changes:
            self._change += 1
            self.plan_change(change, table_diff.facts)

    def _rewrite(self, table_diff: TableDiff) -> None:
        desired, live = table_diff.desired, table_diff.live
        assert desired is not None and live is not None  # `_rewrites` checked
        facts = table_diff.facts
        staging = staging_name(table_diff.table)
        projection = build_projection(desired, live)

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
            note="a full copy is written alongside the table, then dropped again",
        )
        self.emit(
            table_diff.table,
            "REPLACE TABLE",
            "rewrite",
            sql=replace_table_sql(desired, source=staging),
            est_bytes=facts.size_bytes,
            undo_hint=_restore_hint(facts),
        )
        # A query result has names, types and an order and nothing else, so the
        # rest of the shape is put back with ordinary ALTERs — worked out by the
        # differ rather than by a second hand-rolled list.
        finishing = compute_changes(desired, ctas_result(desired))
        unreachable = [change for change in finishing if needs_rewrite(change)]
        for change in finishing:
            if change not in unreachable:
                self.plan_change(change, facts)
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

    # -- table level -------------------------------------------------------
    def _create_table(self, change: Change, facts: TableFacts) -> None:
        table = change.after
        assert isinstance(table, Table)
        self.emit(
            table.name,
            f"CREATE TABLE {table.short_name}",
            "meta",
            sql=create_table_sql(table),
            postcheck=(f"DESCRIBE TABLE {quote_qualified(table.name)}"),
            undo_hint=f"DROP TABLE {quote_qualified(table.name)}",
        )
        # Tags and CHECK constraints are not part of CREATE TABLE.
        # TODO(verify): recent runtimes may accept inline CHECK constraints; we
        # do not rely on it. https://docs.databricks.com/aws/en/tables/constraints
        for check in table.checks():
            self._emit_check(table.name, check)
        if table.tags:
            self.emit(
                table.name,
                "SET TAGS",
                "meta",
                sql=set_tags_sql(table.name, table.tags),
            )

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
            f"({', '.join(quote_ident(name) for name in columns)})" if columns else "NONE"
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
                f"ALTER TABLE {quote_qualified(change.table)} SET TBLPROPERTIES "
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
            sql=set_tags_sql(change.table, ((change.path, value),)),
        )

    # -- columns -----------------------------------------------------------
    def _add_column(self, change: Change, facts: TableFacts) -> None:
        column = change.after
        assert isinstance(column, Field)
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
        if not column.nullable:
            self._emit_set_not_null(
                change,
                facts,
                warnings=(NEW_NOT_NULL_WARNING,),
            )

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


def set_tags_sql(table: str, tags: tuple[tuple[str, str], ...]) -> str:
    """TODO(verify): tag syntax against a live workspace.
    https://docs.databricks.com/aws/en/database-objects/tags
    """
    pairs = ", ".join(
        f"{quote_literal(key)} = {quote_literal(value)}" for key, value in tags
    )
    return f"ALTER TABLE {quote_qualified(table)} SET TAGS ({pairs})"


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
    if table.cluster_by:
        clustering = ", ".join(quote_ident(name) for name in table.cluster_by)
        sql.append(f"CLUSTER BY ({clustering})")
    if table.comment is not None:
        sql.append(f"COMMENT {quote_literal(table.comment)}")
    sql.append(f"TBLPROPERTIES (\n{rendered_properties}\n)")
    return "\n".join(sql)


def _column_definition(column: Field) -> str:
    definition = f"{quote_ident(column.name)} {render_type(column.type, upper=True)}"
    if not column.nullable:
        definition += " NOT NULL"
    if column.comment is not None:
        definition += f" COMMENT {quote_literal(column.comment)}"
    return definition


def _replace_leaf(path: str, leaf: str) -> str:
    parent, _, _ = path.rpartition(".")
    return f"{parent}.{leaf}" if parent else leaf


def _constraint_name(constraint: object, table: str) -> str:
    if isinstance(constraint, Check):
        return constraint.name
    if isinstance(constraint, PrimaryKey):
        return constraint.name or f"{table.split('.')[-1]}_pk"
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
        table_diff.desired is not None
        and table_diff.live is not None
        and any(needs_rewrite(change) for change in table_diff.changes)
    )


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

    @property
    def complete(self) -> bool:
        return not self.problems


def build_projection(desired: Table, live: Table) -> Projection:
    """The SELECT list that turns the live table into the desired one."""
    expressions: list[str] = []
    problems: list[str] = []
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
    return Projection(tuple(expressions), tuple(problems))


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
    if table.cluster_by:
        clustering = ", ".join(quote_ident(name) for name in table.cluster_by)
        clauses.append(f"CLUSTER BY ({clustering})")
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
        properties=(*desired.properties, (MANAGED_PROPERTY, "true")),
    )


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
