"""Drift, back into the spec.

`drift` says a table has been changed by hand. That change is usually *wanted* —
someone added a column at 2am to unblock a load — and the only two ways out were
to retype it into the spec, or to apply the plan and undo their work. This is the
third: write the live shape into the spec file that already describes it, and
leave a git diff for someone to read.

Three rules decide what a file gets:

- **What deltaplan would otherwise have planned comes from the workspace**: a
  column, a type, a nested field, `not null`, a comment, clustering, a view's
  query, a function's body.
- **What the spec never claimed is left alone.** A tag, property or grant the
  file doesn't mention stays unmanaged, exactly as before — adopting drift is not
  the moment to start managing something new. A declared one takes the live
  value; one that is declared and no longer live stops being declared.
- **What only a file can say survives**: `${catalog}`, a `renamed_from` hint, a
  `using:` expression, a seed's rows, hooks — and every comment and blank line
  around them, because the file is *edited* rather than rewritten (`yamledit`).

It then reads its own work back and diffs that against live, so anything it
couldn't express is reported here rather than discovered by the next plan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from deltaplan.differ import diff, diff_function, diff_schema, diff_view, diff_volume
from deltaplan.errors import DeltaplanError
from deltaplan.loader import (
    LoadedSpec,
    SpecError,
    load_spec_text,
    spec_document,
    substitute,
)
from deltaplan.manage import EVERYTHING, Manage
from deltaplan.model.change import Change
from deltaplan.model.function import Function
from deltaplan.model.schema import Schema
from deltaplan.model.table import Grant, Table
from deltaplan.model.types import Column
from deltaplan.model.view import Relation, View
from deltaplan.model.volume import Volume
from deltaplan.yamledit import merge_into


class CannotAdopt(DeltaplanError):
    """This spec can't be rewritten from live state, and why."""


@dataclass(frozen=True, slots=True)
class Adoption:
    """What adopting one spec does to its file."""

    path: Path
    name: str
    before: str
    after: str
    #: What changed, one line each: `+ columns: region string`.
    notes: tuple[str, ...] = ()
    #: Changes a plan would still have after this — a seed's rows, say, which
    #: the workspace can't tell a file.
    remaining: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.after != self.before

    def write(self) -> None:
        """Write the file. Nothing else here touches the disk."""
        self.path.write_text(self.after, encoding="utf-8")


def adopt(
    spec: LoadedSpec,
    live: Relation,
    *,
    variables: Mapping[str, str] | None = None,
    unresolved: Mapping[str, str] | None = None,
    manage: Manage = EVERYTHING,
) -> Adoption:
    """The spec file this relation's live state would be written into.

    Nothing is written: the result carries the new text, what changed, and what
    a plan would still say afterwards.

    Raises `CannotAdopt` for a spec no file edit can express — a `.sql` spec, or
    a live object of another kind than the spec describes.
    """
    path = spec.path
    if path.suffix.lower() == ".sql":
        raise CannotAdopt(
            f"{path} is a SQL spec, and rewriting a CREATE statement from live "
            "state is not something to do by text search. Edit it, or import the "
            "object again as YAML."
        )
    if type(spec.table) is not type(live):
        raise CannotAdopt(
            f"{spec.table.name} is a {_kind(spec.table)} in {path} and a "
            f"{_kind(live)} in the workspace"
        )
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CannotAdopt(f"cannot read {path}: {error}") from error

    adopted = _adopted(spec.table, live)
    wanted = spec_document(adopted, manage=manage)
    rendered = _renderer(variables, unresolved)
    try:
        after = merge_into(source, wanted, rendered=rendered)
    except ValueError as error:
        raise CannotAdopt(f"{path}: {error}") from error
    return Adoption(
        path=path,
        name=spec.table.name,
        before=source,
        after=after,
        notes=_notes(spec_document(spec.table, manage=manage), wanted),
        remaining=_remaining(after, path, live, variables, unresolved, manage),
    )


