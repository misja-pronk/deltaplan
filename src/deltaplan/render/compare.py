"""One object, as it is and as the specs say it should be.

A plan is a list of changes, which is what `apply` needs and not what a person
reading it wants. What they want is the object: what the table looks like now,
what it will look like, and what is different — and then, separately, the
statements that get from one to the other.

So this builds that comparison, once, as data. Each row is one thing about the
object with a side each, aligned by **meaning** rather than by text similarity:
the `amount` column on the left is the `amount` column on the right, however
much its type moved. A renderer can then show those rows as plain language for
someone who has to approve the change, or as spec lines for whoever wrote it,
without the two ever being able to disagree — there is one structure underneath.

Pure, like the differ: no I/O, no clock, nothing about how it will look.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, TypeAlias

from deltaplan.model.change import Change
from deltaplan.model.function import Function
from deltaplan.model.plan import TableDiff
from deltaplan.model.schema import Schema
from deltaplan.model.table import Table, is_bookkeeping, is_platform_default
from deltaplan.model.types import Field, render_type, walk
from deltaplan.model.view import Relation, View, normalise_query
from deltaplan.model.volume import Volume
from deltaplan.render.labels import count, describe, display_name, human_bytes

#: What became of one row: the same on both sides, only on one, or moved.
State: TypeAlias = Literal["same", "added", "removed", "changed"]

#: What a row is about, so a renderer can group or style it.
RowKind: TypeAlias = Literal["column", "detail", "line"]


@dataclass(frozen=True, slots=True)
class Row:
    """One thing about an object, with a side each.

    `left` is the live object, `right` the spec — each already rendered short,
    because what a value *is* differs by kind and this is the only place that
    knows. `said` is the same row in plain language, which for a changed row is
    the sentence the terminal and the pull-request comment use.
    """

    path: str
    state: State
    left: str | None = None
    right: str | None = None
    said: str = ""
    kind: RowKind = "column"
    #: How deep the thing sits: a column is 0, a field inside a struct 1.
    depth: int = 0

    @property
    def marker(self) -> str:
        return {"same": " ", "added": "+", "removed": "-", "changed": "~"}[self.state]


@dataclass(frozen=True, slots=True)
class Comparison:
    """One object's two sides, and a sentence about the difference."""

    name: str
    kind: str
    action: str
    headline: str
    rows: tuple[Row, ...] = ()

    @property
    def changed(self) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if row.state != "same")


def compare(diff: TableDiff) -> Comparison:
    """The live object beside the desired one, row by row."""
    rows: list[Row] = []
    desired, live = diff.desired, diff.live
    if isinstance(desired, Table) or isinstance(live, Table):
        rows = _table_rows(diff)
    elif isinstance(desired, View) or isinstance(live, View):
        rows = _text_rows(diff, "query", _query_of)
    elif isinstance(desired, Function) or isinstance(live, Function):
        rows = _function_rows(diff)
    else:
        rows = _securable_rows(diff)
    return Comparison(
        name=display_name(diff.table),
        kind=diff.kind,
        action=diff.action,
        headline=headline(diff, tuple(rows)),
        rows=tuple(rows),
    )


def headline(diff: TableDiff, rows: tuple[Row, ...]) -> str:
    """What happens to this object, in one sentence.

    The thing someone wants before they read anything else: how much moves, and
    whether it costs a rebuild.
    """
    if diff.action == "create":
        columns = len([row for row in rows if row.kind == "column"])
        made = f"new {diff.kind}"
        return f"{made} with {count(columns, 'column')}" if columns else made
    if diff.action == "destroy":
        return "dropped — its spec is gone and the schema is strict"
    if not diff.changes:
        return "nothing to do"
    added = len([r for r in rows if r.state == "added"])
    removed = len([r for r in rows if r.state == "removed"])
    moved = len([r for r in rows if r.state == "changed"])
    parts = [
        f"{count(added, 'addition')}" if added else "",
        f"{count(removed, 'removal')}" if removed else "",
        f"{count(moved, 'change')}" if moved else "",
    ]
    said = ", ".join(part for part in parts if part) or count(len(diff.changes), "change")
    cost = _cost(diff)
    return f"{said}{cost}"


