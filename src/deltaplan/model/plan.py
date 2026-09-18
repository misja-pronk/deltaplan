"""The plan: what will run, in what order, and how much it can hurt.

A `Plan` is data. The CLI renders it, `-o plan.json` writes it out, and (from
milestone 2) the executor runs it — all from this one object, so what you
reviewed and what runs cannot drift apart.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from deltaplan.model.change import Change
from deltaplan.model.table import Table

#: What a step can cost you.
#:
#: * ``meta`` — a metadata-only change; runs directly.
#: * ``feature`` — enables a Delta table feature first; not reversible, and
#:   older readers/writers may be locked out.
#: * ``rewrite`` — rewrites the data files; slow, and expensive on big tables.
#: * ``destructive`` — drops something; refused without ``--allow-destructive``.
Risk: TypeAlias = Literal["meta", "feature", "rewrite", "destructive"]

RISK_ORDER: dict[Risk, int] = {"meta": 0, "feature": 1, "rewrite": 2, "destructive": 3}


@dataclass(frozen=True, slots=True)
class Step:
    """One statement, and everything needed to run it safely.

    `change` is the index of the change this step implements, in the plan's own
    change order. A prerequisite carries the index of the change that needed it,
    which is what lets a renderer nest "enable columnMapping" under the rename
    that asked for it.
    """

    id: int
    table: str
    title: str
    risk: Risk
    change: int = -1
    path: str = ""
    sql: str | None = None
    precheck: str | None = None
    postcheck: str | None = None
    est_bytes: int | None = None
    undo_hint: str | None = None
    warnings: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class TableFacts:
    """What the planner needs to know about a live table but must not fetch.

    Keeping this an input is what lets the planner stay pure: no SDK, no clock,
    no environment, and a golden plan for every combination of live state.
    """

    name: str
    exists: bool = True
    properties: tuple[tuple[str, str], ...] = ()
    size_bytes: int | None = None
    delta_version: int | None = None

    def property(self, key: str) -> str | None:
        return dict(self.properties).get(key)

    def property_is_true(self, key: str) -> bool:
        return (self.property(key) or "").lower() == "true"


@dataclass(frozen=True, slots=True)
class TableDiff:
    """One table's changes, with the live facts they were computed against."""

    table: str
    changes: tuple[Change, ...] = ()
    facts: TableFacts = field(default_factory=lambda: TableFacts("unknown"))
    unmanaged: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Summary:
    """The one-line count at the bottom of a plan."""

    add: int
    change: int
    destroy: int
    steps: int
    rewrites: int
    warnings: int

    def __str__(self) -> str:
        warnings = "warning" if self.warnings == 1 else "warnings"
        return (
            f"Plan: {self.add} add, {self.change} change, {self.destroy} destroy"
            f" · {self.steps} steps · {self.rewrites} rewrites"
            f" · {self.warnings} {warnings}"
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """Everything `apply` needs, and everything a reviewer needs."""

    tool_version: str
    target: str
    spec_hash: str
    state_fingerprint: str
    diffs: tuple[TableDiff, ...] = ()
    steps: tuple[Step, ...] = ()
    #: Live tables in the schemas we looked at that no spec describes. Reported
    #: so you know they are there; never touched.
    unmanaged_tables: tuple[str, ...] = ()

    @property
    def changes(self) -> tuple[Change, ...]:
        return tuple(change for diff in self.diffs for change in diff.changes)

    @property
    def empty(self) -> bool:
        return not self.steps

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(diff.table for diff in self.diffs if diff.changes)

    def steps_for(self, table: str) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.table == table)

    def steps_for_change(self, change: int) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.change == change)

    @property
    def summary(self) -> Summary:
        created = sum(
            1
            for diff in self.diffs
            if any(change.kind == "create_table" for change in diff.changes)
        )
        changed = sum(
            1
            for diff in self.diffs
            if diff.changes and not any(c.kind == "create_table" for c in diff.changes)
        )
        return Summary(
            add=created,
            change=changed,
            destroy=0,  # v1 never drops a table; strict mode arrives with apply
            steps=len(self.steps),
            rewrites=sum(1 for step in self.steps if step.risk == "rewrite"),
            warnings=sum(len(step.warnings) for step in self.steps),
        )

    @property
    def highest_risk(self) -> Risk:
        return max(
            (step.risk for step in self.steps),
            key=lambda risk: RISK_ORDER[risk],
            default="meta",
        )


def fingerprint(tables: Iterable[Table | None]) -> str:
    """A short digest of live state, used to refuse a plan that has gone stale.

    Deliberately blunt: it hashes the whole model, so *any* difference — a
    comment, a property, a nested field — invalidates the plan. A false "this
    changed" costs a re-plan; a false "nothing changed" would apply a reviewed
    plan to a table that is no longer the one that was reviewed.
    """
    digest = hashlib.sha256()
    for table in tables:
        digest.update(repr(table).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]
