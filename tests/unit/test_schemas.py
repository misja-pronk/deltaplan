"""A table or view in a schema that isn't there yet: the schema comes first.

The case this is for is a fresh target — a new dev catalog with nothing in it.
deltaplan creates the schemas its specs need, once each, just before the first
table or view that needs them. It never creates a catalog (that comes with
storage and ownership decisions) and never drops a schema.
"""

from deltaplan.introspect import Introspector
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY
from deltaplan.model.view import Relation, View
from deltaplan.planning import plan_tables
from fake_warehouse import FakeWarehouse
from helpers import col, fake_runner, run, table

ORDERS = table(col("id", "bigint"), name="dev.sales.orders")
CUSTOMERS = table(col("id", "bigint"), name="dev.sales.customers")
EVENTS = table(col("id", "bigint"), name="dev.raw.events")


def planned(specs: list[Relation], fake: FakeWarehouse, *, strict: bool = False) -> Plan:
    return plan_tables(
        specs,
        Introspector(fake),
        target="dev",
        tool_version="0",
        mode_for=lambda _s: "strict" if strict else "additive",
    )


def test_a_fresh_catalog_gets_its_schemas() -> None:
    fake = FakeWarehouse()
    plan = planned([ORDERS, CUSTOMERS, EVENTS], fake)
    assert [s.title for s in plan.steps] == [
        "CREATE SCHEMA sales",
        "CREATE TABLE orders",
        "CREATE TABLE customers",  # the same schema is created once
        "CREATE SCHEMA raw",
        "CREATE TABLE events",
    ]
    assert plan.steps[0].sql == "CREATE SCHEMA IF NOT EXISTS `dev`.`sales`"

    run(plan, fake)
    assert planned([ORDERS, CUSTOMERS, EVENTS], fake).empty


def test_an_existing_schema_is_left_as_it_is() -> None:
    fake = FakeWarehouse()
    fake.schemas.add("dev.sales")
    plan = planned([ORDERS], fake)
    assert [s.title for s in plan.steps] == ["CREATE TABLE orders"]


def test_views_need_their_schema_too() -> None:
    view = View("dev.reporting.big", "SELECT * FROM dev.sales.orders")
    fake = FakeWarehouse()
    fake.schemas.add("dev.sales")
    plan = planned([ORDERS, view], fake)
    assert [s.title for s in plan.steps] == [
        "CREATE TABLE orders",
        "CREATE SCHEMA reporting",
        "CREATE VIEW big",
    ]


def test_strict_never_drops_a_schema() -> None:
    # Everything deltaplan created in `sales` has lost its spec. The tables go —
    # that is what strict means — but the schema stays.
    managed = ((MANAGED_PROPERTY, "true"),)
    fake = FakeWarehouse.of(
        table(col("id", "bigint"), name="dev.sales.orders", properties=managed)
    )
    plan = planned(
        [table(col("id", "bigint"), name="dev.sales.other")], fake, strict=True
    )
    assert "DROP SCHEMA" not in " ".join(s.sql or "" for s in plan.steps)


def test_a_missing_schema_skips_the_rest_of_introspection() -> None:
    runner = fake_runner(schemata=())
    schema = Introspector(runner).schema("dev", "sales")
    assert not schema.exists
    assert len(runner.statements) == 1, (
        "nothing else to ask about a schema that isn't there"
    )
