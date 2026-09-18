"""Views, end to end: spec, plan, apply against the fake, re-plan.

A view's shape is its query, so that is what is diffed; a changed query replaces
the view. Its governance — comment, tags, grants, the managed marker — works as a
table's does.

https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-view
"""

from pathlib import Path

import pytest

from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_spec, load_table
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, Grant
from deltaplan.model.view import Relation, View
from deltaplan.planning import PlanningError, order_views, plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.markdown import render_markdown
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeWarehouse
from helpers import col, run, table

MANAGED = ((MANAGED_PROPERTY, "true"),)
ORDERS = table(
    col("id", "bigint"),
    col("amount", "decimal(18,2)"),
    name="main.sales.orders",
    properties=MANAGED,
)
BIG = View(
    "main.sales.big_orders",
    "SELECT id, amount FROM main.sales.orders WHERE amount > 1000",
    comment="Orders over 1000",
    tags=(("domain", "sales"),),
    grants=(Grant("analysts", ("SELECT",)),),
)


def managed(view: View) -> View:
    return View(view.name, view.query, view.comment, MANAGED, view.tags, view.grants)


def planned(specs: list[Relation], fake: FakeWarehouse, *, strict: bool = False) -> Plan:
    return plan_tables(
        specs,
        Introspector(fake),
        target="test",
        tool_version="0.1.0",
        mode_for=lambda _schema: "strict" if strict else "additive",
    )


def converge(specs: list[Relation], fake: FakeWarehouse) -> Plan:
    plan = planned(specs, fake)
    run(plan, fake)
    assert planned(specs, fake).empty, "re-planning after apply must be empty"
    return plan


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------


def test_a_view_spec(tmp_path: Path) -> None:
    path = tmp_path / "big_orders.yml"
    path.write_text(
        "view: ${catalog}.sales.big_orders\n"
        "comment: Orders over 1000\n"
        "tags: {domain: sales}\n"
        "grants: [{principal: analysts, privileges: [SELECT]}]\n"
        "query: |\n"
        "  SELECT id, amount\n"
        "  FROM ${catalog}.sales.orders\n"
        "  WHERE amount > 1000\n"
    )
    view = load_spec(path, {"catalog": "main"})
    assert isinstance(view, View)
    assert view.name == "main.sales.big_orders"
    assert "FROM main.sales.orders" in view.query, "variables reach the query too"
    with pytest.raises(SpecError, match="describes a view, not a table"):
        load_table(path, {"catalog": "main"})


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("view: c.s.v\n", "needs a 'query' key"),
        ("view: c.s.v\nquery: '  '\n", "query cannot be empty"),
        ("view: c.s.v\nquery: SELECT 1\ncolumns: []\n", "unknown key 'columns'"),
    ],
)
def test_bad_view_specs(tmp_path: Path, spec: str, message: str) -> None:
    path = tmp_path / "v.yml"
    path.write_text(spec)
    with pytest.raises(SpecError, match=message):
        load_spec(path)


def test_import_round_trips_a_view(tmp_path: Path) -> None:
    written = dump_spec(managed(BIG))
    assert written.startswith("view: main.sales.big_orders")
    assert "query: |" in written
    assert MANAGED_PROPERTY not in written
    path = tmp_path / "big_orders.yml"
    path.write_text(written)
    assert load_spec(path) == BIG


# ---------------------------------------------------------------------------
# planning and applying
# ---------------------------------------------------------------------------


def test_a_new_view_is_created_then_governed() -> None:
    fake = FakeWarehouse.of(ORDERS)
    plan = converge([ORDERS, BIG], fake)
    assert [s.title for s in plan.steps] == [
        "CREATE VIEW big_orders",
        "SET TAGS",
        "GRANT to analysts",
    ]
    assert plan.steps[0].sql == (
        "CREATE VIEW IF NOT EXISTS `main`.`sales`.`big_orders`\n"
        "COMMENT 'Orders over 1000'\n"
        "TBLPROPERTIES (\n  'deltaplan.managed' = 'true'\n)\n"
        "AS\n"
        "SELECT id, amount FROM main.sales.orders WHERE amount > 1000"
    )
    assert plan.steps[1].sql == (
        "ALTER VIEW `main`.`sales`.`big_orders` SET TAGS ('domain' = 'sales')"
    )
    assert fake.views["main.sales.big_orders"].managed


def test_a_changed_query_replaces_the_view_and_keeps_its_governance() -> None:
    fake = FakeWarehouse.of(ORDERS, managed(BIG))
    wider = View(
        BIG.name,
        "SELECT id, amount FROM main.sales.orders WHERE amount > 500",
        BIG.comment,
        (),
        BIG.tags,
        BIG.grants,
    )
    plan = converge([ORDERS, wider], fake)
    titles = [s.title for s in plan.steps]
    assert titles[0] == "REPLACE VIEW"
    # Put back as it was, whatever REPLACE does to it.
    assert "SET TAGS" in titles and "GRANT to analysts" in titles
    assert plan.steps[0].undo_hint is not None
    assert "amount > 1000" in plan.steps[0].undo_hint, "undo restores the old query"
    rendered = plan_text(plan)
    assert "~ query" in rendered
    assert "↻ restore" in rendered


