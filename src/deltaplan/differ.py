"""Desired state minus live state.

A pure function: no I/O, no SDK, no clock, no environment. Give it two tables and
it gives you the same changes every time, which is what makes the plan testable
without a workspace.

Two rules shape everything here:

* **Nothing unmodelled is diffed away.** Properties, tags and constraints that
  exist on the live table but aren't in the spec are left alone and reported as
  unmanaged — deltaplan cannot tell "I stopped managing this" from "someone else
  owns this", so it does not guess.
* **Renames are declared, not inferred.** A column that changed name looks
  exactly like a drop plus an add, and guessing wrong destroys data. The spec
  says so with `renamed_from`, or it doesn't happen.
"""

from __future__ import annotations

from deltaplan.model.change import Change
from deltaplan.model.function import Function
from deltaplan.model.table import (
    CLUSTER_AUTO,
    MANAGED_PROPERTY,
    Check,
    ForeignKey,
    PrimaryKey,
    Securable,
    Table,
    field_at,
    is_bookkeeping,
    is_platform_default,
    type_at,
)
from deltaplan.model.types import Array, DataType, Field, Map, Struct, type_kind, walk
from deltaplan.model.view import Relation, View, normalise_query
from deltaplan.sql import normalise_expression


def diff(
    desired: Table,
    actual: Table | None,
    *,
    compare_order: bool = False,
) -> tuple[Change, ...]:
    """Diff one table. `actual` is None when the table doesn't exist yet.

    `compare_order` opts into diffing column order, which is off by default: a
    reordered spec is usually an edit to the file, not an intent to rewrite the
    table's column layout.
    """
    if actual is None:
        return (Change(desired.name, "create_table", after=desired),)

    changes: list[Change] = []
    changes.extend(_diff_table_metadata(desired, actual))
    changes.extend(_diff_columns(desired, actual, compare_order=compare_order))
    changes.extend(_diff_constraints(desired, actual))
    changes.extend(_diff_grants(desired, actual))
    if desired.row_filter is not None and desired.row_filter != actual.row_filter:
        changes.append(
            Change(
                desired.name,
                "set_row_filter",
                before=actual.row_filter,
                after=desired.row_filter,
            )
        )
    return tuple(changes)


def ownership(desired: Relation, actual: Relation | None) -> tuple[Change, ...]:
    """Claim a live table that a spec now describes but deltaplan didn't create.

    Writing a spec for a table is the decision to manage it, so the first apply
    marks it — which is how `import` hands a table over. It is a change of its
    own rather than something folded into the diff, because it is the one change
    that alters what deltaplan is later *allowed* to do: a managed table can
    become a drop candidate, an unmanaged one never can.
    """
    if actual is None or actual.managed:
        return ()
    return (Change(desired.name, "claim_table", path=MANAGED_PROPERTY, after="true"),)


def diff_view(desired: View, actual: View | None) -> tuple[Change, ...]:
    """Diff one view. A view's shape is its query, so that is what is compared.

    A changed query or comment replaces the view; tags, properties and grants
    are diffed like a table's.
    """
    if actual is None:
        return (Change(desired.name, "create_view", after=desired),)
    changes: list[Change] = []
    if (
        normalise_query(desired.query) != normalise_query(actual.query)
        or desired.comment != actual.comment
    ):
        changes.append(Change(desired.name, "replace_view", before=actual, after=desired))
    changes.extend(_diff_governance(desired, actual))
    return tuple(changes)


def diff_function(desired: Function, actual: Function | None) -> tuple[Change, ...]:
    """Diff one function. Its signature and body are its shape.

    A changed body, return type, parameter list or comment replaces it; grants
    are managed per principal as everywhere else.
    """
    if actual is None:
        return (Change(desired.name, "create_function", after=desired),)
    changes: list[Change] = []
    if _function_shape(desired) != _function_shape(actual):
        changes.append(
            Change(desired.name, "replace_function", before=actual, after=desired)
        )
    changes.extend(_diff_grants(desired, actual))
    return tuple(changes)


