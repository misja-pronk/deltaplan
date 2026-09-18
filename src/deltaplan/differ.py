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
from deltaplan.model.table import (
    MANAGED_PROPERTY,
    PREREQUISITE_PROPERTIES,
    Check,
    PrimaryKey,
    Table,
    field_at,
    type_at,
)
from deltaplan.model.types import Array, DataType, Field, Map, Struct, type_kind
from deltaplan.sql import normalise_expression

# Live properties that are Delta's own bookkeeping rather than anyone's intent.
# They are never reported as unmanaged, because seeing them would be noise.
_BOOKKEEPING_PROPERTIES = frozenset(
    {
        "delta.columnMapping.maxColumnId",
        "delta.minReaderVersion",
        "delta.minWriterVersion",
        # deltaplan's own ownership marker. A spec never writes it — see
        # `ownership()` for how it gets there — and reporting it as unmanaged
        # would be reporting ourselves.
        MANAGED_PROPERTY,
        # Likewise the table features `apply` turns on as prerequisites: after a
        # rename, columnMapping is on because deltaplan put it there.
        *PREREQUISITE_PROPERTIES,
    }
)


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
    return tuple(changes)


def ownership(desired: Table, actual: Table | None) -> tuple[Change, ...]:
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


def unmanaged(desired: Table, actual: Table) -> tuple[str, ...]:
    """Live things the spec says nothing about. Reported, never touched."""
    found: list[str] = []
    desired_properties = desired.properties_map()
    for key in sorted(actual.properties_map()):
        if key not in desired_properties and key not in _BOOKKEEPING_PROPERTIES:
            found.append(f"property {key}")
    desired_tags = desired.tags_map()
    for key in sorted(actual.tags_map()):
        if key not in desired_tags:
            found.append(f"tag {key}")
    if desired.primary_key() is None and actual.primary_key() is not None:
        found.append("primary key")
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
    if desired.cluster_by != actual.cluster_by:
        changes.append(
            Change(
                desired.name,
                "set_cluster_by",
                before=actual.cluster_by,
                after=desired.cluster_by,
            )
        )

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
    live = {field.name for field in actual_fields}
    matched: dict[str, str] = {}
    for field in desired_fields:
        old = field.renamed_from
        if old is not None and old in live and field.name not in live:
            matched[field.name] = old
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
    consumed = set(renames.values())
    live_by_name = {field.name: field for field in actual_fields}

    for field in desired_fields:
        child_path = f"{path}.{field.name}"
        live_name = renames.get(field.name, field.name)
        live = live_by_name.get(live_name)
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

    desired_names = {field.name for field in desired_fields}
    for live in actual_fields:
        if live.name not in desired_names and live.name not in consumed:
            changes.append(
                Change(table, "drop_column", f"{path}.{live.name}", before=live)
            )
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
    return changes


def _primary_key_differs(desired: PrimaryKey, live: PrimaryKey | None) -> bool:
    if live is None:
        return True
    if desired.columns != live.columns:
        return True
    return desired.name is not None and desired.name != live.name


def is_applied(change: Change, live: Table | None) -> bool:
    """Is this change already true of the live table?

    The executor's idempotency check. The design calls for a precheck query per
    step; asking the model instead reuses code that is already tested and adds no
    new assumption about what Databricks accepts — the live table is read the
    same way `plan` read it, and compared the same way the differ compares it.

    Being wrong in the "not applied yet" direction is the safe one: the step runs
    again, and every statement deltaplan generates is safe to repeat.
    """
    if change.kind == "create_table":
        return live is not None
    if change.kind == "drop_table":
        return live is None
    if live is None:
        return False

    match change.kind:
        case "claim_table":
            return live.managed
        case "set_table_comment":
            return live.comment == change.after
        case "set_cluster_by":
            return live.cluster_by == change.after
        case "set_property":
            return live.properties_map().get(change.path) == change.after
        case "set_tag":
            return live.tags_map().get(change.path) == change.after
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
        case "create_table" | "drop_table":  # answered above
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
    return False
