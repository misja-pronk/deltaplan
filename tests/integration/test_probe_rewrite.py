"""Probe: can a rewrite write the table's data once instead of twice?

Not part of the suite's contract — it reports what Databricks does, and fails
so the report lands in the log. Delete it once the answers are in the code.
"""

from __future__ import annotations

import json

import pytest

from deltaplan.introspect import WarehouseRunner
from deltaplan.sql import quote_qualified

pytestmark = pytest.mark.integration

ROWS = 5000


def rows_of(runner: WarehouseRunner, table: str) -> str:
    [row] = runner.query(f"SELECT count(*) AS n FROM {quote_qualified(table)}")
    return str(row["n"])


def written(runner: WarehouseRunner, table: str) -> str:
    """Bytes and files the last write to this table produced."""
    history = runner.query(
        f"SELECT operation, operationMetrics FROM (DESCRIBE HISTORY "
        f"{quote_qualified(table)}) ORDER BY version DESC LIMIT 1"
    )
    if not history:
        return "no history"
    [row] = history
    metrics = row["operationMetrics"] or "{}"
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except ValueError:
            return f"{row['operation']}: {metrics}"
    keep = {k: metrics.get(k) for k in ("numOutputBytes", "numOutputRows", "numFiles")}
    return f"{row['operation']}: {keep}"


def seed(runner: WarehouseRunner, table: str) -> None:
    runner.query(
        f"CREATE OR REPLACE TABLE {quote_qualified(table)} "
        "(id BIGINT, region STRING, amount DECIMAL(18,2))"
    )
    runner.query(
        f"INSERT INTO {quote_qualified(table)} SELECT id, "
        "CASE WHEN id % 3 = 0 THEN 'eu' WHEN id % 3 = 1 THEN 'us' ELSE 'apac' END, "
        f"CAST(id AS DECIMAL(18,2)) FROM range({ROWS})"
    )


def test_probe_one_write_rewrite(runner: WarehouseRunner, schema: str) -> None:
    report: list[str] = []

    def note(line: str) -> None:
        report.append(line)

    # 1. The table reading and replacing itself in one statement.
    a = f"{schema}.self_ref"
    seed(runner, a)
    try:
        runner.query(
            f"CREATE OR REPLACE TABLE {quote_qualified(a)} PARTITIONED BY (region) AS "
            f"SELECT id, region, amount FROM {quote_qualified(a)}"
        )
        note(f"1. self-referencing RTAS: ALLOWED, rows={rows_of(runner, a)}")
        note(f"   write: {written(runner, a)}")
    except Exception as error:  # noqa: BLE001 - the answer is the message
        note(f"1. self-referencing RTAS: REFUSED — {error}")

    # 2. A shallow clone as the source: metadata only, so the data is written once.
    b, clone = f"{schema}.via_clone", f"{schema}.via_clone__dp_source"
    seed(runner, b)
    try:
        runner.query(
            f"CREATE OR REPLACE TABLE {quote_qualified(clone)} "
            f"SHALLOW CLONE {quote_qualified(b)}"
        )
        note(f"2. SHALLOW CLONE of the source: ok, write={written(runner, clone)}")
        runner.query(
            f"CREATE OR REPLACE TABLE {quote_qualified(b)} PARTITIONED BY (region) AS "
            f"SELECT id, region, amount FROM {quote_qualified(clone)}"
        )
        note(f"   REPLACE from the clone: ALLOWED, rows={rows_of(runner, b)}")
        note(f"   write: {written(runner, b)}")
        note(f"   clone still readable after the replace: {rows_of(runner, clone)}")
        runner.query(f"DROP TABLE {quote_qualified(clone)}")
        note(f"   after dropping the clone, the table reads: {rows_of(runner, b)}")
        [desc] = runner.query(f"SELECT * FROM (DESCRIBE DETAIL {quote_qualified(b)})")
        note(f"   partitionColumns={desc.get('partitionColumns')}")
    except Exception as error:  # noqa: BLE001
        note(f"2. clone-sourced replace: FAILED — {error}")

    # 3. What deltaplan does today: staging table, then replace from it.
    c, staging = f"{schema}.via_staging", f"{schema}.via_staging__dp_staging"
    seed(runner, c)
    try:
        runner.query(
            f"CREATE OR REPLACE TABLE {quote_qualified(staging)} AS "
            f"SELECT id, region, amount FROM {quote_qualified(c)}"
        )
        note(f"3. staging copy: write={written(runner, staging)}")
        runner.query(
            f"CREATE OR REPLACE TABLE {quote_qualified(c)} PARTITIONED BY (region) AS "
            f"SELECT id, region, amount FROM {quote_qualified(staging)}"
        )
        note(f"   replace from staging: write={written(runner, c)}")
    except Exception as error:  # noqa: BLE001
        note(f"3. staging route: FAILED — {error}")

    # 4. Does a clone survive its source being replaced *and* vacuumed?
    d, dclone = f"{schema}.vacuumed", f"{schema}.vacuumed__dp_source"
    seed(runner, d)
    try:
        runner.query(
            f"CREATE OR REPLACE TABLE {quote_qualified(dclone)} "
            f"SHALLOW CLONE {quote_qualified(d)}"
        )
        runner.query(
            f"CREATE OR REPLACE TABLE {quote_qualified(d)} AS "
            f"SELECT id, region, CAST(amount AS DECIMAL(20,2)) AS amount "
            f"FROM {quote_qualified(dclone)}"
        )
        note(f"4. replace with a CAST from the clone: rows={rows_of(runner, d)}")
        [col] = runner.query(
            f"SELECT full_data_type FROM {schema.split('.')[0]}."
            "information_schema.columns WHERE table_schema = "
            f"'{schema.split('.')[1]}' AND table_name = 'vacuumed' "
            "AND column_name = 'amount'"
        )
        note(f"   amount is now {col['full_data_type']}")
    except Exception as error:  # noqa: BLE001
        note(f"4. cast from the clone: FAILED — {error}")

    pytest.fail("PROBE REPORT\n" + "\n".join(report))
