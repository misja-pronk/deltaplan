"""Plan, run it, re-plan: the diff must be empty. Offline.

This is the same assertion `tests/integration/` makes against a real workspace,
run here against `tests/fake_warehouse.py` — an in-memory catalog that
interprets the statements the planner generates. It cannot tell us what
Databricks accepts, but it does tell us that every statement deltaplan emits
says what the change it came from meant, that the plan closes the diff, and that
re-running a plan is a no-op.

Anything the fake doesn't recognise raises, so a new statement shape can't slip
past untested.
"""

import pytest

from deltaplan.differ import diff, is_applied
from deltaplan.introspect import Introspector
from deltaplan.model.table import Check, PrimaryKey, Table
from fake_warehouse import FakeWarehouse
from helpers import col, plan_against, run, table

NAME = "main.sales.orders"


def live_now(fake: FakeWarehouse) -> Table | None:
    found = Introspector(fake).table(NAME)
    return found.table if found else None


def converge(desired: Table, live: Table | None = None, **kwargs: bool) -> FakeWarehouse:
    """Plan against the fake, run the plan, and insist nothing is left to do."""
    fake, plan = plan_against(desired, live, **kwargs)  # type: ignore[arg-type]
    assert plan.steps, "expected something to do"

    # Before: every change is outstanding. The executor's idempotency check and
    # the differ have to agree on that.
    before = live_now(fake)
    for change in plan.changes:
        assert not is_applied(change, before), f"{change.kind} {change.path}"

    run(plan, fake)

    after = live_now(fake)
    assert diff(desired, after, compare_order=bool(kwargs.get("check_order"))) == ()
    for change in plan.changes:
        assert is_applied(change, after), f"{change.kind} {change.path} did not take"
    return fake


LIVE = table(
    col("order_id", "bigint", nullable=False),
    col("amount", "decimal(10,2)"),
    col("cust_id", "string"),
    col("address", "struct<street:string,old_zip:string>"),
    col("lines", "array<struct<sku:string,qty:int>>"),
    col("by_code", "map<string,struct<n:int>>"),
    col("legacy_flag", "boolean"),
    name=NAME,
    comment="Order facts",
)


def test_create_table() -> None:
    desired = table(
        col("order_id", "bigint", nullable=False, comment="Surrogate key"),
        col("address", "struct<street:string,zip:string>"),
        name=NAME,
        comment="Order facts",
        cluster_by=("order_id",),
        properties=(("delta.enableChangeDataFeed", "true"),),
        tags=(("domain", "sales"),),
        constraints=(
            PrimaryKey(("order_id",), "orders_pk"),
            Check("positive", "order_id > 0"),
        ),
    )
    fake = converge(desired)
    created = fake.tables[NAME]
    assert created.managed, "a table deltaplan creates must carry its marker"


def test_add_column() -> None:
    converge(
        table(
            *LIVE.columns, col("shipped_at", "timestamp"), name=NAME, comment=LIVE.comment
        ),
        LIVE,
    )


def test_add_nested_field() -> None:
    converge(
        table(
            *[c for c in LIVE.columns if c.name != "address"],
            col(
                "address",
                "struct<street:string,old_zip:string,zip:string comment 'Post'>",
            ),
            name=NAME,
            comment=LIVE.comment,
        ),
        LIVE,
    )


def test_drop_column() -> None:
    fake = converge(
        table(
            *[c for c in LIVE.columns if c.name != "legacy_flag"],
            name=NAME,
            comment=LIVE.comment,
        ),
        LIVE,
    )
    # The prerequisite really did run before the drop.
    assert fake.tables[NAME].properties_map()["delta.columnMapping.mode"] == "name"


def test_drop_nested_field() -> None:
    converge(
        table(
            *[c for c in LIVE.columns if c.name != "address"],
            col("address", "struct<street:string>"),
            name=NAME,
            comment=LIVE.comment,
        ),
        LIVE,
    )


def test_rename_column() -> None:
    converge(
        table(
            *[c for c in LIVE.columns if c.name != "cust_id"],
            col("customer_ref", "string", renamed_from="cust_id"),
            name=NAME,
            comment=LIVE.comment,
        ),
        LIVE,
    )


def test_rename_nested_field() -> None:
    from deltaplan.model.types import Field, Primitive, Struct

    desired = table(
        *[c for c in LIVE.columns if c.name != "address"],
        Field(
            "address",
            Struct(
                (
                    Field("street", Primitive("string")),
                    Field("zip", Primitive("string"), renamed_from="old_zip"),
                )
            ),
        ),
        name=NAME,
        comment=LIVE.comment,
    )
    converge(desired, LIVE)


