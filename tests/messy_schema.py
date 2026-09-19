"""A schema the way a real team ends up with one — built with plain SQL and
years of ALTERs, not by deltaplan.

It is the dogfooding fixture: `deltaplan import` it, and `deltaplan plan` must
come back empty. Every change a plan shows here is a bug in how deltaplan reads
real-world tables. It deliberately includes what deltaplan doesn't manage
(partitioning, a Python UDF, a non-default collation) — those must be reported,
never diffed.

`statements(schema)` gives the SQL for a `catalog.schema` that already exists;
each statement is run on its own.
"""

from __future__ import annotations

PRINCIPAL = "account users"


def statements(schema: str, principal: str = PRINCIPAL) -> list[tuple[str, str]]:
    """(label, SQL) pairs, in order."""
    s = ".".join(f"`{part}`" for part in schema.split("."))
    p = f"`{principal}`"
    return [
        # -- the schema itself ---------------------------------------------------
        ("schema comment", f"COMMENT ON SCHEMA {s} IS 'Sales data \\u2014 it\\'s messy'"),
        (
            "schema tags",
            f"ALTER SCHEMA {s} SET TAGS ('domain' = 'sales', 'tier' = 'gold')",
        ),
        ("schema grant", f"GRANT USE SCHEMA ON SCHEMA {s} TO {p}"),
        # -- functions: SQL ones are managed, a Python one is not -----------------
        (
            "mask function",
            f"CREATE FUNCTION {s}.mask_email(email STRING) RETURNS STRING "
            "COMMENT 'Hides emails from everyone but admins' "
            "RETURN CASE WHEN is_account_group_member('admins') THEN email "
            "ELSE concat('***@', split(email, '@')[1]) END",
        ),
        (
            "filter function",
            f"CREATE FUNCTION {s}.region_filter(region STRING) RETURNS BOOLEAN "
            "RETURN region IS NULL OR region <> 'restricted'",
        ),
        (
            "python udf",
            f"CREATE FUNCTION {s}.py_upper(s STRING) RETURNS STRING LANGUAGE PYTHON "
            "AS $$\nreturn s.upper() if s else s\n$$",
        ),
        ("function grant", f"GRANT EXECUTE ON FUNCTION {s}.mask_email TO {p}"),
        # -- customers: identity, a default, an awkward name, a PK ----------------
        (
            "customers",
            f"CREATE TABLE {s}.customers (\n"
            "  customer_id BIGINT GENERATED ALWAYS AS IDENTITY "
            "(START WITH 1000 INCREMENT BY 1) NOT NULL "
            "COMMENT 'Surrogate key \\u2014 generated',\n"
            "  email STRING COMMENT 'Masked for non-admins',\n"
            "  region STRING,\n"
            "  `Display Name` STRING COMMENT 'Mixed case, with a space',\n"
            "  created_at TIMESTAMP DEFAULT current_timestamp(),\n"
            "  CONSTRAINT customers_pk PRIMARY KEY (customer_id)\n"
            ") COMMENT 'Customers \\u2014 one row each.\\nSecond line of the comment.'\n"
            # A space in a column name needs column mapping from the start
            # (DELTA_INVALID_CHARACTERS_IN_COLUMN_NAMES otherwise).
            "TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported', "
            "'delta.columnMapping.mode' = 'name', 'owner.team' = 'crm')",
        ),
        (
            "customers rows",
            f"INSERT INTO {s}.customers (email, region, `Display Name`) VALUES "
            "('ann@example.com', 'eu', 'Ann'), ('bob@example.com', 'restricted', 'Bob')",
        ),
        (
            "customers mask",
            f"ALTER TABLE {s}.customers ALTER COLUMN email SET MASK {s}.mask_email",
        ),
        (
            "customers column tag",
            f"ALTER TABLE {s}.customers ALTER COLUMN email SET TAGS ('pii' = 'email')",
        ),
        ("customers grant", f"GRANT SELECT ON TABLE {s}.customers TO {p}"),
        # -- orders: years of ALTERs -----------------------------------------------
        (
            "orders",
            f"CREATE TABLE {s}.orders (\n"
            "  order_id BIGINT NOT NULL,\n"
            "  customer_id BIGINT,\n"
            "  placed_at TIMESTAMP,\n"
            "  placed_on DATE GENERATED ALWAYS AS (CAST(placed_at AS DATE)),\n"
            "  amount DECIMAL(10,2),\n"
            "  status STRING DEFAULT 'new',\n"
            # NOT NULL inside an array or map is refused
            # (DELTA_NESTED_NOT_NULL_CONSTRAINT); inside a plain struct it's fine.
            "  lines ARRAY<STRUCT<sku: STRING COMMENT 'stock keeping unit', "
            "qty INT, attrs MAP<STRING, ARRAY<STRUCT<k: STRING, v: STRING>>>>>,\n"
            "  shipping STRUCT<street: STRING NOT NULL, zip: STRING>,\n"
            "  `select` STRING COMMENT 'a reserved word as a name',\n"
            "  legacy_flag BOOLEAN,\n"
            "  CONSTRAINT orders_pk PRIMARY KEY (order_id),\n"
            f"  CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id) "
            f"REFERENCES {s}.customers (customer_id)\n"
            ") CLUSTER BY (placed_on)\n"
            "COMMENT 'Order facts'\n"
            "TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true', "
            "'delta.feature.allowColumnDefaults' = 'supported')",
        ),
        (
            "orders rows",
            # Delta enforces shipping.street NOT NULL even for a row with no
            # shipping at all (DELTA_NOT_NULL_CONSTRAINT_VIOLATED), so give one.
            f"INSERT INTO {s}.orders "
            "(order_id, customer_id, placed_at, amount, lines, shipping) "
            "VALUES (1, 1000, TIMESTAMP'2026-01-02 10:00:00', 12.50, "
            "array(named_struct('sku', 'A-1', 'qty', 2, 'attrs', "
            "map('colour', array(named_struct('k', 'hue', 'v', 'red'))))), "
            "named_struct('street', 'High St 1', 'zip', '1234AB'))",
        ),
        (
            "orders column mapping",
            f"ALTER TABLE {s}.orders SET TBLPROPERTIES "
            "('delta.columnMapping.mode' = 'name')",
        ),
        (
            "orders rename",
            f"ALTER TABLE {s}.orders RENAME COLUMN status TO order_status",
        ),
        ("orders drop", f"ALTER TABLE {s}.orders DROP COLUMN legacy_flag"),
        (
            "orders widening",
            f"ALTER TABLE {s}.orders SET TBLPROPERTIES "
            "('delta.enableTypeWidening' = 'true')",
        ),
        (
            "orders widen",
            f"ALTER TABLE {s}.orders ALTER COLUMN amount TYPE DECIMAL(18,2)",
        ),
        # After the widening: a column a CHECK uses can't change type
        # (DELTA_CONSTRAINT_DEPENDENT_COLUMN_CHANGE).
        (
            "orders check",
            f"ALTER TABLE {s}.orders ADD CONSTRAINT positive_amount CHECK (amount >= 0)",
        ),
        (
            "orders no deletion vectors",
            f"ALTER TABLE {s}.orders SET TBLPROPERTIES "
            "('delta.enableDeletionVectors' = 'false')",
        ),
        (
            "orders tags",
            f"ALTER TABLE {s}.orders SET TAGS ('domain' = 'sales', 'pii' = 'no')",
        ),
        (
            "orders column tag",
            f"ALTER TABLE {s}.orders ALTER COLUMN customer_id SET TAGS "
            "('join_key' = 'customers')",
        ),
        ("orders grant", f"GRANT SELECT, MODIFY ON TABLE {s}.orders TO {p}"),
        # -- events: legacy partitioning and the newer types ------------------------
        (
            "events",
            f"CREATE TABLE {s}.events (\n"
            "  event_id STRING,\n"
            "  ts TIMESTAMP_NTZ,\n"
            "  payload VARIANT,\n"
            "  kind CHAR(8),\n"
            "  note VARCHAR(200),\n"
            "  blob BINARY,\n"
            "  day DATE\n"
            ") PARTITIONED BY (day)\n"
            "TBLPROPERTIES ('delta.logRetentionDuration' = 'interval 60 days')",
        ),
        # -- people: a row filter, a mask and a collation -----------------------------
        (
            "people",
            f"CREATE TABLE {s}.people (\n"
            "  id BIGINT,\n"
            "  name STRING COLLATE UTF8_LCASE,\n"
            f"  email STRING MASK {s}.mask_email,\n"
            "  region STRING\n"
            f") WITH ROW FILTER {s}.region_filter ON (region)",
        ),
        # -- a table left to automatic clustering ------------------------------------
        (
            "sessions",
            f"CREATE TABLE {s}.sessions (session_id STRING, started TIMESTAMP, "
            "user_agent STRING) CLUSTER BY AUTO",
        ),
        # -- a CTAS, as people make them ---------------------------------------------
        (
            "daily",
            f"CREATE TABLE {s}.daily_orders AS SELECT placed_on, count(*) AS orders, "
            f"sum(amount) AS revenue FROM {s}.orders GROUP BY placed_on",
        ),
        # -- a view with a comment and a CTE ---------------------------------------
        (
            "view",
            f"CREATE VIEW {s}.big_orders COMMENT 'Orders over 1000' AS\n"
            "-- keep this comment: it is part of the query as written\n"
            f"WITH o AS (SELECT * FROM {s}.orders)\n"
            "SELECT order_id, customer_id, amount\n"
            "FROM o\n"
            "WHERE amount > 1000",
        ),
        ("view tags", f"ALTER VIEW {s}.big_orders SET TAGS ('domain' = 'sales')"),
        ("view grant", f"GRANT SELECT ON VIEW {s}.big_orders TO {p}"),
        # -- a managed volume --------------------------------------------------------
        ("volume", f"CREATE VOLUME {s}.landing COMMENT 'Raw files from the shop'"),
        ("volume tags", f"ALTER VOLUME {s}.landing SET TAGS ('domain' = 'sales')"),
        ("volume grant", f"GRANT READ VOLUME ON VOLUME {s}.landing TO {p}"),
    ]
