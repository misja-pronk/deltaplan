"""Dogfooding: a schema deltaplan didn't make, imported, planned, adopted, changed.

`messy_schema` builds what a real team ends up with — years of ALTERs, renamed
and dropped columns, masks, a Python UDF, legacy partitioning. The invariant:
importing it and planning finds nothing but ownership claims; once those are
applied, nothing at all; and an ordinary change to the imported specs applies
and converges. The first run of this (2026-09-19) found a reserved word
Databricks leaves unquoted in SHOW CREATE TABLE, and three changes Delta
refuses that deltaplan planned anyway.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.loader import dump_spec, load_spec, validate_spec
from deltaplan.model.plan import Plan
from deltaplan.model.table import Table
from deltaplan.model.types import Field, Primitive
from deltaplan.model.view import Relation
from deltaplan.planning import plan_tables
from deltaplan.typeparser import parse_type
from messy_schema import statements

pytestmark = pytest.mark.integration


def planned(specs: list[Relation], introspector: Introspector) -> Plan:
    return plan_tables(specs, introspector, target="dogfood", tool_version="0")


def changes(plan: Plan) -> list[tuple[str, str, str]]:
    return [(d.table, c.kind, c.path) for d in plan.diffs for c in d.changes]


def test_a_messy_schema_imports_adopts_and_changes(
    runner: WarehouseRunner, introspector: Introspector, schema: str, tmp_path: Path
) -> None:
    for label, sql in statements(schema):
        try:
            runner.query(sql)
        except Exception as error:  # noqa: BLE001 - say which part of the fixture broke
            pytest.fail(f"building the messy schema failed at {label!r}: {error}")

    # Import: every object deltaplan manages, written as a spec and read back.
    catalog, name = schema.split(".")
    live = introspector.schema(catalog, name)
    assert live.definition is not None
    relations: list[Relation] = [
        live.definition,
        *(entry.table for entry in live.tables),
        *live.views,
        *live.functions,
        *live.volumes,
    ]
    assert all(not entry.table.name.endswith("py_upper") for entry in live.tables)
    specs: list[Relation] = []
    for relation in relations:
        path = tmp_path / f"{relation.name}.yml"
        path.write_text(dump_spec(relation))
        spec = load_spec(path)
        errors = [d for d in validate_spec(spec, str(path)) if d.severity == "error"]
        assert errors == [], errors
        specs.append(spec)

    # Plan: nothing but ownership claims.
    first = planned(specs, introspector)
    assert {kind for _, kind, _ in changes(first)} <= {"claim_table"}, changes(first)
    result = Executor(runner, introspector, MemoryHistory()).apply(first)
    assert result.ok, result.error
    assert planned(specs, introspector).empty, changes(planned(specs, introspector))

    # An ordinary change: widen a column a CHECK uses, rename one, and add a
    # column whose name needs column mapping.
    def edited(spec: Relation) -> Relation:
        if not isinstance(spec, Table):
            return spec
        if spec.short_name == "orders":
            columns = tuple(
                replace(c, type=parse_type("decimal(20,2)"))
                if c.name == "amount"
                else replace(c, name="selection", renamed_from="select")
                if c.name == "select"
                else c
                for c in spec.columns
            )
            return replace(spec, columns=columns)
        if spec.short_name == "sessions":
            added = Field("Device Type", Primitive("string"), comment="Phone or desktop")
            return replace(spec, columns=(*spec.columns, added))
        return spec

    changed = [edited(spec) for spec in specs]
    second = planned(changed, introspector)
    assert {kind for _, kind, _ in changes(second)} == {
        "change_type",
        "rename_column",
        "add_column",
    }, changes(second)
    result = Executor(runner, introspector, MemoryHistory()).apply(second)
    assert result.ok, result.error
    assert planned(changed, introspector).empty, changes(planned(changed, introspector))
