"""Schemas as specs: a schema's comment, tags and grants.

Verified live (2026-09-18): CREATE SCHEMA … COMMENT, COMMENT ON SCHEMA and
ALTER SCHEMA … SET TAGS; the fifteen schema privileges in SCHEMA_PRIVILEGES
(CREATE VIEW, BROWSE and bare CREATE are refused on a schema); and
information_schema.schemata / schema_tags / schema_privileges reading it back,
privileges underscored.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-schema
"""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_spec, load_table, validate_spec
from deltaplan.model.plan import Plan
from deltaplan.model.schema import Schema
from deltaplan.model.table import MANAGED_PROPERTY, Grant, Table
from deltaplan.model.view import Relation
from deltaplan.planning import plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeWarehouse
from helpers import col, run, table

SALES = Schema(
    "main.sales",
    comment="Sales data, it's here",
    tags=(("domain", "sales"),),
    grants=(Grant("analysts", ("USE SCHEMA", "SELECT")),),
)
ORDERS = table(
    col("id", "bigint"),
    name="main.sales.orders",
    properties=((MANAGED_PROPERTY, "true"),),
)


def planned(specs: list[Relation], fake: FakeWarehouse, *, strict: bool = False) -> Plan:
    return plan_tables(
        specs,
        Introspector(fake),
        target="t",
        tool_version="0",
        mode_for=lambda _schema: "strict" if strict else "additive",
    )


def converge(specs: list[Relation], fake: FakeWarehouse) -> Plan:
    plan = planned(specs, fake)
    run(plan, fake)
    assert planned(specs, fake).empty, "re-planning after apply must be empty"
    return plan


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------


def test_a_schema_spec(tmp_path: Path) -> None:
    path = tmp_path / "_schema.yml"
    path.write_text(
        "schema: ${catalog}.sales\n"
        "comment: Sales data, it's here\n"
        "tags: {domain: sales}\n"
        "grants: [{principal: analysts, privileges: [use_schema, SELECT]}]\n"
    )
    assert load_spec(path, {"catalog": "main"}) == SALES
    with pytest.raises(SpecError, match="describes a schema, not a table"):
        load_table(path, {"catalog": "main"})


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (
            "schema: main.sales\ngrants: [{principal: a, privileges: [CREATE VIEW]}]\n",
            "CREATE VIEW",
        ),
        ("schema: main.sales\nproperties: {a: b}\n", "unknown key 'properties'"),
    ],
)
def test_bad_schema_specs(tmp_path: Path, spec: str, message: str) -> None:
    path = tmp_path / "s.yml"
    path.write_text(spec)
    with pytest.raises(SpecError, match=message):
        load_spec(path)


def test_a_schema_name_has_two_parts() -> None:
    [problem] = validate_spec(Schema("main.sales.orders"), "s.yml")
    assert "must be catalog.schema" in problem.message


# ---------------------------------------------------------------------------
# planning and applying
# ---------------------------------------------------------------------------


def test_a_new_schema_is_created_with_everything_and_its_tables_follow() -> None:
    fake = FakeWarehouse.of()
    plan = converge([ORDERS, SALES], fake)
    titles = [s.title for s in plan.steps]
    assert titles[:3] == ["CREATE SCHEMA sales", "SET TAGS", "GRANT to analysts"]
    assert titles.count("CREATE SCHEMA sales") == 1, "the table finds it made"
    assert plan.steps[0].sql == (
        "CREATE SCHEMA IF NOT EXISTS `main`.`sales` COMMENT 'Sales data, it\\'s here'"
    )
    assert (
        plan.steps[1].sql == "ALTER SCHEMA `main`.`sales` SET TAGS ('domain' = 'sales')"
    )
    assert plan.steps[2].sql == (
        "GRANT SELECT, USE SCHEMA ON SCHEMA `main`.`sales` TO `analysts`"
    )
    assert plan.summary.add == 2
    assert fake.schema_defs["main.sales"] == SALES


