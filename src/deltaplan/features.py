"""What each spec format can say.

YAML is deltaplan's own format: it can say everything the model holds. SQL specs
are read with sqlglot, so they can say what sqlglot parses into structure — and
nothing it can't, even when Databricks accepts it. Some features are deltaplan's
own hints (`renamed_from`, `using`, hooks) with no SQL spelling at all.

This list is the one place that says which is which. `docs/formats.md` shows it
(regenerate with `python -m deltaplan.features docs/formats.md`), and the tests
prove every row: a SQL example marked supported loads into the model, and one
marked unsupported is refused with a pointer to YAML.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Feature:
    """One thing a spec can say, and whether each format can say it."""

    area: str
    name: str
    yaml: bool
    sql: bool
    #: A complete SQL spec using the feature — or, where SQL can't, the SQL
    #: Databricks would take and deltaplan refuses. None for deltaplan hints,
    #: which have no SQL spelling.
    example: str | None
    note: str = ""


_T = "CREATE TABLE main.sales.orders (\n  id BIGINT NOT NULL,\n  amount DECIMAL(18, 2)"


def _table(extra_columns: str = "", after: str = "", tail: str = "") -> str:
    return f"{_T}{extra_columns}\n){after};{tail}"


FEATURES: tuple[Feature, ...] = (
    # -- tables ----------------------------------------------------------------
    Feature(
        "Tables",
        "Columns and types, nested included",
        True,
        True,
        _table(
            ",\n  lines ARRAY<STRUCT<sku: STRING, qty: INT>>,"
            "\n  attrs MAP<STRING, STRING>"
        ),
        "struct, array, map, decimal, char/varchar, timestamp_ntz, variant",
    ),
    Feature(
        "Tables",
        "NOT NULL, on nested fields too",
        True,
        True,
        _table(",\n  address STRUCT<street: STRING NOT NULL, zip: STRING>"),
    ),
    Feature(
        "Tables",
        "Column comments",
        True,
        True,
        _table(",\n  note STRING COMMENT 'Free text'"),
    ),
    Feature(
        "Tables", "Table comment", True, True, _table(after="\nCOMMENT 'Order facts'")
    ),
    Feature(
        "Tables", "Liquid clustering keys", True, True, _table(after="\nCLUSTER BY (id)")
    ),
    Feature(
        "Tables",
        "Automatic liquid clustering",
        True,
        True,
        _table(after="\nCLUSTER BY AUTO"),
        "`cluster_by: auto` in YAML",
    ),
    Feature(
        "Tables",
        "Table properties",
        True,
        True,
        _table(after="\nTBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')"),
    ),
    Feature(
        "Tables",
        "Primary key",
        True,
        True,
        _table(",\n  CONSTRAINT orders_pk PRIMARY KEY (id)"),
    ),
    Feature(
        "Tables",
        "Foreign keys",
        True,
        True,
        _table(
            ",\n  customer_id BIGINT,\n  CONSTRAINT orders_customer_fk FOREIGN KEY "
            "(customer_id) REFERENCES main.sales.customers (id)"
        ),
    ),
    Feature(
        "Tables",
        "CHECK constraints",
        True,
        True,
        _table(",\n  CONSTRAINT positive_amount CHECK (amount > 0)"),
        "named: `CONSTRAINT <name> CHECK (…)`",
    ),
    Feature(
        "Tables",
        "Identity columns",
        True,
        True,
        _table(
            ",\n  line_id BIGINT GENERATED ALWAYS AS IDENTITY "
            "(START WITH 1 INCREMENT BY 1)"
        ),
    ),
    Feature(
        "Tables",
        "Generated columns",
        True,
        True,
        _table(
            ",\n  placed_at TIMESTAMP,"
            "\n  placed_on DATE GENERATED ALWAYS AS (CAST(placed_at AS DATE))"
        ),
    ),
    Feature(
        "Tables",
        "Column defaults",
        True,
        True,
        _table(",\n  status STRING DEFAULT 'new'"),
    ),
    Feature(
        "Tables",
        "Table tags",
        True,
        True,
        _table(tail="\nALTER TABLE main.sales.orders SET TAGS ('domain' = 'sales');"),
        "an `ALTER TABLE … SET TAGS` after the CREATE",
    ),
    Feature(
        "Tables",
        "Grants",
        True,
        True,
        _table(tail="\nGRANT SELECT ON TABLE main.sales.orders TO `analysts`;"),
        "`GRANT` statements after the CREATE",
    ),
    Feature(
        "Tables",
        "Column tags",
        True,
        False,
        _table(
            tail="\nALTER TABLE main.sales.orders ALTER COLUMN amount "
            "SET TAGS ('pii' = 'no');"
        ),
        "sqlglot passes `ALTER COLUMN … SET TAGS` through as unparsed text",
    ),
    Feature(
        "Tables",
        "Column masks",
        True,
        False,
        _table(",\n  email STRING MASK main.security.mask_email"),
        "sqlglot can't parse `MASK`",
    ),
    Feature(
        "Tables",
        "Row filters",
        True,
        False,
        _table(after="\nWITH ROW FILTER main.security.by_region ON (id)"),
        "sqlglot passes `WITH ROW FILTER` through as unparsed text",
    ),
    Feature(
        "Tables",
        "Removing a tag or property (`null`)",
        True,
        False,
        _table(tail="\nALTER TABLE main.sales.orders UNSET TAGS ('pii');"),
        "a SQL spec says what is there; `pii: null` in YAML says what isn't",
    ),
    Feature(
        "Tables",
        "Column renames (`renamed_from`)",
        True,
        False,
        None,
        "a deltaplan hint; SQL has no way to say it",
    ),
    Feature(
        "Tables",
        "Table renames (`renamed_from`)",
        True,
        False,
        None,
        "a deltaplan hint; SQL has no way to say it",
    ),
    Feature(
        "Tables",
        "Conversions and backfills (`using`)",
        True,
        False,
        None,
        "a deltaplan hint; SQL has no way to say it",
    ),
    Feature(
        "Tables", "Hooks", True, False, None, "deltaplan's own; SQL has no way to say it"
    ),
    Feature(
        "Tables",
        "Partitioning",
        False,
        False,
        _table(",\n  day DATE", after="\nPARTITIONED BY (day)"),
        "not modelled: reported on live tables, never managed",
    ),
    # -- schemas ---------------------------------------------------------------
    Feature(
        "Schemas",
        "Schemas: comment and grants",
        True,
        True,
        "CREATE SCHEMA main.sales COMMENT 'Sales data';\n"
        "GRANT USE SCHEMA, CREATE TABLE ON SCHEMA main.sales TO `analysts`;",
        "never dropped",
    ),
    Feature(
        "Schemas",
        "Schema tags",
        True,
        False,
        "CREATE SCHEMA main.sales;\n"
        "ALTER SCHEMA main.sales SET TAGS ('domain' = 'sales');",
        "sqlglot passes `ALTER SCHEMA … SET TAGS` through as unparsed text",
    ),
    # -- volumes ---------------------------------------------------------------
    Feature(
        "Volumes",
        "Managed volumes: comment, tags, grants",
        True,
        False,
        "CREATE VOLUME main.sales.landing COMMENT 'Raw files';",
        "sqlglot passes `CREATE VOLUME` and `GRANT … ON VOLUME` through as "
        "unparsed text; never dropped",
    ),
    # -- views -----------------------------------------------------------------
    Feature(
        "Views",
        "Views: query, comment, properties",
        True,
        True,
        "CREATE VIEW main.sales.big_orders\nCOMMENT 'Orders over 1000'\nAS\n"
        "SELECT id, amount FROM main.sales.orders WHERE amount > 1000;",
        "the query is kept exactly as written",
    ),
    Feature(
        "Views",
        "View tags and grants",
        True,
        True,
        "CREATE VIEW main.sales.big_orders AS SELECT id FROM main.sales.orders;\n"
        "ALTER VIEW main.sales.big_orders SET TAGS ('domain' = 'sales');\n"
        "GRANT SELECT ON VIEW main.sales.big_orders TO `analysts`;",
    ),
    # -- functions -------------------------------------------------------------
    Feature(
        "Functions",
        "SQL functions: parameters, return type, body, comment",
        True,
        True,
        "CREATE FUNCTION main.sales.order_band(amount DECIMAL(18, 2))\nRETURNS STRING\n"
        "COMMENT 'Small or large'\n"
        "RETURN CASE WHEN amount < 100 THEN 'small' ELSE 'large' END;",
        "the body is kept exactly as written",
    ),
    Feature(
        "Functions",
        "Function grants",
        True,
        True,
        "CREATE FUNCTION main.sales.one() RETURNS INT RETURN 1;\n"
        "GRANT EXECUTE ON FUNCTION main.sales.one TO `analysts`;",
    ),
)


START = "<!-- features:start -->"
END = "<!-- features:end -->"


def markdown() -> str:
    """The table `docs/formats.md` shows, generated from `FEATURES`."""
    lines = ["| | Feature | YAML | SQL | Notes |", "|---|---|:---:|:---:|---|"]
    area = None
    for feature in FEATURES:
        shown = feature.area if feature.area != area else ""
        area = feature.area
        lines.append(
            f"| {shown} | {feature.name} | {_mark(feature.yaml)} | {_mark(feature.sql)} "
            f"| {feature.note} |"
        )
    return "\n".join(lines)


def _mark(supported: bool) -> str:
    return "✓" if supported else "—"


def write(path: Path) -> None:
    """Replace the generated section of a markdown file with the current table."""
    text = path.read_text(encoding="utf-8")
    before, rest = text.split(START, 1)
    _, after = rest.split(END, 1)
    path.write_text(f"{before}{START}\n{markdown()}\n{END}{after}", encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - a maintenance script
    write(Path(sys.argv[1]))