def _cost(diff: TableDiff) -> str:
    """What the riskiest step costs, when it costs something worth saying."""
    risk = diff.risk
    size = human_bytes(
        max((step.est_bytes or 0 for step in diff.steps), default=0) or None
    )
    if risk == "destructive":
        return f" — destroys something ({size})" if size else " — destroys something"
    if risk == "rewrite":
        return f" — rebuilt, {size}" if size else " — rebuilt"
    return ""


# -- tables -----------------------------------------------------------------


def _table_rows(diff: TableDiff) -> list[Row]:
    """A table: what it is made of, then the details around it."""
    desired = diff.desired if isinstance(diff.desired, Table) else None
    live = diff.live if isinstance(diff.live, Table) else None
    by_path = _changes_by_path(diff.changes)
    rows = [*_column_rows(desired, live, by_path), *_detail_rows(desired, live, by_path)]
    # Anything the differ said that no row claimed — a table-level change this
    # doesn't model yet — is still worth showing, rather than quietly lost.
    claimed = {row.path for row in rows}
    for path, changes in by_path.items():
        if path in claimed:
            continue
        rows.append(
            Row(
                path or "table",
                "changed",
                said="; ".join(describe(change)[1] for change in changes),
                kind="detail",
            )
        )
    return rows


def _column_rows(
    desired: Table | None, live: Table | None, by_path: dict[str, list[Change]]
) -> list[Row]:
    """One row per column, and one per nested field, in the spec's order."""
    left = _fields_of(live)
    right = _fields_of(desired)
    order: list[str] = list(right)
    for path in left:
        if path not in right:
            # A column the spec no longer has keeps its place in the reading.
            after = _parent(path)
            index = order.index(after) + 1 if after in order else len(order)
            order.insert(index, path)
    rows: list[Row] = []
    for path in order:
        was, now = left.get(path), right.get(path)
        renamed = now.renamed_from if now is not None else None
        if was is None and renamed:
            was = left.get(renamed)
        rows.append(
            Row(
                path,
                _state(was, now, by_path.get(path)),
                _field_text(was),
                _field_text(now),
                _said(by_path.get(path), was, now, renamed),
                "column",
                path.count(".") + path.count(".element") * 0,
            )
        )
    return rows


def _fields_of(table: Table | None) -> dict[str, Field]:
    if table is None:
        return {}
    found: dict[str, Field] = {}
    for column in table.columns:
        found[column.name] = column
        for path, member in walk(column.type, column.name):
            found[path] = member
    return found


