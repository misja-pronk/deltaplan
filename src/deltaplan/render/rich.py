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

import io
from collections.abc import Iterable

from rich.console import Console
from rich.text import Text

from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, Risk, Step, TableDiff
from deltaplan.render.labels import describe, display_name, human_bytes, table_verb

RISK_STYLE: dict[Risk, str] = {
    "meta": "dim",
    "feature": "yellow",
    "rewrite": "magenta",
    "destructive": "red",
}

MARKER_STYLE = {"+": "green", "-": "red", "~": "yellow", "→": "cyan", "↻": "magenta"}

TITLE_WIDTH = 28


def render_plan(plan: Plan, console: Console) -> None:
    """Print a plan the way the design document draws it."""
    printed = False
    offset = 0
    for diff in plan.diffs:
        if diff.changes or diff.unmanaged or diff.notes:
            if printed:
                console.print()
            _render_table(plan, diff, console, offset)
            printed = True
        offset += len(diff.changes)

    # Whether anything would change decides this — not whether there was anything
    # to say. A table with only a note still has no changes.
    changing = any(diff.changes for diff in plan.diffs)
    if not changing:
        if printed:
            console.print()
        console.print(Text("No changes. Live tables match your specs.", style="green"))
    if plan.orphaned_tables:
        console.print()
        console.print(
            Text(
                f"{len(plan.orphaned_tables)} managed "
                f"{'table has' if len(plan.orphaned_tables) == 1 else 'tables have'} "
                "no spec; the schema is additive, so they stay:",
                style="yellow",
            )
        )
        for name in plan.orphaned_tables:
            console.print(Text(f"  · {name}", style="yellow"))
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
    if not changing:
        return
    console.print()
    console.print(Text(str(plan.summary), style="bold"))
    # What the pull-request comment raises as alerts, the terminal says too — so
    # nobody learns about it from `apply` refusing.
    destructive = [step.id for step in plan.steps if step.risk == "destructive"]
    if destructive:
        console.print(
            Text(
                f"⚠ Destroys something (step {', '.join(map(str, destructive))}): "
                "`apply` needs --allow-destructive.",
                style="red",
            )
        )
    unrunnable = [step.id for step in plan.steps if step.sql is None]
    if unrunnable:
        console.print(
            Text(
                f"⚠ Step {', '.join(map(str, unrunnable))} can't be generated: `apply` "
                "will refuse this plan. See the notes above.",
                style="yellow",
            )
        )


def plan_text(plan: Plan, *, width: int = 100) -> str:
    """The same output as a plain string — for tests, pipes and files."""
    # Its own buffer: a recording console still writes to stdout otherwise.
    console = Console(
        file=io.StringIO(), width=width, no_color=True, highlight=False, record=True
    )
    render_plan(plan, console)
    return console.export_text()


# ---------------------------------------------------------------------------


def number_width(plan: Plan) -> int:
    """Digits in the plan's last step number: step 9 lines up with step 10."""
    return len(str(len(plan.steps)))


def _render_table(plan: Plan, diff: TableDiff, console: Console, offset: int) -> None:
    """One table's block. `offset` is where its changes start in the plan."""
    console.print(_table_header(diff))
    width = number_width(plan)

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
        console.print(_line(1, "↻", Text(_rest_label(rest))))
        for step in rest:
            _render_step(step, console, indent=2, width=width)

    for item in diff.unmanaged:
        console.print(Text(f"  · {item} — unmanaged, left untouched", style="dim"))
    for note in diff.notes:
        console.print(Text(f"  · {note}", style="dim"))


VERB_STYLE = {
    "no changes": "dim",
    "create": "green",
    "destroy": "red",
    "update": "yellow",
}


def _rest_label(rest: list[Step]) -> str:
    """What the steps that belong to the table rather than a change are."""
    if any(step.risk == "rewrite" for step in rest):
        return "rewrite"
    if all(step.title.endswith("hook") for step in rest):
        return "hooks"
    # Put back after a replace, or out of the way of a change and back again.
    return "around the changes"


def _table_header(diff: TableDiff) -> Text:
    marker, verb = table_verb({change.kind for change in diff.changes})
    style = VERB_STYLE[verb]
    line = Text(display_name(diff.table), style="bold")
    line.append("   ")
    line.append(f"{marker} {verb}", style=style)
    size = human_bytes(diff.facts.size_bytes)
    if size is not None:
        line.append(f"  ({size})", style="dim")
    return line


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
    marker, label = describe(change)
    console.print(_line(indent, marker, Text(label)))
    # Steps always sit one level in from the column, whether the change they
    # implement is the column itself or something nested inside it.
    printed: set[int] = set()
    for step in plan.steps_for_change(index):
        _render_step(step, console, indent=2, width=number_width(plan))
        printed.add(step.id)
    return printed


def _line(indent: int, marker: str, label: Text) -> Text:
    line = Text("  " * indent)
    line.append(f"{marker} ", style=MARKER_STYLE.get(marker, ""))
    line.append_text(label)
    return line


def _render_step(step: Step, console: Console, *, indent: int, width: int) -> None:
    line = Text("  " * indent)
    line.append(f"{step.id:>{width}}. ", style="dim")
    line.append(step.title.ljust(TITLE_WIDTH) + " ")
    line.append(f"[{step.risk}]", style=RISK_STYLE[step.risk])
    size = human_bytes(step.est_bytes) if step.risk == "rewrite" else None
    if size is not None:
        line.append(f"  ({size})", style="dim")
    console.print(line)
    # Warnings and notes hang under the step's text, past its number.
    hanging = "  " * indent + " " * (width + 2)
    for warning in step.warnings:
        console.print(Text(f"{hanging}⚠ {warning}", style="yellow"))
    if step.note:
        console.print(Text(f"{hanging}· {step.note}", style="dim"))
