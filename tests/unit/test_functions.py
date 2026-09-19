"""SQL functions, end to end: spec, plan, apply against the fake, re-plan.

A function's shape is its signature, return type, body and comment; a change to
any of them replaces it with CREATE OR REPLACE FUNCTION. Grants are managed per
principal, as for tables. Functions are planned before tables and views, because
masks, row filters and views call them.

https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-sql-function
https://docs.databricks.com/aws/en/sql/language-manual/information-schema/routines
"""

from pathlib import Path

import pytest

from deltaplan.differ import is_applied
from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_spec, load_table, validate_spec
from deltaplan.model.function import Function, Parameter
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, Grant
from deltaplan.model.types import Decimal, Field, Mask, Primitive
from deltaplan.model.view import Relation, View
from deltaplan.planning import PlanningError, plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.markdown import render_markdown
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeWarehouse
from helpers import col, fake_runner, run, table

STRING = Primitive("string")
MASK_EMAIL = Function(
    "main.sales.mask_email",
    (Parameter("email", STRING),),
    STRING,
    "CASE WHEN is_account_group_member('pii') THEN email ELSE '***' END",
    comment="Hide emails from everyone outside pii",
    grants=(Grant("analysts", ("EXECUTE",)),),
)


def planned(specs: list[Relation], fake: FakeWarehouse, *, strict: bool = False) -> Plan:
    return plan_tables(
        specs,
        Introspector(fake),
        target="test",
        tool_version="0.1.0",
        mode_for=lambda _schema: "strict" if strict else "additive",
    )


def converge(specs: list[Relation], fake: FakeWarehouse) -> Plan:
    plan = planned(specs, fake)
    run(plan, fake)
    assert planned(specs, fake).empty, "re-planning after apply must be empty"
    return plan


def with_body(function: Function, body: str) -> Function:
    return Function(
        function.name,
        function.parameters,
        function.returns,
        body,
        function.comment,
        function.grants,
    )


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------


def test_a_function_spec(tmp_path: Path) -> None:
    path = tmp_path / "mask_email.yml"
    path.write_text(
        "function: ${catalog}.sales.mask_email\n"
        "comment: Hide emails from everyone outside pii\n"
        "parameters:\n"
        "  - {name: email, type: string}\n"
        "returns: string\n"
        "grants: [{principal: analysts, privileges: [EXECUTE]}]\n"
        "body: |\n"
        "  CASE WHEN is_account_group_member('pii') THEN email ELSE '***' END\n"
    )
    function = load_spec(path, {"catalog": "main"})
    assert function == MASK_EMAIL
    with pytest.raises(SpecError, match="describes a function, not a table"):
        load_table(path, {"catalog": "main"})


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("function: c.s.f\nbody: '1'\n", "needs a 'returns' key"),
        ("function: c.s.f\nreturns: int\n", "needs a 'body' key"),
        ("function: c.s.f\nreturns: int\nbody: '  '\n", "body cannot be empty"),
        ("function: c.s.f\nreturns: int\nbody: '1'\nquery: x\n", "unknown key 'query'"),
        (
            "function: c.s.f\nreturns: int\nbody: '1'\n"
            "grants: [{principal: a, privileges: [SELECT]}]\n",
            "SELECT",
        ),
        (
            "function: c.s.f\nreturns: int\nbody: '1'\nparameters: [{name: x}]\n",
            "needs a 'type' key",
        ),
    ],
)
def test_bad_function_specs(tmp_path: Path, spec: str, message: str) -> None:
    path = tmp_path / "f.yml"
    path.write_text(spec)
    with pytest.raises(SpecError, match=message):
        load_spec(path)


def test_a_parameter_declared_twice_fails_validation() -> None:
    twice = Function(
        "main.sales.f",
        (Parameter("x", STRING), Parameter("X", STRING)),
        STRING,
        "x",
    )
    found = validate_spec(twice, "f.yml")
    assert [d.message for d in found if d.severity == "error"] == [
        "parameter 'x' is declared twice"
    ]


def test_import_round_trips_a_function(tmp_path: Path) -> None:
    written = dump_spec(MASK_EMAIL)
    assert written.startswith("function: main.sales.mask_email")
    assert "body: |" in written
    path = tmp_path / "mask_email.yml"
    path.write_text(written)
    assert load_spec(path) == MASK_EMAIL


# ---------------------------------------------------------------------------
# introspection
# ---------------------------------------------------------------------------


