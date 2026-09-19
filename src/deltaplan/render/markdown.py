"""The plan as a pull-request comment.

Same plan object, same words as the terminal (`render/labels.py`), laid out for
GitHub: a summary a reviewer can take in at a glance, an alert for anything
destructive or expensive, then one collapsible block per table with the changes,
the numbered steps, and the SQL that `apply` will run.

The first line is a hidden marker. The GitHub Action uses it to find the comment
it posted last time and update it, rather than adding a new one on every push.
"""

from __future__ import annotations

import re

from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, Step, TableDiff
from deltaplan.render.labels import (
    count,
    describe,
    display_name,
    human_bytes,
    table_verb,
)

#: GitHub refuses comments longer than 65,536 characters. Leave room.
COMMENT_LIMIT = 60_000

RISK_ICON = {"meta": "🟢", "feature": "🟡", "rewrite": "🟠", "destructive": "🔴"}
RISK_BADGE = {risk: f"{icon} {risk}" for risk, icon in RISK_ICON.items()}


def marker(heading: str, target: str) -> str:
    """The hidden first line that identifies deltaplan's comment for a target."""
    return f"<!-- deltaplan:{heading}:{target} -->"


def render_markdown(
    plan: Plan, *, heading: str = "plan", limit: int = COMMENT_LIMIT
) -> str:
    """A GitHub-flavoured Markdown rendering of a plan.

    Falls back to leaving the SQL out, then to naming the tables only, when the
    full rendering would be too long for a comment — and says so.
    """
    full = _render(plan, heading, include_sql=True)
    if len(full) <= limit:
        return full
    without_sql = _render(plan, heading, include_sql=False, note=_TRUNCATED_SQL)
    if len(without_sql) <= limit:
        return without_sql
    return _render_summary_only(plan, heading)


_TRUNCATED_SQL = (
    "SQL left out: the full plan is too long for a comment. "
    "Run `deltaplan show plan.json` to see every statement."
)


def _render(
    plan: Plan, heading: str, *, include_sql: bool, note: str | None = None
) -> str:
    lines = [marker(heading, plan.target), _title(plan, heading), ""]
    changed = [diff for diff in plan.diffs if diff.changes]

    if not changed:
        lines += ["✅ **No changes.** Live tables match your specs.", ""]
    else:
        lines += [f"**{plan.summary}**", ""]
        lines += _alerts(plan)

    for diff in changed:
        lines += _table_block(plan, diff, include_sql=include_sql)

    lines += _left_alone(plan)
    if note:
        lines += [f"> [!NOTE]\n> {note}", ""]
    lines += [_footer(plan)]
    return "\n".join(lines) + "\n"


