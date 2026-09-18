"""Automatic liquid clustering: `cluster_by: auto`.

Under AUTO, Databricks picks the clustering keys and may change them, so the
keys a live table shows are its choice — a spec asking for AUTO compares only
that it is on. Naming keys turns AUTO off; so does CLUSTER BY NONE. Both
verified live, with DESCRIBE DETAIL's `clusterByAuto` field, 2026-09-18.
https://docs.databricks.com/aws/en/delta/clustering#automatic-liquid-clustering
"""

from dataclasses import replace
from pathlib import Path

import pytest

from deltaplan.differ import diff
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_table
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.render.json import dumps, loads
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeWarehouse
from helpers import col, plan_against, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
COLUMNS = (col("id", "bigint"), col("placed", "date"))
KEYED = table(*COLUMNS, name=NAME, cluster_by=("id",), properties=MANAGED)
AUTO = replace(table(*COLUMNS, name=NAME, properties=MANAGED), cluster_auto=True)


def converge(desired: Table, live: Table | None) -> list[str]:
    fake, plan = plan_against(desired, live)
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()
    return [step.sql or "" for step in plan.steps]


def test_cluster_by_auto_in_yaml(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        f"table: {NAME}\ncluster_by: auto\ncolumns: [{{name: id, type: bigint}}]\n"
    )
    loaded = load_table(path)
    assert loaded.cluster_auto and loaded.cluster_by == ()


def test_any_other_word_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        f"table: {NAME}\ncluster_by: id\ncolumns: [{{name: id, type: bigint}}]\n"
    )
    with pytest.raises(SpecError, match="a list of columns, or `auto`"):
        load_table(path)


def test_import_writes_auto_not_the_keys_databricks_chose(tmp_path: Path) -> None:
    live = replace(KEYED, cluster_auto=True)  # AUTO, with keys it picked
    written = dump_spec(live)
    assert "cluster_by: auto" in written
    path = tmp_path / "orders.yml"
    path.write_text(written)
    assert diff(load_table(path), live) == ()


def test_a_new_table_is_created_with_auto() -> None:
    [create] = converge(AUTO, None)
    assert "\nCLUSTER BY AUTO\n" in create


def test_keys_under_auto_are_not_compared() -> None:
    chosen = replace(KEYED, cluster_auto=True)
    _, plan = plan_against(AUTO, chosen)
    assert plan.steps == ()


def test_switching_to_auto() -> None:
    assert converge(AUTO, KEYED) == [
        "ALTER TABLE `main`.`sales`.`orders` CLUSTER BY AUTO"
    ]


def test_naming_keys_switches_auto_off_even_the_same_keys() -> None:
    chosen = replace(KEYED, cluster_auto=True)
    assert converge(KEYED, chosen) == [
        "ALTER TABLE `main`.`sales`.`orders` CLUSTER BY (`id`)"
    ]


def test_no_clustering_turns_auto_off() -> None:
    plain = replace(AUTO, cluster_auto=False)
    assert converge(plain, AUTO) == [
        "ALTER TABLE `main`.`sales`.`orders` CLUSTER BY NONE"
    ]


def test_introspection_reads_the_flag() -> None:
    live = Introspector(FakeWarehouse.of(AUTO)).table(NAME)
    assert live is not None and live.table.cluster_auto


def test_auto_renders_and_survives_the_plan_file() -> None:
    _, plan = plan_against(AUTO, KEYED)
    assert "cluster_by auto" in plan_text(plan)
    back = loads(dumps(plan))
    assert back == plan
    assert back.diffs[0].changes[0].after == "auto"


def test_a_rewrite_keeps_auto() -> None:
    converted = replace(AUTO, columns=(col("id", "string"), COLUMNS[1]))
    _, plan = plan_against(converted, replace(KEYED, cluster_auto=True))
    staged = [s.sql or "" for s in plan.steps if s.title == "REPLACE TABLE"]
    assert staged and "\nCLUSTER BY AUTO" in staged[0]