@pytest.mark.parametrize(
    ("column", "before", "after"),
    [
        ("amount", "decimal(10,2)", "decimal(18,2)"),
        (
            "lines",
            "array<struct<sku:string,qty:int>>",
            "array<struct<sku:string,qty:bigint>>",
        ),
        ("by_code", "map<string,struct<n:int>>", "map<string,struct<n:bigint>>"),
    ],
)
def test_widening(column: str, before: str, after: str) -> None:
    del before  # the live table already has it
    converge(
        table(
            *[c for c in LIVE.columns if c.name != column],
            col(column, after),
            name=NAME,
            comment=LIVE.comment,
        ),
        LIVE,
    )


def test_widening_a_map_key() -> None:
    live = table(col("by_code", "map<int,string>"), name=NAME)
    converge(table(col("by_code", "map<bigint,string>"), name=NAME), live)


def test_nullability_both_ways() -> None:
    converge(
        table(
            col("order_id", "bigint"),  # NOT NULL -> nullable
            col("amount", "decimal(10,2)", nullable=False),  # and back
            *[c for c in LIVE.columns if c.name not in {"order_id", "amount"}],
            name=NAME,
            comment=LIVE.comment,
        ),
        LIVE,
    )


def test_comments() -> None:
    converge(
        table(
            col("order_id", "bigint", nullable=False, comment="Surrogate key"),
            *[c for c in LIVE.columns if c.name != "order_id"],
            name=NAME,
            comment="Order facts, one row per order",
        ),
        LIVE,
    )


def test_table_metadata() -> None:
    converge(
        table(
            *LIVE.columns,
            name=NAME,
            comment=LIVE.comment,
            cluster_by=("order_id",),
            properties=(("delta.enableChangeDataFeed", "true"),),
            tags=(("domain", "sales"), ("pii", "false")),
        ),
        LIVE,
    )


def test_constraints() -> None:
    live = table(
        col("id", "bigint", nullable=False),
        name=NAME,
        constraints=(Check("positive", "id > 0"),),
    )
    converge(
        table(
            col("id", "bigint", nullable=False),
            name=NAME,
            constraints=(
                PrimaryKey(("id",), "orders_pk"),
                Check("positive", "id >= 0"),
            ),
        ),
        live,
    )


def test_column_order() -> None:
    live = table(col("a", "int"), col("b", "int"), col("c", "int"), name=NAME)
    desired = table(col("c", "int"), col("a", "int"), col("b", "int"), name=NAME)
    fake = converge(desired, live, check_order=True)
    assert fake.tables[NAME].column_names == ("c", "a", "b")


def test_everything_at_once() -> None:
    """The plan from the design document, plus the rest of the vocabulary."""
    desired = table(
        col("order_id", "bigint", nullable=False, comment="Surrogate key"),
        col("amount", "decimal(18,2)"),
        col("customer_ref", "string", renamed_from="cust_id"),
        col("address", "struct<street:string,old_zip:string,zip:string>"),
        col("lines", "array<struct<sku:string,qty:bigint>>"),
        col("by_code", "map<string,struct<n:int>>"),
        col("shipped_at", "timestamp"),
        name=NAME,
        comment="Order facts, one row per order",
        cluster_by=("order_id",),
        properties=(("delta.enableChangeDataFeed", "true"),),
        tags=(("domain", "sales"),),
        constraints=(PrimaryKey(("order_id",), "orders_pk"),),
    )
    fake = converge(desired, LIVE)
    # Every statement was one the fake recognised — which is the point.
    assert len(fake.ddl) >= 10


def test_running_a_plan_twice_changes_nothing_more() -> None:
    desired = table(
        *[c for c in LIVE.columns if c.name != "cust_id"],
        col("customer_ref", "string", renamed_from="cust_id"),
        name=NAME,
        comment=LIVE.comment,
    )
    fake, plan = plan_against(desired, LIVE)
    run(plan, fake)
    after_once = fake.tables[NAME]

    # Re-running is what `apply` must never do blindly — the rename would fail,
    # because `cust_id` is gone. This is why the executor asks `is_applied`
    # first, and it is exactly what the next test asserts.
    for change in plan.changes:
        assert is_applied(change, after_once)


