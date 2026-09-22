"""Owners: `owner: data-eng` on a table, view, function, schema or volume.

Verified live (2026-09-19): `ALTER … OWNER TO` for all five, and
information_schema's table_owner / schema_owner / volume_owner / routine_owner
read them back; a user's email is stored lower-cased; a replaced table keeps its
owner, while a replaced view or function belongs to whoever replaced it.
https://docs.databricks.com/aws/en/data-governance/unity-catalog/manage-privileges/ownership
"""

from dataclasses import replace
from pathlib import Path

import pytest

from deltaplan.connect import Connection
from deltaplan.differ import diff, is_applied
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_spec
from deltaplan.model.change import Change
from deltaplan.model.function import Function, Parameter
from deltaplan.model.plan import Plan
from deltaplan.model.schema import Schema
from deltaplan.model.table import MANAGED_PROPERTY, Grant
from deltaplan.model.types import Primitive
from deltaplan.model.view import Relation, View
from deltaplan.model.volume import Volume
from deltaplan.planning import plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.rich import plan_text
from fake_warehouse import RUNNER, FakeWarehouse
from helpers import col, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)


def planned(specs: list[Relation], fake: FakeWarehouse) -> Plan:
    return plan_tables(specs, Introspector(fake), target="t", tool_version="0")


def converge(specs: list[Relation], fake: FakeWarehouse) -> Plan:
    plan = planned(specs, fake)
    run(plan, fake)
    assert planned(specs, fake).empty, [
        (c.kind, c.path) for d in planned(specs, fake).diffs for c in d.changes
    ]
    return plan


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "table: main.s.t\nowner: data-eng\ncolumns:\n  - {name: a, type: int}\n",
        "view: main.s.v\nowner: data-eng\nquery: SELECT 1\n",
        "schema: main.s\nowner: data-eng\n",
        "volume: main.s.landing\nowner: data-eng\n",
        "function: main.s.f\nowner: data-eng\nreturns: int\nbody: '1'\n",
    ],
)
def test_every_kind_takes_an_owner(tmp_path: Path, text: str) -> None:
    path = tmp_path / "spec.yml"
    path.write_text(text)
    loaded = load_spec(path)
    assert loaded.owner == "data-eng"
    path.write_text(dump_spec(loaded))
    assert load_spec(path).owner == "data-eng", "and it round-trips"


def test_an_empty_owner_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "spec.yml"
    path.write_text("table: main.s.t\nowner: ' '\ncolumns:\n  - {name: a, type: int}\n")
    with pytest.raises(SpecError, match="owner can't be empty"):
        load_spec(path)


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

LIVE = replace(
    table(col("id", "bigint"), name=NAME, properties=MANAGED), owner="alice@corp.com"
)


def test_a_new_owner_is_the_last_step() -> None:
    desired = replace(
        table(
            col("id", "bigint"),
            col("note", "string"),
            name=NAME,
            grants=(Grant("analysts", ("SELECT",)),),
        ),
        owner="data-eng",
    )
    fake = FakeWarehouse.of(LIVE)
    plan = converge([desired], fake)
    last = plan.steps[-1]
    assert last.title == "SET OWNER"
    assert last.sql == "ALTER TABLE `main`.`sales`.`orders` OWNER TO `data-eng`"
    assert last.undo_hint == (
        "ALTER TABLE `main`.`sales`.`orders` OWNER TO `alice@corp.com`"
    )
    assert "only the owner, its members or MANAGE" in last.warnings[0]
    assert "~ owner → data-eng" in plan_text(plan)
    assert fake.tables[NAME].owner == "data-eng"


def test_the_same_owner_in_another_case_is_no_change() -> None:
    """Unity Catalog stores a user's email lower-cased."""
    desired = replace(LIVE, owner="Alice@Corp.com")
    assert diff(desired, LIVE) == ()


def test_a_spec_without_an_owner_leaves_it_alone() -> None:
    assert diff(replace(LIVE, owner=None), LIVE) == ()


def test_a_table_created_with_an_owner_gets_it_after_its_grants() -> None:
    desired = replace(
        table(col("id", "bigint"), name=NAME, grants=(Grant("analysts", ("SELECT",)),)),
        owner="data-eng",
    )
    fake = FakeWarehouse()
    plan = converge([desired], fake)
    assert [s.title for s in plan.steps][-2:] == ["GRANT to analysts", "SET OWNER"]
    assert fake.tables[NAME].owner == "data-eng"


def test_a_replaced_view_gets_its_owner_back() -> None:
    """A replace makes whoever ran it the owner; the fake does as Databricks
    does, so without the put-back this wouldn't converge."""
    live = View("main.sales.v", "SELECT 1 AS x", properties=MANAGED, owner="data-eng")
    fake = FakeWarehouse.of(live)
    desired = View("main.sales.v", "SELECT 2 AS x", owner="data-eng")
    plan = converge([desired], fake)
    assert [s.title for s in plan.steps] == ["REPLACE VIEW", "SET OWNER"]
    assert fake.views["main.sales.v"].owner == "data-eng"


def test_a_replaced_view_keeps_even_an_owner_the_spec_leaves_out() -> None:
    live = View("main.sales.v", "SELECT 1 AS x", properties=MANAGED, owner="data-eng")
    fake = FakeWarehouse.of(live)
    converge([View("main.sales.v", "SELECT 2 AS x")], fake)
    assert fake.views["main.sales.v"].owner == "data-eng"


def test_a_replaced_function_gets_its_owner_back() -> None:
    def price(body: str, owner: str | None) -> Function:
        return Function(
            "main.sales.price",
            (Parameter("x", Primitive("double")),),
            Primitive("double"),
            body,
            owner=owner,
        )

    fake = FakeWarehouse.of(price("x * 2", "data-eng"))
    converge([price("x * 3", "data-eng")], fake)
    assert fake.functions["main.sales.price"].owner == "data-eng"


@pytest.mark.parametrize(
    "spec",
    [
        Schema("main.sales", owner="data-eng"),
        Volume("main.sales.landing", owner="data-eng"),
    ],
)
def test_schemas_and_volumes_are_created_with_their_owner(spec: Relation) -> None:
    fake = FakeWarehouse()
    plan = converge([spec], fake)
    assert plan.steps[-1].title == "SET OWNER"


def test_a_change_of_owner_survives_the_plan_file() -> None:
    plan = planned([replace(LIVE, owner="data-eng")], FakeWarehouse.of(LIVE))
    assert loads(dumps(plan)) == plan


def test_a_resumed_owner_change_counts_as_done() -> None:
    change = Change(NAME, "set_owner", before="alice@corp.com", after="data-eng")
    assert not is_applied(change, LIVE)
    assert is_applied(change, replace(LIVE, owner="DATA-ENG"))


def test_the_fake_makes_the_creator_the_owner() -> None:
    fake = FakeWarehouse()
    converge([table(col("id", "bigint"), name=NAME)], fake)
    assert fake.tables[NAME].owner == RUNNER


def test_import_leaves_owners_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Often a person's email, and not the same in every workspace."""
    from typer.testing import CliRunner

    from deltaplan import cli

    fake = FakeWarehouse.of(LIVE)
    monkeypatch.setattr(cli, "_connect", lambda *_a, **_k: Connection(runner=fake))
    result = CliRunner().invoke(
        cli.app, ["import", "main.sales", "-o", str(tmp_path / "out")]
    )
    assert result.exit_code == 0, result.output
    assert "owner" not in (tmp_path / "out" / "orders.yml").read_text()
