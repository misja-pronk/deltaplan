"""A plan as one self-contained page, for reading rather than for machines.

The terminal rendering is the one most people see, and it is fine until a plan
has thirty tables in it: then you want to search, to fold a table away, to see
only what is destructive, and to read a rewrite's SQL without scrolling past
everything else. That is navigation, not rendering, and a browser does it for
free.

What this is not: a second source of truth. The page is built from a `Plan` —
the same object `render_plan` and `render_markdown` take, with the same words
from `labels.py` — and it says nothing the plan doesn't. Nothing in it talks to
a workspace, and there is no apply button: a plan is a thing to read, and a
change is a thing to make in a spec.

One file, no dependencies, nothing fetched: the CSS and the script are inline, so
the page opens from disk, survives being attached to a pull request, and works on
a laptop with no network.
"""

from __future__ import annotations

from html import escape

from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, Step, TableDiff
from deltaplan.render.labels import (
    count,
    describe,
    display_name,
    human_bytes,
    listed,
    table_verb,
)

#: Risk classes, worst first — the order the filter offers them in.
RISKS = ("destructive", "rewrite", "feature", "meta")

_CSS = """\
:root { color-scheme: light dark; --edge: #8883; --dim: #8888; }
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 2rem 1.5rem 4rem; max-width: 62rem;
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 { font-size: 1.3rem; margin: 0 0 .2rem; }
.meta, .footnote { color: var(--dim); font-size: .85rem; }
.summary { font-weight: 600; margin: 1rem 0; }
.controls { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center;
  margin: 1rem 0 1.5rem; }
input[type=search] {
  flex: 1 1 14rem; padding: .45rem .6rem; border: 1px solid var(--edge);
  border-radius: .4rem; background: transparent; color: inherit; font: inherit;
}
button {
  padding: .4rem .7rem; border: 1px solid var(--edge); border-radius: .4rem;
  background: transparent; color: inherit; font: inherit; cursor: pointer;
}
button[aria-pressed=true] { border-color: currentColor; font-weight: 600; }
details.table { border-top: 1px solid var(--edge); padding: .5rem 0; }
details.table[hidden] { display: none; }
summary { cursor: pointer; display: flex; gap: .6rem; align-items: baseline; }
summary::marker { color: var(--dim); }
.name { font-weight: 600; }
.verb { color: var(--dim); }
.size { color: var(--dim); margin-left: auto; font-variant-numeric: tabular-nums; }
.changes { list-style: none; margin: .6rem 0 .2rem; padding: 0 0 0 1.2rem; }
.changes li { padding: .1rem 0; }
.changes li.nested { padding-left: 1.4rem; }
.mark { display: inline-block; width: 1rem; color: var(--dim); }
ol.steps { margin: .4rem 0 .2rem; padding-left: 2.4rem; }
ol.steps li { margin: .35rem 0; }
.step-title { font-weight: 600; }
pre {
  margin: .35rem 0 0; padding: .6rem .7rem; overflow-x: auto;
  border: 1px solid var(--edge); border-radius: .4rem;
  font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
}
.note { color: var(--dim); }
.warn { color: #b26a00; }
.risk {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .04em;
  border: 1px solid currentColor; border-radius: .25rem; padding: 0 .3rem;
}
.risk-destructive { color: #c0392b; }
.risk-rewrite { color: #8e44ad; }
.risk-feature { color: #b26a00; }
.risk-meta { color: var(--dim); }
section.aside { margin-top: 2rem; }
section.aside h2 { font-size: .95rem; margin: 0 0 .3rem; }
section.aside ul { margin: 0; padding-left: 1.2rem; color: var(--dim); }
"""

_JS = """\
const search = document.getElementById('search');
const tables = [...document.querySelectorAll('details.table')];
const buttons = [...document.querySelectorAll('button[data-risk]')];
function apply() {
  const needle = search.value.trim().toLowerCase();
  const wanted = buttons.filter(b => b.getAttribute('aria-pressed') === 'true')
                        .map(b => b.dataset.risk);
  for (const table of tables) {
    const name = table.dataset.name;
    const risks = (table.dataset.risks || '').split(' ');
    const matches = !needle || name.includes(needle);
    const risky = !wanted.length || wanted.some(r => risks.includes(r));
    table.hidden = !(matches && risky);
  }
}
search.addEventListener('input', apply);
for (const button of buttons) {
  button.addEventListener('click', () => {
    const on = button.getAttribute('aria-pressed') === 'true';
    button.setAttribute('aria-pressed', String(!on));
    apply();
  });
}
"""


def render_html(plan: Plan, *, title: str | None = None) -> str:
    """A plan as a complete HTML document.

    `title` names it in the browser tab; by default the target does.
    """
    heading = title or f"deltaplan · {plan.target}"
    body = [
        f"<h1>{escape(heading)}</h1>",
        f'<p class="meta">{_meta(plan)}</p>',
        f'<p class="summary">{escape(str(plan.summary))}</p>',
        _controls(),
    ]
    shown = [diff for diff in plan.diffs if diff.changes or diff.unmanaged or diff.notes]
    if not any(diff.changes for diff in plan.diffs):
        body.append("<p>No changes. Live tables match your specs.</p>")
    body.extend(_table(plan, diff) for diff in shown)
    body.extend(_asides(plan))
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(heading)}</title>\n<style>\n{_CSS}</style>\n"
        "</head>\n<body>\n" + "\n".join(body) + f"\n<script>\n{_JS}</script>\n"
        "</body>\n</html>\n"
    )


