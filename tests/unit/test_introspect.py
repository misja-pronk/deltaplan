"""Live rows become the model — tested against a fake runner, no workspace.

The queries themselves are exercised by the integration suite; what is asserted
here is the mapping, which is where the bugs live.
"""

import pytest

from deltaplan.differ import diff
from deltaplan.introspect import (
    IntrospectionError,
    Introspector,
    LiveSchema,
    Row,
)
from deltaplan.model.table import Check, PrimaryKey
from deltaplan.model.types import Primitive, render_type
from deltaplan.sql import normalise_expression
from helpers import col, fake_runner, table

CATALOG = "main"
SCHEMA = "sales"


def live_schema(**responses: tuple[Row, ...]) -> LiveSchema:
    return Introspector(fake_runner(**responses)).schema(CATALOG, SCHEMA)


ORDERS_TABLE: tuple[Row, ...] = (
    {
        "table_name": "orders",
        "comment": "Order facts",
        "table_type": "MANAGED",
        "data_source_format": "DELTA",
    },
)

ORDERS_COLUMNS: tuple[Row, ...] = (
    {
        "table_name": "orders",
        "column_name": "order_id",
        "ordinal_position": "1",
        "full_data_type": "bigint",
        "is_nullable": "NO",
        "comment": "Surrogate key",
    },
    {
        "table_name": "orders",
        "column_name": "address",
        "ordinal_position": "2",
        "full_data_type": "struct<street:string,zip:string>",
        "is_nullable": "YES",
        "comment": None,
    },
    {
        "table_name": "orders",
        "column_name": "lines",
        "ordinal_position": "3",
        "full_data_type": "array<struct<sku:string,qty:int>>",
        "is_nullable": "YES",
        "comment": None,
    },
)

ORDERS_DETAIL: tuple[Row, ...] = (
    {
        "format": "delta",
        "name": "main.sales.orders",
        "clusteringColumns": '["order_id"]',
        "numFiles": "12",
        "sizeInBytes": "442381631488",
        "properties": '{"delta.enableChangeDataFeed":"true","deltaplan.managed":"true"}',
    },
)


def test_columns_types_and_comments() -> None:
    schema = live_schema(
        tables=ORDERS_TABLE, columns=ORDERS_COLUMNS, detail=ORDERS_DETAIL
    )
    live = schema.get("main.sales.orders")
    assert live is not None
    assert live.table.comment == "Order facts"
    assert live.table.column_names == ("order_id", "address", "lines")
    order_id = live.table.column("order_id")
    assert order_id is not None and order_id.nullable is False
    assert order_id.comment == "Surrogate key"
    address = live.table.column("address")
    assert address is not None
    assert render_type(address.type) == "struct<street:string,zip:string>"


def test_detail_gives_clustering_properties_and_size() -> None:
    schema = live_schema(
        tables=ORDERS_TABLE, columns=ORDERS_COLUMNS, detail=ORDERS_DETAIL
    )
    live = schema.get("main.sales.orders")
    assert live is not None
    assert live.table.cluster_by == ("order_id",)
    assert live.table.properties_map()["delta.enableChangeDataFeed"] == "true"
    assert live.table.managed is True
    assert live.size_bytes == 442_381_631_488


def test_tags_and_constraints() -> None:
    schema = live_schema(
        tables=ORDERS_TABLE,
        columns=ORDERS_COLUMNS,
        detail=ORDERS_DETAIL,
        tags=({"table_name": "orders", "tag_name": "domain", "tag_value": "sales"},),
        constraints=(
            {
                "table_name": "orders",
                "constraint_name": "orders_pk",
                "constraint_type": "PRIMARY KEY",
                "check_clause": None,
            },
            {
                "table_name": "orders",
                "constraint_name": "positive",
                "constraint_type": "CHECK",
                "check_clause": "(order_id > 0)",
            },
        ),
        keys=(
            {
                "table_name": "orders",
                "constraint_name": "orders_pk",
                "column_name": "order_id",
            },
        ),
    )
    live = schema.get("main.sales.orders")
    assert live is not None
    assert live.table.tags == (("domain", "sales"),)
    assert live.table.constraints == (
        PrimaryKey(("order_id",), "orders_pk"),
        # The catalog wraps a check clause in parentheses; a spec doesn't.
        Check("positive", "order_id > 0"),
    )


