"""A rewrite can lose data in two quiet ways; neither may be quiet.

1. It copies only the columns the spec lists. A column the spec removed goes with
   it — so the step that replaces the table is destructive, and `apply` refuses
   it without `--allow-destructive`, like any other drop.
2. It converts values with CAST. A value that doesn't convert errors under ANSI
   mode but becomes NULL without it. The staging step checks, before the original
   table is touched, that no row went missing and no converted column gained
   NULLs.
"""

import pytest

from deltaplan.executor import ExecutionError, ExecutionResult, Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector
from deltaplan.model.plan import Plan, fingerprint
from deltaplan.model.table import MANAGED_PROPERTY
from fake_warehouse import FakeWarehouse
from helpers import col, plan_against, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
LIVE = table(
    col("id", "bigint"),
    col("amount", "decimal(10,2)"),
    col("notes", "string"),
    name=NAME,
    properties=MANAGED,
)


def fingerprinted(fake: FakeWarehouse, plan: Plan) -> Plan:
    from dataclasses import replace

    return replace(
        plan, state_fingerprint=fingerprint(Introspector(fake).tables([NAME]).values())
    )


def apply(fake: FakeWarehouse, plan: Plan, **kwargs: bool) -> ExecutionResult:
    return Executor(
        fake, Introspector(fake), MemoryHistory(), new_run_id=lambda: "r"
    ).apply(fingerprinted(fake, plan), **kwargs)


def test_a_rewrite_that_drops_a_column_is_destructive() -> None:
    desired = table(col("id", "bigint"), col("amount", "string"), name=NAME)
    fake, plan = plan_against(desired, LIVE)
    replace_step = next(s for s in plan.steps if s.title == "REPLACE TABLE")
    assert replace_step.risk == "destructive"
    assert replace_step.warnings == ("drops notes along with the rewrite",)

    with pytest.raises(ExecutionError, match="--allow-destructive"):
        apply(fake, plan)
    assert fake.ddl == [], "nothing ran — not even the staging copy"


def test_a_nested_field_dropped_by_a_rewrite_counts_too() -> None:
    live = table(
        col("amount", "decimal(10,2)"),
        col("address", "struct<street:string,old:string>"),
        name=NAME,
        properties=MANAGED,
    )
    desired = table(
        col("amount", "string"), col("address", "struct<street:string>"), name=NAME
    )
    _, plan = plan_against(desired, live)
    replace_step = next(s for s in plan.steps if s.title == "REPLACE TABLE")
    assert replace_step.risk == "destructive"
    assert "address.old" in replace_step.warnings[0]


def test_a_rewrite_that_keeps_every_column_is_not_destructive() -> None:
    desired = table(
        col("id", "bigint"), col("amount", "string"), col("notes", "string"), name=NAME
    )
    _, plan = plan_against(desired, LIVE)
    assert [
        s.risk for s in plan.steps if s.title in {"STAGE rewritten data", "REPLACE TABLE"}
    ] == [
        "rewrite",
        "rewrite",
    ]


def test_staging_is_checked_for_lost_rows_and_values() -> None:
    desired = table(
        col("id", "bigint"),
        col("amount", "string"),  # converted
        col("notes", "string"),  # copied as-is: can't lose anything
        col("added", "int"),  # new: NULL by design
        name=NAME,
    )
    _, plan = plan_against(desired, LIVE)
    stage = plan.steps[0]
    assert stage.postcheck == (
        "SELECT (\n"
        "  (SELECT count(*) FROM `main`.`sales`.`orders__deltaplan_rewrite`) = "
        "(SELECT count(*) FROM `main`.`sales`.`orders`)\n"
        "  AND (SELECT count_if(`amount` IS NULL) FROM "
        "`main`.`sales`.`orders__deltaplan_rewrite`) <= "
        "(SELECT count_if(`amount` IS NULL) FROM `main`.`sales`.`orders`)\n"
        ") AS ok"
    )


def test_a_renamed_conversion_compares_against_the_old_name() -> None:
    from deltaplan.model.types import Field, Primitive

    desired = table(
        col("id", "bigint"),
        Field("total", Primitive("string"), renamed_from="amount"),
        col("notes", "string"),
        name=NAME,
    )
    _, plan = plan_against(desired, LIVE)
    assert (
        "count_if(`total` IS NULL) FROM `main`.`sales`.`orders__deltaplan_rewrite`"
        in (plan.steps[0].postcheck or "")
    )
    assert "count_if(`amount` IS NULL) FROM `main`.`sales`.`orders`)" in (
        plan.steps[0].postcheck or ""
    )


