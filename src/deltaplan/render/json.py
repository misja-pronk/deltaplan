"""The plan file format: the artefact `apply` consumes and CI diffs.

Both directions live here, so the writer and the reader can't drift apart. Model
objects become plain data — types render back to Databricks type strings — and
come back as the same objects, which a round-trip test asserts.

Every planned table is written out, including the ones with no changes: `apply`
recomputes the state fingerprint over exactly the tables that went into it, in
the same order, and a missing entry would make a valid plan look stale.
"""

from __future__ import annotations

import json
from typing import Any

from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, Step, TableDiff, TableFacts
from deltaplan.model.table import Check, Grant, PrimaryKey, RowFilter, Table
from deltaplan.model.types import Field, Mask, as_data_type, render_type
from deltaplan.model.view import View
from deltaplan.typeparser import parse_type

#: Bumped when the shape below changes in a way `apply` has to know about.
PLAN_FORMAT_VERSION = 1


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    summary = plan.summary
    return {
        "format_version": PLAN_FORMAT_VERSION,
        "tool_version": plan.tool_version,
        "target": plan.target,
        "spec_hash": plan.spec_hash,
        "state_fingerprint": plan.state_fingerprint,
        "summary": {
            "add": summary.add,
            "change": summary.change,
            "destroy": summary.destroy,
            "steps": summary.steps,
            "rewrites": summary.rewrites,
            "warnings": summary.warnings,
            "highest_risk": plan.highest_risk,
        },
        "unmanaged_tables": list(plan.unmanaged_tables),
        "orphaned_tables": list(plan.orphaned_tables),
        "tables": [_diff_to_dict(diff) for diff in plan.diffs],
        "steps": [_step_to_dict(step) for step in plan.steps],
    }


def dumps(plan: Plan, *, indent: int = 2) -> str:
    return json.dumps(plan_to_dict(plan), indent=indent) + "\n"


# ---------------------------------------------------------------------------


def _diff_to_dict(diff: TableDiff) -> dict[str, Any]:
    return {
        "table": diff.table,
        "facts": _facts_to_dict(diff.facts),
        "unmanaged": list(diff.unmanaged),
        "notes": list(diff.notes),
        # Both sides are kept: a rewrite needs the shape it builds towards, and a
        # plan file that records what was compared is one you can audit later.
        "desired": _relation_to_dict(diff.desired) if diff.desired else None,
        "live": _relation_to_dict(diff.live) if diff.live else None,
        "changes": [_change_to_dict(change) for change in diff.changes],
    }


def _facts_to_dict(facts: TableFacts) -> dict[str, Any]:
    return {
        "exists": facts.exists,
        "size_bytes": facts.size_bytes,
        "delta_version": facts.delta_version,
        "kind": facts.kind,
        "unmodelled": list(facts.unmodelled),
        "properties": dict(facts.properties),
    }


def _change_to_dict(change: Change) -> dict[str, Any]:
    return {
        "kind": change.kind,
        "path": change.path,
        "before": _value(change.before),
        "after": _value(change.after),
    }


def _step_to_dict(step: Step) -> dict[str, Any]:
    return {
        "id": step.id,
        "table": step.table,
        "change": step.change,
        "path": step.path,
        "title": step.title,
        "risk": step.risk,
        "sql": step.sql,
        "precheck": step.precheck,
        "refusal": step.refusal,
        "postcheck": step.postcheck,
        "failure": step.failure,
        "est_bytes": step.est_bytes,
        "undo_hint": step.undo_hint,
        "warnings": list(step.warnings),
        "note": step.note,
    }


