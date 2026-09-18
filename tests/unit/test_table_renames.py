"""Renaming a table: `renamed_from:` on the table, as on a column.

Without the hint, a new name looks like a new table and the old one like an
orphan — which in a strict schema means creating an empty table and dropping the
full one. With it, the plan is one `ALTER TABLE … RENAME TO`, first, and the
table's other changes follow under the new name.

https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table
"""

from dataclasses import replace
from pathlib import Path

import pytest

from deltaplan.differ import is_applied
from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector
from deltaplan.loader import load_table, validate_spec
from deltaplan.model.change import Change
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, Hooks, Table
from deltaplan.planning import plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeWarehouse
from helpers import col, run, table

MANAGED = ((MANAGED_PROPERTY, "true"),)
OLD = table(
    col("id", "bigint"),
    col("amount", "string"),
    name="main.sales.order_facts",
    properties=MANAGED,
)
NEW = replace(
    table(col("id", "bigint"), col("amount", "string"), name="main.sales.orders"),
    renamed_from="main.sales.order_facts",
)


def planned(specs: list[Table], fake: FakeWarehouse, *, strict: bool = False) -> Plan:
    return plan_tables(
        specs,
        Introspector(fake),
        target="test",
        tool_version="0.1.0",
        mode_for=lambda _schema: "strict" if strict else "additive",
    )


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written", ["order_facts", "main.sales.order_facts", "Main.Sales.Order_Facts"]
)
def test_renamed_from_names_the_old_table(tmp_path: Path, written: str) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: main.sales.orders\n"
        f"renamed_from: {written}\n"
        "columns: [{name: id, type: bigint}]\n"
    )
    assert load_table(path).renamed_from == "main.sales.order_facts"


@pytest.mark.parametrize(
    ("renamed_from", "message"),
    [
        ("main.archive.order_facts", "must be in the same schema"),
        ("other.sales.order_facts", "must be in the same schema"),
        ("sales.order_facts", "must be the old table's name"),
        ("main.sales.orders", "names the table itself"),
    ],
)
def test_a_rename_stays_in_its_schema(renamed_from: str, message: str) -> None:
    spec = replace(NEW, renamed_from=renamed_from)
    errors = [d.message for d in validate_spec(spec, "orders.yml")]
    assert any(message in e for e in errors), errors


# ---------------------------------------------------------------------------
# planning and applying
# ---------------------------------------------------------------------------


def test_a_rename_is_one_step_then_nothing() -> None:
    fake = FakeWarehouse.of(OLD, sizes={OLD.name: 1_000})
    plan = planned([NEW], fake)
    assert [(s.title, s.risk) for s in plan.steps] == [("RENAME TABLE", "meta")]
    step = plan.steps[0]
    assert step.sql == (
        "ALTER TABLE `main`.`sales`.`order_facts` RENAME TO `main`.`sales`.`orders`"
    )
    assert step.undo_hint == (
        "ALTER TABLE `main`.`sales`.`orders` RENAME TO `main`.`sales`.`order_facts`"
    )
    assert "reads main.sales.order_facts by name" in step.warnings[0]
    assert plan.summary.change == 1 and plan.summary.add == 0

    run(plan, fake)
    assert set(fake.tables) == {"main.sales.orders"}
    assert fake.sizes == {"main.sales.orders": 1_000}, "what the fake keeps moves too"

    after = planned([NEW], fake)
    assert after.empty
    assert after.diffs[0].notes == (
        "renamed_from 'order_facts' has done its job — it can be removed",
    )


def test_the_other_changes_follow_under_the_new_name() -> None:
    fake = FakeWarehouse.of(OLD)
    wider = replace(
        table(*NEW.columns, col("region", "string"), name=NEW.name, comment="Orders"),
        renamed_from=NEW.renamed_from,
    )
    plan = planned([wider], fake)
    assert [s.title for s in plan.steps][0] == "RENAME TABLE"
    for step in plan.steps[1:]:
        assert step.sql is not None and "`orders`" in step.sql, step.sql
        assert "order_facts" not in step.sql
    run(plan, fake)
    assert planned([wider], fake).empty


def test_the_executor_sees_the_table_where_it_was() -> None:
    """The staleness check reads live state again at apply time. For a table
    being renamed that must be the old name — the new one doesn't exist yet."""
    fake = FakeWarehouse.of(OLD)
    plan = planned([NEW], fake)
    result = Executor(
        runner=fake,
        introspector=Introspector(fake),
        history=MemoryHistory(),
        new_run_id=lambda: "run1",
    ).apply(plan)
    assert result.ok, result.error
    assert "main.sales.orders" in fake.tables


def test_a_strict_schema_renames_rather_than_drops() -> None:
    fake = FakeWarehouse.of(OLD)
    plan = planned([NEW], fake, strict=True)
    assert [s.title for s in plan.steps] == ["RENAME TABLE"]
    assert plan.summary.destroy == 0


def test_someone_elses_table_is_renamed_then_claimed() -> None:
    fake = FakeWarehouse.of(replace(OLD, properties=()))
    plan = planned([NEW], fake)
    assert [s.title for s in plan.steps] == ["RENAME TABLE", "CLAIM ownership"]
    run(plan, fake)
    assert fake.tables["main.sales.orders"].managed


def test_when_both_names_exist_nothing_is_renamed_and_nothing_dropped() -> None:
    fake = FakeWarehouse.of(OLD, replace(NEW, properties=MANAGED))
    plan = planned([NEW], fake, strict=True)
    assert plan.steps == ()
    assert plan.diffs[0].notes == (
        "both this table and order_facts exist, so renamed_from is ignored and "
        "order_facts is left as it is",
    )


def test_when_neither_exists_the_table_is_created() -> None:
    fake = FakeWarehouse.of()
    fake.schemas.add("main.sales")
    plan = planned([NEW], fake)
    assert [s.title for s in plan.steps] == ["CREATE TABLE orders"]


def test_the_rename_runs_before_the_hooks() -> None:
    hooked = replace(NEW, hooks=Hooks(before="SELECT 1", after="SELECT 2"))
    plan = planned([hooked], FakeWarehouse.of(OLD))
    assert [s.title for s in plan.steps] == [
        "RENAME TABLE",
        "BEFORE hook",
        "AFTER hook",
    ]


def test_a_rename_and_a_rewrite_in_one_plan() -> None:
    """The rewrite reads the table under its new name, so the rename must be
    first — and it is."""
    fake = FakeWarehouse.of(OLD)
    converted = replace(
        table(col("id", "bigint"), col("amount", "int"), name=NEW.name),
        renamed_from=NEW.renamed_from,
    )
    plan = planned([converted], fake)
    titles = [s.title for s in plan.steps]
    assert titles[0] == "RENAME TABLE"
    assert "STAGE rewritten data" in titles
    stage = plan.steps[titles.index("STAGE rewritten data")]
    assert stage.sql is not None and "FROM `main`.`sales`.`orders`" in stage.sql
    run(plan, fake)
    assert planned([converted], fake).empty


def test_is_applied_asks_whether_the_new_name_answers() -> None:
    change = Change(NEW.name, "rename_table", before=OLD.name)
    assert is_applied(change, replace(OLD, name=NEW.name))
    assert not is_applied(change, None)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_a_rename_renders_and_survives_the_plan_file() -> None:
    plan = planned([NEW], FakeWarehouse.of(OLD))
    assert "→ renamed from order_facts" in plan_text(plan)
    assert loads(dumps(plan)) == plan