def _renderer(variables: Mapping[str, str] | None, unresolved: Mapping[str, str] | None):
    """How the file's text reads once its variables are resolved.

    A name the reader can't resolve is left as it stands rather than raised: the
    comparison then simply says the two differ, and a spec that can't be read at
    all is `validate`'s business, not this one's.
    """

    def render(text: str) -> str:
        try:
            return substitute(text, variables or {}, unresolved)
        except KeyError:
            return text

    return render


def _remaining(
    text: str,
    path: Path,
    live: Relation,
    variables: Mapping[str, str] | None,
    unresolved: Mapping[str, str] | None,
    manage: Manage,
) -> tuple[str, ...]:
    """What a plan would still say about this object after the file is written.

    Adopt reads its own work back rather than trusting it. A seed is the usual
    answer: its rows live in the repo, and no workspace can tell a file what
    they should be.
    """
    try:
        written = load_spec_text(text, path, variables, unresolved, manage)
    except SpecError as error:
        raise CannotAdopt(f"the adopted spec wouldn't read back: {error}") from error
    if type(written) is not type(live):  # pragma: no cover - the kind is kept
        raise CannotAdopt("the adopted spec changed kind")
    return tuple(
        change.kind
        for change in _changes(written, live)
        # An ownership claim is something `apply` does, not something a file
        # says: reporting it here would make every unclaimed table look unfixable.
        if change.kind != "claim_table"
    )


def _changes(written: Relation, live: Relation) -> tuple[Change, ...]:
    match written, live:
        case Table(), Table():
            return diff(written, live)
        case View(), View():
            return diff_view(written, live)
        case Function(), Function():
            return diff_function(written, live)
        case Schema(), Schema():
            return diff_schema(written, live)
        case Volume(), Volume():
            return diff_volume(written, live)
        case _:  # pragma: no cover - guarded by the caller
            return ()


def _kind(relation: Relation) -> str:
    match relation:
        case Table():
            return "table"
        case View():
            return "view"
        case Function():
            return "function"
        case Schema():
            return "schema"
        case Volume():
            return "volume"


# ---------------------------------------------------------------------------
# what the spec becomes
# ---------------------------------------------------------------------------


def _adopted(spec: Relation, live: Relation) -> Relation:
    """Live state, minus everything this spec never claimed."""
    match spec, live:
        case Table(), Table():
            return _adopted_table(spec, live)
        case View(), View():
            return replace(
                _claimed(spec, live),
                query=live.query,
                comment=live.comment,
            )
        case Function(), Function():
            return replace(
                _claimed(spec, live),
                body=live.body,
                returns=live.returns,
                parameters=live.parameters,
                comment=live.comment,
            )
        case Schema() | Volume(), _:
            # Both are additive in the differ: a comment the spec doesn't
            # declare is never removed, so it is never adopted either.
            return replace(
                _claimed(spec, live),
                comment=live.comment if spec.comment is not None else None,
            )
        case _:  # pragma: no cover - guarded by the caller
            return live


def _adopted_table(spec: Table, live: Table) -> Table:
    return replace(
        _claimed(spec, live),
        comment=live.comment,
        columns=tuple(_adopted_column(spec.column(c.name), c) for c in live.columns),
        cluster_by=live.cluster_by,
        cluster_auto=live.cluster_auto,
        partitioned_by=live.partitioned_by,
        constraints=live.constraints if spec.constraints else (),
        row_filter=live.row_filter if spec.row_filter is not None else None,
        # Only a file knows these, and the workspace can't be asked.
        seed=spec.seed,
        hooks=spec.hooks,
        renamed_from=spec.renamed_from,
    )


def _adopted_column(spec: Column | None, live: Column) -> Column:
    """One live column, as the spec would write it.

    A column the spec hasn't got is taken whole — that is the drift being
    adopted. One it has keeps its hints, and its tags and mask stay as the file
    left them unless it declared them.
    """
    if spec is None:
        return live
    return replace(
        live,
        tags=_kept(dict(spec.tags), spec.removed_tags, dict(live.tags)),
        removed_tags=_still_gone(spec.removed_tags, dict(live.tags)),
        mask=live.mask if spec.mask is not None else None,
        renamed_from=spec.renamed_from,
        using=spec.using,
    )


