"""The plan as JSON: the artefact `apply` consumes and CI diffs.

Model objects become plain data here — types render back to Databricks type
strings, so a plan file is readable without deltaplan to hand.
"""

from __future__ import annotations

import json
from typing import Any

from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, Step, TableDiff, TableFacts
from deltaplan.model.table import Check, PrimaryKey, Table
from deltaplan.model.types import Field, as_data_type, render_type

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
        "tables": [_diff_to_dict(diff) for diff in plan.diffs if diff.changes],
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
        "changes": [_change_to_dict(change) for change in diff.changes],
    }


def _facts_to_dict(facts: TableFacts) -> dict[str, Any]:
    return {
        "exists": facts.exists,
        "size_bytes": facts.size_bytes,
        "delta_version": facts.delta_version,
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
        "postcheck": step.postcheck,
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
    if isinstance(value, Table):
        return _table_to_dict(value)
    if isinstance(value, PrimaryKey):
        return {"primary_key": {"name": value.name, "columns": list(value.columns)}}
    if isinstance(value, Check):
        return {"check": {"name": value.name, "expression": value.expression}}
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
    return rendered


def _table_to_dict(table: Table) -> dict[str, Any]:
    return {
        "table": table.name,
        "comment": table.comment,
        "cluster_by": list(table.cluster_by),
        "properties": dict(table.properties),
        "tags": dict(table.tags),
        "columns": [_field_to_dict(column) for column in table.columns],
        "constraints": [_value(constraint) for constraint in table.constraints],
    }
