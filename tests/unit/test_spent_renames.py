"""A `renamed_from` hint that has done its job is pointed out.

The design wants `validate` to say a spent hint can be removed. Whether it is
spent depends on the live table, which `validate` never sees — so `plan` and
`drift` say it instead, as a note rather than a change.
"""

from deltaplan.differ import spent_renames
from deltaplan.introspect import Introspector
from deltaplan.model.table import MANAGED_PROPERTY
from deltaplan.model.types import Field, Primitive, Struct
from deltaplan.planning import plan_tables
from deltaplan.render.markdown import render_markdown
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeWarehouse
from helpers import col, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)


def test_a_rename_that_has_happened_is_spent() -> None:
    live = table(col("customer_ref", "string"), name=NAME)
    spec = table(col("customer_ref", "string", renamed_from="cust_id"), name=NAME)
    assert spent_renames(spec, live) == (
        "renamed_from 'cust_id' on customer_ref has done its job — it can be removed",
    )


def test_a_rename_still_to_happen_is_not() -> None:
    live = table(col("cust_id", "string"), name=NAME)
    spec = table(col("customer_ref", "string", renamed_from="cust_id"), name=NAME)
    assert spent_renames(spec, live) == ()


def test_nested_hints_are_checked_too() -> None:
    live = table(col("address", "struct<zip:string>"), name=NAME)
    spec = table(
        Field(
            "address",
            Struct((Field("zip", Primitive("string"), renamed_from="old_zip"),)),
        ),
        name=NAME,
    )
    assert spent_renames(spec, live) == (
        "renamed_from 'old_zip' on address.zip has done its job — it can be removed",
    )


def test_the_plan_says_so_and_still_says_nothing_changes() -> None:
    live = table(col("customer_ref", "string"), name=NAME, properties=MANAGED)
    spec = table(col("customer_ref", "string", renamed_from="cust_id"), name=NAME)
    plan = plan_tables(
        [spec], Introspector(FakeWarehouse.of(live)), target="t", tool_version="0"
    )
    assert plan.empty
    rendered = plan_text(plan)
    assert "has done its job — it can be removed" in rendered
    assert "No changes. Live tables match your specs." in rendered
    assert "has done its job" in render_markdown(plan)