def _parent(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else path


def _field_text(field: Field | None) -> str | None:
    """A column as a spec writes it: type, nullability, and what it carries."""
    if field is None:
        return None
    parts = [render_type(field.type)]
    if not field.nullable:
        parts.append("NOT NULL")
    if field.comment:
        parts.append(f'comment "{field.comment}"')
    if field.tags:
        parts.append("tags " + ", ".join(f"{k}={v}" for k, v in field.tags))
    if field.mask is not None:
        parts.append(f"mask {field.mask.function}")
    if field.default:
        parts.append(f"default {field.default}")
    if field.identity is not None:
        parts.append("identity")
    if field.generated:
        parts.append(f"generated as {field.generated}")
    return " ".join(parts)


#: Which detail row a table-level change belongs to. They all share the empty
#: path, so the kind is what says which of them moved.
DETAIL_OF: dict[str, str] = {
    "set_table_comment": "comment",
    "set_cluster_by": "clustering",
    "set_partitioning": "clustering",
    "set_tag": "tags",
    "unset_tag": "tags",
    "set_property": "properties",
    "unset_property": "properties",
    "set_row_filter": "row filter",
    "grant": "grants",
    "revoke": "grants",
    "set_owner": "owner",
    "claim_table": "managed by deltaplan",
    "add_constraint": "constraints",
    "drop_constraint": "constraints",
    "load_seed": "seed",
    "reorder_columns": "column order",
}


def _detail_rows(
    desired: Table | None, live: Table | None, by_path: dict[str, list[Change]]
) -> list[Row]:
    """Everything about a table that isn't a column.

    A comment, the clustering keys, the tags: all changes at the table level,
    and all sharing the empty path. Which of them moved is in the change's
    kind, so that is what these are grouped by.
    """
    sides: dict[str, tuple[str | None, str | None]] = {
        "comment": (_comment(live), _comment(desired)),
        "clustering": (_clustering(live), _clustering(desired)),
        "tags": (
            _pairs(live.tags if live else ()),
            _pairs(desired.tags if desired else ()),
        ),
        "properties": (_pairs(_own(live)), _pairs(_own(desired))),
        "constraints": (_constraints(live), _constraints(desired)),
        "row filter": (_row_filter(live), _row_filter(desired)),
        "grants": (_grants(live), _grants(desired)),
        "owner": (live.owner if live else None, desired.owner if desired else None),
        "seed": (None, _seed(desired)),
    }
    said: dict[str, list[str]] = {}
    for path, changes in list(by_path.items()):
        for change in changes:
            where = DETAIL_OF.get(change.kind)
            if where is None:
                continue  # a column's own change; the column rows have it
            said.setdefault(where, []).append(describe(change)[1])
        if all(change.kind in DETAIL_OF for change in changes):
            # A tag, a property or a grant hangs off its own key rather than
            # the table, so this is where those paths are accounted for.
            by_path.pop(path, None)
    # A change may be about something with no two sides to show — a claim, say.
    for where in said:
        sides.setdefault(where, (None, None))

    rows: list[Row] = []
    for name, (was, now) in sides.items():
        words = said.get(name, [])
        if not words and (was is None and now is None):
            continue
        if not words and was == now:
            # The same on both sides and nothing planned: worth showing as
            # context, not as a difference.
            rows.append(Row(name, "same", was, now, kind="detail"))
            continue
        if not words:
            # Nothing planned, so this is what the object keeps: the right-hand
            # side is how it will be, not what the spec happens to mention.
            rows.append(Row(name, "same", was, was, kind="detail"))
            continue
        state: State = (
            "added"
            if was is None and now is not None
            else "removed"
            if now is None and was is not None
            else "changed"
        )
        rows.append(Row(name, state, was, now, "; ".join(words), "detail"))
    return rows


def _comment(table: Table | None) -> str | None:
    return table.comment if table else None


def _constraints(table: Table | None) -> str | None:
    if table is None or not table.constraints:
        return None
    return ", ".join(_constraint(constraint) for constraint in table.constraints)


def _constraint(constraint: object) -> str:
    columns = getattr(constraint, "columns", ())
    if hasattr(constraint, "expression"):
        return f"check {getattr(constraint, 'name', '')}"
    if hasattr(constraint, "references"):
        return f"foreign key ({', '.join(columns)}) -> {constraint.references}"  # type: ignore[attr-defined]
    return f"primary key ({', '.join(columns)})"


def _seed(table: Table | None) -> str | None:
    seed = getattr(table, "seed", None)
    if seed is None:
        return None
    where = f" from {seed.source}" if seed.source else ""
    return f"{count(len(seed), 'row')}{where}"


def _clustering(table: Table | None) -> str | None:
    if table is None:
        return None
    if table.cluster_auto:
        return "cluster by auto"
    if table.cluster_by:
        return f"cluster by ({', '.join(table.cluster_by)})"
    if table.partitioned_by:
        return f"partitioned by ({', '.join(table.partitioned_by)})"
    return None


def _pairs(pairs: tuple[tuple[str, str], ...]) -> str | None:
    return ", ".join(f"{key}={value}" for key, value in pairs) or None


def _own(table: Table | None) -> tuple[tuple[str, str], ...]:
    """Properties someone meant, rather than Delta's or deltaplan's own."""
    if table is None:
        return ()
    return tuple(
        (key, value)
        for key, value in table.properties
        if not is_bookkeeping(key) and not is_platform_default(key, value)
    )


def _row_filter(table: Table | None) -> str | None:
    if table is None or table.row_filter is None:
        return None
    columns = ", ".join(table.row_filter.columns)
    return f"{table.row_filter.function}({columns})"


def _grants(table: Relation | None) -> str | None:
    grants = getattr(table, "grants", ())
    return "; ".join(f"{g.principal}: {', '.join(g.privileges)}" for g in grants) or None


# -- views, functions, schemas, volumes -------------------------------------


def _query_of(relation: Relation | None) -> str:
    return getattr(relation, "query", "") or ""


def _body_of(relation: Relation | None) -> str:
    return getattr(relation, "body", "") or ""


def _text_rows(
    diff: TableDiff,
    what: str,
    read: Callable[[Relation | None], str],
    *,
    extra: list[Row] | None = None,
) -> list[Row]:
    """A view or a function: its SQL, line by line, plus what surrounds it."""
    left = _lines(read(diff.live))
    right = _lines(read(diff.desired))
    rows: list[Row] = list(extra or [])
    if normalise_query("\n".join(left)) == normalise_query("\n".join(right)):
        rows.extend(Row(what, "same", line, line, kind="line") for line in right or left)
        return rows
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, left, right).get_opcodes():
        if tag == "equal":
            rows.extend(
                Row(what, "same", left[i], right[j], kind="line")
                for i, j in zip(range(i1, i2), range(j1, j2), strict=True)
            )
        elif tag == "replace":
            for index in range(max(i2 - i1, j2 - j1)):
                was = left[i1 + index] if i1 + index < i2 else None
                now = right[j1 + index] if j1 + index < j2 else None
                rows.append(
                    Row(
                        what,
                        "changed" if was and now else "added" if now else "removed",
                        was,
                        now,
                        kind="line",
                    )
                )
        elif tag == "delete":
            rows.extend(
                Row(what, "removed", left[i], None, kind="line") for i in range(i1, i2)
            )
        else:
            rows.extend(
                Row(what, "added", None, right[j], kind="line") for j in range(j1, j2)
            )
    return rows


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.strip().splitlines()] if text.strip() else []


