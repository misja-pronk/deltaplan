"""The terminal view: the layout drawn in the design document."""

from syrupy.assertion import SnapshotAssertion

from deltaplan.differ import diff, unmanaged
from deltaplan.model.plan import Plan, TableDiff, TableFacts
from deltaplan.model.table import Check, PrimaryKey, Table
from deltaplan.planner import build_plan
from deltaplan.render.labels import human_bytes
from deltaplan.render.rich import plan_text
from helpers import col, table

TABLE = "main.sales.orders"


def plan_for(
    desired: Table,
    actual: Table | None,
    *,
    facts: TableFacts | None = None,
    compare_order: bool = False,
) -> Plan:
    live_facts = facts or TableFacts(TABLE, exists=actual is not None)
    return build_plan(
        [
            TableDiff(
                TABLE,
                diff(desired, actual, compare_order=compare_order),
                live_facts,
                unmanaged(desired, actual) if actual is not None else (),
            )
        ],
        target="dev",
        tool_version="0.1.0",
        spec_hash="spec",
        state_fingerprint="live",
    )


def test_the_design_documents_plan_output(snapshot: SnapshotAssertion) -> None:
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
    rendered = plan_text(
        plan_for(desired, live, facts=TableFacts(TABLE, size_bytes=442_381_631_488))
    )
    # The shape from the design document, line for line.
    assert "sales.orders   ~ update  (412 GB)" in rendered
    assert "  ~ amount  DECIMAL(10,2) → (18,2)" in rendered
    assert "    1. enable typeWidening" in rendered
    assert "  ~ address" in rendered
    assert "    + zip STRING" in rendered
    assert "    3. ADD COLUMN address.zip" in rendered
    assert "  → customer_ref (was cust_id)" in rendered
    assert "⚠ breaks streaming readers" in rendered
    assert "  - legacy_flag" in rendered
    assert "[destructive]" in rendered
    assert (
        "Plan: 0 add, 1 change, 0 destroy · 6 steps · 0 rewrites · 1 warning" in rendered
    )
    assert rendered == snapshot


def test_create_table_output(snapshot: SnapshotAssertion) -> None:
    desired = table(
        col("order_id", "bigint", nullable=False),
        col("amount", "decimal(18,2)"),
        comment="Order facts",
        cluster_by=("order_id",),
        tags=(("domain", "sales"),),
        constraints=(PrimaryKey(("order_id",)), Check("positive", "amount > 0")),
    )
    rendered = plan_text(plan_for(desired, None))
    assert "sales.orders   + create" in rendered
    assert "Plan: 1 add, 0 change, 0 destroy · 3 steps" in rendered
    assert rendered == snapshot


def test_rewrites_show_their_size_and_why(snapshot: SnapshotAssertion) -> None:
    live = table(col("amount", "string"))
    desired = table(col("amount", "bigint"))
    rendered = plan_text(
        plan_for(
            desired,
            live,
            facts=TableFacts(TABLE, size_bytes=442_381_631_488, delta_version=17),
        )
    )
    assert "[rewrite]" in rendered
    assert "(412 GB)" in rendered
    assert "1 rewrite " in rendered
    assert rendered == snapshot


def test_table_level_changes_and_unmanaged_report(snapshot: SnapshotAssertion) -> None:
    live = table(
        col("id", "int"),
        comment="old",
        properties=(("delta.logRetentionDuration", "interval 60 days"),),
        tags=(("owner", "someone-else"),),
    )
    desired = table(
        col("id", "int"),
        comment="new",
        cluster_by=("id",),
        properties=(("delta.enableChangeDataFeed", "true"),),
    )
    rendered = plan_text(plan_for(desired, live))
    assert "  ~ comment" in rendered
    assert "  ~ cluster_by [id]" in rendered
    assert "property delta.logRetentionDuration — unmanaged, left untouched" in rendered
    assert "tag owner — unmanaged, left untouched" in rendered
    assert rendered == snapshot


def test_an_empty_plan_says_so() -> None:
    live = table(col("id", "int"))
    rendered = plan_text(plan_for(live, live))
    assert "No changes. Live tables match your specs." in rendered


def test_human_bytes() -> None:
    assert human_bytes(None) is None
    assert human_bytes(0) == "0 B"
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(442_381_631_488) == "412 GB"
    assert human_bytes(1_125_899_906_842_624) == "1024 TB"


def test_step_numbers_line_up_past_nine() -> None:
    live = table(col("id", "bigint"), name=TABLE)
    desired = table(col("id", "bigint"), *(col(f"c{i}", "int") for i in range(11)))
    text = plan_text(plan_for(desired, live))
    steps = [line for line in text.splitlines() if "ADD COLUMN" in line]
    assert {line.index("ADD COLUMN") for line in steps} == {8}, text
    assert steps[0].startswith("     1. ") and steps[9].startswith("    10. ")


def test_steps_read_in_order_down_the_page() -> None:
    """A grant comes after the columns' changes in the plan, so it is shown
    there too — not hoisted up with the table's tags, out of step order."""
    from deltaplan.model.table import Grant

    live = table(col("id", "bigint"), col("email", "string"), name=TABLE)
    email = col("email", "string")
    desired = table(
        col("id", "bigint"),
        type(email)(email.name, email.type, tags=(("pii", "email"),)),
        tags=(("domain", "crm"),),
        grants=(Grant("analysts", ("SELECT",)),),
    )
    text = plan_text(plan_for(desired, live))
    numbers = [line.split(".")[0].strip() for line in text.splitlines() if ". " in line]
    assert numbers == ["1", "2", "3"], text


def test_constraints_say_what_they_are() -> None:
    from deltaplan.model.table import ForeignKey

    live = table(col("id", "bigint", nullable=False), col("c", "bigint"), name=TABLE)
    desired = table(
        col("id", "bigint", nullable=False),
        col("c", "bigint"),
        constraints=(
            PrimaryKey(("id",)),
            Check("positive", "id > 0"),
            ForeignKey(("c",), "main.sales.customers", ("id",), "orders_c_fk"),
        ),
    )
    text = plan_text(plan_for(desired, live))
    assert "+ constraint PRIMARY KEY (id)\n" in text
    assert "+ constraint CHECK (id > 0) positive\n" in text
    assert "+ constraint FOREIGN KEY (c) → sales.customers (id) orders_c_fk\n" in text


def test_a_before_hook_is_shown_before_the_changes() -> None:
    from dataclasses import replace

    from deltaplan.model.table import Hooks

    live = table(col("id", "bigint"), name=TABLE)
    desired = replace(
        table(col("id", "bigint", nullable=False)),
        hooks=Hooks(before="DELETE FROM t WHERE id IS NULL", after="OPTIMIZE t"),
    )
    plan = build_plan(
        [
            TableDiff(
                TABLE,
                diff(desired, live),
                TableFacts(TABLE, exists=True),
                desired=desired,
                live=live,
            )
        ],
        target="dev",
        tool_version="0.1.0",
        spec_hash="spec",
        state_fingerprint="live",
    )
    text = plan_text(plan)
    numbers = [line.split(".")[0].strip() for line in text.splitlines() if ". " in line]
    assert numbers == ["1", "2", "3"], text
