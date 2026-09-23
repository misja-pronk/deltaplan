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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import TypeAlias, TypeVar

from deltaplan.differ import (
    diff,
    diff_function,
    diff_schema,
    diff_view,
    diff_volume,
    ownership,
    spent_renames,
    unmanaged,
    unmanaged_function,
    unmanaged_schema,
    unmanaged_view,
)
from deltaplan.errors import DeltaplanError
from deltaplan.introspect import Introspector, LiveSchema, LiveTable
from deltaplan.loader import Mode
from deltaplan.manage import EVERYTHING, Manage, strip
from deltaplan.model.change import Change
from deltaplan.model.function import Function
from deltaplan.model.plan import Plan, TableDiff, TableFacts, fingerprint
from deltaplan.model.schema import Schema
from deltaplan.model.table import Table
from deltaplan.model.view import Relation, View
from deltaplan.model.volume import Volume
from deltaplan.planner import build_plan


class PlanningError(DeltaplanError):
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
    select: Callable[[str], bool] | None = None,
    owned_elsewhere: Mapping[str, str] | None = None,
    manage: Manage = EVERYTHING,
) -> Plan:
    """Plan every function, table and view against live state.

    Schemas and volumes first — everything else lives in one. Then functions,
    tables and views in **one order over the objects**, so each comes after
    whatever it names: a function after the tables its body reads, a table
    after the functions its row filter and masks call, a view after what its
    query reads (`order_relations`). Objects that name nothing of each other
    keep the old order — functions, tables, views. A function is never dropped
    — it carries no ownership marker — so one without a spec is left alone.

    `mode_for` answers `strict` or `additive` for a `catalog.schema` — it is a
    callable rather than a mapping because which schemas matter isn't known
    until the specs have been read.

    `select` narrows the plan to the specs whose names it accepts. A selection
    plans what it names and nothing else: no orphans, no unmanaged report — so
    a table left out of it can never look like one whose spec is gone.

    `owned_elsewhere` maps the full name of a catalog, schema or volume another
    tool declares — a Databricks Asset Bundle — to what declares it. deltaplan
    neither creates nor manages those; the tables inside them are its business.
    """
    # Functions have a namespace of their own; only tables and views can clash.
    described = {spec.name for spec in specs if isinstance(spec, Table | View)}
    if select is not None:
        specs = [spec for spec in specs if select(spec.name)]
    schemas = _introspect(specs, introspector)
    relations = order_relations(specs)
    functions = [spec for spec in relations if isinstance(spec, Function)]
    _refuse_bundle_conflicts(specs, schemas, owned_elsewhere or {})
    _refuse_kind_changes([s for s in specs if isinstance(s, Table | View)], schemas)
    _refuse_shared_names(
        [*functions, *[s for s in specs if isinstance(s, Volume)]], described, schemas
    )

    diffs: list[TableDiff] = []
    # Schemas first: everything else lives in one.
    for declared in [spec for spec in specs if isinstance(spec, Schema)]:
        live_schema = schemas[(declared.parts[0], declared.parts[1])].definition
        diffs.append(
            TableDiff(
                declared.name,
                diff_schema(declared, live_schema),
                TableFacts(declared.name, exists=live_schema is not None, kind="schema"),
                unmanaged_schema(declared, live_schema) if live_schema else (),
                desired=declared,
                live=live_schema,
            )
        )
    # Volumes: independent of the rest, so any order will do — here, early.
    volumes = [spec for spec in specs if isinstance(spec, Volume)]
    for volume in volumes:
        found = _schema_of(schemas, volume.name)
        live_volume = found.get_volume(volume.name)
        diffs.append(
            TableDiff(
                volume.name,
                diff_volume(volume, live_volume),
                TableFacts(
                    volume.name,
                    exists=live_volume is not None,
                    kind="volume",
                    schema_exists=found.exists,
                ),
                unmanaged_schema(volume, live_volume) if live_volume else (),
                desired=volume,
                live=live_volume,
            )
        )
    # Then everything that can name something else, in the order that lets it
    # be created: a function after the tables its body reads, a table after the
    # functions its row filter and masks call, a view after what its query reads.
    for relation in relations:
        if isinstance(relation, Function):
            diffs.append(_function_diff(relation, schemas))
            continue
        if isinstance(relation, View):
            diffs.append(_view_diff(relation, schemas, manage))
            continue
        table = relation
        found = _schema_of(schemas, table.name)
        live = found.get(table.name)
        renaming, notes = _rename(table, live, found)
        if table.renamed_from is not None:
            # Whatever else happens, a table a spec says it used to be is not an
            # orphan: strict mode must never drop it on the strength of a hint.
            described.add(table.renamed_from)
        source = renaming or live
        # A table being renamed is compared under its new name, which is the name
        # every step after the rename uses.
        live_table = replace(source.table, name=table.name) if source else None
        changes = (
            *(
                (Change(table.name, "rename_table", before=renaming.table.name),)
                if renaming
                else ()
            ),
            *ownership(table, live_table),
            *diff(
                table,
                strip(live_table, manage) if live_table else None,
                compare_order=check_order,
            ),
        )
        facts = _facts(
            introspector,
            source.table.name if source else table.name,
            source,
            changed=bool(changes),
            schema_exists=found.exists,
        )
        diffs.append(
            TableDiff(
                table.name,
                changes,
                replace(facts, name=table.name),
                (
                    (*unmanaged(table, live_table), *_not_modelled(source))
                    if live_table
                    else ()
                ),
                desired=table,
                # As read, under the name it was read by: `apply` reads it again
                # there to check nothing moved since the plan.
                live=source.table if source else None,
                notes=(*notes, *(spent_renames(table, live_table) if live_table else ())),
            )
        )

    unmanaged_tables: list[str] = []
    orphaned_tables: list[str] = []
    for (catalog, schema), found in sorted(schemas.items() if select is None else ()):
        strict = mode_for(f"{catalog}.{schema}") == "strict"
        for live in found.tables:
            if live.table.name in described:
                continue
            if not live.table.managed:
                unmanaged_tables.append(live.table.name)
            elif strict:
                # About to be dropped: read in full, as apply will read it again
                # to check nothing changed since the plan.
                live = introspector.complete(live)
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
        not_managed=manage.elsewhere,
    )


