"""The pull-request comment: what a reviewer sees on GitHub."""

from syrupy.assertion import SnapshotAssertion

from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY
from deltaplan.render.markdown import marker, render_markdown
from helpers import col, plan_against, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)

LIVE = table(
    col("amount", "decimal(10,2)"),
    col("address", "struct<street:string>"),
    col("cust_id", "string"),
    col("legacy_flag", "boolean"),
    name=NAME,
    properties=MANAGED,
)
DESIRED = table(
    col("amount", "decimal(18,2)"),
    col("address", "struct<street:string,zip:string>"),
    col("customer_ref", "string", renamed_from="cust_id"),
    name=NAME,
)


def design_example() -> Plan:
    _, plan = plan_against(DESIRED, LIVE, size_bytes=442_381_631_488)
    return plan


def test_the_design_documents_plan_as_a_comment(snapshot: SnapshotAssertion) -> None:
    rendered = render_markdown(design_example())
    assert rendered.startswith("<!-- deltaplan:plan:test -->\n")
    assert "**Plan: 0 add, 1 change, 0 destroy · 6 steps · 0 rewrites · 1 warning**" in (
        rendered
    )
    assert rendered == snapshot


def test_changes_are_a_diff_block_so_github_colours_them() -> None:
    rendered = render_markdown(design_example())
    block = rendered.split("```diff\n")[1].split("```")[0]
    assert block.splitlines() == [
        "~ amount  DECIMAL(10,2) → (18,2)",
        "~ address",
        "+   zip STRING",  # the marker leads, so the line is green
        "→ customer_ref (was cust_id)",
        "- legacy_flag",  # and this one red
    ]


def test_destruction_is_a_caution_alert() -> None:
    rendered = render_markdown(design_example())
    assert "> [!CAUTION]" in rendered
    assert "step 6 (DROP COLUMN on `sales.orders`)" in rendered
    assert "--allow-destructive" in rendered


def test_a_rewrite_is_a_warning_alert_with_its_size() -> None:
    live = table(col("amount", "decimal(10,2)"), name=NAME, properties=MANAGED)
    _, plan = plan_against(
        table(col("amount", "string"), name=NAME), live, size_bytes=442_381_631_488
    )
    rendered = render_markdown(plan)
    assert "> [!WARNING]" in rendered
    assert "Rewrites the data of `sales.orders` (412 GB)" in rendered
    assert "### 🟠 deltaplan plan" in rendered


def test_a_step_that_cannot_be_generated_is_called_out() -> None:
    live = table(col("a", "struct<b:int>"), name=NAME, properties=MANAGED)
    _, plan = plan_against(table(col("a", "array<int>"), name=NAME), live)
    rendered = render_markdown(plan)
    assert "> [!IMPORTANT]" in rendered
    assert "`apply` will refuse this plan" in rendered


def test_an_empty_plan() -> None:
    _, plan = plan_against(LIVE, LIVE)
    rendered = render_markdown(plan)
    assert "### ✅ deltaplan plan · `test`" in rendered
    assert "**No changes.** Live tables match your specs." in rendered
    assert "<details" not in rendered


def test_drift_has_its_own_marker() -> None:
    # So a plan comment and a drift comment on the same target don't overwrite
    # each other.
    rendered = render_markdown(design_example(), heading="drift")
    assert rendered.startswith(marker("drift", "test"))
    assert "deltaplan drift" in rendered


def test_the_sql_is_folded_away() -> None:
    rendered = render_markdown(design_example())
    assert "<details><summary>SQL</summary>" in rendered
    assert (
        "-- 6. DROP COLUMN\nALTER TABLE `main`.`sales`.`orders` DROP COLUMN" in rendered
    )


def test_a_long_plan_drops_the_sql_first() -> None:
    plan = design_example()
    full = render_markdown(plan)
    shorter = render_markdown(plan, limit=len(full) - 1)
    assert "```sql" not in shorter
    assert "SQL left out" in shorter
    assert "```diff" in shorter, "the changes are still there"


def test_a_very_long_plan_keeps_only_the_summary() -> None:
    rendered = render_markdown(design_example(), limit=200)
    assert "```diff" not in rendered
    assert "| `sales.orders` | ~ update | 6 |" in rendered
    assert "too long for a comment" in rendered


def test_pipes_cannot_break_a_table_cell() -> None:
    live = table(col("a", "int"), name=NAME, properties=MANAGED)
    desired = table(col("a", "int", comment="x | y"), name=NAME, comment="also | here")
    _, plan = plan_against(desired, live)
    rendered = render_markdown(plan)
    for row in [line for line in rendered.splitlines() if line.startswith("| ")]:
        # Every row has the same number of unescaped separators as the header.
        assert row.replace("\\|", "").count("|") == 5


def test_kept_and_unmanaged_tables_are_listed() -> None:
    from dataclasses import replace

    plan = replace(
        design_example(),
        orphaned_tables=("main.sales.retired",),
        unmanaged_tables=("main.sales.theirs",),
    )
    rendered = render_markdown(plan)
    assert "**Kept:** `sales.retired`" in rendered
    assert "Unmanaged, left untouched: `sales.theirs`" in rendered


def test_an_undo_hint_keeps_its_quoted_names() -> None:
    """The hint quotes every name in backticks; inside a one-backtick span the
    first of them ends the span and GitHub shows the rest as broken text."""
    from dataclasses import replace

    _, plan = plan_against(DESIRED, LIVE)
    hint = "RESTORE TABLE `main`.`sales`.`orders` TO VERSION AS OF 17"
    plan = replace(plan, steps=tuple(replace(s, undo_hint=hint) for s in plan.steps))
    assert f"undo: `` {hint} ``" in render_markdown(plan)


def test_a_dotted_property_is_not_inside_a_column() -> None:
    live = table(col("id", "bigint"), name=NAME)
    desired = table(
        col("id", "bigint"),
        name=NAME,
        properties=(("delta.enableChangeDataFeed", "true"),),
    )
    _, plan = plan_against(desired, live)
    rendered = render_markdown(plan)
    assert "```diff\n~ property delta.enableChangeDataFeed = 'true'\n```" in rendered
