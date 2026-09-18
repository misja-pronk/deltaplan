"""The terminal view of a plan.

The layout is the one in the design document: a table header with its size, one
line per change with a marker, nested changes as a tree under the column they
belong to, and the numbered steps that implement each change with their risk
class and warnings.

    sales.orders   ~ update  (412 GB)
      ~ amount  DECIMAL(10,2) → (18,2)
        1. enable typeWidening        [feature]
        2. ALTER COLUMN TYPE          [meta]
      ~ address
        + zip STRING
        3. ADD COLUMN address.zip     [meta]
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.console import Console
from rich.text import Text

from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, Risk, Step, TableDiff
from deltaplan.model.table import Check, PrimaryKey, Table
from deltaplan.model.types import Decimal, Field, as_data_type, render_type

RISK_STYLE: dict[Risk, str] = {
    "meta": "dim",
    "feature": "yellow",
    "rewrite": "magenta",
    "destructive": "red",
}

MARKER_STYLE = {"+": "green", "-": "red", "~": "yellow", "→": "cyan", "↻": "magenta"}

TITLE_WIDTH = 28


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


def render_plan(plan: Plan, console: Console) -> None:
    """Print a plan the way the design document draws it."""
    printed = False
    offset = 0
    for diff in plan.diffs:
        if diff.changes or diff.unmanaged:
            if printed:
                console.print()
            _render_table(plan, diff, console, offset)
            printed = True
        offset += len(diff.changes)

    if not printed:
        console.print(Text("No changes. Live tables match your specs.", style="green"))
    if plan.unmanaged_tables:
        console.print()
        console.print(
            Text(
                f"{len(plan.unmanaged_tables)} unmanaged "
                f"{'table' if len(plan.unmanaged_tables) == 1 else 'tables'} "
                "in these schemas, left untouched:",
                style="dim",
            )
        )
        for name in plan.unmanaged_tables:
            console.print(Text(f"  · {name}", style="dim"))
    if not printed:
        return
    console.print()
    console.print(Text(str(plan.summary), style="bold"))


def plan_text(plan: Plan, *, width: int = 100) -> str:
    """The same output as a plain string — for tests, pipes and files."""
    console = Console(width=width, no_color=True, highlight=False, record=True)
    render_plan(plan, console)
    return console.export_text()


# ---------------------------------------------------------------------------


def _render_table(plan: Plan, diff: TableDiff, console: Console, offset: int) -> None:
    """One table's block. `offset` is where its changes start in the plan."""
    console.print(_table_header(diff))

    numbered = list(enumerate(diff.changes, start=offset))
    shown: set[int] = set()
    for column, changes in _group_by_column(numbered).items():
        if not column:
            # Table-level changes — comment, clustering, properties, tags,
            # constraints — sit directly under the table, not inside a column.
            for index, change in changes:
                shown |= _render_change(plan, index, change, console, indent=1)
            continue
        direct = [(index, c) for index, c in changes if c.path == column]
        nested = [(index, c) for index, c in changes if c.path != column]
        if nested and not direct:
            # The column itself is unchanged; it is only a container for what is
            # nested below it.
            console.print(_line(1, "~", Text(column)))
        for index, change in direct:
            shown |= _render_change(plan, index, change, console, indent=1)
        for index, change in nested:
            shown |= _render_change(plan, index, change, console, indent=2)

    # A rewrite rebuilds the table rather than patching it, so its steps belong to
    # the table rather than to any one change above.
    rest = [step for step in plan.steps_for(diff.table) if step.id not in shown]
    if rest:
        console.print(_line(1, "↻", Text("rewrite")))
        for step in rest:
            _render_step(step, console, indent=2)

    for item in diff.unmanaged:
        console.print(Text(f"  · {item} — unmanaged, left untouched", style="dim"))


def _table_header(diff: TableDiff) -> Text:
    creating = any(change.kind == "create_table" for change in diff.changes)
    if not diff.changes:
        marker, verb, style = " ", "no changes", "dim"
    elif creating:
        marker, verb, style = "+", "create", "green"
    else:
        marker, verb, style = "~", "update", "yellow"
    line = Text(_display_name(diff.table), style="bold")
    line.append("   ")
    line.append(f"{marker} {verb}", style=style)
    size = human_bytes(diff.facts.size_bytes)
    if size is not None:
        line.append(f"  ({size})", style="dim")
    return line