_Ordered = TypeVar("_Ordered", View, Function, "Orderable")

#: What can name something else, and so has to be planned in an order.
Orderable: TypeAlias = Table | View | Function


def _function_diff(
    function: Function, schemas: dict[tuple[str, str], LiveSchema]
) -> TableDiff:
    """One function, compared with what the workspace has."""
    found = _schema_of(schemas, function.name)
    live_function = found.get_function(function.name)
    return TableDiff(
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


def _view_diff(
    view: View, schemas: dict[tuple[str, str], LiveSchema], manage: Manage
) -> TableDiff:
    """One view, compared with what the workspace has."""
    found = _schema_of(schemas, view.name)
    live_view = found.get_view(view.name)
    return TableDiff(
        view.name,
        (
            *ownership(view, live_view),
            *diff_view(view, strip(live_view, manage) if live_view else None),
        ),
        _view_facts(view.name, live_view, schema_exists=found.exists),
        unmanaged_view(view, live_view) if live_view else (),
        desired=view,
        live=live_view,
    )


def order_views(views: Sequence[View]) -> list[View]:
    """Views in an order where each comes after the views its query reads."""
    return _order(views, lambda view: view.query, "views read each other")


def order_relations(specs: Sequence[Relation]) -> list[Orderable]:
    """Functions, tables and views in an order that can actually be created.

    One graph over the objects, not a sequence of kinds. Three edges matter and
    they don't run one way between kinds:

    * a view's query reads tables, views and functions;
    * a **function's body reads tables** — a row filter that consults a lookup
      table is the ordinary way to write row-level security, and Databricks
      resolves a function's body when it is created;
    * a table's row filter and column masks call functions.

    Planning by kind — functions, then tables, then views — gets the last one
    right and the middle one wrong, so a fresh schema could never converge in
    one apply: the function was created before the table it reads.

    Objects with no edge between them keep the old order, so a project without
    such a reference plans exactly as it did.
    """
    ordered: list[Orderable] = [
        *[spec for spec in specs if isinstance(spec, Function)],
        *[spec for spec in specs if isinstance(spec, Table)],
        *[spec for spec in specs if isinstance(spec, View)],
    ]
    return _order(ordered, _references, "objects depend on each other")


def _references(spec: Orderable) -> str:
    """The SQL of a spec that can name another object, as one piece of text.

    A table has no query, but its row filter and column masks name functions;
    treating those names as its text puts it after them, which is where it has
    to be.
    """
    if isinstance(spec, View):
        return spec.query
    if isinstance(spec, Function):
        return spec.body
    if isinstance(spec, Table):
        names = [spec.row_filter.function] if spec.row_filter else []
        names += [
            column.mask.function
            for column in spec.columns
            if getattr(column, "mask", None) is not None and column.mask
        ]
        return " ".join(names)
    return ""


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


def _refuse_bundle_conflicts(
    specs: Sequence[Relation],
    schemas: dict[tuple[str, str], LiveSchema],
    owned: Mapping[str, str],
) -> None:
    """What a bundle declares is the bundle's: deltaplan won't manage it too.

    Two tools creating the same schema is how a `databricks bundle deploy` ends
    up meeting an object it didn't make. So a spec for one is refused, and a
    table whose schema the bundle hasn't deployed yet says so rather than
    creating it.
    """
    if not owned:
        return
    for spec in specs:
        declares = owned.get(spec.name.lower())
        if declares is not None and isinstance(spec, Schema | Volume):
            raise PlanningError(
                f"the bundle declares {spec.name} as {declares}, so deltaplan "
                "won't manage it too — remove the spec, or the bundle's resource"
            )
        if isinstance(spec, Schema | Volume):
            continue
        declares = owned.get(spec.schema.lower())
        if declares is not None and not _schema_of(schemas, spec.name).exists:
            raise PlanningError(
                f"{spec.schema} is the bundle's {declares}, and isn't there yet — "
                "run `databricks bundle deploy` first; deltaplan won't create it"
            )


def _refuse_shared_names(
    others: Sequence[Function | Volume],
    described: set[str],
    schemas: dict[tuple[str, str], LiveSchema],
) -> None:
    """A function or volume with the name of something else.

    Unity Catalog allows it — functions and volumes have namespaces of their
    own; a table and a volume can share a name (verified live) — but a plan, its
    history and its renderings are keyed by name, so deltaplan would confuse
    the two.
    """
    kinds: dict[str, str] = {}
    for spec in others:
        kind = "function" if isinstance(spec, Function) else "volume"
        found = _schema_of(schemas, spec.name)
        clash = None
        if spec.name in described or found.get(spec.name) or found.get_view(spec.name):
            clash = "a table or view"
        elif kind == "volume" and found.get_function(spec.name):
            clash = "a function"
        elif kind == "function" and found.get_volume(spec.name):
            clash = "a volume"
        elif kinds.get(spec.name, kind) != kind:
            clash = f"a {kinds[spec.name]}"
        if clash is not None:
            raise PlanningError(
                f"{spec.name} names both a {kind} and {clash}. deltaplan keys a "
                "plan by name, so it cannot manage both; rename one."
            )
        kinds[spec.name] = kind


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
    for spec in specs:
        if isinstance(spec, Schema):
            if len(spec.parts) != 2:
                raise PlanningError(f"schema {spec.name!r} must be catalog.schema")
        elif len(spec.parts) != 3:
            raise PlanningError(f"name {spec.name!r} must be catalog.schema.name")
    # Only the tables a spec describes are read in full — and a rename's old
    # name, which is about to become one. The rest of each schema gets a light
    # read: enough to list it and see whether it is deltaplan's.
    described = [spec.name for spec in specs if isinstance(spec, Table)]
    described += [
        spec.renamed_from
        for spec in specs
        if isinstance(spec, Table) and spec.renamed_from
    ]
    schemas: dict[tuple[str, str], LiveSchema] = {}
    for spec in specs:
        key = (spec.parts[0], spec.parts[1])
        if key not in schemas:
            schemas[key] = introspector.schema(*key, full=described)
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


def _rename(
    table: Table, live: LiveTable | None, found: LiveSchema
) -> tuple[LiveTable | None, tuple[str, ...]]:
    """The live table to rename to this spec's name, if there is one — and what
    to say about the hint when there isn't."""
    if table.renamed_from is None:
        return None, ()
    old = found.get(table.renamed_from)
    short = table.renamed_from.rsplit(".", 1)[-1]
    if live is None:
        return old, ()
    if old is None:
        return None, (f"renamed_from {short!r} has done its job — it can be removed",)
    return None, (
        f"both this table and {short} exist, so renamed_from is ignored and {short} "
        "is left as it is",
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
        features=live.features if live else (),
    )


def _not_modelled(live: LiveTable | None) -> tuple[str, ...]:
    return tuple(
        f"{feature} (not modelled)" for feature in (live.unmodelled if live else ())
    )
