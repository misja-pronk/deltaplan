"""The planner: changes become ordered, risk-classified, prerequisite-aware steps.

Databricks references behind the behaviour asserted here:
  column mapping  https://docs.databricks.com/aws/en/delta/column-mapping
  type widening   https://docs.databricks.com/aws/en/delta/type-widening
  ALTER TABLE     https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table
  constraints     https://docs.databricks.com/aws/en/tables/constraints
"""

import pytest
from syrupy.assertion import SnapshotAssertion

from deltaplan.differ import diff
from deltaplan.model.plan import Plan, Step, TableDiff, TableFacts
from deltaplan.model.table import Check, PrimaryKey, Table
from deltaplan.planner import build_plan, widens
from deltaplan.typeparser import parse_type
from helpers import col, table

TABLE = "main.sales.orders"


def plan_of(
    desired: Table,
    actual: Table | None,
    *,
    facts: TableFacts | None = None,
    compare_order: bool = False,
) -> Plan:
    changes = diff(desired, actual, compare_order=compare_order)
    return build_plan(
        [
            TableDiff(
                TABLE, changes, facts or TableFacts(TABLE, exists=actual is not None)
            )
        ],
        target="dev",
        tool_version="0.1.0",
        spec_hash="spec",
        state_fingerprint="live",
    )


def outline(plan: Plan) -> list[tuple[int, str, str]]:
    return [(step.id, step.title, step.risk) for step in plan.steps]


def only(plan: Plan) -> Step:
    assert len(plan.steps) == 1, outline(plan)
    return plan.steps[0]


# ---------------------------------------------------------------------------
# type widening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("tinyint", "smallint"),
        ("smallint", "int"),
        ("int", "bigint"),
        ("tinyint", "bigint"),
        ("float", "double"),
        ("int", "double"),
        ("date", "timestamp_ntz"),
        ("decimal(10,2)", "decimal(18,2)"),
        ("decimal(10,2)", "decimal(20,4)"),
        ("int", "decimal(12,2)"),
    ],
)
def test_supported_widenings(before: str, after: str) -> None:
    assert widens(parse_type(before), parse_type(after))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("bigint", "int"),  # narrowing
        ("string", "bigint"),
        ("double", "float"),
        ("timestamp_ntz", "date"),
        ("decimal(18,2)", "decimal(10,2)"),  # precision loss
        ("decimal(10,2)", "decimal(10,4)"),  # integer digits lost
        ("int", "decimal(5,2)"),  # 10 integer digits don't fit in 3
        ("int", "int"),
        ("struct<a:int>", "struct<a:bigint>"),  # handled field by field, not here
    ],
)
def test_unsupported_widenings(before: str, after: str) -> None:
    assert not widens(parse_type(before), parse_type(after))


# ---------------------------------------------------------------------------
# prerequisites
# ---------------------------------------------------------------------------


def test_a_rename_inserts_column_mapping_first() -> None:
    live = table(col("cust_id", "string"))
    desired = table(col("customer_ref", "string", renamed_from="cust_id"))
    plan = plan_of(desired, live)
    assert outline(plan) == [
        (1, "enable columnMapping", "feature"),
        (2, "RENAME COLUMN", "meta"),
    ]
    assert plan.steps[0].warnings == (
        "breaks streaming readers — they must be restarted from scratch",
    )
    assert "delta.columnMapping.mode" in (plan.steps[0].sql or "")
    assert plan.steps[1].sql == (
        "ALTER TABLE `main`.`sales`.`orders` RENAME COLUMN `cust_id` TO `customer_ref`"
    )


def test_column_mapping_is_skipped_when_the_table_already_has_it() -> None:
    live = table(col("cust_id", "string"))
    desired = table(col("customer_ref", "string", renamed_from="cust_id"))
    facts = TableFacts(TABLE, properties=(("delta.columnMapping.mode", "name"),))
    assert outline(plan_of(desired, live, facts=facts)) == [(1, "RENAME COLUMN", "meta")]


def test_prerequisites_are_planned_once_per_table() -> None:
    live = table(col("cust_id", "string"), col("legacy", "int"), col("keep", "int"))
    desired = table(
        col("customer_ref", "string", renamed_from="cust_id"), col("keep", "int")
    )
    plan = plan_of(desired, live)
    assert [title for _, title, _ in outline(plan)].count("enable columnMapping") == 1


def test_widening_inserts_type_widening_first() -> None:
    live = table(col("amount", "decimal(10,2)"))
    desired = table(col("amount", "decimal(18,2)"))
    plan = plan_of(desired, live)
    assert outline(plan) == [
        (1, "enable typeWidening", "feature"),
        (2, "ALTER COLUMN TYPE", "meta"),
    ]
    assert plan.steps[1].sql == (
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `amount` TYPE DECIMAL(18,2)"
    )


