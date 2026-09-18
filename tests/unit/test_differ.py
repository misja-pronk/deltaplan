"""The differ: desired minus live, as semantic changes at nested paths."""

from syrupy.assertion import SnapshotAssertion

from deltaplan.differ import diff, unmanaged
from deltaplan.model.change import Change
from deltaplan.model.table import Check, PrimaryKey
from deltaplan.model.types import Primitive
from helpers import col, table

LIVE = table(
    col("order_id", "bigint", nullable=False),
    col("order_date", "date"),
    col("cust_id", "string"),
    col("amount", "decimal(10,2)"),
    col("address", "struct<street:string>"),
    col("legacy_flag", "boolean"),
    comment="Order facts",
    properties=(("deltaplan.managed", "true"),),
)


def kinds(changes: tuple[Change, ...]) -> list[tuple[str, str]]:
    return [(change.kind, change.path) for change in changes]


def test_identical_tables_produce_nothing() -> None:
    assert diff(LIVE, LIVE) == ()


def test_a_missing_table_is_one_create(snapshot: SnapshotAssertion) -> None:
    desired = table(col("id", "bigint", nullable=False))
    changes = diff(desired, None)
    assert kinds(changes) == [("create_table", "")]
    assert changes[0].after == desired
    assert changes == snapshot


def test_table_metadata(snapshot: SnapshotAssertion) -> None:
    desired = table(
        *LIVE.columns,
        comment="Order facts, one row per order",
        cluster_by=("order_date",),
        properties=(
            ("deltaplan.managed", "true"),
            ("delta.enableChangeDataFeed", "true"),
        ),
        tags=(("domain", "sales"),),
    )
    changes = diff(desired, LIVE)
    assert kinds(changes) == [
        ("set_table_comment", ""),
        ("set_cluster_by", ""),
        ("set_property", "delta.enableChangeDataFeed"),
        ("set_tag", "domain"),
    ]
    assert changes == snapshot


def test_add_and_drop_columns() -> None:
    desired = table(
        *[column for column in LIVE.columns if column.name != "legacy_flag"],
        col("shipped_at", "timestamp"),
        comment=LIVE.comment,
        properties=LIVE.properties,
    )
    assert kinds(diff(desired, LIVE)) == [
        ("add_column", "shipped_at"),
        ("drop_column", "legacy_flag"),
    ]


def test_a_declared_rename_is_a_rename_not_a_drop_and_an_add() -> None:
    desired = table(
        *[c for c in LIVE.columns if c.name != "cust_id"],
        col("customer_ref", "string", renamed_from="cust_id"),
        comment=LIVE.comment,
        properties=LIVE.properties,
    )
    changes = diff(desired, LIVE)
    assert kinds(changes) == [("rename_column", "customer_ref")]
    assert changes[0].before == "cust_id"
    assert changes[0].after == "customer_ref"


def test_a_spent_rename_hint_diffs_clean() -> None:
    # The old name is gone and the new one is live: the hint has done its job and
    # must not keep showing up as a change.
    live = table(col("customer_ref", "string"))
    desired = table(col("customer_ref", "string", renamed_from="cust_id"))
    assert diff(desired, live) == ()


def test_a_rename_hint_is_ignored_when_both_names_are_live() -> None:
    # Ambiguous: the destination already exists. The hint is dropped, which makes
    # the old column an ordinary drop candidate rather than a silent rename.
    live = table(col("cust_id", "string"), col("customer_ref", "string"))
    desired = table(col("customer_ref", "string", renamed_from="cust_id"))
    assert kinds(diff(desired, live)) == [("drop_column", "cust_id")]


def test_renamed_column_still_diffs_its_other_attributes() -> None:
    live = table(col("cust_id", "string", comment="old"))
    desired = table(
        col(
            "customer_ref",
            "string",
            nullable=False,
            comment="new",
            renamed_from="cust_id",
        )
    )
    assert kinds(diff(desired, live)) == [
        ("rename_column", "customer_ref"),
        ("set_nullable", "customer_ref"),
        ("set_comment", "customer_ref"),
    ]


def test_type_change_carries_both_types() -> None:
    desired = table(
        *[c for c in LIVE.columns if c.name != "amount"], col("amount", "decimal(18,2)")
    )
    changes = [c for c in diff(desired, LIVE) if c.kind == "change_type"]
    assert len(changes) == 1
    assert changes[0].path == "amount"


