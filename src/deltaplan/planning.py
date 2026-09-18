"""Specs and a warehouse in, a plan out.

The pipeline `plan`, `drift` and the GitHub Action share: introspect each schema
once, diff every spec against what is live, decide what happens to live tables
no spec describes, and hand the lot to the planner.

This is where ownership is settled, because it is the one place that sees both
sides at once:

* a live table a spec describes, but deltaplan didn't create, is **claimed** —
  writing the spec was the decision to manage it;
* a live table no spec describes is **unmanaged** if deltaplan didn't create it,
  and left alone whatever the mode;
* a table deltaplan created whose spec has gone is **orphaned**. An additive
  schema keeps it and says so; a strict schema drops it — a destructive step,
  which `apply` refuses without `--allow-destructive`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import TypeVar

from deltaplan.differ import (
    diff,
    diff_function,
    diff_view,
    ownership,
    spent_renames,
    unmanaged,
    unmanaged_function,
    unmanaged_view,
)
from deltaplan.introspect import Introspector, LiveSchema, LiveTable
from deltaplan.loader import Mode
from deltaplan.model.change import Change
from deltaplan.model.function import Function
from deltaplan.model.plan import Plan, TableDiff, TableFacts, fingerprint
from deltaplan.model.table import Table
from deltaplan.model.view import Relation, View
from deltaplan.planner import build_plan


class PlanningError(Exception):
    """A spec that can't be planned, for a reason the loader couldn't see."""


def plan_tables(
    specs: Sequence[Relation],
    introspector: Introspector,
    *,
    target: str,
    tool_version: str,
    mode_for: Callable[[str], Mode] = lambda _schema: "additive",
    check_order: bool = False,
    clone: bool = False,
) -> Plan:
    """Plan every function, table and view against live state.

    Functions come first, in the order their bodies call each other, because
    masks, row filters and views call them. Then tables, then views in
    dependency order: a view is planned after anything its query reads that is
    also being planned. A function is never dropped — it carries no ownership
    marker — so one without a spec is simply left alone.

    `mode_for` answers `strict` or `additive` for a `catalog.schema` — it is a
    callable rather than a mapping because which schemas matter isn't known
    until the specs have been read.
    """
    schemas = _introspect(specs, introspector)
    # Functions have a namespace of their own; only tables and views can clash.
    described = {spec.name for spec in specs if not isinstance(spec, Function)}
    tables = [spec for spec in specs if isinstance(spec, Table)]
    views = order_views([spec for spec in specs if isinstance(spec, View)])
    functions = _order(
        [spec for spec in specs if isinstance(spec, Function)],
        lambda function: function.body,
        "functions call each other",
    )
    _refuse_kind_changes([s for s in specs if not isinstance(s, Function)], schemas)
    _refuse_shared_names(functions, described, schemas)

    diffs: list[TableDiff] = []
    # Functions first: masks, row filters and views call them.
    for function in functions:
        found = _schema_of(schemas, function.name)
        live_function = found.get_function(function.name)
        diffs.append(
            TableDiff(
                function.name,
                diff_function(function, live_function),
                TableFacts(
                    function.name,
                    exists=live_function is not None,
                    kind="function",
                    schema_exists=found.exists,
                ),
                unmanaged_function(function, live_function) if live_function else (),
                desired=function,
                live=live_function,
            )
        )
    for table in tables:
        live = _schema_of(schemas, table.name).get(table.name)
        live_table = live.table if live else None
        changes = (
            *ownership(table, live_table),
            *diff(table, live_table, compare_order=check_order),
        )
        diffs.append(
            TableDiff(
                table.name,
                changes,
                _facts(
                    introspector,
                    table.name,
                    live,
                    changed=bool(changes),
                    schema_exists=_schema_of(schemas, table.name).exists,
                ),
                (
                    (*unmanaged(table, live_table), *_not_modelled(live))
                    if live_table
                    else ()
                ),
                desired=table,
                live=live_table,
                notes=spent_renames(table, live_table) if live_table else (),
            )
        )

    for view in views:
        live_view = _schema_of(schemas, view.name).get_view(view.name)
        changes = (*ownership(view, live_view), *diff_view(view, live_view))
        diffs.append(
            TableDiff(
                view.name,
                changes,
                _view_facts(
                    view.name,
                    live_view,
                    schema_exists=_schema_of(schemas, view.name).exists,
                ),
                unmanaged_view(view, live_view) if live_view else (),
                desired=view,
                live=live_view,
            )
        )

    unmanaged_tables: list[str] = []
    orphaned_tables: list[str] = []
    for (catalog, schema), found in sorted(schemas.items()):
        strict = mode_for(f"{catalog}.{schema}") == "strict"
        for live in found.tables:
            if live.table.name in described:
                continue
            if not live.table.managed:
                unmanaged_tables.append(live.table.name)
            elif strict:
                drop = Change(live.table.name, "drop_table", before=live.table)
                diffs.append(
                    TableDiff(
                        live.table.name,
                        (drop,),
                        _facts(introspector, live.table.name, live, changed=True),
                        live=live.table,
                    )
                )
            else:
                orphaned_tables.append(live.table.name)
        for live_view in found.views:
            if live_view.name in described:
                continue
            if not live_view.managed:
                unmanaged_tables.append(live_view.name)
            elif strict:
                drop = Change(live_view.name, "drop_table", before=live_view)
                diffs.append(
                    TableDiff(
                        live_view.name,
                        (drop,),
                        _view_facts(live_view.name, live_view),
                        live=live_view,
                    )
                )
            else:
                orphaned_tables.append(live_view.name)

    built = build_plan(
        diffs,
        target=target,
        tool_version=tool_version,
        spec_hash=fingerprint(specs),
        state_fingerprint=fingerprint(d.live for d in diffs),
        clone=clone,
    )
    return replace(
        built,
        unmanaged_tables=tuple(sorted(unmanaged_tables)),
        orphaned_tables=tuple(sorted(orphaned_tables)),
    )


_Ordered = TypeVar("_Ordered", View, Function)


def order_views(views: Sequence[View]) -> list[View]:
    """Views in an order where each comes after the views its query reads."""
    return _order(views, lambda view: view.query, "views read each other")


def _order(
    items: Sequence[_Ordered], text: Callable[[_Ordered], str], what: str
) -> list[_Ordered]:
    """Items in an order where each comes after the ones its SQL names.

    Stable: items that don't depend on each other keep their spec order. A cycle
    is an error — no order could create them.
    """
    names = [item.name for item in items]
    depends_on = {
        item.name: {o for o in names if o != item.name and _reads(text(item), o)}
        for item in items
    }
    ordered: list[_Ordered] = []
    placed: set[str] = set()
    while len(ordered) < len(items):
        ready = [
            i for i in items if i.name not in placed and depends_on[i.name] <= placed
        ]
        if not ready:
            stuck = sorted(name for name in names if name not in placed)
            raise PlanningError(f"these {what} in a cycle: {', '.join(stuck)}")
        for item in ready:
            ordered.append(item)
            placed.add(item.name)
    return ordered


def _reads(query: str, name: str) -> bool:
    """Does the query name this table or view — quoted or not, any case?"""
    pattern = r"\s*\.\s*".join(rf"`?{re.escape(part)}`?" for part in name.split("."))
    found = re.search(rf"(?<![\w`.]){pattern}(?![\w`])", query, re.IGNORECASE)
    return found is not None


def _refuse_shared_names(
    functions: Sequence[Function],
    described: set[str],
    schemas: dict[tuple[str, str], LiveSchema],
) -> None:
    """A function with a table's or view's name.

    Unity Catalog allows it — functions have a namespace of their own — but a
    plan, its history and its renderings are keyed by name, so deltaplan would
    confuse the two.
    """
    for function in functions:
        found = _schema_of(schemas, function.name)
        if function.name in described or (
            found.get(function.name) or found.get_view(function.name)
        ):
            raise PlanningError(
                f"{function.name} names both a function and a table or view. "
                "deltaplan keys a plan by name, so it cannot manage both; rename one."
            )


def _refuse_kind_changes(
    specs: Sequence[Relation], schemas: dict[tuple[str, str], LiveSchema]
) -> None:
    """A table the spec calls a view, or the other way round, is not converted."""
    for spec in specs:
        found = _schema_of(schemas, spec.name)
        live_kind = (
            "table"
            if found.get(spec.name) is not None
            else "view"
            if found.get_view(spec.name) is not None
            else None
        )
        spec_kind = "view" if isinstance(spec, View) else "table"
        if live_kind is not None and live_kind != spec_kind:
            raise PlanningError(
                f"{spec.name} is a {live_kind} in the catalog but a {spec_kind} in "
                "its spec. deltaplan won't turn one into the other — drop it by "
                "hand first"
            )


def _introspect(
    specs: Sequence[Relation], introspector: Introspector
) -> dict[tuple[str, str], LiveSchema]:
    schemas: dict[tuple[str, str], LiveSchema] = {}
    for spec in specs:
        parts = spec.parts
        if len(parts) != 3:
            raise PlanningError(f"name {spec.name!r} must be catalog.schema.name")
        key = (parts[0], parts[1])
        if key not in schemas:
            schemas[key] = introspector.schema(*key)
    return schemas


def _schema_of(schemas: dict[tuple[str, str], LiveSchema], name: str) -> LiveSchema:
    catalog, schema, _ = name.split(".")
    return schemas[(catalog, schema)]


def _view_facts(
    name: str, live: View | None, *, schema_exists: bool = True
) -> TableFacts:
    return TableFacts(
        name,
        exists=live is not None,
        properties=live.properties if live else (),
        kind="view",
        schema_exists=schema_exists,
    )


def _facts(
    introspector: Introspector,
    name: str,
    live: LiveTable | None,
    *,
    changed: bool,
    schema_exists: bool = True,
) -> TableFacts:
    return TableFacts(
        name,
        exists=live is not None,
        properties=live.table.properties if live else (),
        size_bytes=live.size_bytes if live else None,
        # A restore point is only worth a query for a table that is changing.
        delta_version=(
            introspector.latest_version(name) if live is not None and changed else None
        ),
        unmodelled=live.unmodelled if live else (),
        schema_exists=schema_exists,
    )


def _not_modelled(live: LiveTable | None) -> tuple[str, ...]:
    return tuple(
        f"{feature} (not modelled)" for feature in (live.unmodelled if live else ())
    )
