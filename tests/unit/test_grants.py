"""Grants: a principal the spec names has exactly those privileges.

Principals the spec doesn't name are someone else's business — reported, never
touched. That is the line between managing access to a table and taking it over.

https://docs.databricks.com/aws/en/data-governance/unity-catalog/manage-privileges/privileges
"""

from pathlib import Path

import pytest

from deltaplan.differ import diff, is_applied, unmanaged
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_table
from deltaplan.model.table import MANAGED_PROPERTY, Grant, Table
from deltaplan.render.json import dumps, loads
from deltaplan.sql import privilege_sql
from helpers import col, plan_against, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)


def orders(*grants: Grant) -> Table:
    return table(col("id", "bigint"), name=NAME, properties=MANAGED, grants=grants)


def test_the_spec_declares_them(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: c.s.t\n"
        "columns: [{name: id, type: bigint}]\n"
        "grants:\n"
        "  - {principal: analysts, privileges: [select]}\n"
        "  - {principal: etl@example.com, privileges: [MODIFY, SELECT]}\n"
    )
    assert load_table(path).grants == (
        Grant("analysts", ("SELECT",)),
        Grant("etl@example.com", ("MODIFY", "SELECT")),
    )


@pytest.mark.parametrize(
    ("grants", "message"),
    [
        ("[{principal: a, privileges: [DROP EVERYTHING]}]", "unknown privilege"),
        ("[{principal: a, privileges: ['SELECT; DROP TABLE x']}]", "unknown privilege"),
        (
            "[{principal: a, privileges: [SELECT]}, "
            "{principal: a, privileges: [MODIFY]}]",
            "granted twice",
        ),
        ("[{principal: a}]", "needs a 'privileges' key"),
    ],
)
def test_bad_grants_fail_at_load(tmp_path: Path, grants: str, message: str) -> None:
    # A privilege is a keyword and can't be quoted, so it is checked instead —
    # at load time, where the error can point at the line.
    path = tmp_path / "orders.yml"
    path.write_text(
        f"table: c.s.t\ncolumns: [{{name: id, type: bigint}}]\ngrants: {grants}\n"
    )
    with pytest.raises(SpecError, match=message):
        load_table(path)


def test_privileges_are_normalised_and_checked() -> None:
    assert privilege_sql("select") == "SELECT"
    assert privilege_sql("all_privileges") == "ALL PRIVILEGES"
    assert privilege_sql("apply  tag") == "APPLY TAG"
    with pytest.raises(ValueError, match="unknown privilege"):
        privilege_sql("SELECT, MODIFY")


def test_a_named_principal_is_managed_exactly() -> None:
    live = orders(Grant("etl", ("MODIFY", "SELECT")), Grant("bi", ("SELECT",)))
    desired = orders(Grant("etl", ("SELECT",)), Grant("analysts", ("SELECT",)))
    changes = diff(desired, live)
    assert [(c.kind, c.path, c.before, c.after) for c in changes] == [
        ("grant", "analysts", None, ("SELECT",)),
        ("revoke", "etl", ("MODIFY",), None),
    ]
    # `bi` isn't named: reported, never revoked.
    assert "grants to bi" in unmanaged(desired, live)


def test_sql_and_convergence() -> None:
    live = orders(Grant("etl", ("MODIFY", "SELECT")), Grant("bi", ("SELECT",)))
    desired = orders(Grant("etl", ("SELECT",)), Grant("analysts", ("SELECT",)))
    fake, plan = plan_against(desired, live)
    assert [s.sql for s in plan.steps] == [
        "GRANT SELECT ON TABLE `main`.`sales`.`orders` TO `analysts`",
        "REVOKE MODIFY ON TABLE `main`.`sales`.`orders` FROM `etl`",
    ]
    revoke = plan.steps[1]
    assert revoke.warnings == ("takes MODIFY away from etl",)
    assert revoke.undo_hint == "GRANT MODIFY ON TABLE `main`.`sales`.`orders` TO `etl`"

    for change in plan.changes:
        assert not is_applied(change, live)
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None
    assert diff(desired, after.table) == ()
    assert after.table.grants_map()["bi"] == ("SELECT",), "someone else's, untouched"
    for change in plan.changes:
        assert is_applied(change, after.table)


def test_principals_are_quoted() -> None:
    live = orders()
    desired = orders(Grant("data team`; DROP", ("SELECT",)))
    _, plan = plan_against(desired, live)
    assert plan.steps[0].sql == (
        "GRANT SELECT ON TABLE `main`.`sales`.`orders` TO `data team``; DROP`"
    )


def test_a_new_table_is_granted_after_it_is_created() -> None:
    desired = table(
        col("id", "bigint"), name=NAME, grants=(Grant("analysts", ("SELECT",)),)
    )
    fake, plan = plan_against(desired)
    assert [s.title for s in plan.steps] == ["CREATE TABLE orders", "GRANT to analysts"]
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()


def test_a_rewrite_keeps_everyone_s_access() -> None:
    live = table(
        col("amount", "decimal(10,2)"),
        name=NAME,
        properties=MANAGED,
        grants=(Grant("analysts", ("SELECT",)), Grant("bi", ("SELECT",))),
    )
    desired = table(
        col("amount", "string"),  # forces the rewrite
        name=NAME,
        grants=(Grant("analysts", ("SELECT",)),),
    )
    fake, plan = plan_against(desired, live)
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None
    assert after.table.grants_map() == {"analysts": ("SELECT",), "bi": ("SELECT",)}


def test_inherited_grants_are_not_the_tables_to_manage() -> None:
    from deltaplan.introspect import Introspector as RowIntrospector
    from helpers import fake_runner

    runner = fake_runner(
        tables=(
            {
                "table_name": "orders",
                "table_type": "MANAGED",
                "data_source_format": "DELTA",
            },
        ),
        columns=(
            {"table_name": "orders", "column_name": "id", "full_data_type": "bigint"},
        ),
    )
    runner.responses["information_schema.table_privileges"] = (
        {
            "table_name": "orders",
            "grantee": "analysts",
            "privilege_type": "SELECT",
            "inherited_from": "NONE",
        },
        {
            "table_name": "orders",
            "grantee": "everyone",
            "privilege_type": "SELECT",
            "inherited_from": "SCHEMA",
        },
    )
    found = RowIntrospector(runner).table(NAME)
    assert found is not None
    assert found.table.grants == (Grant("analysts", ("SELECT",)),)


def test_they_survive_the_plan_file_and_import() -> None:
    live = orders()
    desired = orders(Grant("analysts", ("SELECT",)))
    _, plan = plan_against(desired, live)
    assert loads(dumps(plan)) == plan
    assert "principal: analysts" in dump_spec(desired)