def test_nested_widening_uses_the_nested_path() -> None:
    live = table(col("address", "struct<zip:int>"))
    desired = table(col("address", "struct<zip:bigint>"))
    plan = plan_of(desired, live)
    assert plan.steps[1].sql == (
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `address`.`zip` TYPE BIGINT"
    )


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_an_unsupported_type_change_is_a_rewrite() -> None:
    live = table(col("amount", "string"))
    desired = table(col("amount", "bigint"))
    facts = TableFacts(TABLE, size_bytes=442_381_631_488, delta_version=17)
    step = only(plan_of(desired, live, facts=facts))
    assert step.risk == "rewrite"
    assert step.sql is None  # rewrites are classified in v1, generated in milestone 3
    assert step.est_bytes == 442_381_631_488
    assert step.undo_hint == "RESTORE TABLE `main`.`sales`.`orders` TO VERSION AS OF 17"


def test_a_map_key_change_is_always_a_rewrite() -> None:
    # Even a widening of the key type: the key cannot be altered in place.
    live = table(col("by_code", "map<int,string>"))
    desired = table(col("by_code", "map<bigint,string>"))
    step = only(plan_of(desired, live))
    assert step.risk == "rewrite"
    assert step.note is not None and "map key" in step.note


def test_a_kind_change_is_a_rewrite() -> None:
    live = table(col("address", "struct<street:string>"))
    desired = table(col("address", "array<string>"))
    assert only(plan_of(desired, live)).risk == "rewrite"


def test_not_null_on_a_nested_field_is_a_rewrite() -> None:
    # TODO(verify): no runtime we know of can alter a nested field's nullability
    # in place; planned as a rewrite rather than a step that fails at apply time.
    live = table(col("address", "struct<zip:string>"))
    desired = table(col("address", "struct<zip:string not null>"))
    step = only(plan_of(desired, live))
    assert step.risk == "rewrite"
    assert step.note is not None and "nested field" in step.note


def test_dropping_a_column_is_destructive_and_needs_column_mapping() -> None:
    live = table(col("keep", "int"), col("legacy_flag", "boolean"))
    desired = table(col("keep", "int"))
    plan = plan_of(desired, live)
    assert outline(plan) == [
        (1, "enable columnMapping", "feature"),
        (2, "DROP COLUMN", "destructive"),
    ]
    assert plan.highest_risk == "destructive"


def test_adding_a_not_null_column_is_two_steps_and_says_why() -> None:
    live = table(col("id", "bigint"))
    desired = table(col("id", "bigint"), col("region", "string", nullable=False))
    plan = plan_of(desired, live)
    assert outline(plan) == [
        (1, "ADD COLUMN region", "meta"),
        (2, "SET NOT NULL", "meta"),
    ]
    assert plan.steps[0].sql == (
        "ALTER TABLE `main`.`sales`.`orders` ADD COLUMNS (`region` STRING)"
    )
    assert plan.steps[1].warnings == (
        "a new column is NULL for every existing row, so this fails until they are "
        "backfilled",
    )
    assert plan.steps[1].precheck == (
        "SELECT count(*) AS nulls FROM `main`.`sales`.`orders` WHERE `region` IS NULL"
    )


def test_adding_a_nested_field_is_metadata_only() -> None:
    live = table(col("address", "struct<street:string>"))
    desired = table(col("address", "struct<street:string,zip:string comment 'Postal'>"))
    step = only(plan_of(desired, live))
    assert step.risk == "meta"
    assert step.sql == (
        "ALTER TABLE `main`.`sales`.`orders` "
        "ADD COLUMNS (`address`.`zip` STRING COMMENT 'Postal')"
    )


# ---------------------------------------------------------------------------
# table-level SQL
# ---------------------------------------------------------------------------


def test_create_table(snapshot: SnapshotAssertion) -> None:
    desired = table(
        col("order_id", "bigint", nullable=False, comment="Surrogate key"),
        col("order_date", "date", nullable=False),
        col("address", "struct<street:string,zip:string>"),
        comment="Order facts",
        cluster_by=("order_date",),
        properties=(("delta.enableChangeDataFeed", "true"),),
        tags=(("domain", "sales"),),
        constraints=(PrimaryKey(("order_id",)), Check("positive", "order_id > 0")),
    )
    plan = plan_of(desired, None)
    assert outline(plan) == [
        (1, "CREATE TABLE orders", "meta"),
        (2, "ADD CONSTRAINT positive CHECK", "meta"),
        (3, "SET TAGS", "meta"),
    ]
    # Every created table is marked managed — that marker is what makes it a drop
    # candidate later. Nothing else ever becomes one.
    assert "'deltaplan.managed' = 'true'" in (plan.steps[0].sql or "")
    assert plan.summary.add == 1
    assert plan.steps[0].sql == snapshot