def _function_shape(function: Function) -> tuple[object, ...]:
    """TODO(verify): that routine_definition comes back as the body was written."""
    return (
        tuple((p.name.casefold(), p.type) for p in function.parameters),
        function.returns,
        normalise_query(function.body),
        function.comment,
    )


def _diff_clustering(desired: Table, actual: Table) -> list[Change]:
    """Keys, or automatic. Under AUTO the live keys are Databricks' choice, so a
    spec asking for AUTO compares only that it is on; one naming keys turns it
    off — `CLUSTER BY (…)` does, verified live."""
    before = CLUSTER_AUTO if actual.cluster_auto else actual.cluster_by
    if desired.cluster_auto:
        if actual.cluster_auto:
            return []
        return [Change(desired.name, "set_cluster_by", before=before, after=CLUSTER_AUTO)]
    if actual.cluster_auto or desired.cluster_by != actual.cluster_by:
        return [
            Change(
                desired.name, "set_cluster_by", before=before, after=desired.cluster_by
            )
        ]
    return []


def _diff_governance(desired: Securable, actual: Securable) -> list[Change]:
    """Properties and tags, additively, then grants per principal."""
    changes: list[Change] = []
    live_properties = actual.properties_map()
    for key, value in desired.properties:
        if live_properties.get(key) != value:
            changes.append(
                Change(desired.name, "set_property", key, live_properties.get(key), value)
            )
    live_tags = actual.tags_map()
    for key, value in desired.tags:
        if live_tags.get(key) != value:
            changes.append(
                Change(desired.name, "set_tag", key, live_tags.get(key), value)
            )
    changes.extend(_diff_grants(desired, actual))
    return changes


def unmanaged_properties(
    desired: Securable, actual: Securable
) -> tuple[tuple[str, str], ...]:
    """Live properties the spec doesn't declare, bookkeeping aside.

    What a rebuild — a rewrite, a view replace — has to carry across, so that
    replacing an object never diffs away what deltaplan doesn't manage.
    """
    declared = desired.properties_map()
    return tuple(
        (key, value)
        for key, value in actual.properties
        if key not in declared and not is_bookkeeping(key)
    )


def unmanaged_view(desired: View, actual: View) -> tuple[str, ...]:
    """What a live view carries that its spec doesn't mention."""
    return tuple(_unmanaged_governance(desired, actual))


def unmanaged_function(desired: Function, actual: Function) -> tuple[str, ...]:
    """What a live function carries that its spec doesn't mention: grants."""
    return tuple(_unmanaged_governance(desired, actual))


def _unmanaged_governance(desired: Securable, actual: Securable) -> list[str]:
    found: list[str] = []
    declared_properties = desired.properties_map()
    for key in sorted(actual.properties_map()):
        if key not in declared_properties and not is_bookkeeping(key):
            found.append(f"property {key}")
    declared_tags = desired.tags_map()
    for key in sorted(actual.tags_map()):
        if key not in declared_tags:
            found.append(f"tag {key}")
    declared_grants = desired.grants_map()
    for grant in actual.grants:
        if grant.principal not in declared_grants:
            found.append(f"grants to {grant.principal}")
    return found


def spent_renames(desired: Table, actual: Table) -> tuple[str, ...]:
    """`renamed_from` hints that have done their job and can be deleted.

    A hint is spent once the old name is gone and the new one is live. It does
    no harm — the differ ignores it — but a spec is easier to read without it.
    The design puts this in `validate`; it needs the live table, so it lives here.
    """
    found: list[str] = []
    for column in desired.columns:
        candidates = [(column.name, column), *walk(column.type, column.name)]
        for path, field in candidates:
            old = field.renamed_from
            if old is None:
                continue
            old_path = _sibling_path(path, old)
            if type_at(actual, path) is not None and type_at(actual, old_path) is None:
                found.append(
                    f"renamed_from {old!r} on {path} has done its job — it can be removed"
                )
    return tuple(found)


