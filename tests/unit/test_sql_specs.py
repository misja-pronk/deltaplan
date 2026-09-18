"""SQL specs: a Databricks CREATE statement, read with sqlglot into the same
model a YAML spec becomes.

The rule: what sqlglot parses into structure, a SQL spec may use; what it can't,
a SQL spec can't — and the error says to use YAML. `deltaplan.features` is the
list, `docs/formats.md` shows it, and these tests prove every row of it.
https://sqlglot.com/sqlglot/dialects/databricks.html
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from deltaplan import features
from deltaplan.features import FEATURES
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, load_project, load_spec, spec_files
from deltaplan.model.function import Function, Parameter
from deltaplan.model.table import Check, ForeignKey, Grant, PrimaryKey, Table
from deltaplan.model.types import Decimal, Field, Identity, Primitive
from deltaplan.model.view import Relation, View
from deltaplan.planning import plan_tables
from deltaplan.typeparser import parse_type
from fake_warehouse import FakeWarehouse
from helpers import run

DOCS = Path(__file__).parents[2] / "docs" / "formats.md"


def sql(tmp_path: Path, text: str, name: str = "spec.sql") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def load(tmp_path: Path, text: str, **variables: str) -> Relation:
    return load_spec(sql(tmp_path, text), variables)


def table_of(relation: Relation) -> Table:
    assert isinstance(relation, Table)
    return relation


def column(relation: Relation, name: str) -> Field:
    found = table_of(relation).column(name)
    assert found is not None, f"no column {name!r}"
    return found


# ---------------------------------------------------------------------------
# every row of the feature list
# ---------------------------------------------------------------------------

EXPECTED: dict[str, Callable[[Relation], bool]] = {
    "Columns and types, nested included": lambda r: (
        column(r, "lines") is not None
        and column(r, "lines").type == parse_type("array<struct<sku:string,qty:int>>")
        and column(r, "attrs").type == parse_type("map<string,string>")
        and column(r, "amount").type == Decimal(18, 2)
    ),
    "NOT NULL, on nested fields too": lambda r: (
        column(r, "id").nullable is False
        and column(r, "address").type
        == parse_type("struct<street:string not null,zip:string>")
    ),
    "Column comments": lambda r: column(r, "note").comment == "Free text",
    "Table comment": lambda r: table_of(r).comment == "Order facts",
    "Liquid clustering keys": lambda r: table_of(r).cluster_by == ("id",),
    "Automatic liquid clustering": lambda r: table_of(r).cluster_auto,
    "Table properties": lambda r: (
        table_of(r).properties_map() == {"delta.enableChangeDataFeed": "true"}
    ),
    "Primary key": lambda r: (
        table_of(r).constraints == (PrimaryKey(("id",), "orders_pk"),)
    ),
    "Foreign keys": lambda r: (
        table_of(r).constraints
        == (
            ForeignKey(
                ("customer_id",), "main.sales.customers", ("id",), "orders_customer_fk"
            ),
        )
    ),
    "CHECK constraints": lambda r: (
        table_of(r).constraints == (Check("positive_amount", "amount > 0"),)
    ),
    "Identity columns": lambda r: (
        column(r, "line_id").identity == Identity(always=True, start=1, increment=1)
    ),
    "Generated columns": lambda r: (
        column(r, "placed_on").generated == "CAST(placed_at AS DATE)"
    ),
    "Column defaults": lambda r: column(r, "status").default == "'new'",
    "Table tags": lambda r: table_of(r).tags == (("domain", "sales"),),
    "Grants": lambda r: table_of(r).grants == (Grant("analysts", ("SELECT",)),),
    "Views: query, comment, properties": lambda r: (
        isinstance(r, View)
        and r.comment == "Orders over 1000"
        and r.query == "SELECT id, amount FROM main.sales.orders WHERE amount > 1000"
    ),
    "View tags and grants": lambda r: (
        isinstance(r, View)
        and r.tags == (("domain", "sales"),)
        and r.grants == (Grant("analysts", ("SELECT",)),)
    ),
    "SQL functions: parameters, return type, body, comment": lambda r: (
        isinstance(r, Function)
        and r.parameters == (Parameter("amount", Decimal(18, 2)),)
        and r.returns == Primitive("string")
        and r.comment == "Small or large"
        and r.body == "CASE WHEN amount < 100 THEN 'small' ELSE 'large' END"
    ),
    "Function grants": lambda r: (
        isinstance(r, Function) and r.grants == (Grant("analysts", ("EXECUTE",)),)
    ),
}


def test_every_supported_feature_has_an_expectation() -> None:
    """The list can't grow a ✓ without a test saying what it loads into."""
    supported = {f.name for f in FEATURES if f.sql}
    assert supported == set(EXPECTED)


