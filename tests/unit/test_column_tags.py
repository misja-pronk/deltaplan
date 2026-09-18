"""Column tags, from spec to live table and back.

Tags are governance metadata, so they follow the same rule as table tags: the
spec's tags are set, anything else on the live column is reported and left alone
— including across a rewrite, which rebuilds the columns they hang off.

https://docs.databricks.com/aws/en/database-objects/tags
"""

from pathlib import Path

from deltaplan.differ import diff, is_applied, unmanaged
from deltaplan.introspect import Introspector
from deltaplan.loader import load_table, validate_table
from deltaplan.model.table import MANAGED_PROPERTY
from deltaplan.model.types import Field, Primitive
from deltaplan.render.json import dumps, loads
from fake_warehouse import FakeWarehouse
from helpers import col, plan_against, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)


def tagged(name: str, type_text: str, **tags: str) -> Field:
    column = col(name, type_text)
    return Field(column.name, column.type, tags=tuple(tags.items()))


def test_the_spec_declares_them(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: c.s.t\n"
        "columns:\n"
        "  - name: email\n"
        "    type: string\n"
        "    tags: {pii: email, owner: crm}\n"
    )
    email = load_table(path).column("email")
    assert email is not None
    assert email.tags == (("owner", "crm"), ("pii", "email")), "stored sorted"


def test_they_belong_on_columns_not_nested_fields(tmp_path: Path) -> None:
    path = tmp_path / "orders.yml"
    path.write_text(
        "table: c.s.t\n"
        "columns:\n"
        "  - name: a\n"
        "    type:\n"
        "      struct:\n"
        "        - {name: b, type: string, tags: {pii: 'yes'}}\n"
    )
    messages = [d.message for d in validate_table(load_table(path), "spec.yml")]
    assert any("tags go on columns" in m for m in messages)


def test_they_are_diffed_additively() -> None:
    live = table(tagged("email", "string", pii="email", legacy="x"), name=NAME)
    desired = table(tagged("email", "string", pii="contact", owner="crm"), name=NAME)
    changes = diff(desired, live)
    assert [(c.kind, c.path, c.before, c.after) for c in changes] == [
        ("set_column_tag", "email", None, ("owner", "crm")),
        ("set_column_tag", "email", ("pii", "email"), ("pii", "contact")),
    ]
    # `legacy` isn't in the spec: reported, never removed.
    assert "tag legacy on column email" in unmanaged(desired, live)


def test_sql_and_convergence() -> None:
    live = table(col("email", "string"), name=NAME, properties=MANAGED)
    desired = table(tagged("email", "string", pii="email"), name=NAME)
    fake, plan = plan_against(desired, live)
    assert [s.sql for s in plan.steps] == [
        "ALTER TABLE `main`.`sales`.`orders` "
        "ALTER COLUMN `email` SET TAGS ('pii' = 'email')"
    ]
    change = plan.changes[0]
    assert not is_applied(change, live)
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None
    assert is_applied(change, after.table)
    assert diff(desired, after.table) == ()


def test_a_new_column_is_added_then_tagged() -> None:
    live = table(col("id", "bigint"), name=NAME, properties=MANAGED)
    desired = table(
        col("id", "bigint"), tagged("email", "string", pii="email"), name=NAME
    )
    fake, plan = plan_against(desired, live)
    assert [s.title for s in plan.steps] == ["ADD COLUMN email", "SET COLUMN TAGS"]
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()


def test_a_new_table_gets_its_column_tags() -> None:
    desired = table(
        col("id", "bigint"), tagged("email", "string", pii="email"), name=NAME
    )
    fake, plan = plan_against(desired)
    assert [s.title for s in plan.steps] == ["CREATE TABLE orders", "SET COLUMN TAGS"]
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(desired, after.table) == ()


def test_a_rewrite_puts_back_tags_the_spec_doesnt_manage() -> None:
    # `legacy` was set by someone else. The rewrite rebuilds the column; the tag
    # must survive it, because deltaplan never claimed it.
    live = table(
        tagged("email", "string", legacy="x"),
        col("amount", "decimal(10,2)"),
        name=NAME,
        properties=MANAGED,
        tags=(("owner", "finance"),),
    )
    desired = table(
        tagged("email", "string", pii="email"),
        col("amount", "string"),  # forces the rewrite
        name=NAME,
    )
    fake, plan = plan_against(desired, live)
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None
    email = after.table.column("email")
    assert email is not None
    assert dict(email.tags) == {"legacy": "x", "pii": "email"}
    assert dict(after.table.tags) == {"owner": "finance"}


def test_introspection_reads_them() -> None:
    live = table(tagged("email", "string", pii="email"), name=NAME, properties=MANAGED)
    found = Introspector(FakeWarehouse.of(live)).table(NAME)
    assert found is not None
    email = found.table.column("email")
    assert email is not None and email.tags == (("pii", "email"),)


def test_they_survive_the_plan_file() -> None:
    live = table(col("email", "string"), name=NAME, properties=MANAGED)
    desired = table(tagged("email", "string", pii="email"), name=NAME)
    _, plan = plan_against(desired, live)
    assert loads(dumps(plan)) == plan


def test_a_tag_value_can_hold_anything() -> None:
    column = Field("c", Primitive("string"), tags=(("note", "it's | fine"),))
    live = table(col("c", "string"), name=NAME, properties=MANAGED)
    fake, plan = plan_against(table(column, name=NAME), live)
    assert "'it''s | fine'" in (plan.steps[0].sql or "")
    run(plan, fake)
    after = Introspector(fake).table(NAME)
    assert after is not None and diff(table(column, name=NAME), after.table) == ()
