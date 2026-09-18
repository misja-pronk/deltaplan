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
from deltaplan.model.types import Field, Primitive, render_type
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
    """Keys come from information_schema; CHECK constraints don't — Delta keeps
    them as `delta.constraints.<name>` properties, which DESCRIBE DETAIL shows
    and information_schema doesn't. Verified live, 2026-09-18."""
    detail = dict(ORDERS_DETAIL[0])
    detail["properties"] = (
        '{"deltaplan.managed":"true","delta.constraints.positive":"order_id > 0"}'
    )
    schema = live_schema(
        tables=ORDERS_TABLE,
        columns=ORDERS_COLUMNS,
        detail=(detail,),
        tags=({"table_name": "orders", "tag_name": "domain", "tag_value": "sales"},),
        constraints=(
            {
                "table_name": "orders",
                "constraint_name": "orders_pk",
                "constraint_type": "PRIMARY KEY",
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
        Check("positive", "order_id > 0"),
    )
    assert "delta.constraints.positive" not in live.table.properties_map(), (
        "a CHECK is a constraint, not a property to report or import"
    )


def test_views_are_read_and_other_kinds_are_skipped() -> None:
    from deltaplan.model.view import View

    runner = fake_runner(
        tables=(
            {"table_name": "v_orders", "table_type": "VIEW", "data_source_format": None},
            {
                "table_name": "csv_dump",
                "table_type": "EXTERNAL",
                "data_source_format": "CSV",
                "comment": None,
            },
            {
                # Stored as Delta, but not a table deltaplan can alter.
                "table_name": "daily_totals",
                "table_type": "MATERIALIZED_VIEW",
                "data_source_format": "DELTA",
            },
        ),
    )
    runner.responses["information_schema.views"] = (
        {"table_name": "v_orders", "view_definition": "SELECT 1 AS one"},
    )
    schema = Introspector(runner).schema(CATALOG, SCHEMA)
    assert schema.tables == ()
    assert schema.views == (View("main.sales.v_orders", "SELECT 1 AS one"),)
    assert schema.skipped == (
        ("main.sales.csv_dump", "csv"),
        ("main.sales.daily_totals", "materialized view"),
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
        # Spelled differently, meaning the same: the catalog's echo of a
        # generation, verified live, against what a spec might say.
        ("( CAST(placed_at AS DATE) )", "CAST(placed_at AS DATE)"),
        ("cast(Placed_At as date)", "CAST(placed_at AS DATE)"),
        ("Amount>0", "amount > 0"),
        # A string literal keeps its case; a quoted identifier its spelling.
        ("status = 'New'", "status = 'New'"),
        # Not something sqlglot parses: the plain normalisation still applies.
        ("  (a  ===  b)  ", "a === b"),
    ],
)
def test_normalise_expression(clause: str, expected: str) -> None:
    assert normalise_expression(clause) == expected


def test_a_second_read_sees_what_changed_in_between() -> None:
    """One Introspector, read twice with a change in between: the second read
    must see it. It once cached DESCRIBE DETAIL and key usage for its whole
    life, so the second read returned the first — and apply's staleness check,
    reading through the same object as plan, could never see a change."""
    from deltaplan.model.table import PrimaryKey
    from fake_warehouse import FakeWarehouse

    fake = FakeWarehouse.of(
        table(col("id", "bigint", nullable=False), name="main.sales.t")
    )
    introspector = Introspector(fake)
    assert introspector.table("main.sales.t") is not None

    fake.query("ALTER TABLE `main`.`sales`.`t` CLUSTER BY (`id`)")
    fake.query("ALTER TABLE `main`.`sales`.`t` SET TBLPROPERTIES ('a' = 'b')")
    fake.query("ALTER TABLE `main`.`sales`.`t` ADD CONSTRAINT `t_pk` PRIMARY KEY (`id`)")

    again = introspector.table("main.sales.t")
    assert again is not None
    assert again.table.cluster_by == ("id",)
    assert again.table.properties_map().get("a") == "b"
    assert PrimaryKey(("id",), "t_pk") in again.table.constraints


def test_column_details_come_from_show_create_table() -> None:
    """On a live workspace information_schema.columns says NO to identity and
    generation and has no default — and drops NOT NULL inside structs. SHOW
    CREATE TABLE has them (verified 2026-09-18); introspection reads it."""
    from deltaplan.model.types import Identity

    columns = (
        {
            "table_name": "orders",
            "column_name": name,
            "ordinal_position": str(position),
            "full_data_type": full_type,
            "is_nullable": "YES",
            "comment": None,
            "is_identity": "NO",
            "is_generated": "NO",
            "column_default": None,
        }
        for position, (name, full_type) in enumerate(
            [
                ("line_id", "bigint"),
                ("placed_on", "date"),
                ("status", "string"),
                ("address", "struct<street:string,zip:string>"),
            ],
            start=1,
        )
    )
    schema = live_schema(
        tables=ORDERS_TABLE,
        columns=tuple(columns),
        detail=ORDERS_DETAIL,
        show_create=(
            {
                "createtab_stmt": "CREATE TABLE main.sales.orders (\n"
                "  line_id BIGINT GENERATED BY DEFAULT AS IDENTITY "
                "(START WITH 100 INCREMENT BY 10),\n"
                "  placed_on DATE GENERATED ALWAYS AS ( CAST(placed_at AS DATE) ),\n"
                "  status STRING COLLATE UTF8_BINARY DEFAULT 'new',\n"
                "  address STRUCT<street: STRING COLLATE UTF8_BINARY NOT NULL, "
                "zip: STRING COLLATE UTF8_BINARY>)\nUSING delta"
            },
        ),
    )
    live = schema.get("main.sales.orders")
    assert live is not None
    table = live.table

    def column(name: str) -> Field:
        found = table.column(name)
        assert found is not None
        return found

    assert column("line_id").identity == Identity(False, 100, 10)
    assert column("placed_on").generated == "CAST(placed_at AS DATE)"
    assert column("status").default == "'new'"
    assert render_type(column("address").type) == (
        "struct<street:string not null,zip:string>"
    )
    assert live.unmodelled == ()


@pytest.mark.parametrize(
    ("answer", "note"),
    [
        ((), "a definition SHOW CREATE TABLE didn't return"),
        (({"createtab_stmt": "CREATE TABLE ("},), "a definition deltaplan couldn't read"),
        (
            (
                {
                    "createtab_stmt": "CREATE TABLE main.sales.orders "
                    "(order_id BIGINT, address STRING COLLATE UTF8_LCASE) USING delta"
                },
            ),
            "collation UTF8_LCASE on address",
        ),
    ],
)
def test_what_show_create_table_cannot_tell_is_reported(
    answer: tuple[Row, ...], note: str
) -> None:
    """Without the definition deltaplan can't see identity columns, so a table
    whose definition it couldn't read is marked unmodelled — which also keeps
    it from ever being rewritten."""
    schema = live_schema(
        tables=ORDERS_TABLE,
        columns=ORDERS_COLUMNS,
        detail=ORDERS_DETAIL,
        show_create=answer,
    )
    live = schema.get("main.sales.orders")
    assert live is not None
    assert any(entry.startswith(note) for entry in live.unmodelled), live.unmodelled