def _value(value: object) -> Any:
    """Whatever a change carries, as plain data."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Field):
        return _field_to_dict(value)
    if isinstance(value, Table | View):
        return _relation_to_dict(value)
    if isinstance(value, PrimaryKey):
        return {"primary_key": {"name": value.name, "columns": list(value.columns)}}
    if isinstance(value, Check):
        return {"check": {"name": value.name, "expression": value.expression}}
    if isinstance(value, Mask):
        return _mask_to_dict(value)
    if isinstance(value, RowFilter):
        return _row_filter_to_dict(value)
    data_type = as_data_type(value)
    if data_type is not None:
        return render_type(data_type)
    if isinstance(value, tuple):
        return [_value(item) for item in value]
    return str(value)  # pragma: no cover - every ChangeValue is covered above


def _field_to_dict(field: Field) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "name": field.name,
        "type": render_type(field.type),
        "nullable": field.nullable,
    }
    if field.comment is not None:
        rendered["comment"] = field.comment
    # Hints take no part in equality, but a plan file should still say what the
    # spec said.
    if field.renamed_from is not None:
        rendered["renamed_from"] = field.renamed_from
    if field.using is not None:
        rendered["using"] = field.using
    if field.tags:
        rendered["tags"] = dict(field.tags)
    if field.mask is not None:
        rendered["mask"] = _mask_to_dict(field.mask)
    return rendered


def _mask_to_dict(mask: Mask) -> dict[str, Any]:
    return {"function": mask.function, "using_columns": list(mask.using_columns)}


def _row_filter_to_dict(row_filter: RowFilter) -> dict[str, Any]:
    return {"function": row_filter.function, "columns": list(row_filter.columns)}


def _relation_to_dict(relation: Table | View) -> dict[str, Any]:
    if isinstance(relation, View):
        return {
            "view": relation.name,
            "query": relation.query,
            "comment": relation.comment,
            "properties": dict(relation.properties),
            "tags": dict(relation.tags),
            "grants": {g.principal: list(g.privileges) for g in relation.grants},
        }
    return _table_to_dict(relation)


def _relation_from_dict(entry: dict[str, Any]) -> Table | View:
    if "view" in entry:
        return View(
            name=str(entry["view"]),
            query=str(entry["query"]),
            comment=entry.get("comment"),
            properties=tuple(sorted(entry.get("properties", {}).items())),
            tags=tuple(sorted(entry.get("tags", {}).items())),
            grants=tuple(
                Grant(principal, tuple(privileges))
                for principal, privileges in entry.get("grants", {}).items()
            ),
        )
    return _table_from_dict(entry)


def _table_to_dict(table: Table) -> dict[str, Any]:
    return {
        "table": table.name,
        "comment": table.comment,
        "cluster_by": list(table.cluster_by),
        "properties": dict(table.properties),
        "tags": dict(table.tags),
        "columns": [_field_to_dict(column) for column in table.columns],
        "constraints": [_value(constraint) for constraint in table.constraints],
        "grants": {grant.principal: list(grant.privileges) for grant in table.grants},
        "row_filter": _row_filter_to_dict(table.row_filter) if table.row_filter else None,
        "hooks": (
            {"before": table.hooks.before, "after": table.hooks.after}
            if table.hooks
            else None
        ),
    }


# ---------------------------------------------------------------------------
# reading a plan file back
# ---------------------------------------------------------------------------


class PlanFileError(Exception):
    """A plan file deltaplan can't read."""