def _render_summary_only(plan: Plan, heading: str) -> str:
    lines = [marker(heading, plan.target), _title(plan, heading), ""]
    lines += [f"**{plan.summary}**", ""]
    lines += _alerts(plan)
    lines += ["| Table | | Steps |", "|---|---|--:|"]
    for diff in plan.diffs:
        if diff.changes:
            symbol, verb = table_verb({change.kind for change in diff.changes})
            count = len(plan.steps_for(diff.table))
            name = _code(_cell(display_name(diff.table)))
            lines.append(f"| {name} | {symbol} {verb} | {count} |")
    lines += [
        "",
        "> [!NOTE]\n> The full plan is too long for a comment. "
        "Run `deltaplan show plan.json` to see it.",
        "",
        _footer(plan),
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------


def _title(plan: Plan, heading: str) -> str:
    status = RISK_ICON[plan.highest_risk] if plan.steps else "✅"
    return f"### {status} deltaplan {heading} · {_code(_cell(plan.target))}"


def _alerts(plan: Plan) -> list[str]:
    """GitHub alert blocks for what a reviewer must not miss."""
    lines: list[str] = []
    destructive = [step for step in plan.steps if step.risk == "destructive"]
    if destructive:
        listed = ", ".join(
            f"{step.id} ({step.title} on {_code(display_name(step.table))})"
            for step in destructive
        )
        lines += [
            "> [!CAUTION]",
            f"> This plan destroys something — step {listed}. "
            "`apply` refuses it without `--allow-destructive`.",
            "",
        ]
    rewritten = sorted({step.table for step in plan.steps if step.risk == "rewrite"})
    if rewritten:
        sizes = []
        for name in rewritten:
            size = next(
                (
                    human_bytes(step.est_bytes)
                    for step in plan.steps_for(name)
                    if step.est_bytes
                ),
                None,
            )
            sizes.append(_code(display_name(name)) + (f" ({size})" if size else ""))
        lines += [
            "> [!WARNING]",
            f"> Rewrites the data of {', '.join(sizes)}. "
            "A restore point is recorded first.",
            "",
        ]
    unrunnable = [step for step in plan.steps if step.sql is None]
    if unrunnable:
        lines += [
            "> [!IMPORTANT]",
            f"> {count(len(unrunnable), 'step')} can't be generated, so `apply` "
            "will refuse this plan. See the notes below.",
            "",
        ]
    return lines


def _table_block(plan: Plan, diff: TableDiff, *, include_sql: bool) -> list[str]:
    symbol, verb = table_verb({change.kind for change in diff.changes})
    size = human_bytes(diff.facts.size_bytes)
    summary = f"<b>{_html(display_name(diff.table))}</b> · {symbol} {verb}"
    if size:
        summary += f" · {size}"

    lines = ["<details open>", f"<summary>{summary}</summary>", ""]
    lines += ["```diff", *_change_lines(diff.changes), "```", ""]

    steps = plan.steps_for(diff.table)
    if steps:
        lines += ["| # | Step | Risk | |", "|--:|---|---|---|"]
        lines += [_step_row(step) for step in steps]
        lines.append("")
        if include_sql:
            lines += _sql_block(steps)

    if diff.unmanaged:
        listed = ", ".join(_code(_cell(item)) for item in diff.unmanaged)
        lines += [f"<sub>Left untouched (unmanaged): {listed}</sub>", ""]
    for note in diff.notes:
        lines += [f"<sub>{_html(note)}</sub>", ""]

    lines += ["</details>", ""]
    return lines


def _change_lines(changes: tuple[Change, ...]) -> list[str]:
    """One line per change, marker first so GitHub colours `+` and `-`."""
    lines: list[str] = []
    containers: set[str] = set()
    for change in changes:
        marker_, label = describe(change)
        if change.nested:
            if change.column not in containers and not any(
                c.path == change.column for c in changes
            ):
                lines.append(f"~ {change.column}")
            containers.add(change.column)
            lines.append(f"{marker_}   {label}")
        else:
            lines.append(f"{marker_} {label}")
    return lines


def _step_row(step: Step) -> str:
    notes: list[str] = [f"⚠️ {warning}" for warning in step.warnings]
    if step.note:
        notes.append(step.note)
    if step.undo_hint:
        notes.append(f"undo: {_code(_cell(step.undo_hint))}")
    return (
        f"| {step.id} | {_cell(step.title)} | {RISK_BADGE[step.risk]} | "
        f"{'<br>'.join(_cell(note) for note in notes)} |"
    )


def _sql_block(steps: tuple[Step, ...]) -> list[str]:
    statements = [
        f"-- {step.id}. {step.title}\n{step.sql};"
        for step in steps
        if step.sql is not None
    ]
    if not statements:
        return []
    return [
        "<details><summary>SQL</summary>",
        "",
        "```sql",
        "\n\n".join(statements),
        "```",
        "",
        "</details>",
        "",
    ]


def _left_alone(plan: Plan) -> list[str]:
    lines: list[str] = []
    # Notes on tables with changes are in their own block; these are the rest.
    quiet = [
        (diff.table, note)
        for diff in plan.diffs
        if not diff.changes
        for note in diff.notes
    ]
    for name, note in quiet:
        lines += [f"<sub>{_code(display_name(name))}: {_html(note)}</sub>", ""]
    if plan.orphaned_tables:
        names = ", ".join(_code(display_name(n)) for n in plan.orphaned_tables)
        lines += [
            f"**Kept:** {names} — created by deltaplan, no longer in any spec. "
            "The schema is additive, so they stay.",
            "",
        ]
    if plan.unmanaged_tables:
        names = ", ".join(_code(display_name(n)) for n in plan.unmanaged_tables)
        lines += [f"<sub>Unmanaged, left untouched: {names}</sub>", ""]
    return lines


def _footer(plan: Plan) -> str:
    return (
        f"<sub>deltaplan {_cell(plan.tool_version)} · specs `{plan.spec_hash}` · "
        f"live state `{plan.state_fingerprint}`</sub>"
    )


def _code(text: str) -> str:
    """Inline code that survives backticks in it — an undo hint quotes every name,
    and `RESTORE TABLE `a`.`b`` would end the span at the first one. CommonMark:
    a longer fence than any run inside, padded with a space it strips again."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    if not longest:
        return f"`{text}`"
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}"


def _cell(text: str) -> str:
    """Safe inside a Markdown table cell: no pipes, no line breaks."""
    return text.replace("|", "\\|").replace("\n", " ")


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