def test_views_and_other_formats_are_skipped_never_touched() -> None:
    schema = live_schema(
        tables=(
            {"table_name": "v_orders", "table_type": "VIEW", "data_source_format": None},
            {
                "table_name": "csv_dump",
                "table_type": "EXTERNAL",
                "data_source_format": "CSV",
                "comment": None,
            },
        ),
    )
    assert schema.tables == ()
    assert schema.skipped == (
        ("main.sales.csv_dump", "csv table"),
        ("main.sales.v_orders", "view table"),
    )


def test_an_unparseable_type_does_not_lose_the_table() -> None:
    schema = live_schema(
        tables=ORDERS_TABLE,
        columns=(
            {
                "table_name": "orders",
                "column_name": "odd",
                "ordinal_position": "1",
                "full_data_type": "quantum<42>",
                "is_nullable": "YES",
                "comment": None,
            },
        ),
        detail=ORDERS_DETAIL,
    )
    live = schema.get("main.sales.orders")
    assert live is not None
    odd = live.table.column("odd")
    assert odd is not None and odd.type == Primitive("quantum<42>")


def test_a_column_with_no_type_is_an_error() -> None:
    with pytest.raises(IntrospectionError, match="has no type"):
        live_schema(
            tables=ORDERS_TABLE,
            columns=(
                {
                    "table_name": "orders",
                    "column_name": "broken",
                    "full_data_type": None,
                    "is_nullable": "YES",
                    "comment": None,
                },
            ),
        )


def test_an_introspected_table_diffs_clean_against_its_spec() -> None:
    # The whole point of introspecting into the same model: a spec that already
    # describes the live table produces no changes.
    schema = live_schema(
        tables=ORDERS_TABLE, columns=ORDERS_COLUMNS, detail=ORDERS_DETAIL
    )
    live = schema.get("main.sales.orders")
    assert live is not None
    spec = table(
        col("order_id", "bigint", nullable=False, comment="Surrogate key"),
        col("address", "struct<street:string,zip:string>"),
        col("lines", "array<struct<sku:string,qty:int>>"),
        name="main.sales.orders",
        comment="Order facts",
        cluster_by=("order_id",),
        properties=(
            ("delta.enableChangeDataFeed", "true"),
            ("deltaplan.managed", "true"),
        ),
    )
    assert diff(spec, live.table) == ()


def test_latest_version() -> None:
    runner = fake_runner(history=({"version": "17"},))
    assert Introspector(runner).latest_version("main.sales.orders") == 17
    assert "DESCRIBE HISTORY `main`.`sales`.`orders` LIMIT 1" in runner.statements[0]


def test_names_are_quoted_and_filters_are_literals() -> None:
    runner = fake_runner()
    Introspector(runner).schema("odd catalog", "odd'schema")
    statements = " ".join(runner.statements)
    assert "`odd catalog`.information_schema" in statements
    assert "'odd''schema'" in statements


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ("(order_id > 0)", "order_id > 0"),
        ("order_id > 0", "order_id > 0"),
        ("((a > 0) AND (b > 0))", "(a > 0) AND (b > 0)"),
        # Starts and ends with a parenthesis, but isn't wrapped in one.
        ("(a > 0) AND (b > 0)", "(a > 0) AND (b > 0)"),
        ("  amount   >   0  ", "amount > 0"),
    ],
)
def test_normalise_expression(clause: str, expected: str) -> None:
    assert normalise_expression(clause) == expected