def unmanaged(desired: Table, actual: Table) -> tuple[str, ...]:
    """Live things the spec says nothing about. Reported, never touched."""
    found: list[str] = []
    desired_properties = desired.properties_map()
    for key, value in sorted(actual.properties_map().items()):
        if (
            key not in desired_properties
            and not is_bookkeeping(key)
            and not is_platform_default(key, value)
        ):
            found.append(f"property {key}")
    desired_tags = desired.tags_map()
    for key in sorted(actual.tags_map()):
        if key not in desired_tags:
            found.append(f"tag {key}")
    for live_column in actual.columns:
        column = desired.column(live_column.name)
        declared = dict(column.tags) if column else {}
        for key, _ in live_column.tags:
            if key not in declared:
                found.append(f"tag {key} on column {live_column.name}")
    if desired.primary_key() is None and actual.primary_key() is not None:
        found.append("primary key")
    # A security control the spec doesn't mention is never removed by deltaplan:
    # silently weakening one is the worst mistake a plan can make.
    if desired.row_filter is None and actual.row_filter is not None:
        found.append("row filter")
    for live_column in actual.columns:
        spec_column = desired.column(live_column.name)
        if live_column.mask is not None and (
            spec_column is None or spec_column.mask is None
        ):
            found.append(f"mask on column {live_column.name}")
    declared = desired.grants_map()
    for grant in actual.grants:
        if grant.principal not in declared:
            found.append(f"grants to {grant.principal}")
    for key in actual.foreign_keys():
        if not any(key.same_as(declared) for declared in desired.foreign_keys()):
            found.append(f"foreign key {key.name or 'unnamed'}")
    desired_checks = {check.name for check in desired.checks()}
    for check in actual.checks():
        if check.name not in desired_checks:
            found.append(f"check constraint {check.name}")
    return tuple(found)


# ---------------------------------------------------------------------------
# table level
# ---------------------------------------------------------------------------


def _diff_table_metadata(desired: Table, actual: Table) -> list[Change]:
    changes: list[Change] = []
    if desired.comment != actual.comment:
        changes.append(
            Change(
                desired.name,
                "set_table_comment",
                before=actual.comment,
                after=desired.comment,
            )
        )
    changes.extend(_diff_clustering(desired, actual))

    live_properties = actual.properties_map()
    for key, value in desired.properties:
        if live_properties.get(key) != value:
            changes.append(
                Change(
                    desired.name,
                    "set_property",
                    path=key,
                    before=live_properties.get(key),
                    after=value,
                )
            )

    live_tags = actual.tags_map()
    for key, value in desired.tags:
        if live_tags.get(key) != value:
            changes.append(
                Change(
                    desired.name,
                    "set_tag",
                    path=key,
                    before=live_tags.get(key),
                    after=value,
                )
            )
    return changes


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------


def _match_renames(
    desired_fields: tuple[Field, ...],
    actual_fields: tuple[Field, ...],
) -> dict[str, str]:
    """Desired field name -> the live field name it used to have.

    A `renamed_from` hint applies only while it still describes reality: the old
    name is there and the new one isn't. Once the rename has happened the hint is
    spent, and a spec that still carries it diffs clean.
    """
    live = {field.name.casefold(): field.name for field in actual_fields}
    matched: dict[str, str] = {}
    for field in desired_fields:
        old = field.renamed_from
        if (
            old is not None
            and old.casefold() in live
            and field.name.casefold() not in live
        ):
            matched[field.name] = live[old.casefold()]
    return matched


def _diff_columns(
    desired: Table,
    actual: Table,
    *,
    compare_order: bool,
) -> list[Change]:
    changes: list[Change] = []
    renames = _match_renames(desired.columns, actual.columns)
    consumed = set(renames.values())

    for column in desired.columns:
        live_name = renames.get(column.name)
        live = actual.column(live_name) if live_name else actual.column(column.name)
        if live is None:
            changes.append(Change(desired.name, "add_column", column.name, after=column))
            continue
        if live_name is not None:
            changes.append(
                Change(
                    desired.name,
                    "rename_column",
                    column.name,
                    before=live_name,
                    after=column.name,
                )
            )
        changes.extend(_diff_field(desired.name, column.name, column, live))
        changes.extend(_diff_column_tags(desired.name, column, live))
        changes.extend(_diff_generation(desired.name, column, live))
        if column.mask is not None and column.mask != live.mask:
            changes.append(
                Change(
                    desired.name,
                    "set_mask",
                    column.name,
                    before=live.mask,
                    after=column.mask,
                )
            )

    for live in actual.columns:
        if live.name in consumed:
            continue
        if desired.column(live.name) is None:
            changes.append(Change(desired.name, "drop_column", live.name, before=live))

    if compare_order:
        shared = [name for name in desired.column_names if actual.column(name)]
        live_order = [name for name in actual.column_names if name in set(shared)]
        if shared != live_order:
            changes.append(
                Change(
                    desired.name,
                    "reorder_columns",
                    before=tuple(live_order),
                    after=tuple(shared),
                )
            )
    return changes


