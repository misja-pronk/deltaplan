"""What Databricks does that deltaplan's plans are built around.

Each test settles what was once a TODO(verify) in the code, and was first run on
2026-09-19. Several turned out other than assumed: a nested field's NOT NULL is an
ordinary ALTER, a map key widens in place, a shallow clone copies the ownership
marker, and REPLACE keeps a table's tags and grants while a replaced view or
function loses them.

  REPLACE, RESTORE  https://docs.databricks.com/aws/en/delta/history
  clones            https://docs.databricks.com/aws/en/delta/clone
  views             https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-view
  functions         https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-sql-function
  NOT NULL, CHECK   https://docs.databricks.com/aws/en/tables/constraints
  generated columns https://docs.databricks.com/aws/en/delta/generated-columns
  UNDROP            https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-undrop-table
  ANSI mode         https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-ansi-compliance
  clustering        https://docs.databricks.com/aws/en/delta/clustering
  privileges        https://docs.databricks.com/aws/en/sql/language-manual/information-schema/table_privileges
  table types       https://docs.databricks.com/aws/en/sql/language-manual/information-schema/tables
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import (
    IntrospectionError,
    Introspector,
    Row,
    WarehouseRunner,
)
from deltaplan.loader import MAX_CLUSTER_COLUMNS
from deltaplan.model.plan import Plan
from deltaplan.model.table import Grant
from deltaplan.model.view import Relation
from deltaplan.planner import create_table_sql
from deltaplan.planning import plan_tables
from deltaplan.sql import quote_qualified
from helpers import col, table

pytestmark = pytest.mark.integration

PRINCIPAL = os.environ.get("DELTAPLAN_TEST_PRINCIPAL", "account users")


def planned(
    specs: list[Relation],
    introspector: Introspector,
    *,
    strict: bool = False,
    clone: bool = False,
) -> Plan:
    return plan_tables(
        specs,
        introspector,
        target="integration",
        tool_version="0",
        mode_for=lambda _schema: "strict" if strict else "additive",
        clone=clone,
    )


def apply(plan: Plan, runner: WarehouseRunner, introspector: Introspector) -> None:
    result = Executor(runner, introspector, MemoryHistory()).apply(
        plan, allow_destructive=True
    )
    assert result.ok, result.error


def changes(plan: Plan) -> list[tuple[str, str]]:
    return [(c.kind, c.path) for d in plan.diffs for c in d.changes]


def names(rows: tuple[Row, ...], key: str) -> set[str]:
    return {str(row[key]) for row in rows}


def fails(runner: WarehouseRunner, sql: str, match: str) -> None:
    with pytest.raises(IntrospectionError, match=match):
        runner.query(sql)


# ---------------------------------------------------------------------------
# replacing a table
# ---------------------------------------------------------------------------


def test_replace_keeps_tags_and_grants_and_restore_undoes_it(
    runner: WarehouseRunner, schema: str
) -> None:
    """What a rewrite rests on: REPLACE keeps the table (tags, grants, history),
    and RESTORE to the version before it brings the old table back."""
    name = quote_qualified(f"{schema}.r")
    catalog, bare = schema.split(".")
    runner.query(f"CREATE TABLE {name} (id INT, label STRING)")
    runner.query(f"INSERT INTO {name} VALUES (1, 'a'), (2, 'b')")
    runner.query(f"ALTER TABLE {name} SET TAGS ('domain' = 'x')")
    runner.query(f"ALTER TABLE {name} ALTER COLUMN label SET TAGS ('pii' = 'none')")
    runner.query(f"GRANT SELECT ON TABLE {name} TO `{PRINCIPAL}`")
    [before] = runner.query(f"DESCRIBE HISTORY {name} LIMIT 1")
    runner.query(
        f"CREATE OR REPLACE TABLE {name} AS "
        f"SELECT CAST(id AS STRING) AS id, label FROM {name}"
    )

    where = f"WHERE schema_name = '{bare}' AND table_name = 'r'"
    info = f"`{catalog}`.information_schema"
    tags = runner.query(f"SELECT tag_name FROM {info}.table_tags {where}")
    assert names(tags, "tag_name") == {"domain"}
    column_tags = runner.query(f"SELECT column_name FROM {info}.column_tags {where}")
    assert names(column_tags, "column_name") == {"label"}
    grants = runner.query(
        f"SELECT privilege_type FROM {info}.table_privileges "
        f"WHERE table_schema = '{bare}' AND table_name = 'r'"
    )
    assert names(grants, "privilege_type") == {"SELECT"}

    runner.query(f"RESTORE TABLE {name} TO VERSION AS OF {before['version']}")
    types = runner.query(
        f"SELECT data_type FROM {info}.columns "
        f"WHERE table_schema = '{bare}' AND table_name = 'r' AND column_name = 'id'"
    )
    assert names(types, "data_type") == {"INT"}
    assert len(runner.query(f"SELECT * FROM {name}")) == 2


def test_a_backup_outlives_the_rewrite_and_is_not_deltaplans(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """`plan --clone`: the clone stays readable after the table is replaced, and
    carries `deltaplan.managed = false` — a clone copies the source's properties,
    so without it a strict schema would plan to drop the backup next time."""
    spec = table(col("id", "bigint"), col("amount", "decimal(10,2)"), name=f"{schema}.o")
    apply(planned([spec], introspector), runner, introspector)
    runner.query(f"INSERT INTO {quote_qualified(spec.name)} VALUES (1, 9.5)")

    rewritten = replace(spec, columns=(spec.columns[0], col("amount", "string")))
    plan = planned([rewritten], introspector, strict=True, clone=True)
    assert plan.steps[0].title == "CLONE backup"
    apply(plan, runner, introspector)

    [backup] = [
        live
        for live in introspector.schema(*schema.split(".")).tables
        if "__deltaplan_backup_" in live.table.name
    ]
    assert not backup.table.managed
    rows = runner.query(f"SELECT amount FROM {quote_qualified(backup.table.name)}")
    assert [row["amount"] for row in rows] == ["9.50"]
    assert changes(planned([rewritten], introspector, strict=True)) == []


# ---------------------------------------------------------------------------
# replacing a view or a function
# ---------------------------------------------------------------------------


def test_replacing_a_view_drops_its_tags_grants_and_properties(
    runner: WarehouseRunner, schema: str
) -> None:
    """Why the planner puts them back after `REPLACE VIEW`."""
    catalog, bare = schema.split(".")
    view = quote_qualified(f"{schema}.v")
    runner.query(f"CREATE VIEW {view} TBLPROPERTIES ('team' = 'x') AS SELECT 1 AS id")
    runner.query(f"ALTER VIEW {view} SET TAGS ('domain' = 'x')")
    runner.query(f"GRANT SELECT ON TABLE {view} TO `{PRINCIPAL}`")
    assert runner.query(f"SHOW TBLPROPERTIES {view}") == ({"key": "team", "value": "x"},)

    runner.query(f"CREATE OR REPLACE VIEW {view} AS SELECT 2 AS id")
    info = f"`{catalog}`.information_schema"
    assert not runner.query(
        f"SELECT * FROM {info}.table_tags "
        f"WHERE schema_name = '{bare}' AND table_name = 'v'"
    )
    assert not runner.query(
        f"SELECT * FROM {info}.table_privileges "
        f"WHERE table_schema = '{bare}' AND table_name = 'v'"
    )
    assert not runner.query(f"SHOW TBLPROPERTIES {view}")


def test_replacing_a_function_drops_its_grants(
    runner: WarehouseRunner, schema: str
) -> None:
    """Why the planner puts them back after `REPLACE FUNCTION`."""
    catalog, bare = schema.split(".")
    function = quote_qualified(f"{schema}.f")
    runner.query(f"CREATE FUNCTION {function}(x INT) RETURNS INT RETURN x + 1")
    runner.query(f"GRANT EXECUTE ON FUNCTION {function} TO `{PRINCIPAL}`")
    runner.query(f"CREATE OR REPLACE FUNCTION {function}(x INT) RETURNS INT RETURN x")
    assert not runner.query(
        f"SELECT * FROM `{catalog}`.information_schema.routine_privileges "
        f"WHERE routine_schema = '{bare}'"
    )


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------


def test_a_nested_not_null_is_an_alter(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """SET NOT NULL and DROP NOT NULL take a struct field's path, and a row whose
    struct is NULL counts as NULL there — which is what the precheck counts."""
    loose = table(
        col("id", "int"), col("address", "struct<zip:string>"), name=f"{schema}.n"
    )
    strict = replace(
        loose, columns=(loose.columns[0], col("address", "struct<zip:string not null>"))
    )
    apply(planned([loose], introspector), runner, introspector)

    runner.query(f"INSERT INTO {quote_qualified(loose.name)} VALUES (1, NULL)")
    plan = planned([strict], introspector)
    assert [step.title for step in plan.steps] == ["SET NOT NULL"]
    blocked = Executor(runner, introspector, MemoryHistory()).apply(plan)
    assert not blocked.ok, "a NULL struct must count as a NULL field"

    runner.query(f"DELETE FROM {quote_qualified(loose.name)}")
    apply(planned([strict], introspector), runner, introspector)
    assert planned([strict], introspector).empty
    apply(planned([loose], introspector), runner, introspector)
    assert planned([loose], introspector).empty


def test_generated_and_identity_columns_cannot_be_added(
    runner: WarehouseRunner, schema: str
) -> None:
    """Why adding one is planned as a rewrite."""
    name = quote_qualified(f"{schema}.g")
    runner.query(f"CREATE TABLE {name} (id INT, ts TIMESTAMP)")
    fails(
        runner,
        f"ALTER TABLE {name} ADD COLUMN k BIGINT GENERATED ALWAYS AS IDENTITY",
        "PARSE_SYNTAX_ERROR",
    )
    fails(
        runner,
        f"ALTER TABLE {name} ADD COLUMN d DATE GENERATED ALWAYS AS (CAST(ts AS DATE))",
        "PARSE_SYNTAX_ERROR",
    )


def test_a_check_cannot_be_declared_inline(runner: WarehouseRunner, schema: str) -> None:
    """Why CREATE TABLE is followed by ADD CONSTRAINT."""
    fails(
        runner,
        f"CREATE TABLE {quote_qualified(f'{schema}.c')} "
        "(id INT, CONSTRAINT positive CHECK (id > 0))",
        "Only PRIMARY KEY and FOREIGN KEY",
    )


def test_liquid_clustering_takes_four_keys(runner: WarehouseRunner, schema: str) -> None:
    """`MAX_CLUSTER_COLUMNS` is Databricks' limit, not ours."""
    columns = [f"c{i}" for i in range(MAX_CLUSTER_COLUMNS + 1)]
    definition = ", ".join(f"{c} INT" for c in columns)
    runner.query(
        f"CREATE TABLE {quote_qualified(f'{schema}.k4')} ({definition}) "
        f"CLUSTER BY ({', '.join(columns[:MAX_CLUSTER_COLUMNS])})"
    )
    fails(
        runner,
        f"CREATE TABLE {quote_qualified(f'{schema}.k5')} ({definition}) "
        f"CLUSTER BY ({', '.join(columns)})",
        "DELTA_CLUSTER_BY_INVALID_NUM_COLUMNS",
    )