def test_table_metadata_sql() -> None:
    live = table(col("id", "int"), comment="old")
    desired = table(
        col("id", "int"),
        comment="new",
        cluster_by=("id",),
        properties=(("delta.enableChangeDataFeed", "true"),),
        tags=(("domain", "sales"),),
    )
    plan = plan_of(desired, live)
    assert [step.sql for step in plan.steps] == [
        "COMMENT ON TABLE `main`.`sales`.`orders` IS 'new'",
        "ALTER TABLE `main`.`sales`.`orders` CLUSTER BY (`id`)",
        "ALTER TABLE `main`.`sales`.`orders` SET TBLPROPERTIES "
        "('delta.enableChangeDataFeed' = 'true')",
        "ALTER TABLE `main`.`sales`.`orders` SET TAGS ('domain' = 'sales')",
    ]


def test_dropping_a_comment_sets_it_to_null() -> None:
    live = table(col("id", "int", comment="old"))
    desired = table(col("id", "int"))
    assert only(plan_of(desired, live)).sql == (
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `id` COMMENT NULL"
    )


def test_constraints_are_replaced_not_altered() -> None:
    live = table(
        col("id", "bigint", nullable=False),
        constraints=(Check("positive", "id > 0"),),
    )
    desired = table(
        col("id", "bigint", nullable=False),
        constraints=(Check("positive", "id >= 0"),),
    )
    plan = plan_of(desired, live)
    assert [step.sql for step in plan.steps] == [
        "ALTER TABLE `main`.`sales`.`orders` DROP CONSTRAINT `positive`",
        "ALTER TABLE `main`.`sales`.`orders` ADD CONSTRAINT `positive` CHECK (id >= 0)",
    ]


def test_reordering_walks_the_columns_into_place() -> None:
    live = table(col("a", "int"), col("b", "int"), col("c", "int"))
    desired = table(col("c", "int"), col("a", "int"), col("b", "int"))
    plan = plan_of(desired, live, compare_order=True)
    assert [step.sql for step in plan.steps] == [
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `c` FIRST",
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `a` AFTER `c`",
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `b` AFTER `a`",
    ]


def test_identifiers_are_always_quoted() -> None:
    live = table(col("odd name", "int"), name="main.sales.odd table")
    desired = table(
        col("odd name", "bigint"), col("new`col", "int"), name="main.sales.odd table"
    )
    plan = plan_of(desired, live)
    statements = " ".join(step.sql or "" for step in plan.steps)
    assert "`odd table`" in statements
    assert "`odd name`" in statements
    assert "`new``col`" in statements


# ---------------------------------------------------------------------------
# the design document's worked example
# ---------------------------------------------------------------------------


def test_the_design_documents_example_plan(snapshot: SnapshotAssertion) -> None:
    live = table(
        col("amount", "decimal(10,2)"),
        col("address", "struct<street:string>"),
        col("cust_id", "string"),
        col("legacy_flag", "boolean"),
    )
    desired = table(
        col("amount", "decimal(18,2)"),
        col("address", "struct<street:string,zip:string>"),
        col("customer_ref", "string", renamed_from="cust_id"),
    )
    plan = plan_of(desired, live, facts=TableFacts(TABLE, size_bytes=442_381_631_488))
    assert outline(plan) == [
        (1, "enable typeWidening", "feature"),
        (2, "ALTER COLUMN TYPE", "meta"),
        (3, "ADD COLUMN address.zip", "meta"),
        (4, "enable columnMapping", "feature"),
        (5, "RENAME COLUMN", "meta"),
        (6, "DROP COLUMN", "destructive"),
    ]
    assert str(plan.summary) == (
        "Plan: 0 add, 1 change, 0 destroy · 6 steps · 0 rewrites · 1 warning"
    )
    assert plan.steps == snapshot


def test_an_empty_diff_plans_nothing() -> None:
    plan = build_plan(
        [TableDiff(TABLE, ())],
        target="dev",
        tool_version="0.1.0",
        spec_hash="spec",
        state_fingerprint="live",
    )
    assert plan.empty
    assert str(plan.summary) == (
        "Plan: 0 add, 0 change, 0 destroy · 0 steps · 0 rewrites · 0 warnings"
    )