def test_an_existing_schema_is_brought_in_line() -> None:
    fake = FakeWarehouse.of(Schema("main.sales", comment="old"), ORDERS)
    plan = converge([SALES], fake)
    titles = [s.title for s in plan.steps]
    assert titles == ["COMMENT ON SCHEMA", "SET TAGS", "GRANT to analysts"]
    assert plan.steps[0].undo_hint == "COMMENT ON SCHEMA `main`.`sales` IS 'old'"
    assert "~ comment" in plan_text(plan)


def test_what_a_schema_spec_leaves_out_is_left_alone() -> None:
    """A schema is shared ground: its comment isn't cleared because a spec
    doesn't give one, and grants and tags it doesn't name are reported."""
    live = Schema(
        "main.sales",
        comment="someone's comment",
        tags=(("owner", "finance"),),
        grants=(Grant("auditors", ("USE SCHEMA",)),),
    )
    fake = FakeWarehouse.of(live)
    plan = planned([Schema("main.sales")], fake)
    assert plan.empty
    assert plan.diffs[0].unmanaged == ("tag owner", "grants to auditors")


def test_a_schema_is_never_dropped() -> None:
    fake = FakeWarehouse.of(SALES, ORDERS)
    plan = planned([ORDERS], fake, strict=True)
    assert plan.empty


def test_the_executor_applies_it_and_knows_when_it_is_done() -> None:
    fake = FakeWarehouse.of(Schema("main.sales"))
    plan = planned([SALES], fake)
    result = Executor(
        runner=fake,
        introspector=Introspector(fake),
        history=MemoryHistory(),
        new_run_id=lambda: "run1",
    ).apply(plan)
    assert result.ok, result.error
    assert fake.schema_defs["main.sales"] == SALES


def test_a_schema_plan_survives_the_plan_file() -> None:
    changed = planned([SALES], FakeWarehouse.of(Schema("main.sales", comment="old")))
    created = planned([SALES], FakeWarehouse.of())
    for plan in (changed, created):
        assert loads(dumps(plan)) == plan


# ---------------------------------------------------------------------------
# import, and SQL
# ---------------------------------------------------------------------------


def test_import_writes_the_schema_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWarehouse.of(SALES, ORDERS)
    monkeypatch.setattr(cli, "_warehouse", lambda *_args, **_kwargs: fake)
    for spec_format, name in (("yaml", "_schema.yml"), ("sql", "_schema.yml")):
        destination = tmp_path / spec_format
        result = CliRunner().invoke(
            cli.app, ["import", "main.sales", "-o", str(destination), "-f", spec_format]
        )
        assert result.exit_code == 0, result.output
        written = destination / name
        assert load_spec(written) == SALES, "tags keep it YAML even with -f sql"
    assert "SQL can't say schema tags" in " ".join(result.output.split())


def test_a_schema_without_tags_imports_as_sql(tmp_path: Path) -> None:
    from deltaplan.sqlspec import dump_sql_spec

    plain = Schema("main.sales", comment="It's sales", grants=SALES.grants)
    path = tmp_path / "_schema.sql"
    path.write_text(dump_sql_spec(plain, catalog_variable="catalog"))
    assert path.read_text().startswith("CREATE SCHEMA ${catalog}.sales\nCOMMENT")
    assert load_spec(path, {"catalog": "main"}) == plain


def test_a_schema_spec_validates_against_the_editor_schema() -> None:
    from jsonschema import Draft7Validator

    from deltaplan.spec_schema import spec_schema

    document = yaml.safe_load(dump_spec(SALES))
    assert list(Draft7Validator(spec_schema()).iter_errors(document)) == []


def test_tables_still_plan_bare_schemas_without_a_spec() -> None:
    fake = FakeWarehouse.of()
    plan = converge([ORDERS], fake)
    assert plan.steps[0].sql == "CREATE SCHEMA IF NOT EXISTS `main`.`sales`"
    assert isinstance(plan.diffs[0].desired, Table)