def loads(text: str) -> Plan:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanFileError(f"not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise PlanFileError("a plan file is a JSON object")
    return plan_from_dict(document)


def plan_from_dict(document: dict[str, Any]) -> Plan:
    version = document.get("format_version")
    if version != PLAN_FORMAT_VERSION:
        raise PlanFileError(
            f"plan format version {version!r} — this deltaplan writes and reads "
            f"version {PLAN_FORMAT_VERSION}. Re-run `deltaplan plan`."
        )
    try:
        return Plan(
            tool_version=str(document["tool_version"]),
            target=str(document["target"]),
            spec_hash=str(document["spec_hash"]),
            state_fingerprint=str(document["state_fingerprint"]),
            diffs=tuple(_diff_from_dict(entry) for entry in document["tables"]),
            steps=tuple(_step_from_dict(entry) for entry in document["steps"]),
            unmanaged_tables=tuple(document.get("unmanaged_tables", ())),
            orphaned_tables=tuple(document.get("orphaned_tables", ())),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PlanFileError(f"malformed plan file: {error}") from error


def _diff_from_dict(entry: dict[str, Any]) -> TableDiff:
    table = str(entry["table"])
    facts = entry.get("facts", {})
    return TableDiff(
        table=table,
        changes=tuple(_change_from_dict(table, c) for c in entry.get("changes", ())),
        facts=TableFacts(
            table,
            exists=bool(facts.get("exists", True)),
            properties=tuple(sorted(facts.get("properties", {}).items())),
            size_bytes=facts.get("size_bytes"),
            delta_version=facts.get("delta_version"),
            kind="view" if facts.get("kind") == "view" else "table",
            unmodelled=tuple(facts.get("unmodelled", ())),
        ),
        unmanaged=tuple(entry.get("unmanaged", ())),
        desired=_relation_from_dict(entry["desired"]) if entry.get("desired") else None,
        live=_relation_from_dict(entry["live"]) if entry.get("live") else None,
        notes=tuple(entry.get("notes", ())),
    )


def _step_from_dict(entry: dict[str, Any]) -> Step:
    return Step(
        id=int(entry["id"]),
        table=str(entry["table"]),
        title=str(entry["title"]),
        risk=entry["risk"],
        change=int(entry.get("change", -1)),
        path=str(entry.get("path", "")),
        sql=entry.get("sql"),
        precheck=entry.get("precheck"),
        refusal=entry.get("refusal"),
        postcheck=entry.get("postcheck"),
        failure=entry.get("failure"),
        est_bytes=entry.get("est_bytes"),
        undo_hint=entry.get("undo_hint"),
        warnings=tuple(entry.get("warnings", ())),
        note=entry.get("note"),
    )


def _change_from_dict(table: str, entry: dict[str, Any]) -> Change:
    kind = entry["kind"]
    return Change(
        table=table,
        kind=kind,
        path=str(entry.get("path", "")),
        before=_value_from(kind, entry.get("before")),
        after=_value_from(kind, entry.get("after")),
    )


def _value_from(kind: str, raw: Any) -> Any:
    """Rebuild what a change carries. The kind says how to read it."""
    if raw is None:
        return None
    match kind:
        case "create_table" | "drop_table" | "create_view" | "replace_view":
            return _relation_from_dict(raw)
        case "add_column" | "drop_column":
            return _field_from_dict(raw)
        case "change_type":
            return parse_type(str(raw))
        case "set_mask":
            return _mask_from_dict(raw)
        case "set_row_filter":
            return _row_filter_from_dict(raw)
        case "add_constraint" | "drop_constraint":
            return _constraint_from_dict(raw)
        case "set_cluster_by" | "reorder_columns" | "set_column_tag" | "grant" | "revoke":
            return tuple(str(item) for item in raw)
        case _:
            return raw


def _field_from_dict(entry: dict[str, Any]) -> Field:
    return Field(
        str(entry["name"]),
        parse_type(str(entry["type"])),
        nullable=bool(entry.get("nullable", True)),
        comment=entry.get("comment"),
        renamed_from=entry.get("renamed_from"),
        using=entry.get("using"),
        tags=tuple(sorted(entry.get("tags", {}).items())),
        mask=_mask_from_dict(entry["mask"]) if entry.get("mask") else None,
    )


def _mask_from_dict(entry: dict[str, Any]) -> Mask:
    return Mask(str(entry["function"]), tuple(entry.get("using_columns", ())))


def _row_filter_from_dict(entry: dict[str, Any]) -> RowFilter:
    return RowFilter(str(entry["function"]), tuple(entry.get("columns", ())))


def _constraint_from_dict(entry: dict[str, Any]) -> PrimaryKey | Check:
    if "primary_key" in entry:
        body = entry["primary_key"]
        return PrimaryKey(tuple(body["columns"]), body.get("name"))
    body = entry["check"]
    return Check(str(body["name"]), str(body["expression"]))


def _table_from_dict(entry: dict[str, Any]) -> Table:
    return Table(
        name=str(entry["table"]),
        columns=tuple(_field_from_dict(column) for column in entry["columns"]),
        comment=entry.get("comment"),
        cluster_by=tuple(entry.get("cluster_by", ())),
        properties=tuple(sorted(entry.get("properties", {}).items())),
        tags=tuple(sorted(entry.get("tags", {}).items())),
        constraints=tuple(
            _constraint_from_dict(item) for item in entry.get("constraints", ())
        ),
        grants=tuple(
            Grant(principal, tuple(privileges))
            for principal, privileges in entry.get("grants", {}).items()
        ),
        row_filter=(
            _row_filter_from_dict(entry["row_filter"])
            if entry.get("row_filter")
            else None
        ),
    )
