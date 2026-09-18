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

from collections.abc import Callable, Sequence
from dataclasses import replace

from deltaplan.differ import diff, ownership, unmanaged
from deltaplan.introspect import Introspector, LiveSchema, LiveTable
from deltaplan.loader import Mode
from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, TableDiff, TableFacts, fingerprint
from deltaplan.model.table import Table
from deltaplan.planner import build_plan


class PlanningError(Exception):
    """A spec that can't be planned, for a reason the loader couldn't see."""


def plan_tables(
    tables: Sequence[Table],
    introspector: Introspector,
    *,
    target: str,
    tool_version: str,
    mode_for: Callable[[str], Mode] = lambda _schema: "additive",
    check_order: bool = False,
    clone: bool = False,
) -> Plan:
    """Plan every table against live state.

    `mode_for` answers `strict` or `additive` for a `catalog.schema` — it is a
    callable rather than a mapping because which schemas matter isn't known
    until the specs have been read.
    """
    schemas = _introspect(tables, introspector)
    described = {table.name for table in tables}

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
                unmanaged(table, live_table) if live_table else (),
                desired=table,
                live=live_table,
            )
        )

    unmanaged_tables: list[str] = []
    orphaned_tables: list[str] = []
    for (catalog, schema), found in sorted(schemas.items()):
        for live in found.tables:
            if live.table.name in described:
                continue
            if not live.table.managed:
                unmanaged_tables.append(live.table.name)
            elif mode_for(f"{catalog}.{schema}") == "strict":
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

    built = build_plan(
        diffs,
        target=target,
        tool_version=tool_version,
        spec_hash=fingerprint(tables),
        state_fingerprint=fingerprint(d.live for d in diffs),
        clone=clone,
    )
    return replace(
        built,
        unmanaged_tables=tuple(sorted(unmanaged_tables)),
        orphaned_tables=tuple(sorted(orphaned_tables)),
    )


def _introspect(
    tables: Sequence[Table], introspector: Introspector
) -> dict[tuple[str, str], LiveSchema]:
    schemas: dict[tuple[str, str], LiveSchema] = {}
    for table in tables:
        parts = table.parts
        if len(parts) != 3:
            raise PlanningError(f"table name {table.name!r} must be catalog.schema.table")
        key = (parts[0], parts[1])
        if key not in schemas:
            schemas[key] = introspector.schema(*key)
    return schemas


def _schema_of(schemas: dict[tuple[str, str], LiveSchema], name: str) -> LiveSchema:
    catalog, schema, _ = name.split(".")
    return schemas[(catalog, schema)]


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
    )