def test_nested_struct_paths(snapshot: SnapshotAssertion) -> None:
    live = table(col("address", "struct<street:string,old_zip:string,gone:int>"))
    desired = table(
        col(
            "address",
            "struct<street:string not null,"
            "zip:string comment 'Postal code',country:string>",
        )
    )
    # `zip` is an add here, not a rename: no renamed_from was declared.
    changes = diff(desired, live)
    assert kinds(changes) == [
        ("set_nullable", "address.street"),
        ("add_column", "address.zip"),
        ("add_column", "address.country"),
        ("drop_column", "address.old_zip"),
        ("drop_column", "address.gone"),
    ]
    assert changes == snapshot


def test_nested_rename_uses_the_nested_path() -> None:
    from deltaplan.model.types import Field, Struct

    live = table(col("address", "struct<old_zip:string>"))
    desired = table(
        Field(
            "address",
            Struct((Field("zip", Primitive("string"), renamed_from="old_zip"),)),
        )
    )
    changes = diff(desired, live)
    assert kinds(changes) == [("rename_column", "address.zip")]
    assert changes[0].before == "old_zip"


def test_array_and_map_paths() -> None:
    live = table(
        col("lines", "array<struct<sku:string,qty:int>>"),
        col("by_code", "map<string,struct<n:int>>"),
    )
    desired = table(
        col("lines", "array<struct<sku:string,qty:bigint>>"),
        col("by_code", "map<string,struct<n:bigint>>"),
    )
    assert kinds(diff(desired, live)) == [
        ("change_type", "lines.element.qty"),
        ("change_type", "by_code.value.n"),
    ]


def test_a_kind_change_stops_the_descent() -> None:
    live = table(col("address", "struct<street:string>"))
    desired = table(col("address", "array<string>"))
    changes = diff(desired, live)
    assert kinds(changes) == [("change_type", "address")]


def test_column_order_is_only_diffed_when_asked() -> None:
    live = table(col("a", "int"), col("b", "int"))
    desired = table(col("b", "int"), col("a", "int"))
    assert diff(desired, live) == ()
    changes = diff(desired, live, compare_order=True)
    assert kinds(changes) == [("reorder_columns", "")]
    assert changes[0].before == ("a", "b")
    assert changes[0].after == ("b", "a")


def test_constraints(snapshot: SnapshotAssertion) -> None:
    live = table(
        col("id", "bigint", nullable=False),
        constraints=(PrimaryKey(("id",), "orders_pk"), Check("positive", "id > 0")),
    )
    desired = table(
        col("id", "bigint", nullable=False),
        col("sku", "string", nullable=False),
        constraints=(
            PrimaryKey(("id", "sku"), "orders_pk"),
            Check("positive", "id > 0"),
            Check("has_sku", "sku IS NOT NULL"),
        ),
    )
    changes = diff(desired, live)
    assert kinds(changes) == [
        ("add_column", "sku"),
        ("drop_constraint", ""),
        ("add_constraint", ""),
        ("add_constraint", ""),
    ]
    assert changes == snapshot


def test_a_changed_check_is_replaced() -> None:
    live = table(col("id", "bigint"), constraints=(Check("positive", "id > 0"),))
    desired = table(col("id", "bigint"), constraints=(Check("positive", "id >= 0"),))
    changes = diff(desired, live)
    assert kinds(changes) == [("drop_constraint", ""), ("add_constraint", "")]
    assert changes[0].before == Check("positive", "id > 0")


def test_nothing_unmodelled_is_diffed_away() -> None:
    live = table(
        col("id", "bigint"),
        properties=(
            ("deltaplan.managed", "true"),
            ("delta.logRetentionDuration", "interval 60 days"),
            ("delta.minReaderVersion", "3"),
        ),
        tags=(("owner", "someone-else"),),
        constraints=(PrimaryKey(("id",), "pk"), Check("by_hand", "id > 0")),
    )
    desired = table(col("id", "bigint"), properties=(("deltaplan.managed", "true"),))

    assert diff(desired, live) == ()
    assert unmanaged(desired, live) == (
        "property delta.logRetentionDuration",
        "tag owner",
        "primary key",
        "check constraint by_hand",
    )


def test_platform_defaults_and_internals_are_not_reported() -> None:
    """Every new table on a current workspace carries these (seen live,
    2026-09-18). A default that still holds its value, and Unity Catalog's own
    bookkeeping, aren't anyone's intent — but a default someone changed is."""
    live = table(
        col("id", "bigint"),
        properties=(
            ("delta.enableDeletionVectors", "true"),
            ("delta.enableRowTracking", "false"),  # changed by someone: report it
            ("delta.checkpointPolicy", "v2"),
            ("delta.parquet.format.version.afe.internal", "2.12.0"),
            ("delta.rowTracking.materializedRowIdColumnName", "_row-id-col-1"),
            ("io.unitycatalog.tableId", "4dcae1f4"),
        ),
    )
    desired = table(col("id", "bigint"))
    assert unmanaged(desired, live) == ("property delta.enableRowTracking",)