@pytest.mark.parametrize("feature", [f for f in FEATURES if f.sql], ids=lambda f: f.name)
def test_a_supported_feature_loads(tmp_path: Path, feature: features.Feature) -> None:
    assert feature.example is not None
    relation = load(tmp_path, feature.example)
    assert EXPECTED[feature.name](relation), relation


@pytest.mark.parametrize(
    "feature",
    [f for f in FEATURES if not f.sql and f.example is not None],
    ids=lambda f: f.name,
)
def test_an_unsupported_feature_is_refused(
    tmp_path: Path, feature: features.Feature
) -> None:
    assert feature.example is not None
    with pytest.raises(SpecError) as refused:
        load(tmp_path, feature.example)
    if feature.yaml:
        assert "YAML" in refused.value.message, refused.value.message
    else:
        assert "YAML, which supports it" not in refused.value.message


def test_the_docs_show_the_current_list() -> None:
    text = DOCS.read_text()
    shown = text.split(features.START, 1)[1].split(features.END, 1)[0].strip()
    assert shown == features.markdown(), (
        "docs/formats.md is out of date: run "
        "`uv run python -m deltaplan.features docs/formats.md`"
    )


# ---------------------------------------------------------------------------
# the same model as YAML
# ---------------------------------------------------------------------------

ORDERS_SQL = """\
-- Order facts, one row per order.
CREATE TABLE IF NOT EXISTS ${catalog}.sales.orders (
  order_id    BIGINT NOT NULL COMMENT 'Surrogate key',
  amount      DECIMAL(18, 2),
  address     STRUCT<street: STRING NOT NULL, zip: STRING>,
  customer_id BIGINT,
  CONSTRAINT orders_pk PRIMARY KEY (order_id),
  CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id)
    REFERENCES ${catalog}.sales.customers (id),
  CONSTRAINT positive_amount CHECK (amount > 0)
)
USING DELTA
COMMENT 'Order facts'
CLUSTER BY (order_id)
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

ALTER TABLE ${catalog}.sales.orders SET TAGS ('domain' = 'sales');
GRANT SELECT ON TABLE ${catalog}.sales.orders TO `analysts`;
"""

ORDERS_YAML = """\
table: ${catalog}.sales.orders
comment: Order facts
cluster_by: [order_id]
tags: {domain: sales}
properties:
  delta.enableChangeDataFeed: "true"
grants:
  - {principal: analysts, privileges: [SELECT]}
columns:
  - {name: order_id, type: bigint, nullable: false, comment: Surrogate key}
  - {name: amount, type: "decimal(18,2)"}
  - {name: address, type: "struct<street:string not null,zip:string>"}
  - {name: customer_id, type: bigint}
constraints:
  - primary_key: {columns: [order_id], name: orders_pk}
  - foreign_key:
      name: orders_customer_fk
      columns: [customer_id]
      references: ${catalog}.sales.customers
      referenced_columns: [id]
  - check: {name: positive_amount, expression: "amount > 0"}
"""


def test_a_sql_spec_and_its_yaml_twin_are_the_same_model(tmp_path: Path) -> None:
    from_sql = load_spec(sql(tmp_path, ORDERS_SQL), {"catalog": "main"})
    yaml = tmp_path / "orders.yml"
    yaml.write_text(ORDERS_YAML)
    assert from_sql == load_spec(yaml, {"catalog": "main"})


def test_a_sql_spec_plans_applies_and_converges(tmp_path: Path) -> None:
    spec = load_spec(sql(tmp_path, ORDERS_SQL), {"catalog": "main"})
    customers = load(
        tmp_path / "..",
        "CREATE TABLE main.sales.customers (id BIGINT NOT NULL, "
        "CONSTRAINT customers_pk PRIMARY KEY (id));",
    )
    fake = FakeWarehouse.of()
    fake.schemas.add("main.sales")
    specs = [customers, spec]
    plan = plan_tables(specs, Introspector(fake), target="t", tool_version="0")
    run(plan, fake)
    assert plan_tables(specs, Introspector(fake), target="t", tool_version="0").empty