# ---------------------------------------------------------------------------
# rewrites
# ---------------------------------------------------------------------------
#
# A rewrite rebuilds the table out of a query rather than patching it, so
# convergence is the only thing that says the query was right: the fake types
# the projection, so a column that comes out the wrong shape fails here.


def test_rewrite_of_a_scalar_type() -> None:
    fake = converge(
        table(
            *[c for c in LIVE.columns if c.name != "amount"],
            col("amount", "string"),  # decimal -> string is no widening
            name=NAME,
            comment=LIVE.comment,
        ),
        LIVE,
    )
    assert f"{NAME}__deltaplan_rewrite" not in fake.tables, "staging is cleaned up"


def test_rewrite_restructures_a_struct() -> None:
    from deltaplan.model.types import Field, Primitive, Struct

    desired = table(
        *[c for c in LIVE.columns if c.name not in {"address", "amount"}],
        col("amount", "string"),  # forces the rewrite
        Field(
            "address",
            Struct(
                (
                    Field("street", Primitive("string")),
                    # renamed, and the type changes under it
                    Field("zip", Primitive("int"), renamed_from="old_zip"),
                    Field("country", Primitive("string"), comment="ISO 3166"),
                )
            ),
        ),
        name=NAME,
        comment=LIVE.comment,
    )
    converge(desired, LIVE)


def test_rewrite_changes_an_array_element() -> None:
    converge(
        table(
            *[c for c in LIVE.columns if c.name != "lines"],
            col("lines", "array<struct<sku:string,qty:string>>"),
            name=NAME,
            comment=LIVE.comment,
        ),
        LIVE,
    )


def test_rewrite_carries_renames_adds_and_drops_at_once() -> None:
    converge(
        table(
            col("order_id", "bigint", nullable=False, comment="Surrogate key"),
            col("amount", "string"),  # rewrite
            col("customer_ref", "string", renamed_from="cust_id"),
            col("address", "struct<street:string>"),
            col("lines", "array<struct<sku:string,qty:int>>"),
            col("by_code", "map<string,struct<n:int>>"),
            col("shipped_at", "timestamp"),  # new column
            name=NAME,
            comment="Order facts, one row per order",
            cluster_by=("order_id",),
            tags=(("domain", "sales"),),
            constraints=(PrimaryKey(("order_id",), "orders_pk"),),
        ),
        LIVE,
    )


def test_a_using_expression_is_taken_as_written() -> None:
    from deltaplan.model.types import Field, Primitive

    desired = table(
        *[c for c in LIVE.columns if c.name != "address"],
        # A struct becoming a string: deltaplan has no conversion for that, so the
        # spec supplies one.
        Field("address", Primitive("string"), using="CAST(address.street AS STRING)"),
        name=NAME,
        comment=LIVE.comment,
    )
    _, plan = plan_against(desired, LIVE)
    assert all(step.sql for step in plan.steps), "nothing should be unrunnable"
    assert "CAST(address.street AS STRING) AS `address`" in (plan.steps[0].sql or "")
    converge(desired, LIVE)


@pytest.mark.parametrize(
    ("column", "type_text", "reason"),
    [
        # A struct becoming an array: no conversion to guess at.
        ("address", "array<string>", "address"),
        # A map's value type moving: transform_values would do it, but not
        # without a decision deltaplan can't make for you.
        ("by_code", "map<string,struct<n:string>>", "by_code"),
    ],
)
def test_what_deltaplan_refuses_to_invent(
    column: str, type_text: str, reason: str
) -> None:
    desired = table(
        *[c for c in LIVE.columns if c.name != column],
        col(column, type_text),
        name=NAME,
        comment=LIVE.comment,
    )
    _, plan = plan_against(desired, LIVE)
    unrunnable = [step for step in plan.steps if step.sql is None]
    assert len(unrunnable) == 1
    assert reason in (unrunnable[0].note or "")
    assert "using:" in (unrunnable[0].note or "")


def test_a_nested_not_null_cannot_be_reached_by_a_rewrite() -> None:
    desired = table(
        *[c for c in LIVE.columns if c.name not in {"amount", "address"}],
        col("amount", "string"),  # forces the rewrite
        col("address", "struct<street:string not null,old_zip:string>"),
        name=NAME,
        comment=LIVE.comment,
    )
    _, plan = plan_against(desired, LIVE)
    unreachable = [step for step in plan.steps if step.title == "UNREACHABLE"]
    assert len(unreachable) == 1
    assert "address.street" in (unreachable[0].note or "")
