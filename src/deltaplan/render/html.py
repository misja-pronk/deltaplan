"""A plan as one self-contained page: every object, as it is and as it will be.

The terminal rendering is a list of changes, which is what `apply` needs. What a
person reading a plan wants is the object — what the table looks like now, what
it will look like afterwards, and then, separately, the statements that get from
one to the other. `compare.py` builds that as data; this draws it.

Two readings of the same rows, because a plan has two readers. **Changes only**
is for whoever has to approve it: the rows that move, each with the sentence the
rest of deltaplan uses for it. **Full object** is for whoever wrote the spec: the
same rows with everything the object is made of, so the two sides can be checked
against each other. One table underneath, so they can never say different things.

One file, nothing fetched: the CSS and the script are inline, so the page opens
from disk, survives being attached to a pull request, and works with no network.
It is a way of reading. Nothing here writes, and there is no apply button — a
change is a thing to make in a spec.
"""

from __future__ import annotations

from html import escape

from deltaplan.model.plan import Plan, Step, TableDiff
from deltaplan.render.compare import Comparison, Row, compare
from deltaplan.render.labels import count, human_bytes, listed

#: Risk classes, worst first — the order the filter offers them in.
RISKS = ("destructive", "rewrite", "feature", "meta")

_CSS = """\
:root { color-scheme: light dark; --edge: #8883; --dim: #8888; --sunk: #8881; }
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 2rem 1.5rem 5rem; max-width: 72rem;
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 { font-size: 1.3rem; margin: 0 0 .2rem; }
.meta, .hint, .footnote { color: var(--dim); font-size: .82rem; font-weight: 400; }
.summary { font-weight: 600; margin: 1rem 0; }
.controls {
  display: flex; gap: .5rem; flex-wrap: wrap; align-items: center;
  margin: 1rem 0 1.5rem; position: sticky; top: 0; padding: .6rem 0;
  background: Canvas; border-bottom: 1px solid var(--edge); z-index: 1;
}
input[type=search] {
  flex: 1 1 12rem; padding: .45rem .6rem; border: 1px solid var(--edge);
  border-radius: .4rem; background: transparent; color: inherit; font: inherit;
}
button {
  padding: .4rem .7rem; border: 1px solid var(--edge); border-radius: .4rem;
  background: transparent; color: inherit; font: inherit; cursor: pointer;
}
button[aria-pressed=true] { border-color: currentColor; font-weight: 600; }
.lens { margin-left: auto; display: flex; gap: .3rem; }
details.object { border-top: 1px solid var(--edge); padding: .6rem 0; }
details.object[hidden] { display: none; }
summary { cursor: pointer; display: flex; gap: .6rem; align-items: baseline;
  flex-wrap: wrap; }
summary::marker { color: var(--dim); }
.mark { display: inline-block; width: 1ch; color: var(--dim); }
.name { font-weight: 600; }
.kind, .headline { color: var(--dim); }
.headline { flex: 1 1 20rem; }
table.rows { width: 100%; border-collapse: collapse; margin: .7rem 0 .3rem; }
table.rows th {
  text-align: left; font-size: .78rem; text-transform: uppercase;
  letter-spacing: .04em; color: var(--dim); font-weight: 600;
  border-bottom: 1px solid var(--edge); padding: .3rem .5rem;
}
table.rows td { padding: .22rem .5rem; vertical-align: top; }
table.rows tr.state-changed td.now, table.rows tr.state-removed td.now {
  background: #c0392b18;
}
table.rows tr.state-changed td.after, table.rows tr.state-added td.after {
  background: #2e8b5718;
}
td.what { white-space: nowrap; }
td.now, td.after { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; }
td.said { color: var(--dim); font-size: .85rem; }
.depth-1 td.what { padding-left: 1.6rem; }
.depth-2 td.what { padding-left: 2.8rem; }
tr.detail td.what { font-style: italic; }
.gone { color: var(--dim); }
ol.steps { margin: .5rem 0 .2rem; padding-left: 2.4rem; }
ol.steps li { margin: .35rem 0; }
.step-title { font-weight: 600; }
pre {
  margin: .35rem 0 0; padding: .6rem .7rem; overflow-x: auto;
  border: 1px solid var(--edge); border-radius: .4rem; background: var(--sunk);
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
/* The two readings: one table, two ways of looking at it. */
body[data-lens=changes] tr.state-same { display: none; }
body[data-lens=full] td.said, body[data-lens=full] th.said { display: none; }
body[data-lens=full] .steps-note { display: none; }
"""

_JS = """\
const body = document.body;
const search = document.getElementById('search');
const objects = [...document.querySelectorAll('details.object')];
const risks = [...document.querySelectorAll('button[data-risk]')];
const lenses = [...document.querySelectorAll('button[data-lens]')];
function apply() {
  const needle = search.value.trim().toLowerCase();
  const wanted = risks.filter(b => b.getAttribute('aria-pressed') === 'true')
                      .map(b => b.dataset.risk);
  for (const object of objects) {
    const matches = !needle || object.dataset.name.includes(needle);
    const has = (object.dataset.risks || '').split(' ');
    object.hidden = !(matches && (!wanted.length || wanted.some(r => has.includes(r))));
  }
}
function lens(name) {
  body.dataset.lens = name;
  for (const button of lenses) {
    button.setAttribute('aria-pressed', String(button.dataset.lens === name));
  }
  try { localStorage.setItem('deltaplan-lens', name); } catch (e) { /* private mode */ }
}
search.addEventListener('input', apply);
for (const button of risks) {
  button.addEventListener('click', () => {
    button.setAttribute('aria-pressed',
      String(button.getAttribute('aria-pressed') !== 'true'));
    apply();
  });
}
for (const button of lenses) {
  button.addEventListener('click', () => lens(button.dataset.lens));
}
let remembered = null;
try { remembered = localStorage.getItem('deltaplan-lens'); } catch (e) { /* ignore */ }
lens(remembered === 'full' ? 'full' : 'changes');
"""


