"""The order a plan puts things in, when they refer to each other.

Three kinds of reference exist between the objects a project describes, and
they don't run one way between kinds:

* a view's query reads tables, views and functions;
* a **function's body reads tables** — a row filter consulting a lookup table
  is the ordinary way to write row-level security, and Databricks resolves a
  function's body when it is created;
* a table's row filter and column masks call functions.

Planning by kind — functions, then tables, then views — got the last one right
and the middle one wrong, so a fresh schema could never converge in one apply:
the function was created before the table it reads. Found on a real project.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-sql-function
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from deltaplan.introspect import Introspector
from deltaplan.model.function import Function, Parameter
from deltaplan.model.plan import Plan
from deltaplan.model.table import RowFilter, Table
from deltaplan.model.types import Primitive
from deltaplan.model.view import Relation, View
from deltaplan.planning import PlanningError, plan_tables
from fake_warehouse import FakeWarehouse
from helpers import col, table

SCHEMA = "main.sales"
LOOKUP = f"{SCHEMA}.mailbox_access"
FILTER = f"{SCHEMA}.mailbox_filter"
FILTERED = f"{SCHEMA}.email_messages"


def reads_the_lookup(name: str = FILTER, reads: str = LOOKUP) -> Function:
    return Function(
        name=name,
        parameters=(Parameter("val", Primitive("string")),),
        returns=Primitive("boolean"),
        body=f"EXISTS (SELECT 1 FROM {reads} a WHERE a.mailbox = val)",
    )


def empty() -> FakeWarehouse:
    fake = FakeWarehouse()
    fake.schemas.add(SCHEMA)
    return fake


def planned(specs: list[Relation], fake: FakeWarehouse) -> Plan:
    return plan_tables(specs, Introspector(fake), target="dev", tool_version="0")


def order_of(plan: Plan) -> list[str]:
    return [diff.table for diff in plan.diffs if diff.changes]


def test_a_function_is_planned_after_the_table_its_body_reads() -> None:
    lookup = table(col("mailbox", "string"), name=LOOKUP)
    plan = planned([reads_the_lookup(), lookup], empty())
    assert order_of(plan) == [LOOKUP, FILTER]
    steps = [step.table for step in plan.steps]
    assert steps.index(LOOKUP) < steps.index(FILTER), (
        "the table has to exist when the function body is resolved"
    )


def test_the_whole_chain_lands_in_one_order() -> None:
    """lookup table → function reading it → table whose row filter calls it."""
    lookup = table(col("mailbox", "string"), name=LOOKUP)
    filtered = replace(
        table(col("id", "bigint"), col("mailbox", "string"), name=FILTERED),
        row_filter=RowFilter(FILTER, ("mailbox",)),
    )
    # Written in the order that used to break, to prove it isn't spec order.
    plan = planned([filtered, reads_the_lookup(), lookup], empty())
    assert order_of(plan) == [LOOKUP, FILTER, FILTERED]


def test_a_project_with_no_such_reference_plans_as_it_always_did() -> None:
    """Functions, then tables, then views — the tiebreak is the old order."""
    plain = Function(
        name=f"{SCHEMA}.hide",
        parameters=(Parameter("val", Primitive("string")),),
        returns=Primitive("string"),
        body="'***'",
    )
    first = table(col("id", "bigint"), name=f"{SCHEMA}.a")
    second = table(col("id", "bigint"), name=f"{SCHEMA}.b")
    plan = planned([second, first, plain], empty())
    assert order_of(plan) == [plain.name, f"{SCHEMA}.b", f"{SCHEMA}.a"]


def test_a_real_cycle_is_refused_and_named() -> None:
    """A function reading a table whose row filter calls that function."""
    circular = replace(
        table(col("mailbox", "string"), name=LOOKUP),
        row_filter=RowFilter(FILTER, ("mailbox",)),
    )
    with pytest.raises(PlanningError) as raised:
        planned([reads_the_lookup(), circular], empty())
    said = str(raised.value)
    assert "cycle" in said
    assert LOOKUP in said and FILTER in said


def test_it_applies_in_that_order() -> None:
    """The point of all this: a fresh schema converges in one apply."""
    from deltaplan import api
    from deltaplan.connect import Connection
    from deltaplan.history import NoHistory

    fake = empty()
    lookup = table(col("mailbox", "string"), name=LOOKUP)
    specs: list[Relation] = [reads_the_lookup(), lookup]
    assert api.apply(
        planned(specs, fake), Connection(runner=fake), history=NoHistory()
    ).ok
    assert planned(specs, fake).empty, "and nothing is left to do"


def test_a_view_still_comes_after_what_it_reads() -> None:
    source = table(col("id", "bigint"), name=f"{SCHEMA}.orders")
    over_it = View(f"{SCHEMA}.recent", query=f"SELECT * FROM {SCHEMA}.orders")
    plan = planned([over_it, source], empty())
    assert order_of(plan) == [source.name, over_it.name]


def test_a_table_the_function_does_not_read_keeps_its_place() -> None:
    """Only a real reference moves anything: no edge, no change in order."""
    unrelated: Table = table(col("id", "bigint"), name=f"{SCHEMA}.unrelated")
    plan = planned([reads_the_lookup(reads=f"{SCHEMA}.elsewhere"), unrelated], empty())
    assert order_of(plan) == [FILTER, unrelated.name]