def _function_rows(diff: TableDiff) -> list[Row]:
    desired = diff.desired if isinstance(diff.desired, Function) else None
    live = diff.live if isinstance(diff.live, Function) else None
    extra = [
        Row(
            "signature",
            "same" if _signature(live) == _signature(desired) else "changed",
            _signature(live),
            _signature(desired),
            kind="detail",
        ),
        *_common_details(live, desired),
    ]
    return _text_rows(
        diff, "body", _body_of, extra=[row for row in extra if row.left or row.right]
    )


def _signature(function: Function | None) -> str | None:
    if function is None:
        return None
    parameters = ", ".join(f"{p.name} {render_type(p.type)}" for p in function.parameters)
    return f"({parameters}) returns {render_type(function.returns)}"


def _securable_rows(diff: TableDiff) -> list[Row]:
    """A schema or a volume: a comment, and who may do what."""
    desired = diff.desired if isinstance(diff.desired, Schema | Volume) else None
    live = diff.live if isinstance(diff.live, Schema | Volume) else None
    return [row for row in _common_details(live, desired) if row.left or row.right]


def _common_details(live: Relation | None, desired: Relation | None) -> list[Row]:
    """The comment, tags, grants and owner any securable can have."""

    def row(name: str, was: str | None, now: str | None) -> Row:
        state: State = (
            "same"
            if was == now
            else "added"
            if was is None
            else "removed"
            if now is None
            else "changed"
        )
        return Row(name, state, was, now, kind="detail")

    return [
        row(
            "comment",
            getattr(live, "comment", None),
            getattr(desired, "comment", None),
        ),
        row(
            "tags",
            _pairs(getattr(live, "tags", ())),
            _pairs(getattr(desired, "tags", ())),
        ),
        row("grants", _grants(live), _grants(desired)),
        row("owner", getattr(live, "owner", None), getattr(desired, "owner", None)),
    ]


# -- shared -----------------------------------------------------------------


def _changes_by_path(changes: tuple[Change, ...]) -> dict[str, list[Change]]:
    found: dict[str, list[Change]] = {}
    for change in changes:
        found.setdefault(change.path, []).append(change)
    return found


def _state(was: Field | None, now: Field | None, changes: list[Change] | None) -> State:
    if now is None:
        return "removed"
    if was is None:
        return "added"
    if changes:
        return "changed"
    return (
        "same"
        if replace(was, tags=now.tags) == replace(now, tags=now.tags)
        else "changed"
    )


def _said(
    changes: list[Change] | None,
    was: Field | None,
    now: Field | None,
    renamed: str | None,
) -> str:
    """The row in plain language — the words the rest of deltaplan uses."""
    said = [describe(change)[1] for change in changes or []]
    if renamed and was is not None:
        said.insert(0, f"renamed from {renamed}")
    if not said and now is not None and was is None:
        said.append("added")
    if not said and now is None:
        said.append("dropped")
    return "; ".join(said)