def test_functions_are_read_with_their_parameters_in_order() -> None:
    """Parameters come back by ordinal position, and only direct grants count —
    the same rule as for tables.

    The column names of routines, parameters and routine_privileges, and
    specific_name being the routine's name, are verified live by
    `test_a_function_reads_back_as_its_spec`.
    https://docs.databricks.com/aws/en/sql/language-manual/information-schema/parameters
    """
    runner = fake_runner(
        routines=(
            {
                "routine_name": "price",
                "routine_definition": "amount * rate",
                "full_data_type": "decimal(18,2)",
                "comment": None,
            },
        ),
        parameters=(
            {
                "specific_name": "price",
                "parameter_name": "amount",
                "ordinal_position": "0",
                "full_data_type": "decimal(18,2)",
            },
            {
                "specific_name": "price",
                "parameter_name": "rate",
                "ordinal_position": "1",
                "full_data_type": "double",
            },
        ),
        routine_grants=(
            {
                "routine_name": "price",
                "grantee": "analysts",
                "privilege_type": "EXECUTE",
                "inherited_from": "NONE",
            },
            {
                "routine_name": "price",
                "grantee": "admins",
                "privilege_type": "EXECUTE",
                "inherited_from": "SCHEMA",
            },
        ),
    )
    live = Introspector(runner).schema("main", "sales")
    price = live.get_function("main.sales.price")
    assert price == Function(
        "main.sales.price",
        (Parameter("amount", Decimal(18, 2)), Parameter("rate", Primitive("double"))),
        Decimal(18, 2),
        "amount * rate",
        grants=(Grant("analysts", ("EXECUTE",)),),
    )
    assert live.relation("main.sales.price") == price


def test_a_schema_without_functions_asks_nothing_more() -> None:
    runner = fake_runner()
    Introspector(runner).schema("main", "sales")
    assert not any("parameters" in s for s in runner.statements)


# ---------------------------------------------------------------------------
# planning and applying
# ---------------------------------------------------------------------------


def test_a_new_function_is_created_then_granted() -> None:
    fake = FakeWarehouse.of()
    fake.schemas.add("main.sales")
    plan = converge([MASK_EMAIL], fake)
    assert [s.title for s in plan.steps] == [
        "CREATE FUNCTION mask_email",
        "GRANT to analysts",
    ]
    assert plan.steps[0].sql == (
        "CREATE FUNCTION IF NOT EXISTS `main`.`sales`.`mask_email`(`email` STRING)\n"
        "RETURNS STRING\n"
        "COMMENT 'Hide emails from everyone outside pii'\n"
        "RETURN CASE WHEN is_account_group_member('pii') THEN email ELSE '***' END"
    )
    assert plan.steps[1].sql == (
        "GRANT EXECUTE ON FUNCTION `main`.`sales`.`mask_email` TO `analysts`"
    )
    assert plan.summary.add == 1 and plan.summary.change == 0
    assert fake.functions["main.sales.mask_email"] == MASK_EMAIL


def test_a_changed_body_replaces_the_function_and_keeps_its_grants() -> None:
    fake = FakeWarehouse.of(MASK_EMAIL)
    stricter = with_body(MASK_EMAIL, "'***'")
    plan = converge([stricter], fake)
    titles = [s.title for s in plan.steps]
    assert titles == ["REPLACE FUNCTION mask_email", "GRANT to analysts"]
    replace_step = plan.steps[0]
    assert replace_step.sql is not None
    assert replace_step.sql.startswith("CREATE OR REPLACE FUNCTION")
    assert replace_step.undo_hint is not None
    assert "is_account_group_member" in replace_step.undo_hint, "undo restores the body"
    assert any("sees the new definition" in w for w in replace_step.warnings)
    assert "~ definition" in plan_text(plan)


@pytest.mark.parametrize(
    "changed",
    [
        Function(
            MASK_EMAIL.name,
            (Parameter("address", STRING),),
            STRING,
            MASK_EMAIL.body,
            MASK_EMAIL.comment,
            MASK_EMAIL.grants,
        ),
        Function(
            MASK_EMAIL.name,
            MASK_EMAIL.parameters,
            Primitive("binary"),
            MASK_EMAIL.body,
            MASK_EMAIL.comment,
            MASK_EMAIL.grants,
        ),
        Function(
            MASK_EMAIL.name,
            MASK_EMAIL.parameters,
            MASK_EMAIL.returns,
            MASK_EMAIL.body,
            "a new comment",
            MASK_EMAIL.grants,
        ),
    ],
    ids=["parameter", "returns", "comment"],
)
def test_any_part_of_the_signature_replaces_it(changed: Function) -> None:
    plan = converge([changed], FakeWarehouse.of(MASK_EMAIL))
    assert plan.steps[0].title == "REPLACE FUNCTION mask_email"


def test_whitespace_in_the_body_is_not_a_change() -> None:
    fake = FakeWarehouse.of(MASK_EMAIL)
    spaced = with_body(MASK_EMAIL, MASK_EMAIL.body.replace(" THEN ", "\n   THEN "))
    assert planned([spaced], fake).empty


def test_grants_change_without_a_replace() -> None:
    fake = FakeWarehouse.of(MASK_EMAIL)
    regranted = Function(
        MASK_EMAIL.name,
        MASK_EMAIL.parameters,
        MASK_EMAIL.returns,
        MASK_EMAIL.body,
        MASK_EMAIL.comment,
        (Grant("analysts", ("MANAGE",)), Grant("support", ("EXECUTE",))),
    )
    plan = converge([regranted], fake)
    assert [s.title for s in plan.steps] == [
        "GRANT to analysts",
        "REVOKE from analysts",
        "GRANT to support",
    ]