def _claimed(spec: Relation, live: Relation) -> Any:
    """Live, with the tags, properties, grants and owner the spec claims.

    Everything else a live object carries is unmanaged, and reported as such by
    every plan; adopting drift doesn't change who manages what.
    """
    return replace(
        live,
        tags=_kept(spec.tags_map(), spec.removed_tags, live.tags_map()),
        removed_tags=_still_gone(spec.removed_tags, live.tags_map()),
        properties=_kept(
            spec.properties_map(), spec.removed_properties, live.properties_map()
        ),
        removed_properties=_still_gone(spec.removed_properties, live.properties_map()),
        grants=_kept_grants(spec.grants, live.grants),
        owner=live.owner if spec.owner is not None else None,
    )


def _kept(
    declared: Mapping[str, str], removed: Sequence[str], found: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    """The live values of the keys this spec declares, and no others."""
    return tuple(
        sorted(
            (key, value)
            for key, value in found.items()
            if key in declared or key in removed
        )
    )


def _still_gone(removed: Sequence[str], found: Mapping[str, str]) -> tuple[str, ...]:
    """`tags: {pii: null}` stays only while the tag really is gone: one that is
    back is now the live value, and the file says so."""
    return tuple(key for key in removed if key not in found)


def _kept_grants(declared: Sequence[Grant], found: Sequence[Grant]) -> tuple[Grant, ...]:
    """The live privileges of the principals this spec names."""
    named = {grant.principal.casefold() for grant in declared}
    return tuple(grant for grant in found if grant.principal.casefold() in named)


# ---------------------------------------------------------------------------
# what changed
# ---------------------------------------------------------------------------


def _notes(before: Mapping[str, object], after: Mapping[str, object]) -> tuple[str, ...]:
    """One line per difference between two spec documents, for a person to read."""
    lines: list[str] = []
    _note_mapping(before, after, "", lines)
    return tuple(lines)


def _note_mapping(
    before: Mapping[str, object],
    after: Mapping[str, object],
    where: str,
    lines: list[str],
) -> None:
    for key, value in after.items():
        at = f"{where}{key}"
        if key not in before:
            lines.append(f"+ {at}: {_short(value)}")
        elif before[key] != value:
            _note_value(before[key], value, at, lines)
    for key in before:
        if key not in after:
            lines.append(f"- {where}{key}")


def _note_value(before: object, after: object, at: str, lines: list[str]) -> None:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        _note_mapping(before, after, f"{at}.", lines)
        return
    if _named(before) is not None and (now := _named(after)) is not None:
        _note_items(_named(before) or [], now, at, lines)
        return
    lines.append(f"~ {at}: {_short(before)} → {_short(after)}")


def _note_items(
    before: Sequence[Mapping[str, object]],
    after: Sequence[Mapping[str, object]],
    at: str,
    lines: list[str],
) -> None:
    was = {str(item["name"]): item for item in before}
    now = {str(item["name"]): item for item in after}
    for name, item in now.items():
        if name not in was:
            lines.append(f"+ {at}: {name} {_short(item.get('type', ''))}".rstrip())
        elif was[name] != item:
            _note_mapping(was[name], item, f"{at}.{name}.", lines)
    for name in was:
        if name not in now:
            lines.append(f"- {at}: {name}")


def _named(value: object) -> list[Mapping[str, object]] | None:
    """A list of things with names — columns, parameters — or None."""
    if isinstance(value, Sequence) and not isinstance(value, str) and value:
        items = [item for item in value if isinstance(item, Mapping) and "name" in item]
        if len(items) == len(value):
            return items
    return None


def _short(value: object) -> str:
    """A value as one readable piece, cut off before it fills a terminal."""
    if isinstance(value, Mapping):
        text = ", ".join(f"{key}: {_short(one)}" for key, one in value.items())
    elif isinstance(value, Sequence) and not isinstance(value, str):
        text = ", ".join(_short(one) for one in value)
    elif value is None:
        text = "nothing"
    else:
        text = str(value)
    flat = " ".join(text.split())
    return flat if len(flat) <= 60 else f"{flat[:57]}…"