def _diff_generation(table: str, desired: Field, actual: Field) -> list[Change]:
    """How a column gets a value it wasn't given: identity, generated, default.

    All three are modelled, so a spec that leaves one out means the column has
    none — as a missing comment means no comment.
    """
    changes: list[Change] = []
    if desired.identity != actual.identity:
        changes.append(
            Change(table, "set_identity", desired.name, actual.identity, desired.identity)
        )
    if _expression(desired.generated) != _expression(actual.generated):
        changes.append(
            Change(
                table, "set_generated", desired.name, actual.generated, desired.generated
            )
        )
    if _expression(desired.default) != _expression(actual.default):
        changes.append(
            Change(table, "set_default", desired.name, actual.default, desired.default)
        )
    return changes


def _expression(text: str | None) -> str | None:
    """Compared by meaning: the catalog echoes a generation as
    `( CAST(placed_at AS DATE) )` (verified live), which normalises to the same
    expression a spec writes."""
    return normalise_expression(text) if text is not None else None


def _diff_column_tags(table: str, desired: Field, actual: Field) -> list[Change]:
    """Column tags, additively: set what the spec names, leave the rest."""
    live = dict(actual.tags)
    return [
        Change(
            table,
            "set_column_tag",
            desired.name,
            before=(key, live[key]) if key in live else None,
            after=(key, value),
        )
        for key, value in desired.tags
        if live.get(key) != value
    ]


def _diff_field(table: str, path: str, desired: Field, actual: Field) -> list[Change]:
    """Compare one field with its live counterpart, then descend into its type."""
    changes: list[Change] = []
    if desired.nullable != actual.nullable:
        changes.append(
            Change(
                table,
                "set_nullable",
                path,
                before=actual.nullable,
                after=desired.nullable,
            )
        )
    if desired.comment != actual.comment:
        changes.append(
            Change(
                table, "set_comment", path, before=actual.comment, after=desired.comment
            )
        )
    changes.extend(_diff_type(table, path, desired.type, actual.type))
    return changes


def _diff_type(
    table: str,
    path: str,
    desired: DataType,
    actual: DataType,
) -> list[Change]:
    """Descend two types in step, emitting changes at nested paths.

    A *kind* change (struct becoming an array, say) stops the descent: there is
    nothing to compare field by field, and the planner will call it a rewrite.
    """
    if type_kind(desired) != type_kind(actual):
        return [Change(table, "change_type", path, before=actual, after=desired)]

    match (desired, actual):
        case (Struct(desired_fields), Struct(actual_fields)):
            return _diff_struct(table, path, desired_fields, actual_fields)
        case (Array(desired_element, _), Array(actual_element, _)):
            # containsNull isn't expressible in a type string, so it is carried
            # for fidelity but never diffed — it would flap against live state.
            return _diff_type(table, f"{path}.element", desired_element, actual_element)
        case (Map(desired_key, desired_value), Map(actual_key, actual_value)):
            return [
                *_diff_type(table, f"{path}.key", desired_key, actual_key),
                *_diff_type(table, f"{path}.value", desired_value, actual_value),
            ]
        case _ if desired != actual:
            return [Change(table, "change_type", path, before=actual, after=desired)]
        case _:
            return []