def test_grants_to_principals_the_spec_does_not_name_are_left_alone() -> None:
    live = Function(
        MASK_EMAIL.name,
        MASK_EMAIL.parameters,
        MASK_EMAIL.returns,
        MASK_EMAIL.body,
        MASK_EMAIL.comment,
        (*MASK_EMAIL.grants, Grant("auditors", ("EXECUTE",))),
    )
    plan = planned([MASK_EMAIL], FakeWarehouse.of(live))
    assert plan.empty
    assert plan.diffs[0].unmanaged == ("grants to auditors",)


def test_functions_are_planned_before_the_tables_that_mask_with_them() -> None:
    customers = table(
        col("id", "bigint"),
        Field("email", STRING, mask=Mask("main.sales.mask_email")),
        name="main.sales.customers",
        properties=((MANAGED_PROPERTY, "true"),),
    )
    fake = FakeWarehouse.of()
    fake.schemas.add("main.sales")
    plan = converge([customers, MASK_EMAIL], fake)
    titles = [s.title for s in plan.steps]
    assert titles.index("CREATE FUNCTION mask_email") < titles.index(
        "CREATE TABLE customers"
    )
    create_table = plan.steps[titles.index("CREATE TABLE customers")].sql
    assert create_table is not None and "MASK `main`.`sales`.`mask_email`" in create_table


def test_views_that_call_a_function_come_after_it() -> None:
    fake = FakeWarehouse.of()
    fake.schemas.add("main.sales")
    view = View("main.sales.emails", "SELECT main.sales.mask_email('x') AS email")
    plan = converge([view, MASK_EMAIL], fake)
    titles = [s.title for s in plan.steps]
    assert titles.index("CREATE FUNCTION mask_email") < titles.index("CREATE VIEW emails")


def test_functions_follow_the_functions_they_call() -> None:
    inner = Function("main.sales.inner_f", (), STRING, "'x'")
    outer = Function("main.sales.outer_f", (), STRING, "main.sales.inner_f()")
    fake = FakeWarehouse.of()
    fake.schemas.add("main.sales")
    plan = converge([outer, inner], fake)
    assert [s.title for s in plan.steps] == [
        "CREATE FUNCTION inner_f",
        "CREATE FUNCTION outer_f",
    ]


def test_functions_calling_each_other_in_a_cycle_are_an_error() -> None:
    a = Function("main.sales.a", (), STRING, "main.sales.b()")
    b = Function("main.sales.b", (), STRING, "main.sales.a()")
    with pytest.raises(PlanningError, match="functions call each other in a cycle"):
        planned([a, b], FakeWarehouse.of())


def test_a_function_without_a_spec_is_left_alone_even_in_a_strict_schema() -> None:
    """There's no ownership marker on a function, so nothing proves deltaplan made
    it — and nothing it didn't make is ever dropped."""
    orders = table(
        col("id", "bigint"),
        name="main.sales.orders",
        properties=((MANAGED_PROPERTY, "true"),),
    )
    fake = FakeWarehouse.of(orders, MASK_EMAIL)
    assert planned([orders], fake, strict=True).empty


def test_a_function_named_like_a_table_is_refused() -> None:
    orders = table(col("id", "bigint"), name="main.sales.orders")
    clash = Function("main.sales.orders", (), STRING, "'x'")
    with pytest.raises(PlanningError, match="names both a function and a table"):
        planned([clash], FakeWarehouse.of(orders))
    with pytest.raises(PlanningError, match="names both a function and a table"):
        planned([orders, clash], FakeWarehouse.of())


def test_the_executor_applies_a_replace_and_knows_when_it_is_done() -> None:
    """The fingerprint check and the skip-if-applied check both see functions."""
    fake = FakeWarehouse.of(MASK_EMAIL)
    stricter = with_body(MASK_EMAIL, "'***'")
    plan = planned([stricter], fake)
    result = Executor(
        runner=fake,
        introspector=Introspector(fake),
        history=MemoryHistory(),
        new_run_id=lambda: "run1",
    ).apply(plan)
    assert result.ok
    assert fake.functions[MASK_EMAIL.name].body == "'***'"

    replaced = plan.diffs[0].changes[0]
    assert replaced.kind == "replace_function"
    assert is_applied(replaced, fake.functions[MASK_EMAIL.name])
    assert not is_applied(replaced, MASK_EMAIL)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_functions_survive_the_plan_file() -> None:
    fake = FakeWarehouse.of(MASK_EMAIL)
    plan = planned([with_body(MASK_EMAIL, "'***'")], fake)
    assert loads(dumps(plan)) == plan


def test_a_new_function_renders_as_a_creation() -> None:
    fake = FakeWarehouse.of()
    fake.schemas.add("main.sales")
    plan = planned([MASK_EMAIL], fake)
    text = plan_text(plan)
    assert "sales.mask_email   + create" in text
    assert "+ function" in text
    rendered = render_markdown(plan)
    assert "+ function" in rendered
    assert "CREATE FUNCTION IF NOT EXISTS" in rendered