def _meta(plan: Plan) -> str:
    parts = [
        f"target <strong>{escape(plan.target)}</strong>",
        f"deltaplan {escape(plan.tool_version)}",
        f"specs <code>{escape(plan.spec_hash)}</code>",
        f"live state <code>{escape(plan.state_fingerprint)}</code>",
    ]
    return " · ".join(parts)


def _controls() -> str:
    buttons = "".join(
        f'<button type="button" data-risk="{risk}" aria-pressed="false">{risk}</button>'
        for risk in RISKS
    )
    return (
        '<div class="controls">'
        '<input id="search" type="search" placeholder="Filter tables by name…" '
        'aria-label="Filter tables by name">'
        f"{buttons}</div>"
    )


def _table(plan: Plan, diff: TableDiff) -> str:
    """One table: its header, what changes about it, and the steps that do it."""
    marker, verb = table_verb({change.kind for change in diff.changes})
    size = human_bytes(diff.facts.size_bytes)
    risks = sorted({step.risk for step in diff.steps})
    head = (
        "<summary>"
        f'<span class="mark">{escape(marker)}</span>'
        f'<span class="name">{escape(display_name(diff.table))}</span>'
        f'<span class="verb">{escape(verb)}</span>'
        + "".join(f'<span class="risk risk-{r}">{r}</span>' for r in risks)
        + (f'<span class="size">{escape(size)}</span>' if size else "")
        + "</summary>"
    )
    inside = [_changes(diff), _steps(plan, diff)]
    if diff.notes:
        inside.append(
            '<p class="note">' + "<br>".join(escape(note) for note in diff.notes) + "</p>"
        )
    if diff.unmanaged:
        inside.append(
            '<p class="footnote">not modelled, left alone: '
            + escape(", ".join(diff.unmanaged))
            + "</p>"
        )
    return (
        f'<details class="table" open data-name="{escape(diff.table.lower())}" '
        f'data-risks="{escape(" ".join(risks))}">{head}'
        + "".join(part for part in inside if part)
        + "</details>"
    )


def _changes(diff: TableDiff) -> str:
    """Each change, indented under the column it is about."""
    if not diff.changes:
        return ""
    items = []
    for change in diff.changes:
        marker, text = describe(change)
        nested = _depth(change) > 1
        items.append(
            f'<li class="{"nested" if nested else ""}">'
            f'<span class="mark">{escape(marker)}</span>{escape(text)}</li>'
        )
    return f'<ul class="changes">{"".join(items)}</ul>'


def _depth(change: Change) -> int:
    """How deep the thing it changes sits: a column is 1, a field inside it 2."""
    return len(change.path.split(".")) if change.path else 0


def _steps(plan: Plan, diff: TableDiff) -> str:
    """The statements, in the order they run."""
    steps = plan.steps_for(diff.table)
    if not steps:
        return ""
    return f'<ol class="steps">{"".join(_step(step) for step in steps)}</ol>'


def _step(step: Step) -> str:
    size = human_bytes(step.est_bytes)
    head = (
        f'<span class="step-title">{escape(step.title)}</span> '
        f'<span class="risk risk-{step.risk}">{step.risk}</span>'
        + (f' <span class="size">{escape(size)}</span>' if size else "")
    )
    lines = [head]
    if step.note:
        lines.append(f'<div class="note">{escape(step.note)}</div>')
    for warning in step.warnings:
        lines.append(f'<div class="warn">⚠ {escape(warning)}</div>')
    if step.sql:
        lines.append(f"<pre>{escape(step.sql)}</pre>")
    if step.undo_hint:
        lines.append(
            f'<div class="note">undo: <code>{escape(step.undo_hint)}</code></div>'
        )
    return f'<li value="{step.id}">{"".join(lines)}</li>'


def _asides(plan: Plan) -> list[str]:
    """What the plan reports without changing: orphans, strangers, handoffs."""
    sections = []
    if plan.orphaned_tables:
        sections.append(
            _aside(
                f"{count(len(plan.orphaned_tables), 'managed table')} with no spec; "
                "the schema is additive, so they stay",
                plan.orphaned_tables,
            )
        )
    if plan.unmanaged_tables:
        sections.append(
            _aside(
                f"{count(len(plan.unmanaged_tables), 'unmanaged table')} in these "
                "schemas, left untouched",
                plan.unmanaged_tables,
            )
        )
    if plan.not_managed:
        sections.append(
            '<section class="aside"><p class="footnote">'
            f"{escape(listed(list(plan.not_managed)))} are managed elsewhere: "
            "deltaplan doesn't read or change them here.</p></section>"
        )
    return sections


def _aside(heading: str, names: tuple[str, ...]) -> str:
    items = "".join(f"<li>{escape(name)}</li>" for name in names)
    return f'<section class="aside"><h2>{escape(heading)}</h2><ul>{items}</ul></section>'
