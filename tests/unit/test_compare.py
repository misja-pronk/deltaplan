"""One object, as it is and as the specs say it should be.

`compare()` is what makes the page readable for two kinds of reader without
building two pages: one set of rows, aligned by meaning — the `amount` column on
the left is the `amount` column on the right, however far its type moved — with
both a short value per side and the sentence the rest of deltaplan uses.

It is pure, so this is where the behaviour is pinned; the HTML only draws it.
"""

from __future__ import annotations

from dataclasses import replace

from deltaplan.introspect import Introspector
from deltaplan.model.function import Function, Parameter
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, Grant, RowFilter
from deltaplan.model.types import Primitive
from deltaplan.model.view import Relation, View
from deltaplan.planning import plan_tables
from deltaplan.render.compare import Comparison, compare
from fake_warehouse import FakeWarehouse
from helpers import col, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
LIVE = replace(
    table(
        col("order_id", "bigint", nullable=False),
        col("amount", "int", comment="Gross"),
        col("legacy", "string"),
        name=NAME,
        properties=MANAGED,
    ),
    comment="Order facts",
)


def seen(desired: Relation, fake: FakeWarehouse, *, which: int = 0) -> Comparison:
    plan: Plan = plan_tables(
        [desired], Introspector(fake), target="dev", tool_version="0"
    )
    return compare(plan.diffs[which])


def rows_of(comparison: Comparison) -> dict[str, tuple[str, str | None, str | None]]:
    return {row.path: (row.state, row.left, row.right) for row in comparison.rows}


def test_a_column_that_moved_has_both_sides() -> None:
    desired = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "decimal(18,2)", comment="Gross"),
        col("legacy", "string"),
        name=NAME,
    )
    rows = rows_of(seen(desired, FakeWarehouse.of(LIVE)))
    assert rows["amount"] == (
        "changed",
        'int comment "Gross"',
        'decimal(18,2) comment "Gross"',
    )
    assert rows["order_id"][0] == "same", "and one that didn't is context"


def test_an_added_and_a_dropped_column_read_as_such() -> None:
    desired = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "int", comment="Gross"),
        col("segment", "string"),
        name=NAME,
    )
    rows = rows_of(seen(desired, FakeWarehouse.of(LIVE)))
    assert rows["segment"] == ("added", None, "string")
    assert rows["legacy"] == ("removed", "string", None)


def test_the_right_hand_side_is_how_it_will_be_not_what_the_spec_says() -> None:
    """A spec silent about partitioning keeps it — so the page says so."""
    live = replace(LIVE, partitioned_by=("region",))
    desired = table(*LIVE.columns, name=NAME)
    rows = rows_of(seen(desired, FakeWarehouse.of(live)))
    assert rows["clustering"] == (
        "same",
        "partitioned by (region)",
        "partitioned by (region)",
    )


def test_a_nested_field_sits_under_its_column() -> None:
    live = replace(
        table(
            col("address", "struct<street:string,old_zip:string>"),
            name=NAME,
            properties=MANAGED,
        ),
        comment=None,
    )
    desired = table(col("address", "struct<street:string,zip:string>"), name=NAME)
    comparison = seen(desired, FakeWarehouse.of(live))
    depths = {row.path: row.depth for row in comparison.rows if row.kind == "column"}
    assert depths["address"] == 0
    assert depths["address.zip"] == 1
    assert rows_of(comparison)["address.old_zip"][0] == "removed"


def test_a_rename_says_where_the_column_came_from() -> None:
    desired = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "int", comment="Gross"),
        col("archived", "string", renamed_from="legacy"),
        name=NAME,
    )
    comparison = seen(desired, FakeWarehouse.of(LIVE))
    [row] = [r for r in comparison.rows if r.path == "archived"]
    assert "renamed from legacy" in row.said
    assert row.left == "string", "the old column is the left-hand side"


