"""Seeds, against a real workspace: does the statement deltaplan writes work?

The offline suite proves a seed converges against deltaplan's own reading of
Databricks. Only this proves the `INSERT OVERWRITE … (columns) VALUES …` it
builds is accepted, that the rows arrive as their declared types, and that
loading again replaces rather than appends.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-dml-insert-into
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from deltaplan.connect import Connection
from deltaplan.differ import diff
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.model.plan import Plan, TableDiff, TableFacts, fingerprint
from deltaplan.model.table import SEED_PROPERTY, Seed, Table
from deltaplan.planner import build_plan
from deltaplan.sql import quote_qualified
from helpers import col, table

pytestmark = pytest.mark.integration

ROWS = (
    ("NL", "Netherlands", "17800000", "true", "2026-01-01"),
    ("NO", "Norway", "5500000", "false", "2026-01-02"),
    # An apostrophe, because a value must never reach SQL unquoted.
    ("CI", "Côte d'Ivoire", "28000000", None, None),
)


def countries(name: str, rows: tuple[tuple[str | None, ...], ...] = ROWS) -> Table:
    return replace(
        table(
            col("code", "string", nullable=False),
            col("name", "string"),
            col("population", "bigint"),
            col("eu", "boolean"),
            col("joined", "date"),
            name=name,
        ),
        seed=Seed(("code", "name", "population", "eu", "joined"), rows),
    )


def plan_for(desired: Table, introspector: Introspector) -> Plan:
    live = introspector.table(desired.name)
    live_table = live.table if live else None
    return build_plan(
        [
            TableDiff(
                desired.name,
                diff(desired, live_table),
                TableFacts(
                    desired.name,
                    exists=live_table is not None,
                    properties=live_table.properties if live_table else (),
                    size_bytes=live.size_bytes if live else None,
                ),
                desired=desired,
                live=live_table,
            )
        ],
        target="integration",
        tool_version="0.1.0",
        spec_hash="spec",
        state_fingerprint=fingerprint([live_table]),
    )


def test_a_seed_is_loaded_typed_and_replaced(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    from deltaplan import api

    name = f"{schema}.countries"
    connection = Connection(runner=runner)
    desired = countries(name)

    assert api.apply(
        plan_for(desired, introspector), connection, history=MemoryHistory()
    ).ok

    rows = runner.query(
        f"SELECT code, name, population, eu, joined FROM {quote_qualified(name)} "
        "ORDER BY code"
    )
    assert [row["code"] for row in rows] == ["CI", "NL", "NO"]
    assert rows[0]["name"] == "Côte d'Ivoire", "a quote goes in as a quote"
    assert rows[0]["eu"] is None and rows[0]["joined"] is None
    assert rows[1]["population"] == "17800000"
    assert rows[1]["eu"] == "true", "a boolean is a boolean, not the text"
    assert rows[1]["joined"] == "2026-01-01", "and a date is a date"

    live = introspector.table(name)
    assert live is not None and desired.seed is not None
    assert live.table.properties_map()[SEED_PROPERTY] == desired.seed.digest
    assert plan_for(desired, introspector).empty, "a loaded seed plans nothing"


def test_loading_again_replaces_rather_than_appends(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    from deltaplan import api

    name = f"{schema}.countries"
    connection = Connection(runner=runner)
    assert api.apply(
        plan_for(countries(name), introspector), connection, history=MemoryHistory()
    ).ok
    fewer = countries(name, ROWS[:1])
    assert api.apply(
        plan_for(fewer, introspector),
        connection,
        history=MemoryHistory(),
        allow_destructive=True,
    ).ok
    [count] = runner.query(f"SELECT count(*) AS n FROM {quote_qualified(name)}")
    assert count["n"] == "1", "the file is the whole content, not an addition to it"
