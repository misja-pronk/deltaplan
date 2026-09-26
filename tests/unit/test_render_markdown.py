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


def test_the_object_is_shown_as_it_is_beside_what_it_becomes() -> None:
    """The same comparison the page shows, in the columns its *changes only*
    lens shows: the sides for whoever wrote the spec, the sentence for whoever
    approves it."""
    rendered = render_markdown(design_example())
    rows = [line for line in rendered.splitlines() if line.startswith("| ~ |")]
    assert "| ~ | `amount` | `decimal(10,2)` | `decimal(18,2)` |" in rows[0]
    assert rows[0].endswith("DECIMAL(10,2) → (18,2) |")
    assert "| + | `address.zip` | — | `string` |" in rendered
    assert "| - | `legacy_flag` | `boolean` | — |" in rendered
    assert "<sub>1 row unchanged</sub>" in rendered, "and the rest are counted"


def test_a_rename_reads_as_one_column_gone_and_one_arrived() -> None:
    """Rows are aligned by meaning, so a rename is two of them — and the
    sentence on each says which."""
    rendered = render_markdown(design_example())
    assert "| - | `cust_id` | `string` | — | dropped |" in rendered
    arrived = [line for line in rendered.splitlines() if "`customer_ref`" in line]
    assert "renamed from cust_id" in arrived[0]


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
    assert "| what | now | after |" in shorter, "the comparison is still there"


def test_a_longer_plan_falls_back_to_the_list_of_changes() -> None:
    """A table of rows is longer than a list of changes, so there is a rung
    between the comparison and giving up on the objects entirely."""
    plan = design_example()
    without_sql = render_markdown(plan, limit=len(render_markdown(plan)) - 1)
    shorter = render_markdown(plan, limit=len(without_sql) - 1)
    assert "| what | now | after |" not in shorter
    assert "```diff" in shorter
    assert shorter.split("```diff\n")[1].split("```")[0].splitlines() == [
        "~ amount  DECIMAL(10,2) → (18,2)",
        "~ address",
        "+   zip STRING",  # the marker leads, so the line is green
        "→ customer_ref (was cust_id)",
        "- legacy_flag",  # and this one red
    ]
    assert "too long for a comment" in shorter


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
    # Every row has as many unescaped separators as the table it is in — which
    # the `|---|` line under each header settles.
    lines = rendered.splitlines()

    def separator(line: str) -> bool:
        return line.startswith("|") and set(line) <= set("|-: ")

    expected: int | None = None
    for index, line in enumerate(lines):
        if separator(line):
            expected = line.count("|")
        elif line.startswith("| ") and not separator(lines[index + 1]):
            assert expected is not None, line
            assert line.replace("\\|", "").count("|") == expected, line


def test_every_change_the_differ_made_reaches_the_comment() -> None:
    """The same promise the page makes: nothing the plan says is lost on the way
    to the pull request. The comment's cells are shorter than the page's rows, so
    a change is accounted for by its path or by its sentence."""
    from dataclasses import replace

    from deltaplan.introspect import Introspector
    from deltaplan.planning import plan_tables
    from deltaplan.render.labels import describe
    from fake_warehouse import FakeWarehouse

    live = replace(LIVE, partitioned_by=("region",), properties=MANAGED)
    desired = replace(
        table(
            col("amount", "decimal(18,2)", comment="Net"),
            col("address", "struct<street:string,zip:string>"),
            col("customer_ref", "string", renamed_from="cust_id"),
            col("segment", "string"),
            name=NAME,
        ),
        comment="Orders",
        cluster_by=("amount",),
        tags=(("domain", "sales"),),
    )
    plan = plan_tables(
        [desired], Introspector(FakeWarehouse.of(live)), target="dev", tool_version="0"
    )
    rendered = render_markdown(plan)
    rows = "\n".join(line for line in rendered.splitlines() if line.startswith("| "))
    for change in plan.diffs[0].changes:
        label = describe(change)[1]
        path = change.path or change.kind
        assert path.split(".")[-1] in rows or label in rows, (
            f"{change.kind} at {change.path!r} went missing"
        )


def test_a_plan_with_hundreds_of_tables_still_fits_in_a_comment() -> None:
    """The ladder's last rung. A comparison is longer than a change list, which
    is longer than a name — and GitHub refuses a comment over 65,536
    characters, so something has to give and say that it did."""
    from dataclasses import replace

    one = design_example()
    many = replace(
        one,
        diffs=tuple(
            replace(one.diffs[0], table=f"main.sales.orders_{index:03d}")
            for index in range(300)
        ),
    )
    rendered = render_markdown(many)
    assert len(rendered) <= 60_000
    assert "too long for a comment" in rendered
    assert "`sales.orders_299`" in rendered, "every table is still named"


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
    assert "| + | `properties` |" in rendered
    assert "delta.enableChangeDataFeed = 'true'" in rendered
    assert "`id`" not in rendered.split("| + | `properties` |")[1].split("\n")[0]
