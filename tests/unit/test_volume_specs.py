"""Managed volumes: comment, tags and grants.

Verified live (2026-09-18): CREATE VOLUME … COMMENT, COMMENT ON VOLUME and
ALTER VOLUME … SET TAGS; the volume privileges in VOLUME_PRIVILEGES (SELECT and
BROWSE are refused on a volume); information_schema.volumes / volume_tags /
volume_privileges reading it back; and a table and a volume sharing a name.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-volume
"""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_spec, validate_spec
from deltaplan.model.plan import Plan
from deltaplan.model.schema import Schema
from deltaplan.model.table import MANAGED_PROPERTY, Grant
from deltaplan.model.view import Relation
from deltaplan.model.volume import Volume
from deltaplan.planning import PlanningError, plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeSqlError, FakeWarehouse
from helpers import col, run, table

LANDING = Volume(
    "main.sales.landing",
    comment="Raw files, it's here",
    tags=(("domain", "sales"),),
    grants=(Grant("etl", ("READ VOLUME", "WRITE VOLUME")),),
)


def planned(specs: list[Relation], fake: FakeWarehouse, *, strict: bool = False) -> Plan:
    return plan_tables(
        specs,
        Introspector(fake),
        target="t",
        tool_version="0",
        mode_for=lambda _schema: "strict" if strict else "additive",
    )


def converge(specs: list[Relation], fake: FakeWarehouse) -> Plan:
    plan = planned(specs, fake)
    run(plan, fake)
    assert planned(specs, fake).empty, "re-planning after apply must be empty"
    return plan


def test_a_volume_spec(tmp_path: Path) -> None:
    path = tmp_path / "landing.yml"
    path.write_text(
        "volume: ${catalog}.sales.landing\n"
        "comment: Raw files, it's here\n"
        "tags: {domain: sales}\n"
        "grants: [{principal: etl, privileges: [read_volume, WRITE VOLUME]}]\n"
    )
    assert load_spec(path, {"catalog": "main"}) == LANDING


def test_a_volume_takes_only_volume_privileges(tmp_path: Path) -> None:
    path = tmp_path / "v.yml"
    path.write_text("volume: main.s.v\ngrants: [{principal: a, privileges: [SELECT]}]\n")
    with pytest.raises(SpecError, match="SELECT"):
        load_spec(path)


def test_a_volume_name_has_three_parts() -> None:
    [problem] = validate_spec(Volume("main.landing"), "v.yml")
    assert "must be catalog.schema.volume" in problem.message


def test_a_new_volume_in_a_new_schema() -> None:
    fake = FakeWarehouse.of()
    plan = converge([LANDING], fake)
    assert [s.title for s in plan.steps] == [
        "CREATE SCHEMA sales",
        "CREATE VOLUME landing",
        "SET TAGS",
        "GRANT to etl",
    ]
    assert plan.steps[1].sql == (
        "CREATE VOLUME IF NOT EXISTS `main`.`sales`.`landing` "
        "COMMENT 'Raw files, it\\'s here'"
    )
    assert plan.steps[3].sql == (
        "GRANT READ VOLUME, WRITE VOLUME ON VOLUME `main`.`sales`.`landing` TO `etl`"
    )
    assert "+ volume" in plan_text(plan)


def test_an_existing_volume_is_brought_in_line() -> None:
    fake = FakeWarehouse.of(Schema("main.sales"), Volume(LANDING.name, comment="old"))
    plan = converge([LANDING], fake)
    assert [s.title for s in plan.steps] == [
        "COMMENT ON VOLUME",
        "SET TAGS",
        "GRANT to etl",
    ]


def test_what_a_volume_spec_leaves_out_is_left_alone() -> None:
    live = Volume(LANDING.name, "kept", grants=(Grant("auditors", ("READ VOLUME",)),))
    plan = planned([Volume(LANDING.name)], FakeWarehouse.of(live))
    assert plan.empty
    assert plan.diffs[0].unmanaged == ("grants to auditors",)


def test_a_volume_is_never_dropped() -> None:
    orders = table(
        col("id", "bigint"),
        name="main.sales.orders",
        properties=((MANAGED_PROPERTY, "true"),),
    )
    fake = FakeWarehouse.of(orders, LANDING)
    assert planned([orders], fake, strict=True).empty


def test_an_external_volume_is_listed_and_left_alone() -> None:
    fake = FakeWarehouse.of(Schema("main.sales"))
    fake.external_volumes.add("main.sales.raw")
    live = Introspector(fake).schema("main", "sales")
    assert live.volumes == ()
    assert ("main.sales.raw", "external volume") in live.skipped


def test_a_volume_named_like_a_table_is_refused() -> None:
    orders = table(col("id", "bigint"), name="main.sales.orders")
    with pytest.raises(PlanningError, match="names both a volume and a table"):
        planned([orders, Volume("main.sales.orders")], FakeWarehouse.of())


def test_the_executor_applies_it() -> None:
    fake = FakeWarehouse.of(Schema("main.sales"), Volume(LANDING.name))
    plan = planned([LANDING], fake)
    result = Executor(
        runner=fake,
        introspector=Introspector(fake),
        history=MemoryHistory(),
        new_run_id=lambda: "run1",
    ).apply(plan)
    assert result.ok, result.error
    assert fake.volumes[LANDING.name] == LANDING


def test_a_volume_plan_survives_the_plan_file() -> None:
    plan = planned([LANDING], FakeWarehouse.of(Schema("main.sales")))
    assert loads(dumps(plan)) == plan


def test_import_writes_volumes_as_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWarehouse.of(Schema("main.sales"), LANDING)
    monkeypatch.setattr(cli, "_warehouse", lambda *_args, **_kwargs: fake)
    result = CliRunner().invoke(
        cli.app, ["import", "main.sales", "-o", str(tmp_path), "-f", "sql"]
    )
    assert result.exit_code == 0, result.output
    assert load_spec(tmp_path / "landing.yml") == LANDING
    assert "SQL can't say volumes" in " ".join(result.output.split())


def test_a_volume_spec_validates_against_the_editor_schema() -> None:
    from jsonschema import Draft7Validator

    from deltaplan.spec_schema import spec_schema

    document = yaml.safe_load(dump_spec(LANDING))
    assert list(Draft7Validator(spec_schema()).iter_errors(document)) == []


def test_the_fake_refuses_a_volume_in_a_schema_that_is_not_there() -> None:
    with pytest.raises(FakeSqlError, match="no such schema"):
        FakeWarehouse().query("CREATE VOLUME IF NOT EXISTS `main`.`nowhere`.`v`")
