"""How a plan is described, independent of how it is drawn.

The terminal and the pull-request comment say the same thing about the same
change; only the styling differs. Keeping the words here means they can't drift
apart.
"""

from __future__ import annotations

from deltaplan.model.change import CREATE_KINDS, Change
from deltaplan.model.table import Check, ForeignKey, PrimaryKey, RowFilter, Table
from deltaplan.model.types import Decimal, Field, Mask, as_data_type, render_type
from deltaplan.model.view import View, normalise_query


def human_bytes(size: int | None) -> str | None:
    """`442381631488` -> `412 GB`."""
    if size is None:
        return None
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            rendered = f"{value:.0f}" if value >= 10 or unit == "B" else f"{value:.1f}"
            return f"{rendered} {unit}"
        value /= 1024
    return None  # pragma: no cover - the loop always returns


def display_name(name: str) -> str:
    """`main.sales.orders` -> `sales.orders`: the catalog is the target's job."""
    parts = name.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else name


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def table_verb(kinds: set[str]) -> tuple[str, str]:
    """The marker and verb for a table's header line."""
    if not kinds:
        return " ", "no changes"
    if kinds & CREATE_KINDS:
        return "+", "create"
    if "drop_table" in kinds:
        return "-", "destroy"
    return "~", "update"


def describe(change: Change) -> tuple[str, str]:
    """The marker and the text for one change."""
    leaf = relative_path(change)
    match change.kind:
        case "create_table":
            table = change.after
            columns = len(table.columns) if isinstance(table, Table) else 0
            return "+", _count(columns, "column")
        case "rename_table":
            # Renames stay within a schema, so the old table name says it all.
            return "→", f"renamed from {str(change.before).rsplit('.', 1)[-1]}"
        case "drop_table":
            table = change.before
            columns = len(table.columns) if isinstance(table, Table) else 0
            return "-", (
                f"{_count(columns, 'column')} — its spec is gone and the schema is strict"
            )
        case "create_view":
            return "+", ("view")
        case "create_function":
            return "+", ("function")
        case "create_schema":
            return "+", ("schema")
        case "set_schema_comment":
            return "~", ("comment")
        case "replace_function":
            return "~", ("definition")
        case "replace_view":
            before, after = change.before, change.after
            if isinstance(before, View) and isinstance(after, View):
                changed = []
                if normalise_query(before.query) != normalise_query(after.query):
                    changed.append("query")
                if before.comment != after.comment:
                    changed.append("comment")
                return "~", (" and ".join(changed) or "definition")
            return "~", ("definition")
        case "claim_table":
            return "+", ("ownership — deltaplan manages this table from now on")
        case "add_column":
            column = change.after
            rendered = (
                render_type(column.type, upper=True) if isinstance(column, Field) else ""
            )
            return "+", (f"{leaf} {rendered}".strip())
        case "drop_column":
            return "-", (leaf)
        case "rename_column":
            return "→", (f"{leaf} (was {change.before})")
        case "change_type":
            return "~", (f"{leaf}  {_type_change(change)}")
        case "set_nullable":
            state = "NOT NULL" if change.after is False else "nullable"
            return "~", (f"{leaf}  {state}")
        case "set_comment":
            return "~", (f"{leaf}  comment")
        case "set_table_comment":
            return "~", ("comment")
        case "set_cluster_by":
            columns = change.after if isinstance(change.after, tuple) else ()
            label = (
                "cluster_by auto"
                if change.after == "auto"
                else f"cluster_by [{', '.join(columns)}]"
                if columns
                else "cluster_by none"
            )
            return "~", (label)
        case "set_property":
            return "~", (f"property {change.path} = {change.after!r}")
        case "set_tag":
            return "~", (f"tag {change.path} = {change.after!r}")
        case "set_mask":
            mask = change.after
            name = mask.function if isinstance(mask, Mask) else "?"
            return "~", (f"{leaf}  mask → {name}")
        case "set_row_filter":
            row_filter = change.after
            if isinstance(row_filter, RowFilter):
                columns = ", ".join(row_filter.columns)
                return "~", (f"row filter → {row_filter.function} ON ({columns})")
            return "~", ("row filter")
        case "set_default":
            after = change.after
            return "~", (f"{leaf}  default → {after}" if after else f"{leaf}  no default")
        case "set_identity":
            return "~", (f"{leaf}  identity" if change.after else f"{leaf}  no identity")
        case "set_generated":
            after = change.after
            return "~", (
                f"{leaf}  generated as {after}" if after else f"{leaf}  not generated"
            )
        case "grant":
            privileges = change.after if isinstance(change.after, tuple) else ()
            return "+", (f"grant {', '.join(privileges)} to {change.path}")
        case "revoke":
            privileges = change.before if isinstance(change.before, tuple) else ()
            return "-", (f"revoke {', '.join(privileges)} from {change.path}")
        case "set_column_tag":
            key, value = change.after if isinstance(change.after, tuple) else ("", "")
            return "~", (f"{leaf}  tag {key} = {value!r}")
        case "reorder_columns":
            order = change.after if isinstance(change.after, tuple) else ()
            return "~", (f"column order [{', '.join(order)}]")
        case "add_constraint":
            return "+", (f"constraint {_constraint_label(change.after)}")
        case "drop_constraint":
            return "-", (f"constraint {_constraint_label(change.before)}")


def relative_path(change: Change) -> str:
    """`address.zip` shown under `address` is just `zip`."""
    if change.nested and change.path.startswith(f"{change.column}."):
        return change.path[len(change.column) + 1 :]
    return change.path


def _type_change(change: Change) -> str:
    before = as_data_type(change.before)
    after = as_data_type(change.after)
    if before is None or after is None:
        return "type"
    rendered_before = render_type(before, upper=True)
    rendered_after = render_type(after, upper=True)
    if isinstance(before, Decimal) and isinstance(after, Decimal):
        # `DECIMAL(10,2) → (18,2)` — the design's shorthand for when only the
        # parameters moved.
        rendered_after = f"({after.precision},{after.scale})"
    return f"{rendered_before} → {rendered_after}"


def _constraint_label(constraint: object) -> str:
    if isinstance(constraint, Check):
        return f"{constraint.name} CHECK"
    if isinstance(constraint, PrimaryKey):
        columns = ", ".join(constraint.columns)
        return f"{constraint.name or 'primary key'} PRIMARY KEY ({columns})"
    if isinstance(constraint, ForeignKey):
        columns = ", ".join(constraint.columns)
        referenced = ", ".join(constraint.referenced_columns)
        return (
            f"{constraint.name or 'foreign key'} FOREIGN KEY ({columns}) → "
            f"{display_name(constraint.references)} ({referenced})"
        )
    return "unknown"