def test_view_queries_and_function_bodies_are_kept_as_written(tmp_path: Path) -> None:
    view = load(
        tmp_path,
        "CREATE OR REPLACE VIEW main.sales.v AS\n"
        "SELECT id,   amount  -- the amount, as booked\n"
        "FROM main.sales.orders ;",
    )
    assert isinstance(view, View)
    assert view.query == (
        "SELECT id,   amount  -- the amount, as booked\nFROM main.sales.orders"
    )
    function = load(
        tmp_path,
        "CREATE FUNCTION main.sales.f(x INT) RETURNS INT RETURN   x  +  1",
    )
    assert isinstance(function, Function) and function.body == "x  +  1"


def test_a_comment_reading_as_is_not_where_a_query_starts(tmp_path: Path) -> None:
    view = load(tmp_path, "CREATE VIEW main.sales.v COMMENT 'as' AS SELECT 1 AS one;")
    assert isinstance(view, View) and view.query == "SELECT 1 AS one"


# ---------------------------------------------------------------------------
# refusals, each at the right place
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message", "line"),
    [
        (
            "CREATE TABLE ${catalog}.s.t (\n  id BIGINT,\n  x ${missing}\n);",
            r"undefined variable \$\{missing\}",
            3,
        ),
        (
            "CREATE TABLE c.s.t (\n  id BIGINT,\n  email STRING MASK c.s.m\n);",
            "sqlglot can't parse this",
            3,
        ),
        (
            "CREATE TABLE c.s.t (id BIGINT);\n"
            "ALTER TABLE c.s.t ALTER COLUMN id SET TAGS ('a' = 'b');",
            "sqlglot doesn't understand `ALTER TABLE C",
            2,
        ),
        ("CREATE TABLE c.s.t AS SELECT 1 AS id;", "AS SELECT isn't a spec", 1),
        (
            "CREATE TABLE c.s.t (id BIGINT)\nUSING PARQUET;",
            "not USING PARQUET",
            2,
        ),
        (
            "CREATE TABLE c.s.t (\n  id INT,\n  CHECK (id > 0)\n);",
            "a CHECK needs a name",
            3,
        ),
        (
            "CREATE TABLE c.s.t (id BIGINT);\nCOMMENT ON TABLE c.s.t IS 'x';",
            "may only have ALTER … SET TAGS and GRANT",
            2,
        ),
        (
            "CREATE TABLE c.s.t (id BIGINT);\n"
            "ALTER TABLE c.s.other SET TAGS ('a' = 'b');",
            "must be about the object it creates",
            2,
        ),
        (
            "CREATE TABLE c.s.t (id BIGINT);\nGRANT SELECT ON TABLE c.s.other TO `x`;",
            "must be on the object it creates",
            2,
        ),
        (
            "CREATE FUNCTION c.s.f() RETURNS INT RETURN 1;\n"
            "GRANT SELECT ON FUNCTION c.s.f TO `x`;",
            "unknown privilege 'SELECT'",
            2,
        ),
        (
            "CREATE FUNCTION c.s.f(x INT) RETURNS INT LANGUAGE PYTHON AS $$ return x $$;",
            "only SQL functions",
            1,
        ),
        ("CREATE VIEW c.s.v (a) AS SELECT 1;", "column list", 1),
        ("SELECT 1;", "starts with CREATE TABLE", 1),
        ("-- nothing here\n", "the SQL spec is empty", 1),
    ],
)
def test_refusals_point_at_their_line(
    tmp_path: Path, text: str, message: str, line: int
) -> None:
    with pytest.raises(SpecError, match=message) as refused:
        load(tmp_path, text, catalog="main")
    assert refused.value.loc.line == line


def test_an_unresolved_bundle_variable_says_why(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="comes from the bundle but is a lookup"):
        load_spec(
            sql(tmp_path, "CREATE TABLE ${catalog}.s.t (id BIGINT);"),
            {},
            {"catalog": "is a lookup of a catalog"},
        )


# ---------------------------------------------------------------------------
# in a project
# ---------------------------------------------------------------------------


def test_a_project_mixes_yaml_and_sql(tmp_path: Path) -> None:
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "orders.sql").write_text(ORDERS_SQL)
    (tmp_path / "tables" / "customers.yml").write_text(
        "table: ${catalog}.sales.customers\ncolumns: [{name: id, type: bigint}]\n"
    )
    (tmp_path / "deltaplan.yml").write_text(
        "specs: [tables]\ntargets:\n  dev: {vars: {catalog: dev}}\n"
    )
    project = load_project(tmp_path / "deltaplan.yml")
    assert [p.name for p in spec_files(project)] == ["customers.yml", "orders.sql"]


