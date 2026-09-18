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

from deltaplan.differ import diff, diff_view, ownership, unmanaged, unmanaged_view
from deltaplan.introspect import Introspector, LiveSchema, LiveTable
from deltaplan.loader import Mode
from deltaplan.model.change import Change
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
    """Plan every table and view against live state.

    Tables come first, then views in dependency order: a view is planned after
    anything its query reads that is also being planned.

    `mode_for` answers `strict` or `additive` for a `catalog.schema` — it is a
    callable rather than a mapping because which schemas matter isn't known
    until the specs have been read.
    """
    schemas = _introspect(specs, introspector)
    described = {spec.name for spec in specs}
    tables = [spec for spec in specs if isinstance(spec, Table)]
    views = order_views([spec for spec in specs if isinstance(spec, View)])
    _refuse_kind_changes(specs, schemas)

    diffs: list[TableDiff] = []
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
                _facts(introspector, table.name, live, changed=bool(changes)),
                (
                    (*unmanaged(table, live_table), *_not_modelled(live))
                    if live_table
                    else ()
                ),
                desired=table,
                live=live_table,
            )
        )

    for view in views:
        live_view = _schema_of(schemas, view.name).get_view(view.name)
        changes = (*ownership(view, live_view), *diff_view(view, live_view))
        diffs.append(
            TableDiff(
                view.name,
                changes,
                _view_facts(view.name, live_view),
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


def order_views(views: Sequence[View]) -> list[View]:
    """Views in an order where each comes after the views its query reads.

    Stable: views that don't depend on each other keep their spec order. A cycle
    is an error — no order could create them.
    """
    names = [view.name for view in views]
    depends_on = {
        view.name: {o for o in names if o != view.name and _reads(view.query, o)}
        for view in views
    }
    ordered: list[View] = []
    placed: set[str] = set()
    while len(ordered) < len(views):
        ready = [
            v for v in views if v.name not in placed and depends_on[v.name] <= placed
        ]
        if not ready:
            stuck = sorted(name for name in names if name not in placed)
            raise PlanningError(
                f"these views read each other in a cycle: {', '.join(stuck)}"
            )
        for view in ready:
            ordered.append(view)
            placed.add(view.name)
    return ordered


def _reads(query: str, name: str) -> bool:
    """Does the query name this table or view — quoted or not, any case?"""
    pattern = r"\s*\.\s*".join(rf"`?{re.escape(part)}`?" for part in name.split("."))
    found = re.search(rf"(?<![\w`.]){pattern}(?![\w`])", query, re.IGNORECASE)
    return found is not None


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


def _view_facts(name: str, live: View | None) -> TableFacts:
    return TableFacts(
        name,
        exists=live is not None,
        properties=live.properties if live else (),
        kind="view",
    )


def _facts(
    introspector: Introspector,
    name: str,
    live: LiveTable | None,
    *,
    changed: bool,
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
    )


def _not_modelled(live: LiveTable | None) -> tuple[str, ...]:
    return tuple(
        f"{feature} (not modelled)" for feature in (live.unmodelled if live else ())
    )