def test_what_is_not_a_column_gets_its_own_row() -> None:
    desired = replace(
        table(*LIVE.columns, name=NAME),
        comment="Order facts, in money",
        tags=(("domain", "sales"),),
        grants=(Grant("analysts", ("SELECT",)),),
        row_filter=RowFilter("main.sales.only_mine", ("order_id",)),
    )
    rows = rows_of(seen(desired, FakeWarehouse.of(LIVE)))
    assert rows["comment"] == ("changed", "Order facts", "Order facts, in money")
    assert rows["tags"] == ("added", None, "domain=sales")
    assert rows["grants"][2] == "analysts: SELECT"
    assert rows["row filter"][2] == "main.sales.only_mine(order_id)"


def test_a_tag_change_lands_on_the_tags_row_not_its_own() -> None:
    """Tags, properties and grants hang off their key, not off the table."""
    desired = replace(
        table(*LIVE.columns, name=NAME, properties=MANAGED),
        comment="Order facts",
        tags=(("pii", "no"),),
    )
    comparison = seen(desired, FakeWarehouse.of(LIVE))
    assert "pii" not in {row.path for row in comparison.rows}
    [tags] = [row for row in comparison.rows if row.path == "tags"]
    assert "pii" in tags.said and tags.right == "pii=no"


def test_a_new_table_is_all_right_hand_side() -> None:
    fake = FakeWarehouse()
    fake.schemas.add("main.sales")
    comparison = seen(table(col("id", "bigint"), name=NAME), fake)
    assert comparison.action == "create"
    assert comparison.headline == "new table with 1 column"
    assert all(row.left is None for row in comparison.rows if row.kind == "column")


def test_the_headline_says_what_it_costs() -> None:
    fake = FakeWarehouse.of(LIVE)
    fake.sizes[NAME] = 442381631488
    desired = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "string"),  # a cast: the table is rebuilt
        col("legacy", "string"),
        name=NAME,
    )
    assert "rebuilt, 412 GB" in seen(desired, fake).headline


def test_a_view_is_compared_line_by_line() -> None:
    live = View(f"{NAME}_v", query="SELECT id, amount\nFROM main.sales.orders")
    fake = FakeWarehouse.of(live)
    desired = View(f"{NAME}_v", query="SELECT id, amount, region\nFROM main.sales.orders")
    comparison = seen(desired, fake)
    assert comparison.kind == "view"
    lines = [row for row in comparison.rows if row.kind == "line"]
    assert lines[0].state == "changed"
    assert lines[0].left == "SELECT id, amount"
    assert lines[0].right == "SELECT id, amount, region"
    assert lines[1].state == "same"


def test_a_function_shows_its_signature_and_its_body() -> None:
    live = Function(
        name="main.sales.hide",
        parameters=(Parameter("val", Primitive("string")),),
        returns=Primitive("string"),
        body="'***'",
    )
    desired = replace(live, body="concat('*', val)")
    comparison = seen(desired, FakeWarehouse.of(live))
    rows = {row.path: row for row in comparison.rows}
    assert rows["signature"].state == "same"
    assert rows["signature"].right == "(val string) returns string"
    [body] = [row for row in comparison.rows if row.kind == "line"]
    assert (body.left, body.right) == ("'***'", "concat('*', val)")


def test_every_change_the_differ_made_is_accounted_for() -> None:
    """Nothing the plan says may be lost between the differ and the page."""
    live = replace(LIVE, partitioned_by=("region",))
    fake = FakeWarehouse.of(live)
    desired = replace(
        table(
            col("order_id", "bigint", nullable=False),
            col("amount", "decimal(18,2)", comment="Net"),
            col("segment", "string"),
            name=NAME,
        ),
        comment="Orders",
        cluster_by=("order_id",),
        tags=(("domain", "sales"),),
    )
    plan = plan_tables([desired], Introspector(fake), target="dev", tool_version="0")
    comparison = compare(plan.diffs[0])
    said = " ".join(row.said for row in comparison.rows)
    for change in plan.diffs[0].changes:
        from deltaplan.render.labels import describe

        assert describe(change)[1] in said, (
            f"{change.kind} at {change.path!r} went missing"
        )