def _display_name(name: str) -> str:
    """`main.sales.orders` -> `sales.orders`: the catalog is the target's job."""
    parts = name.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else name


def _group_by_column(
    changes: Iterable[tuple[int, Change]],
) -> dict[str, list[tuple[int, Change]]]:
    grouped: dict[str, list[tuple[int, Change]]] = {}
    for index, change in changes:
        grouped.setdefault(change.column, []).append((index, change))
    return grouped


def _render_change(
    plan: Plan,
    index: int,
    change: Change,
    console: Console,
    *,
    indent: int,
) -> set[int]:
    """Print one change and its steps. Returns the step ids it printed."""
    marker, label = _describe(change)
    console.print(_line(indent, marker, label))
    # Steps always sit one level in from the column, whether the change they
    # implement is the column itself or something nested inside it.
    printed: set[int] = set()
    for step in plan.steps_for_change(index):
        _render_step(step, console, indent=2)
        printed.add(step.id)
    return printed


def _line(indent: int, marker: str, label: Text) -> Text:
    line = Text("  " * indent)
    line.append(f"{marker} ", style=MARKER_STYLE.get(marker, ""))
    line.append_text(label)
    return line


def _render_step(step: Step, console: Console, *, indent: int) -> None:
    line = Text("  " * indent)
    line.append(f"{step.id}. ", style="dim")
    line.append(step.title.ljust(TITLE_WIDTH) + " ")
    line.append(f"[{step.risk}]", style=RISK_STYLE[step.risk])
    size = human_bytes(step.est_bytes) if step.risk == "rewrite" else None
    if size is not None:
        line.append(f"  ({size})", style="dim")
    console.print(line)
    # Warnings and notes hang under the step's text, past its number.
    hanging = "  " * indent + "   "
    for warning in step.warnings:
        console.print(Text(f"{hanging}⚠ {warning}", style="yellow"))
    if step.note:
        console.print(Text(f"{hanging}· {step.note}", style="dim"))


# ---------------------------------------------------------------------------
# change labels
# ---------------------------------------------------------------------------


def _describe(change: Change) -> tuple[str, Text]:
    """The marker and the text for one change."""
    leaf = _relative_path(change)
    match change.kind:
        case "create_table":
            table = change.after
            columns = len(table.columns) if isinstance(table, Table) else 0
            return "+", Text(f"{columns} columns")
        case "add_column":
            column = change.after
            rendered = (
                render_type(column.type, upper=True) if isinstance(column, Field) else ""
            )
            return "+", Text(f"{leaf} {rendered}".strip())
        case "drop_column":
            return "-", Text(leaf)
        case "rename_column":
            return "→", Text(f"{leaf} (was {change.before})")
        case "change_type":
            return "~", Text(f"{leaf}  {_type_change(change)}")
        case "set_nullable":
            state = "NOT NULL" if change.after is False else "nullable"
            return "~", Text(f"{leaf}  {state}")
        case "set_comment":
            return "~", Text(f"{leaf}  comment")
        case "set_table_comment":
            return "~", Text("comment")
        case "set_cluster_by":
            columns = change.after if isinstance(change.after, tuple) else ()
            label = f"cluster_by [{', '.join(columns)}]" if columns else "cluster_by none"
            return "~", Text(label)
        case "set_property":
            return "~", Text(f"property {change.path} = {change.after!r}")
        case "set_tag":
            return "~", Text(f"tag {change.path} = {change.after!r}")
        case "reorder_columns":
            order = change.after if isinstance(change.after, tuple) else ()
            return "~", Text(f"column order [{', '.join(order)}]")
        case "add_constraint":
            return "+", Text(f"constraint {_constraint_label(change.after)}")
        case "drop_constraint":
            return "-", Text(f"constraint {_constraint_label(change.before)}")


def _relative_path(change: Change) -> str:
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
    return "unknown"