def test_lost_values_stop_the_run_before_the_table_is_touched() -> None:
    desired = table(
        col("id", "bigint"), col("amount", "string"), col("notes", "string"), name=NAME
    )
    fake, plan = plan_against(desired, LIVE)
    fake.postcheck_ok = False  # the conversion NULLed something
    result = apply(fake, plan)

    assert not result.ok
    assert result.failed == 1
    assert "is untouched" in (result.error or "")
    assert f"{NAME}__deltaplan_rewrite" in fake.tables, "kept to inspect"
    live = fake.tables[NAME]
    assert live.column("amount") == LIVE.column("amount"), "the original is as it was"
    assert not any(
        s.startswith("CREATE OR REPLACE TABLE `main`.`sales`.`orders`\n")
        for s in fake.ddl
    )


# ---------------------------------------------------------------------------
# what a rewrite must carry across
# ---------------------------------------------------------------------------
#
# The replacement table is built from a query, so it has only the properties and
# constraints deltaplan gives it. Anything the spec doesn't declare has to be
# handed across explicitly, or replacing the table diffs it away.


def test_properties_nobody_declared_survive_a_rewrite() -> None:
    from helpers import run

    live = table(
        col("id", "bigint"),
        col("amount", "decimal(10,2)"),
        name=NAME,
        properties=(
            *MANAGED,
            # How far back RESTORE can reach — the last thing to lose in a rewrite.
            ("delta.logRetentionDuration", "interval 90 days"),
            ("delta.minReaderVersion", "3"),  # bookkeeping: not carried
        ),
    )
    desired = table(col("id", "bigint"), col("amount", "string"), name=NAME)
    fake, plan = plan_against(desired, live)
    replace_sql = next(s.sql for s in plan.steps if s.title == "REPLACE TABLE") or ""
    assert "'delta.logRetentionDuration' = 'interval 90 days'" in replace_sql
    assert "minReaderVersion" not in replace_sql

    run(plan, fake)
    after = fake.tables[NAME]
    assert after.properties_map()["delta.logRetentionDuration"] == "interval 90 days"


def test_constraints_nobody_declared_survive_a_rewrite() -> None:
    from deltaplan.model.table import Check, PrimaryKey
    from helpers import run

    live = table(
        col("id", "bigint", nullable=False),
        col("amount", "decimal(10,2)"),
        name=NAME,
        properties=MANAGED,
        constraints=(PrimaryKey(("id",), "orders_pk"), Check("positive", "id > 0")),
    )
    desired = table(
        col("id", "bigint", nullable=False), col("amount", "string"), name=NAME
    )
    fake, plan = plan_against(desired, live)
    run(plan, fake)
    after = fake.tables[NAME]
    assert after.primary_key() == PrimaryKey(("id",), "orders_pk")
    assert after.checks() == (Check("positive", "id > 0"),)


def test_tags_grants_and_an_owner_survive_a_rewrite_without_a_step() -> None:
    """A replace keeps them (verified live), so the plan doesn't say it will put
    them back — but they have to still be there afterwards."""
    from dataclasses import replace as replace_fields

    from deltaplan.model.table import Grant
    from deltaplan.model.types import Field, Primitive
    from helpers import run

    live = replace_fields(
        table(
            col("id", "bigint"),
            Field("amount", Primitive("string"), tags=(("pii", "no"),)),
            name=NAME,
            properties=MANAGED,
            tags=(("domain", "sales"), ("unmanaged", "yes")),
            grants=(Grant("analysts", ("SELECT",)), Grant("finance", ("MODIFY",))),
        ),
        owner="data-eng",
    )
    desired = table(
        col("id", "bigint"),
        Field("amount", Primitive("bigint"), tags=(("pii", "no"),)),
        name=NAME,
        tags=(("domain", "sales"),),
        grants=(Grant("analysts", ("SELECT",)),),
    )
    fake, plan = plan_against(desired, live)
    titles = [step.title for step in plan.steps]
    assert "SET TAGS" not in titles
    assert "SET COLUMN TAGS" not in titles
    assert not any(title.startswith("GRANT") for title in titles)
    assert "SET OWNER" not in titles

    run(plan, fake)
    after = fake.tables[NAME]
    assert dict(after.tags) == {"domain": "sales", "unmanaged": "yes"}
    assert dict(after.columns[1].tags) == {"pii": "no"}
    assert {g.principal for g in after.grants} == {"analysts", "finance"}
    assert after.owner == "data-eng"


def test_a_renamed_column_gets_its_tags_back() -> None:
    """They stay behind on the old name, so these the plan does put back."""
    from deltaplan.model.types import Field, Primitive
    from helpers import run

    live = table(
        col("id", "bigint"),
        Field("old_name", Primitive("string"), tags=(("pii", "name"), ("team", "crm"))),
        name=NAME,
        properties=MANAGED,
    )
    desired = table(
        col("id", "string"),  # forces the rewrite
        Field(
            "new_name",
            Primitive("string"),
            tags=(("pii", "name"),),
            renamed_from="old_name",
        ),
        name=NAME,
    )
    fake, plan = plan_against(desired, live)
    assert [s.title for s in plan.steps].count("SET COLUMN TAGS") == 2, "declared + not"

    run(plan, fake)
    after = fake.tables[NAME]
    assert dict(after.columns[1].tags) == {"pii": "name", "team": "crm"}