def _diff_struct(
    table: str,
    path: str,
    desired_fields: tuple[Field, ...],
    actual_fields: tuple[Field, ...],
) -> list[Change]:
    changes: list[Change] = []
    renames = _match_renames(desired_fields, actual_fields)
    consumed = {name.casefold() for name in renames.values()}
    live_by_name = {field.name.casefold(): field for field in actual_fields}

    for field in desired_fields:
        child_path = f"{path}.{field.name}"
        live_name = renames.get(field.name, field.name)
        live = live_by_name.get(live_name.casefold())
        if live is None:
            changes.append(Change(table, "add_column", child_path, after=field))
            continue
        if field.name in renames:
            changes.append(
                Change(
                    table,
                    "rename_column",
                    child_path,
                    before=live_name,
                    after=field.name,
                )
            )
        changes.extend(_diff_field(table, child_path, field, live))

    desired_names = {field.name.casefold() for field in desired_fields}
    for live in actual_fields:
        folded = live.name.casefold()
        if folded not in desired_names and folded not in consumed:
            changes.append(
                Change(table, "drop_column", f"{path}.{live.name}", before=live)
            )
    return changes


# ---------------------------------------------------------------------------
# grants
# ---------------------------------------------------------------------------


def _diff_grants(desired: Securable, actual: Securable) -> list[Change]:
    """Per principal: a principal the spec names has exactly those privileges.

    Principals it doesn't name are left alone — they are reported by
    `unmanaged()`. That is the line between managing access and taking it over.
    """
    changes: list[Change] = []
    live = actual.grants_map()
    for grant in desired.grants:
        held = set(live.get(grant.principal, ()))
        wanted = set(grant.privileges)
        if missing := tuple(sorted(wanted - held)):
            changes.append(Change(desired.name, "grant", grant.principal, after=missing))
        if extra := tuple(sorted(held - wanted)):
            changes.append(Change(desired.name, "revoke", grant.principal, before=extra))
    return changes


# ---------------------------------------------------------------------------
# constraints
# ---------------------------------------------------------------------------


def _diff_constraints(desired: Table, actual: Table) -> list[Change]:
    changes: list[Change] = []

    desired_key = desired.primary_key()
    live_key = actual.primary_key()
    if desired_key is not None and _primary_key_differs(desired_key, live_key):
        if live_key is not None:
            changes.append(Change(desired.name, "drop_constraint", before=live_key))
        changes.append(Change(desired.name, "add_constraint", after=desired_key))

    live_checks = {check.name: check for check in actual.checks()}
    for check in desired.checks():
        live = live_checks.get(check.name)
        if live is None:
            changes.append(Change(desired.name, "add_constraint", after=check))
        elif normalise_expression(live.expression) != normalise_expression(
            check.expression
        ):
            # A check's definition can't be altered in place, so it is replaced.
            changes.append(Change(desired.name, "drop_constraint", before=live))
            changes.append(Change(desired.name, "add_constraint", after=check))

    # A foreign key is matched by what it means. A declared one that no live key
    # means is added; one that means it under another name is renamed by being
    # replaced, but only when the spec names it — an unnamed spec key is happy
    # with any name. Live keys nobody declared are someone else's.
    live_keys = actual.foreign_keys()
    for key in desired.foreign_keys():
        match = next((live for live in live_keys if live.same_as(key)), None)
        if match is None:
            same_name = next(
                (live for live in live_keys if key.name and live.name == key.name), None
            )
            if same_name is not None:
                changes.append(Change(desired.name, "drop_constraint", before=same_name))
            changes.append(Change(desired.name, "add_constraint", after=key))
        elif key.name and match.name != key.name:
            changes.append(Change(desired.name, "drop_constraint", before=match))
            changes.append(Change(desired.name, "add_constraint", after=key))
    return changes


def _primary_key_differs(desired: PrimaryKey, live: PrimaryKey | None) -> bool:
    if live is None:
        return True
    if desired.columns != live.columns:
        return True
    return desired.name is not None and desired.name != live.name