def test_whitespace_is_not_a_change() -> None:
    fake = FakeWarehouse.of(ORDERS, managed(BIG))
    reflowed = View(
        BIG.name,
        "SELECT id, amount\n  FROM main.sales.orders\n  WHERE amount > 1000;\n",
        BIG.comment,
        (),
        BIG.tags,
        BIG.grants,
    )
    assert planned([ORDERS, reflowed], fake).empty


def test_governance_changes_without_a_replace() -> None:
    fake = FakeWarehouse.of(ORDERS, managed(BIG))
    regoverned = View(
        BIG.name,
        BIG.query,
        BIG.comment,
        (),
        (("domain", "finance"),),
        (Grant("analysts", ("SELECT",)), Grant("bi", ("SELECT",))),
    )
    plan = converge([ORDERS, regoverned], fake)
    assert [s.title for s in plan.steps] == ["SET TAGS", "GRANT to bi"]


def test_someone_elses_view_is_claimed() -> None:
    fake = FakeWarehouse.of(ORDERS, BIG)  # no marker
    plan = converge([ORDERS, BIG], fake)
    assert [s.sql for s in plan.steps] == [
        "ALTER VIEW `main`.`sales`.`big_orders` SET TBLPROPERTIES "
        "('deltaplan.managed' = 'true')"
    ]


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_views_follow_what_they_read() -> None:
    top = View("main.sales.top", "SELECT * FROM main.sales.big_orders LIMIT 10")
    ordered = order_views([top, BIG])
    assert [v.name for v in ordered] == [BIG.name, top.name]


def test_backticks_and_case_still_count_as_reading() -> None:
    top = View("main.sales.top", "select * from `MAIN`.`sales`.`Big_Orders`")
    assert [v.name for v in order_views([top, BIG])] == [BIG.name, top.name]


def test_a_prefix_is_not_a_reference() -> None:
    # `big_orders_archive` is a different name, not a read of `big_orders`.
    archive = View("main.sales.big_orders_archive", "SELECT 1")
    reader = View("main.sales.reader", "SELECT * FROM main.sales.big_orders_archive")
    assert [v.name for v in order_views([reader, BIG, archive])] == [
        BIG.name,
        archive.name,
        reader.name,
    ]


def test_a_cycle_is_an_error() -> None:
    a = View("main.sales.a", "SELECT * FROM main.sales.b")
    b = View("main.sales.b", "SELECT * FROM main.sales.a")
    with pytest.raises(PlanningError, match="cycle"):
        order_views([a, b])


def test_tables_are_planned_before_views() -> None:
    fake = FakeWarehouse()
    new_orders = table(
        col("id", "bigint"), col("amount", "decimal(18,2)"), name="main.sales.orders"
    )
    plan = converge([BIG, new_orders], fake)  # spec order: view first
    titles = [s.title for s in plan.steps]
    assert titles[:3] == [
        "CREATE SCHEMA sales",
        "CREATE TABLE orders",
        "CREATE VIEW big_orders",
    ]


def test_a_table_is_not_turned_into_a_view() -> None:
    fake = FakeWarehouse.of(table(col("id", "bigint"), name=BIG.name))
    with pytest.raises(PlanningError, match="is a table in the catalog but a view"):
        planned([BIG], fake)


# ---------------------------------------------------------------------------
# strict schemas, the plan file, the comment
# ---------------------------------------------------------------------------


def test_strict_drops_an_orphaned_view_and_keeps_its_definition_as_undo() -> None:
    theirs = View("main.sales.theirs", "SELECT 1")
    fake = FakeWarehouse.of(ORDERS, managed(BIG), theirs)
    plan = planned([ORDERS], fake, strict=True)
    assert [(s.title, s.risk) for s in plan.steps] == [("DROP VIEW", "destructive")]
    assert (plan.steps[0].undo_hint or "").startswith(
        "CREATE OR REPLACE VIEW `main`.`sales`.`big_orders`"
    )
    assert plan.unmanaged_tables == ("main.sales.theirs",)
    run(plan, fake)
    assert BIG.name not in fake.views
    assert "main.sales.theirs" in fake.views


def test_views_survive_the_plan_file() -> None:
    fake = FakeWarehouse.of(ORDERS, managed(BIG))
    changed = View(
        BIG.name, "SELECT id FROM main.sales.orders", BIG.comment, (), BIG.tags, ()
    )
    plan = planned([ORDERS, changed], fake)
    assert loads(dumps(plan)) == plan


def test_a_view_in_a_pull_request_comment() -> None:
    fake = FakeWarehouse.of(ORDERS)
    rendered = render_markdown(planned([ORDERS, BIG], fake))
    assert "+ view" in rendered
    assert "CREATE VIEW IF NOT EXISTS" in rendered


def test_a_replace_carries_properties_nobody_declared() -> None:
    live = View(
        BIG.name,
        BIG.query,
        BIG.comment,
        (*MANAGED, ("owner.team", "finance")),
        BIG.tags,
        BIG.grants,
    )
    fake = FakeWarehouse.of(ORDERS, live)
    changed = View(
        BIG.name,
        "SELECT id FROM main.sales.orders",
        BIG.comment,
        (),
        BIG.tags,
        BIG.grants,
    )
    plan = converge([ORDERS, changed], fake)
    assert "'owner.team' = 'finance'" in (plan.steps[0].sql or "")
    assert fake.views[BIG.name].properties_map()["owner.team"] == "finance"