# ---------------------------------------------------------------------------
# import --format sql: dump, then load back the same model
# ---------------------------------------------------------------------------

EVERYTHING = """\
CREATE TABLE main.sales.`odd orders` (
  order_id BIGINT NOT NULL COMMENT 'It\\'s the key',
  placed_at TIMESTAMP,
  placed_on DATE GENERATED ALWAYS AS (CAST(placed_at AS DATE)),
  line_id BIGINT GENERATED BY DEFAULT AS IDENTITY (START WITH 100 INCREMENT BY 10),
  status STRING DEFAULT 'new',
  `my amount` DECIMAL(18, 2),
  address STRUCT<street: STRING NOT NULL COMMENT 'o\\'brien lane', `zip code`: STRING>,
  lines ARRAY<STRUCT<sku: STRING, qty: INT>>,
  attrs MAP<STRING, STRING>,
  customer_id BIGINT,
  PRIMARY KEY (order_id),
  CONSTRAINT fk_c FOREIGN KEY (customer_id) REFERENCES main.sales.customers (id),
  CONSTRAINT positive CHECK (`my amount` > 0)
)
COMMENT 'Order\\'s facts'
CLUSTER BY AUTO
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true', 'note' = 'back\\\\slash');
ALTER TABLE main.sales.`odd orders` SET TAGS ('domain' = 'sales', 'owner' = 'o\\'brien');
GRANT SELECT ON TABLE main.sales.`odd orders` TO `data analysts`;
GRANT MODIFY, SELECT ON TABLE main.sales.`odd orders` TO `etl`;
"""


@pytest.mark.parametrize(
    "text",
    [
        EVERYTHING,
        "CREATE VIEW main.sales.v\nCOMMENT 'It\\'s big'\n"
        "TBLPROPERTIES ('a' = 'b')\nAS\nSELECT id,\n  amount -- as booked\n"
        "FROM main.sales.orders;\n"
        "ALTER VIEW main.sales.v SET TAGS ('domain' = 'sales');\n"
        "GRANT SELECT ON VIEW main.sales.v TO `analysts`;",
        "CREATE FUNCTION main.sales.band(amount DECIMAL(18, 2), r STRING)\n"
        "RETURNS STRING\nCOMMENT 'b'\n"
        "RETURN CASE WHEN amount < 100 THEN r ELSE 'x' END;\n"
        "GRANT EXECUTE ON FUNCTION main.sales.band TO `analysts`;",
    ],
    ids=["table", "view", "function"],
)
def test_a_dump_loads_back_as_the_same_model(tmp_path: Path, text: str) -> None:
    from deltaplan.sqlspec import dump_sql_spec

    original = load(tmp_path, text)
    dumped = dump_sql_spec(original, catalog_variable="catalog")
    again = load_spec(sql(tmp_path, dumped, "dumped.sql"), {"catalog": "main"})
    assert again == original, dumped


def test_a_dump_puts_the_catalog_behind_its_variable(tmp_path: Path) -> None:
    from deltaplan.sqlspec import dump_sql_spec

    dumped = dump_sql_spec(load(tmp_path, EVERYTHING), catalog_variable="catalog")
    assert "CREATE TABLE ${catalog}.sales.`odd orders` (" in dumped
    assert "REFERENCES ${catalog}.sales.customers (id)" in dumped, (
        "a foreign key into the same catalog follows the variable too"
    )


def test_what_sql_cannot_say_goes_to_yaml() -> None:
    from dataclasses import replace

    from deltaplan.model.table import RowFilter
    from deltaplan.model.types import Mask
    from deltaplan.sqlspec import dump_sql_spec, sql_cannot_say

    plain = Table("main.s.t", (Field("email", Primitive("string")),))
    assert sql_cannot_say(plain) is None
    masked = replace(
        plain,
        columns=(
            Field(
                "email", Primitive("string"), mask=Mask("main.s.m"), tags=(("pii", "y"),)
            ),
        ),
        row_filter=RowFilter("main.s.f", ("email",)),
    )
    assert sql_cannot_say(masked) == "column tags, column masks, a row filter"
    with pytest.raises(ValueError, match="can't say column tags"):
        dump_sql_spec(masked)