# ---------------------------------------------------------------------------
# the rest
# ---------------------------------------------------------------------------


def test_undrop_brings_back_a_managed_table(runner: WarehouseRunner, schema: str) -> None:
    """The undo hint on DROP TABLE."""
    name = quote_qualified(f"{schema}.u")
    runner.query(create_table_sql(table(col("id", "int"), name=f"{schema}.u")))
    runner.query(f"INSERT INTO {name} VALUES (1)")
    runner.query(f"DROP TABLE {name}")
    runner.query(f"UNDROP TABLE {name}")
    assert runner.query(f"SELECT id FROM {name}") == ({"id": "1"},)


def test_warehouses_run_in_ansi_mode(runner: WarehouseRunner) -> None:
    """The rewrite's NULL-count check assumes a bad cast fails rather than
    quietly becoming NULL."""
    assert runner.query("SET ANSI_MODE") == ({"key": "ANSI_MODE", "value": "true"},)
    fails(runner, "SELECT CAST('abc' AS INT) AS x", "CAST_INVALID_INPUT")


def test_a_missing_catalog_fails_introspection(introspector: Introspector) -> None:
    """deltaplan creates schemas but never catalogs: a missing one is an error,
    not an empty schema to fill."""
    with pytest.raises(IntrospectionError, match="TABLE_OR_VIEW_NOT_FOUND"):
        introspector.schema("deltaplan_no_such_catalog", "anything")