def render_html(plan: Plan, *, title: str | None = None) -> str:
    """A plan as a complete HTML document.

    `title` names it in the browser tab; by default the target does.
    """
    heading = title or f"deltaplan · {plan.target}"
    shown = [diff for diff in plan.diffs if diff.changes or diff.unmanaged or diff.notes]
    body = [
        f"<h1>{escape(heading)}</h1>",
        f'<p class="meta">{_meta(plan)}</p>',
        f'<p class="summary">{escape(str(plan.summary))}</p>',
        _controls(),
    ]
    if not any(diff.changes for diff in plan.diffs):
        body.append("<p>No changes. Live objects match your specs.</p>")
    body.extend(_object(plan, diff) for diff in shown)
    body.extend(_asides(plan))
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(heading)}</title>\n<style>\n{_CSS}</style>\n"
        '</head>\n<body data-lens="changes">\n'
        + "\n".join(body)
        + f"\n<script>\n{_JS}</script>\n</body>\n</html>\n"
    )


def _meta(plan: Plan) -> str:
    return " · ".join(
        [
            f"target <strong>{escape(plan.target)}</strong>",
            f"deltaplan {escape(plan.tool_version)}",
            f"specs <code>{escape(plan.spec_hash)}</code>",
            f"live state <code>{escape(plan.state_fingerprint)}</code>",
        ]
    )


def _controls() -> str:
    filters = "".join(
        f'<button type="button" data-risk="{risk}" aria-pressed="false">{risk}</button>'
        for risk in RISKS
    )
    lenses = (
        '<div class="lens">'
        '<button type="button" data-lens="changes" aria-pressed="true">Changes only'
        "</button>"
        '<button type="button" data-lens="full" aria-pressed="false">Full object</button>'
        "</div>"
    )
    return (
        '<div class="controls">'
        '<input id="search" type="search" placeholder="Filter by name…" '
        'aria-label="Filter objects by name">'
        f"{filters}{lenses}</div>"
    )


def _object(plan: Plan, diff: TableDiff) -> str:
    """One object: what it is, what it becomes, and the steps between."""
    seen = compare(diff)
    risks = sorted({step.risk for step in diff.steps})
    head = (
        "<summary>"
        f'<span class="mark">{escape(_marker(seen))}</span>'
        f'<span class="name">{escape(seen.name)}</span>'
        f'<span class="kind">{escape(seen.kind)}</span>'
        f'<span class="headline">{escape(seen.headline)}</span>'
        + "".join(f'<span class="risk risk-{r}">{r}</span>' for r in risks)
        + "</summary>"
    )
    parts = [head, _rows(seen), _steps(plan, diff)]
    if diff.notes:
        parts.append(
            '<p class="note">' + "<br>".join(escape(note) for note in diff.notes) + "</p>"
        )
    if diff.unmanaged:
        parts.append(
            '<p class="footnote">not modelled, left alone: '
            + escape(", ".join(diff.unmanaged))
            + "</p>"
        )
    return (
        f'<details class="object" open data-name="{escape(diff.table.lower())}" '
        f'data-risks="{escape(" ".join(risks))}">' + "".join(parts) + "</details>"
    )


def _marker(seen: Comparison) -> str:
    return {"create": "+", "destroy": "-", "update": "~", "unchanged": " "}.get(
        seen.action, "~"
    )


def _rows(seen: Comparison) -> str:
    """The object's two sides, aligned by what each row is about."""
    if not seen.rows:
        return ""
    head = (
        "<thead><tr>"
        "<th>what</th>"
        '<th>now <span class="hint">as read when this plan was made</span></th>'
        "<th>after</th>"
        '<th class="said">change</th>'
        "</tr></thead>"
    )
    body = "".join(_row(row) for row in seen.rows)
    unchanged = len(seen.rows) - len(seen.changed)
    note = (
        f'<p class="hint steps-note">{count(unchanged, "row")} unchanged, hidden — '
        "see <em>Full object</em></p>"
        if unchanged
        else ""
    )
    return f'<table class="rows">{head}<tbody>{body}</tbody></table>{note}'


def _row(row: Row) -> str:
    def side(value: str | None, css: str) -> str:
        if value is None:
            return f'<td class="{css} gone">—</td>'
        return f'<td class="{css}">{escape(value)}</td>'

    return (
        f'<tr class="state-{row.state} {row.kind} depth-{min(row.depth, 2)}">'
        f'<td class="what"><span class="mark">{escape(row.marker)}</span>'
        f"{escape(row.path)}</td>"
        + side(row.left, "now")
        + side(row.right, "after")
        + f'<td class="said">{escape(row.said)}</td></tr>'
    )


def _steps(plan: Plan, diff: TableDiff) -> str:
    """The statements, in the order they run."""
    steps = plan.steps_for(diff.table)
    if not steps:
        return ""
    return f'<ol class="steps">{"".join(_step(step) for step in steps)}</ol>'


def _step(step: Step) -> str:
    size = human_bytes(step.est_bytes)
    lines = [
        f'<span class="step-title">{escape(step.title)}</span> '
        f'<span class="risk risk-{step.risk}">{step.risk}</span>'
        + (f' <span class="hint">{escape(size)}</span>' if size else "")
    ]
    if step.note:
        lines.append(f'<div class="note">{escape(step.note)}</div>')
    lines.extend(
        f'<div class="warn">⚠ {escape(warning)}</div>' for warning in step.warnings
    )
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
