"""Drift, back into the spec — against a real workspace.

The loop the offline tests can only rehearse: apply a spec, change the table by
hand the way someone would at 2am, adopt, and plan again. What makes this worth a
live run is the shape of what comes back from Databricks — a type as the catalog
spells it, a comment as it stores it — which is exactly what a spec has to be
able to hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deltaplan.adopt import adopt
from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.loader import LoadedSpec, load_spec_text
from deltaplan.planning import plan_tables
from deltaplan.sql import quote_qualified

pytestmark = pytest.mark.integration

SPEC = """\
# Orders, from the ingest pipeline
table: ${catalog}.orders
comment: Order facts

columns:
  - name: id            # the surrogate key
    type: bigint
    nullable: false
  - {name: placed, type: date}
"""


def test_a_column_added_by_hand_is_adopted(
    runner: WarehouseRunner, introspector: Introspector, schema: str, tmp_path: Path
) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(SPEC, encoding="utf-8")
    variables = {"catalog": schema}

    def spec() -> LoadedSpec:
        text = path.read_text(encoding="utf-8")
        return LoadedSpec(path, load_spec_text(text, path, variables))

    def planned():  # noqa: ANN202 - a Plan, named by what it is
        return plan_tables(
            [spec().table], introspector, target="integration", tool_version="0"
        )

    result = Executor(runner, introspector, MemoryHistory()).apply(planned())
    assert result.ok, result.error
    assert planned().empty

    name = quote_qualified(f"{schema}.orders")
    runner.query(f"ALTER TABLE {name} ADD COLUMNS (region STRING COMMENT 'ISO code')")
    runner.query(f"ALTER TABLE {name} ALTER COLUMN placed COMMENT 'when it was placed'")
    drifted = planned()
    assert not drifted.empty, "the plan should want the hand-made column gone"

    live = introspector.table(f"{schema}.orders")
    assert live is not None
    adoption = adopt(spec(), live.table, variables=variables)
    assert adoption.changed
    assert adoption.remaining == ()
    adoption.write()

    after = path.read_text(encoding="utf-8")
    assert "${catalog}" in after, "the variable survived"
    assert "# Orders, from the ingest pipeline" in after, "and so did the comments"
    assert "# the surrogate key" in after
    assert "region" in after
    assert planned().empty, [
        (change.kind, change.path) for diff in planned().diffs for change in diff.changes
    ]