def test_a_grant_on_the_schema_is_not_the_tables(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """table_privileges lists schema grants too, with inherited_from = 'SCHEMA'
    (a direct grant says 'NONE'); only the direct one is the table's."""
    spec = table(col("id", "int"), name=f"{schema}.i")
    runner.query(create_table_sql(spec))
    runner.query(f"GRANT SELECT ON SCHEMA {quote_qualified(schema)} TO `{PRINCIPAL}`")
    runner.query(f"GRANT MODIFY ON TABLE {quote_qualified(spec.name)} TO `{PRINCIPAL}`")
    live = introspector.table(spec.name)
    assert live is not None
    assert live.table.grants == (Grant(PRINCIPAL, ("MODIFY",)),)


def test_materialized_views_and_streaming_tables_are_left_alone(
    runner: WarehouseRunner, introspector: Introspector, schema: str
) -> None:
    """Their `table_type`s, and that the storage Databricks makes for them is not
    mistaken for tables of the schema's own. The pipelines' event logs are
    ordinary tables anyone can query, so they are left as such. Slow: each
    starts a pipeline."""
    source = table(col("id", "int"), name=f"{schema}.src")
    runner.query(create_table_sql(source))
    runner.query(
        f"CREATE MATERIALIZED VIEW {quote_qualified(f'{schema}.mv')} "
        f"AS SELECT id FROM {quote_qualified(source.name)}"
    )
    runner.query(
        f"CREATE STREAMING TABLE {quote_qualified(f'{schema}.st')} "
        f"AS SELECT id FROM STREAM({quote_qualified(source.name)})"
    )
    live = introspector.schema(*schema.split("."))
    skipped = dict(live.skipped)
    assert skipped.pop(f"{schema}.mv") == "materialized view"
    assert skipped.pop(f"{schema}.st") == "streaming table"
    assert skipped, "each keeps its data in a table of its own"
    assert all(
        name.split(".")[-1].startswith("__materialization_")
        and reason == "materialized view storage"
        for name, reason in skipped.items()
    ), skipped
    assert all(
        not t.table.short_name.startswith("__materialization_") for t in live.tables
    ), [t.table.name for t in live.tables]
