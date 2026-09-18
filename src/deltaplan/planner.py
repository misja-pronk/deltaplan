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

from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, Risk, Step, TableDiff, TableFacts
from deltaplan.model.table import (
    MANAGED_PROPERTY,
    Check,
    PrimaryKey,
    Table,
    default_primary_key_name,
)
from deltaplan.model.types import (
    Decimal,
    Field,
    Primitive,
    as_data_type,
    render_type,
)
from deltaplan.sql import quote_ident, quote_literal, quote_qualified

COLUMN_MAPPING_PROPERTY = "delta.columnMapping.mode"
TYPE_WIDENING_PROPERTY = "delta.enableTypeWidening"

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
    def plan_table(self, diff: TableDiff) -> None:
        for change in diff.changes:
            self._change += 1
            self.plan_change(change, diff.facts)

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
        before, after = change.before, change.after
        # A map key cannot be altered in place, whatever the types involved.
        is_map_key = change.path.endswith(".key")
        if is_map_key or not widens(before, after):
            self._emit_rewrite(
                change,
                facts,
                title="REWRITE",
                note=(
                    "a map key cannot be altered in place"
                    if is_map_key
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
        if change.nested:
            # TODO(verify): nested fields cannot be made NOT NULL in place on any
            # runtime we know of; plan it as a rewrite rather than a failing step.
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
            precheck=(
                f"SELECT count(*) AS nulls FROM {table} "
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