def is_applied(change: Change, live: Relation | None) -> bool:
    """Is this change already true of the live table or view?

    The executor's idempotency check. The design calls for a precheck query per
    step; asking the model instead reuses code that is already tested and adds no
    new assumption about what Databricks accepts — the live object is read the
    same way `plan` read it, and compared the same way the differ compares it.

    Being wrong in the "not applied yet" direction is the safe one: the step runs
    again, and every statement deltaplan generates is safe to repeat.
    """
    if change.kind in {"create_table", "create_view", "create_function"}:
        return live is not None
    if change.kind == "drop_table":
        return live is None
    if live is None:
        return False

    match change.kind:
        case "claim_table":
            return live.managed
        case "rename_table":
            # `live` is whatever answers to the new name: the rename happened.
            return True
        case "set_property":
            return live.properties_map().get(change.path) == change.after
        case "set_tag":
            return live.tags_map().get(change.path) == change.after
        case "grant":
            wanted = change.after if isinstance(change.after, tuple) else ()
            return set(wanted) <= set(live.grants_map().get(change.path, ()))
        case "revoke":
            gone = change.before if isinstance(change.before, tuple) else ()
            return not set(gone) & set(live.grants_map().get(change.path, ()))
        case "replace_function":
            wanted = change.after
            return (
                isinstance(live, Function)
                and isinstance(wanted, Function)
                and _function_shape(live) == _function_shape(wanted)
            )
        case "replace_view":
            wanted = change.after
            return (
                isinstance(live, View)
                and isinstance(wanted, View)
                and normalise_query(live.query) == normalise_query(wanted.query)
                and live.comment == wanted.comment
            )
        case _:
            return isinstance(live, Table) and _is_applied_to_table(change, live)


def _is_applied_to_table(change: Change, live: Table) -> bool:
    """The kinds only a table has: columns, constraints, clustering, filters."""
    match change.kind:
        case "set_table_comment":
            return live.comment == change.after
        case "set_cluster_by":
            if change.after == CLUSTER_AUTO:
                return live.cluster_auto
            return not live.cluster_auto and live.cluster_by == change.after
        case "set_mask":
            column = live.column(change.path)
            return column is not None and column.mask == change.after
        case "set_identity":
            column = live.column(change.path)
            return column is not None and column.identity == change.after
        case "set_generated" | "set_default":
            column = live.column(change.path)
            if column is None:
                return False
            live_value = (
                column.generated if change.kind == "set_generated" else column.default
            )
            wanted = change.after if isinstance(change.after, str) else None
            return _expression(live_value) == _expression(wanted)
        case "set_row_filter":
            return live.row_filter == change.after
        case "set_column_tag":
            column = live.column(change.path)
            wanted = change.after if isinstance(change.after, tuple) else ()
            return column is not None and tuple(wanted) in column.tags
        case "add_column":
            return type_at(live, change.path) is not None
        case "drop_column":
            return type_at(live, change.path) is None
        case "rename_column":
            old = change.before if isinstance(change.before, str) else ""
            old_path = _sibling_path(change.path, old)
            return (
                type_at(live, change.path) is not None and type_at(live, old_path) is None
            )
        case "change_type":
            return type_at(live, change.path) == change.after
        case "set_nullable":
            field = field_at(live, change.path)
            return field is not None and field.nullable == change.after
        case "set_comment":
            field = field_at(live, change.path)
            return field is not None and field.comment == change.after
        case "reorder_columns":
            wanted = change.after if isinstance(change.after, tuple) else ()
            return tuple(n for n in live.column_names if n in set(wanted)) == wanted
        case "add_constraint":
            return _has_constraint(live, change.after)
        case "drop_constraint":
            return not _has_constraint(live, change.before)
        case _:
            # Every other kind is answered by `is_applied` before it gets here.
            return False


def _sibling_path(path: str, name: str) -> str:
    parent, _, _ = path.rpartition(".")
    return f"{parent}.{name}" if parent else name


def _has_constraint(live: Table, constraint: object) -> bool:
    if isinstance(constraint, PrimaryKey):
        key = live.primary_key()
        return key is not None and key.columns == constraint.columns
    if isinstance(constraint, Check):
        wanted = normalise_expression(constraint.expression)
        return any(
            check.name == constraint.name
            and normalise_expression(check.expression) == wanted
            for check in live.checks()
        )
    if isinstance(constraint, ForeignKey):
        return any(
            key.same_as(constraint)
            and (not constraint.name or key.name == constraint.name)
            for key in live.foreign_keys()
        )
    return False
