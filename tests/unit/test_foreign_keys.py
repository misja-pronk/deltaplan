"""Foreign keys: the first constraint that spans two tables.

A key can only be added once the table it references exists with its primary
key, so foreign keys are planned last — after every table and view. A key is
matched by what it means, not only by its name; live keys nobody declared are
reported and left alone.

https://docs.databricks.com/aws/en/tables/constraints
"""

from pathlib import Path

import pytest

from deltaplan.differ import diff, unmanaged
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_table, validate_table
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, ForeignKey, PrimaryKey, Table
from deltaplan.planning import plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeWarehouse
from helpers import col, run, table

MANAGED = ((MANAGED_PROPERTY, "true"),)
TO_CUSTOMERS = ForeignKey(("customer_id",), "main.sales.customers", ("id",))


def customers(*, properties: tuple[tuple[str, str], ...] = ()) -> Table:
    return table(
        col("id", "bigint", nullable=False),
        name="main.sales.customers",
        constraints=(PrimaryKey(("id",), "customers_pk"),),
        properties=properties,
    )


def orders(*keys: ForeignKey, managed: bool = False) -> Table:
    return table(
        col("id", "bigint", nullable=False),
        col("customer_id", "bigint"),
        name="main.sales.orders",
        constraints=(PrimaryKey(("id",), "orders_pk"), *keys),
        properties=MANAGED if managed else (),
    )


def planned(specs: list[Table], fake: FakeWarehouse) -> Plan:
    return plan_tables(specs, Introspector(fake), target="test", tool_version="0")


def test_the_spec_declares_one(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: ${catalog}.sales.orders\n"
        "columns: [{name: id, type: bigint}, {name: customer_id, type: bigint}]\n"
        "constraints:\n"
        "  - foreign_key:\n"
        "      columns: [customer_id]\n"
        "      references: ${catalog}.sales.customers\n"
        "      referenced_columns: [id]\n"
    )
    loaded = load_table(path, {"catalog": "Main"})
    assert loaded.foreign_keys() == (TO_CUSTOMERS,), "the reference is lower-cased too"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "{columns: [a], references: sales.x, referenced_columns: [id]}",
            "catalog.schema.table",
        ),
        (
            "{columns: [a, b], references: c.s.x, referenced_columns: [id]}",
            "must reference 2",
        ),
        ("{columns: [a], referenced_columns: [id]}", "needs a 'references' key"),
    ],
)
def test_bad_foreign_keys(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "t.yml"
    path.write_text(
        "table: c.s.t\ncolumns: [{name: a, type: int}]\n"
        f"constraints: [{{foreign_key: {body}}}]\n"
    )
    with pytest.raises(SpecError, match=message):
        load_table(path)


def test_a_key_on_a_missing_column_is_a_lint_error(tmp_path: Path) -> None:
    path = tmp_path / "t.yml"
    path.write_text(
        "table: c.s.t\ncolumns: [{name: a, type: int}]\n"
        "constraints: [{foreign_key: "
        "{columns: [nope], references: c.s.x, referenced_columns: [id]}}]\n"
    )
    assert any(
        "foreign key column 'nope'" in d.message
        for d in validate_table(load_table(path), "t")
    )


def test_keys_are_planned_after_every_table() -> None:
    # Spec order puts orders first; its key still waits for customers to exist.
    fake = FakeWarehouse()
    fake.schemas.add("main.sales")
    plan = planned([orders(TO_CUSTOMERS), customers()], fake)
    titles = [s.title for s in plan.steps]
    assert titles == [
        "CREATE TABLE orders",
        "CREATE TABLE customers",
        "ADD CONSTRAINT orders_customer_id_fk FOREIGN KEY",
    ]
    assert plan.steps[-1].sql == (
        "ALTER TABLE `main`.`sales`.`orders` ADD CONSTRAINT `orders_customer_id_fk` "
        "FOREIGN KEY (`customer_id`) REFERENCES `main`.`sales`.`customers` (`id`)"
    )
    # It runs last, but still renders under the table it belongs to.
    rendered = plan_text(plan)
    orders_block = rendered.split("sales.customers")[0]
    assert "3. ADD CONSTRAINT orders_customer_id_fk" in orders_block

    run(plan, fake)
    assert planned([orders(TO_CUSTOMERS), customers()], fake).empty


def test_a_key_is_matched_by_meaning_not_name() -> None:
    live = orders(
        ForeignKey(
            ("customer_id",), "main.sales.customers", ("id",), "whatever_they_called_it"
        ),
        managed=True,
    )
    assert diff(orders(TO_CUSTOMERS), live) == (), (
        "an unnamed spec key is happy with any name"
    )


def test_a_named_key_under_another_name_is_replaced() -> None:
    live = orders(
        ForeignKey(("customer_id",), "main.sales.customers", ("id",), "old_name"),
        managed=True,
    )
    named = ForeignKey(
        ("customer_id",), "main.sales.customers", ("id",), "orders_customer_fk"
    )
    assert [c.kind for c in diff(orders(named), live)] == [
        "drop_constraint",
        "add_constraint",
    ]


def test_a_key_nobody_declared_is_left_alone() -> None:
    live = orders(TO_CUSTOMERS, managed=True)
    desired = orders()
    assert diff(desired, live) == ()
    assert "foreign key unnamed" in unmanaged(desired, live)


def test_introspection_reads_them_back() -> None:
    fake = FakeWarehouse.of(
        customers(properties=MANAGED),
        orders(
            ForeignKey(
                ("customer_id",), "main.sales.customers", ("id",), "orders_customer_fk"
            ),
            managed=True,
        ),
    )
    found = Introspector(fake).table("main.sales.orders")
    assert found is not None
    assert found.table.foreign_keys() == (
        ForeignKey(
            ("customer_id",), "main.sales.customers", ("id",), "orders_customer_fk"
        ),
    )


def test_they_survive_the_plan_file_and_import() -> None:
    fake = FakeWarehouse()
    fake.schemas.add("main.sales")
    plan = planned([orders(TO_CUSTOMERS), customers()], fake)
    assert loads(dumps(plan)) == plan
    assert "references: main.sales.customers" in dump_spec(orders(TO_CUSTOMERS))
