"""Properties Delta maintains itself are nobody's intent, and never a spec's.

`delta.columnMapping.maxColumnId` grows as columns are added. A spec that declared
it would have every later plan set it back to an old value — corrupting column
mapping rather than managing it. The same goes for the protocol versions, and
`delta.feature.*` merely records what another setting enabled.
"""

from pathlib import Path

from deltaplan.differ import diff, unmanaged
from deltaplan.loader import dump_spec, load_table, validate_table
from deltaplan.model.table import MANAGED_PROPERTY, is_bookkeeping
from helpers import col, table

LIVE = table(
    col("id", "bigint"),
    name="main.sales.orders",
    properties=(
        (MANAGED_PROPERTY, "true"),
        ("delta.columnMapping.mode", "name"),
        ("delta.columnMapping.maxColumnId", "7"),
        ("delta.minReaderVersion", "3"),
        ("delta.feature.deletionVectors", "supported"),
        ("delta.enableChangeDataFeed", "true"),
    ),
)


def test_import_writes_intent_not_bookkeeping() -> None:
    spec = dump_spec(LIVE)
    assert "delta.enableChangeDataFeed" in spec
    assert "delta.columnMapping.mode" in spec, "a real setting, kept"
    for key in ("maxColumnId", "minReaderVersion", "delta.feature.", MANAGED_PROPERTY):
        assert key not in spec


def test_an_imported_spec_plans_nothing(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(dump_spec(LIVE))
    assert diff(load_table(path), LIVE) == ()


def test_a_spec_may_not_declare_what_delta_maintains(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: c.s.t\n"
        "columns: [{name: id, type: bigint}]\n"
        "properties: {delta.columnMapping.maxColumnId: '7'}\n"
    )
    messages = [d.message for d in validate_table(load_table(path), "orders.yml")]
    assert any("maintained by Delta itself" in m for m in messages)


def test_bookkeeping_is_not_reported_as_unmanaged() -> None:
    desired = table(col("id", "bigint"), name="main.sales.orders")
    assert unmanaged(desired, LIVE) == ("property delta.enableChangeDataFeed",)


def test_what_counts_as_bookkeeping() -> None:
    assert is_bookkeeping("delta.columnMapping.maxColumnId")
    assert is_bookkeeping("delta.feature.deletionVectors")
    assert is_bookkeeping(MANAGED_PROPERTY)
    assert not is_bookkeeping("delta.enableChangeDataFeed")
    assert not is_bookkeeping("delta.logRetentionDuration")
