"""What deltaplan assumes Databricks does, as a list anyone can run.

Every plan rests on behaviour: that `REPLACE` keeps a table's tags and grants,
that a nested field's `NOT NULL` is an ordinary `ALTER`, that a warehouse runs
in ANSI mode. Those were settled against one workspace, on one runtime, on the
day the live suite last ran. Every other workspace is an assumption — so they
live here as probes, and `deltaplan verify` runs them in a scratch schema of
yours.

A probe is a name that reads true when it holds, the Databricks page it rests
on, what deltaplan does because of it, and a check. A check makes its own
objects through `Bench.named()` and says what the workspace did: `Disagrees`
(or a plain failed assertion) means this workspace does something else, and
`matters` says what that costs.

Nothing here touches anything outside the bench's schema, and the bench's schema
is one the caller made for the purpose. `tests/integration/test_live_assumptions.py`
runs this same list, so an assumption is written down once.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, TypeAlias

from deltaplan.advice import with_advice
from deltaplan.errors import DeltaplanError
from deltaplan.introspect import Introspector, Row, SqlRunner
from deltaplan.loader import MAX_CLUSTER_COLUMNS
from deltaplan.model.plan import Plan
from deltaplan.model.table import Seed, Table
from deltaplan.model.types import Column, Field
from deltaplan.model.view import Relation
from deltaplan.planning import plan_tables
from deltaplan.sql import quote_ident, quote_literal, quote_qualified
from deltaplan.typeparser import parse_type

if TYPE_CHECKING:
    from deltaplan.executor import ExecutionResult


class Disagrees(DeltaplanError):
    """This workspace does something other than deltaplan assumes.

    Raised by a probe's own checks and caught by `run`: it is a finding about a
    workspace, not a failure of the tool.
    """


#: How a probe came out. `unknown` is a probe that couldn't be carried out at
#: all — a statement the workspace refused for a reason of its own, a privilege
#: the caller hasn't got — which is worth telling apart from an answer.
Outcome: TypeAlias = Literal["held", "differed", "unknown"]


@dataclass(frozen=True, slots=True)
class Probe:
    """One assumption: what it says, where it is written down, what rests on it."""

    name: str
    docs: str
    matters: str
    check: Callable[[Bench], None]
    #: Minutes rather than seconds — `verify` leaves these out unless asked.
    slow: bool = False
    #: Needs a schema that keeps what it drops. Only `UNDROP` does, and such a
    #: schema holds the metastore's table quota for its recovery period.
    keeps_dropped: bool = False


@dataclass(frozen=True, slots=True)
class Result:
    """What one probe did, and the workspace's own words when it didn't hold."""

    probe: Probe
    outcome: Outcome
    detail: str | None = None

    @property
    def held(self) -> bool:
        return self.outcome == "held"

    @property
    def mark(self) -> str:
        return {"held": "✓", "differed": "✗", "unknown": "!"}[self.outcome]


@dataclass(frozen=True, slots=True)
class Bench:
    """Where a probe may make things, and what it plans and applies with.

    `schema` is a `catalog.schema` the caller created and will drop; a probe
    writes nothing outside it, and names everything through `named()` so that
    probes sharing one schema can't collide.

    `recoverable` makes a second schema that keeps what it drops — for the one
    probe about `UNDROP`. It is a callable because such a schema holds the
    metastore's table quota for a week, so it is only made if a probe asks.
    """

    runner: SqlRunner
    introspector: Introspector
    schema: str
    principal: str = "account users"
    recoverable: Callable[[], str] | None = None
    #: Set per probe by `run`, so every object it makes is its own.
    prefix: str = ""

    # -- where things go ---------------------------------------------------
    @property
    def catalog(self) -> str:
        return self.schema.split(".")[0]

    @property
    def bare(self) -> str:
        return self.schema.split(".")[-1]

    @property
    def info(self) -> str:
        """This catalog's `information_schema`, quoted."""
        return f"{quote_ident(self.catalog)}.information_schema"

    def full(self, stem: str) -> str:
        """The name of one of this probe's objects, as deltaplan spells it."""
        return f"{self.schema}.{self.prefix}{stem}"

    def short(self, stem: str) -> str:
        """The same name without its schema — what `information_schema` holds."""
        return f"{self.prefix}{stem}"

    def named(self, stem: str) -> str:
        """The same name, quoted, ready for a statement."""
        return quote_qualified(self.full(stem))

    def about(self, stem: str, *, schema_column: str = "table_schema") -> str:
        """A `WHERE` clause for one of this probe's objects in an info view."""
        return (
            f"WHERE {schema_column} = {quote_literal(self.bare)} "
            f"AND table_name = {quote_literal(self.short(stem))}"
        )

    # -- running things ----------------------------------------------------
    def sql(self, statement: str) -> tuple[Row, ...]:
        return self.runner.query(statement)

    def expect(self, held: bool, said: str) -> None:
        """Say what this workspace did, when it isn't what deltaplan assumes."""
        if not held:
            raise Disagrees(said)

    @contextmanager
    def refuses(self, match: str) -> Iterator[None]:
        """A statement deltaplan counts on being refused, and how."""
        try:
            yield
        except Disagrees:
            raise
        except Exception as error:  # noqa: BLE001 - whatever the workspace raised
            if match.casefold() not in str(error).casefold():
                raise Disagrees(f"it was refused, but with: {error}") from error
        else:
            raise Disagrees(f"it was allowed, and deltaplan counts on {match}")

    def fails(self, statement: str, match: str) -> None:
        with self.refuses(match):
            self.sql(statement)

    def values(self, rows: Sequence[Row], key: str) -> set[str]:
        return {str(row[key]) for row in rows}

    # -- planning and applying --------------------------------------------
    def planned(
        self,
        specs: Sequence[Relation],
        *,
        strict: bool = False,
        clone: bool = False,
    ) -> Plan:
        return plan_tables(
            specs,
            self.introspector,
            target="verify",
            tool_version="0",
            mode_for=lambda _schema: "strict" if strict else "additive",
            clone=clone,
        )

    def tried(self, plan: Plan, *, allow_destructive: bool = False) -> ExecutionResult:
        """Run a plan and hand back what happened, for a probe that expects it
        to stop."""
        from deltaplan.executor import Executor
        from deltaplan.history import MemoryHistory

        return Executor(self.runner, self.introspector, MemoryHistory()).apply(
            plan, allow_destructive=allow_destructive
        )

    def apply(self, plan: Plan) -> None:
        result = self.tried(plan, allow_destructive=True)
        self.expect(result.ok, f"deltaplan's own plan stopped: {result.error}")

    def changes(self, plan: Plan, table: str | None = None) -> list[tuple[str, str]]:
        """Every change in a plan, or only the ones about one table.

        A probe asks about its own table: several probes share a schema, and a
        plan in `strict` mode reports on everything in one.
        """
        return [
            (change.kind, change.path)
            for diff in plan.diffs
            if table is None or diff.table == table
            for change in diff.changes
        ]


def col(name: str, type_text: str) -> Column:
    """A column, from the type string a spec would have written."""
    return Field(name, parse_type(type_text))


def spec(*columns: Column, name: str) -> Table:
    """A table spec, the way a probe reads best. Anything else it needs —
    a seed, clustering — goes on with `dataclasses.replace`."""
    return Table(name=name, columns=columns)


# ---------------------------------------------------------------------------
# replacing a table
# ---------------------------------------------------------------------------


def _a_table_can_read_itself_in_a_replace(bench: Bench) -> None:
    name = bench.named("self")
    bench.sql(f"CREATE TABLE {name} (id INT)")
    bench.sql(f"INSERT INTO {name} VALUES (1), (2)")
    bench.sql(
        f"CREATE OR REPLACE TABLE {name} AS SELECT CAST(id AS STRING) AS id FROM {name}"
    )
    rows = bench.sql(f"SELECT id FROM {name}")
    bench.expect(len(rows) == 2, f"the table holds {len(rows)} rows, not the 2 it read")


def _a_replace_keeps_tags_and_grants(bench: Bench) -> None:
    name = bench.named("r")
    bench.sql(f"CREATE TABLE {name} (id INT, label STRING)")
    bench.sql(f"ALTER TABLE {name} SET TAGS ('domain' = 'x')")
    bench.sql(f"ALTER TABLE {name} ALTER COLUMN label SET TAGS ('pii' = 'none')")
    bench.sql(f"GRANT SELECT ON TABLE {name} TO {quote_ident(bench.principal)}")
    bench.sql(
        f"CREATE OR REPLACE TABLE {name} AS "
        f"SELECT CAST(id AS STRING) AS id, label FROM {name}"
    )

    where = bench.about("r", schema_column="schema_name")
    tags = bench.values(
        bench.sql(f"SELECT tag_name FROM {bench.info}.table_tags {where}"), "tag_name"
    )
    bench.expect(tags == {"domain"}, f"the table's tags after a replace: {sorted(tags)}")
    columns = bench.values(
        bench.sql(f"SELECT column_name FROM {bench.info}.column_tags {where}"),
        "column_name",
    )
    bench.expect(
        columns == {"label"}, f"the tagged columns after a replace: {sorted(columns)}"
    )
    grants = bench.values(
        bench.sql(
            f"SELECT privilege_type FROM {bench.info}.table_privileges {bench.about('r')}"
        ),
        "privilege_type",
    )
    bench.expect(grants == {"SELECT"}, f"the grants after a replace: {sorted(grants)}")


def _restore_undoes_a_replace(bench: Bench) -> None:
    name = bench.named("rs")
    bench.sql(f"CREATE TABLE {name} (id INT)")
    bench.sql(f"INSERT INTO {name} VALUES (1), (2)")
    [before] = bench.sql(f"DESCRIBE HISTORY {name} LIMIT 1")
    version = int(before["version"] or 0)
    bench.sql(
        f"CREATE OR REPLACE TABLE {name} AS SELECT CAST(id AS STRING) AS id FROM {name}"
    )
    bench.sql(f"RESTORE TABLE {name} TO VERSION AS OF {version}")

    types = bench.values(
        bench.sql(
            f"SELECT data_type FROM {bench.info}.columns {bench.about('rs')} "
            "AND column_name = 'id'"
        ),
        "data_type",
    )
    bench.expect(types == {"INT"}, f"the column's type after a restore: {sorted(types)}")
    rows = bench.sql(f"SELECT * FROM {name}")
    bench.expect(len(rows) == 2, f"the restored table holds {len(rows)} rows, not 2")


def _a_clone_outlives_the_rewrite(bench: Bench) -> None:
    wanted = spec(
        col("id", "bigint"), col("amount", "decimal(10,2)"), name=bench.full("o")
    )
    bench.apply(bench.planned([wanted]))
    bench.sql(f"INSERT INTO {bench.named('o')} VALUES (1, 9.5)")

    rewritten = replace(wanted, columns=(wanted.columns[0], col("amount", "string")))
    plan = bench.planned([rewritten], clone=True)
    first = plan.steps[0].title if plan.steps else "nothing"
    bench.expect(first == "CLONE backup", f"the first step of the rewrite is {first!r}")
    bench.apply(plan)

    backups = [
        live
        for live in bench.introspector.schema(bench.catalog, bench.bare).tables
        if "__deltaplan_backup_" in live.table.name
    ]
    bench.expect(len(backups) == 1, f"the rewrite left {len(backups)} backups, not 1")
    backup = backups[0].table
    bench.expect(
        not backup.managed,
        "the backup carries deltaplan's ownership marker, so a strict schema "
        "would plan to drop it",
    )
    rows = bench.sql(f"SELECT amount FROM {quote_qualified(backup.name)}")
    kept = [row["amount"] for row in rows]
    bench.expect(kept == ["9.50"], f"the backup holds {kept}, not the row the table had")

    after = bench.planned([rewritten], strict=True)
    bench.expect(
        bench.changes(after, rewritten.name) == [],
        f"the table still differs from its spec: {bench.changes(after, rewritten.name)}",
    )
    bench.expect(
        bench.changes(after, backup.name) == [],
        "a strict plan would change the backup the clone made",
    )


# ---------------------------------------------------------------------------
# replacing a view or a function
# ---------------------------------------------------------------------------


def _replacing_a_view_drops_what_it_carried(bench: Bench) -> None:
    view = bench.named("v")
    bench.sql(f"CREATE VIEW {view} TBLPROPERTIES ('team' = 'x') AS SELECT 1 AS id")
    bench.sql(f"ALTER VIEW {view} SET TAGS ('domain' = 'x')")
    bench.sql(f"GRANT SELECT ON TABLE {view} TO {quote_ident(bench.principal)}")
    properties = bench.sql(f"SHOW TBLPROPERTIES {view}")
    bench.expect(
        ("team", "x") in {(row["key"], row["value"]) for row in properties},
        "the view didn't take the property it was created with",
    )

    bench.sql(f"CREATE OR REPLACE VIEW {view} AS SELECT 2 AS id")
    where = bench.about("v", schema_column="schema_name")
    bench.expect(
        not bench.sql(f"SELECT tag_name FROM {bench.info}.table_tags {where}"),
        "a replaced view kept its tags, so deltaplan sets them twice",
    )
    bench.expect(
        not bench.sql(
            f"SELECT privilege_type FROM {bench.info}.table_privileges {bench.about('v')}"
        ),
        "a replaced view kept its grants, so deltaplan grants them twice",
    )
    bench.expect(
        not bench.sql(f"SHOW TBLPROPERTIES {view}"),
        "a replaced view kept its properties, so deltaplan sets them twice",
    )


def _replacing_a_function_drops_its_grants(bench: Bench) -> None:
    function = bench.named("f")
    bench.sql(f"CREATE FUNCTION {function}(x INT) RETURNS INT RETURN x + 1")
    bench.sql(f"GRANT EXECUTE ON FUNCTION {function} TO {quote_ident(bench.principal)}")
    bench.sql(f"CREATE OR REPLACE FUNCTION {function}(x INT) RETURNS INT RETURN x")
    left = bench.sql(
        f"SELECT privilege_type FROM {bench.info}.routine_privileges "
        f"WHERE routine_schema = {quote_literal(bench.bare)} "
        f"AND routine_name = {quote_literal(bench.short('f'))}"
    )
    bench.expect(
        not left, "a replaced function kept its grants, so deltaplan grants them twice"
    )


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------


def _a_nested_not_null_is_an_alter(bench: Bench) -> None:
    loose = spec(
        col("id", "int"), col("address", "struct<zip:string>"), name=bench.full("n")
    )
    strict = replace(
        loose, columns=(loose.columns[0], col("address", "struct<zip:string not null>"))
    )
    bench.apply(bench.planned([loose]))

    bench.sql(f"INSERT INTO {bench.named('n')} VALUES (1, NULL)")
    plan = bench.planned([strict])
    titles = [step.title for step in plan.steps]
    bench.expect(
        titles == ["SET NOT NULL"], f"deltaplan plans {titles} for a nested NOT NULL"
    )
    blocked = bench.tried(plan)
    bench.expect(
        not blocked.ok,
        "a row whose struct is NULL didn't count as a NULL field, so the "
        "precondition deltaplan checks isn't the one Databricks enforces",
    )

    bench.sql(f"DELETE FROM {bench.named('n')}")
    bench.apply(bench.planned([strict]))
    bench.expect(
        bench.changes(bench.planned([strict]), strict.name) == [],
        "the field's NOT NULL didn't read back",
    )
    bench.apply(bench.planned([loose]))
    bench.expect(
        bench.changes(bench.planned([loose]), loose.name) == [],
        "the field's NOT NULL couldn't be dropped again",
    )


def _generated_and_identity_columns_cannot_be_added(bench: Bench) -> None:
    name = bench.named("g")
    bench.sql(f"CREATE TABLE {name} (id INT, ts TIMESTAMP)")
    bench.fails(
        f"ALTER TABLE {name} ADD COLUMN k BIGINT GENERATED ALWAYS AS IDENTITY",
        "PARSE_SYNTAX_ERROR",
    )
    bench.fails(
        f"ALTER TABLE {name} ADD COLUMN d DATE GENERATED ALWAYS AS (CAST(ts AS DATE))",
        "PARSE_SYNTAX_ERROR",
    )


def _a_check_cannot_be_declared_inline(bench: Bench) -> None:
    bench.fails(
        f"CREATE TABLE {bench.named('c')} (id INT, CONSTRAINT positive CHECK (id > 0))",
        "Only PRIMARY KEY and FOREIGN KEY",
    )


def _the_widenings_we_call_metadata_are_allowed(bench: Bench) -> None:
    name = bench.named("w1")
    bench.sql(
        f"CREATE TABLE {name} (value BIGINT) "
        "TBLPROPERTIES ('delta.enableTypeWidening' = 'true')"
    )
    bench.sql(f"ALTER TABLE {name} ALTER COLUMN value TYPE DECIMAL(20,0)")
    live = bench.introspector.table(bench.full("w1"))
    bench.expect(live is not None, "the widened table couldn't be read back")
    assert live is not None
    found = live.table.columns[0].type
    bench.expect(
        found == parse_type("decimal(20,0)"),
        f"a bigint widened to decimal(20,0) reads back as {found}",
    )


def _the_widenings_we_call_rewrites_are_refused(bench: Bench) -> None:
    name = bench.named("w2")
    bench.sql(
        f"CREATE TABLE {name} (value BIGINT) "
        "TBLPROPERTIES ('delta.enableTypeWidening' = 'true')"
    )
    bench.fails(
        f"ALTER TABLE {name} ALTER COLUMN value TYPE DECIMAL(19,0)", "not supported"
    )


# ---------------------------------------------------------------------------
# clustering and partitioning
# ---------------------------------------------------------------------------


def _liquid_clustering_takes_four_keys(bench: Bench) -> None:
    columns = [f"c{i}" for i in range(MAX_CLUSTER_COLUMNS + 1)]
    definition = ", ".join(f"{name} INT" for name in columns)
    bench.sql(
        f"CREATE TABLE {bench.named('k4')} ({definition}) "
        f"CLUSTER BY ({', '.join(columns[:MAX_CLUSTER_COLUMNS])})"
    )
    bench.fails(
        f"CREATE TABLE {bench.named('k5')} ({definition}) "
        f"CLUSTER BY ({', '.join(columns)})",
        "DELTA_CLUSTER_BY_INVALID_NUM_COLUMNS",
    )


def _cluster_by_auto_is_accepted(bench: Bench) -> None:
    wanted = replace(
        spec(col("id", "bigint"), col("placed", "date"), name=bench.full("auto")),
        cluster_auto=True,
    )
    bench.apply(bench.planned([wanted]))
    live = bench.introspector.table(wanted.name)
    bench.expect(live is not None, "the table couldn't be read back")
    assert live is not None
    bench.expect(
        live.table.cluster_auto, "the table doesn't read back as CLUSTER BY AUTO"
    )
    bench.expect(
        bench.changes(bench.planned([wanted]), wanted.name) == [],
        "planning it again isn't empty, so every plan would cluster it afresh",
    )


def _a_clustered_table_can_be_partitioned_after_cluster_by_none(bench: Bench) -> None:
    name = bench.named("cp")
    bench.sql(f"CREATE TABLE {name} (id BIGINT, day DATE) CLUSTER BY (day)")
    bench.sql(f"ALTER TABLE {name} CLUSTER BY NONE")
    bench.sql(
        f"CREATE OR REPLACE TABLE {name} (id BIGINT, day DATE) PARTITIONED BY (day)"
    )
    live = bench.introspector.table(bench.full("cp"))
    bench.expect(live is not None, "the partitioned table couldn't be read back")
    assert live is not None
    bench.expect(
        live.table.partitioned_by == ("day",),
        f"it reads back partitioned by {live.table.partitioned_by}",
    )


# ---------------------------------------------------------------------------
# seeds
# ---------------------------------------------------------------------------


def _a_seed_loads_and_reads_back(bench: Bench) -> None:
    wanted = replace(
        spec(col("code", "string"), col("label", "string"), name=bench.full("seed")),
        seed=Seed(columns=("code", "label"), rows=(("EUR", "Euro"), ("USD", "Dollar"))),
    )
    bench.apply(bench.planned([wanted]))
    rows = bench.sql(f"SELECT code, label FROM {bench.named('seed')} ORDER BY code")
    found = [(row["code"], row["label"]) for row in rows]
    bench.expect(
        found == [("EUR", "Euro"), ("USD", "Dollar")],
        f"the seeded table holds {found}",
    )
    bench.expect(
        bench.changes(bench.planned([wanted]), wanted.name) == [],
        "the table differs from its spec after the seed was loaded, so every "
        "plan would load it again",
    )


# ---------------------------------------------------------------------------
# the rest
# ---------------------------------------------------------------------------


def _undrop_brings_back_a_table(bench: Bench) -> None:
    if bench.recoverable is None:
        # Not an answer about the workspace: the caller left out the schema this
        # needs, so the probe is reported as one that couldn't be tried.
        raise DeltaplanError(
            "no schema that keeps what it drops, so UNDROP can't be tried here"
        )
    name = quote_qualified(f"{bench.recoverable()}.{bench.prefix}u")
    bench.sql(f"CREATE TABLE {name} (id INT)")
    bench.sql(f"INSERT INTO {name} VALUES (1)")
    bench.sql(f"DROP TABLE {name}")
    bench.sql(f"UNDROP TABLE {name}")
    rows = bench.sql(f"SELECT id FROM {name}")
    bench.expect(
        [row["id"] for row in rows] == ["1"], f"the table came back holding {rows}"
    )


def _warehouses_run_in_ansi_mode(bench: Bench) -> None:
    setting = bench.sql("SET ANSI_MODE")
    found = {(row["key"], row["value"]) for row in setting}
    bench.expect(
        ("ANSI_MODE", "true") in found, f"the warehouse says ANSI_MODE is {found}"
    )
    bench.fails("SELECT CAST('abc' AS INT) AS x", "CAST_INVALID_INPUT")


def _a_missing_catalog_fails_introspection(bench: Bench) -> None:
    with bench.refuses("TABLE_OR_VIEW_NOT_FOUND"):
        bench.introspector.schema("deltaplan_no_such_catalog", "anything")


def _a_grant_on_the_schema_is_not_the_tables(bench: Bench) -> None:
    wanted = spec(col("id", "int"), name=bench.full("i"))
    bench.apply(bench.planned([wanted]))
    bench.sql(
        f"GRANT SELECT ON SCHEMA {quote_qualified(bench.schema)} "
        f"TO {quote_ident(bench.principal)}"
    )
    bench.sql(
        f"GRANT MODIFY ON TABLE {bench.named('i')} TO {quote_ident(bench.principal)}"
    )
    live = bench.introspector.table(wanted.name)
    bench.expect(live is not None, "the table couldn't be read back")
    assert live is not None
    grants = live.table.grants
    bench.expect(
        [(g.principal, g.privileges) for g in grants] == [(bench.principal, ("MODIFY",))],
        f"deltaplan reads the table's grants as {grants}, which mixes in the "
        "schema's own",
    )


def _materialized_views_and_streaming_tables_are_left_alone(bench: Bench) -> None:
    source = spec(col("id", "int"), name=bench.full("src"))
    bench.apply(bench.planned([source]))
    bench.sql(
        f"CREATE MATERIALIZED VIEW {bench.named('mv')} AS "
        f"SELECT id FROM {bench.named('src')}"
    )
    bench.sql(
        f"CREATE STREAMING TABLE {bench.named('st')} AS "
        f"SELECT id FROM STREAM({bench.named('src')})"
    )
    live = bench.introspector.schema(bench.catalog, bench.bare)
    skipped = dict(live.skipped)
    bench.expect(
        skipped.pop(bench.full("mv"), None) == "materialized view",
        "a materialized view isn't reported as one",
    )
    bench.expect(
        skipped.pop(bench.full("st"), None) == "streaming table",
        "a streaming table isn't reported as one",
    )
    storage = {
        name: reason
        for name, reason in skipped.items()
        if name.split(".")[-1].startswith("__materialization_")
    }
    bench.expect(bool(storage), "neither kept its data in a table of its own")
    bench.expect(
        all(reason == "materialized view storage" for reason in storage.values()),
        f"the storage tables are reported as {sorted(set(storage.values()))}",
    )
    bench.expect(
        not [
            entry
            for entry in live.tables
            if entry.table.short_name.startswith("__materialization_")
        ],
        "a table Databricks made for one of them is read as a table of the schema",
    )


#: Every assumption, in the order the live suite settled them. Each is a probe
#: `deltaplan verify` can run in a workspace of its own.
PROBES: tuple[Probe, ...] = (
    Probe(
        "a table can read itself in a REPLACE … AS SELECT",
        "https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-table-using",
        "deltaplan stages a rewrite in a table of its own, so a refusal here "
        "costs nothing — this is why the staging exists.",
        _a_table_can_read_itself_in_a_replace,
    ),
    Probe(
        "a replace keeps a table's tags, column tags and grants",
        "https://docs.databricks.com/aws/en/delta/history",
        "A rewrite replaces the table rather than making a new one. If a replace "
        "lost them, every rewrite here would silently drop a table's tags and "
        "grants.",
        _a_replace_keeps_tags_and_grants,
    ),
    Probe(
        "RESTORE puts back the table a replace changed",
        "https://docs.databricks.com/aws/en/delta/history",
        "The restore point every risky step records is a Delta version. Without "
        "this, undoing a rewrite means the clone `plan --clone` makes.",
        _restore_undoes_a_replace,
    ),
    Probe(
        "a clone outlives the rewrite, and isn't deltaplan's",
        "https://docs.databricks.com/aws/en/delta/clone",
        "`plan --clone` takes a backup you can query afterwards. A clone copies "
        "the source's properties, so if it kept the ownership marker a strict "
        "schema would plan to drop the backup.",
        _a_clone_outlives_the_rewrite,
    ),
    Probe(
        "replacing a view drops its tags, grants and properties",
        "https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-view",
        "deltaplan sets them again after every `REPLACE VIEW`. If a view kept "
        "them, that is wasted work, not a wrong plan.",
        _replacing_a_view_drops_what_it_carried,
    ),
    Probe(
        "replacing a function drops its grants",
        "https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-sql-function",
        "deltaplan grants them again after every `REPLACE FUNCTION`.",
        _replacing_a_function_drops_its_grants,
    ),
    Probe(
        "a nested field's NOT NULL is an ordinary ALTER",
        "https://docs.databricks.com/aws/en/tables/constraints",
        "If it isn't, adding `not null` to a struct field means rebuilding the "
        "whole table — which is what the design first assumed and this settled.",
        _a_nested_not_null_is_an_alter,
    ),
    Probe(
        "generated and identity columns can't be added later",
        "https://docs.databricks.com/aws/en/delta/generated-columns",
        "Adding one is planned as a rewrite. If ALTER took it, that rewrite is "
        "more work than the change needs.",
        _generated_and_identity_columns_cannot_be_added,
    ),
    Probe(
        "a CHECK constraint can't be declared inside CREATE TABLE",
        "https://docs.databricks.com/aws/en/tables/constraints",
        "deltaplan follows a create with `ALTER TABLE … ADD CONSTRAINT`.",
        _a_check_cannot_be_declared_inline,
    ),
    Probe(
        "the widenings deltaplan calls metadata are allowed",
        "https://docs.databricks.com/aws/en/delta/type-widening",
        "A widening is planned as a metadata change. If this runtime refuses "
        "one, that plan fails at the step instead of rebuilding the table.",
        _the_widenings_we_call_metadata_are_allowed,
    ),
    Probe(
        "the widenings deltaplan calls rewrites are refused",
        "https://docs.databricks.com/aws/en/delta/type-widening",
        "The other edge: deltaplan rebuilds a table for these. If ALTER took "
        "them, the rebuild is more work than the change needs.",
        _the_widenings_we_call_rewrites_are_refused,
    ),
    Probe(
        "liquid clustering takes at most four keys",
        "https://docs.databricks.com/aws/en/delta/clustering",
        "`validate` refuses a fifth key before a plan is made. A workspace that "
        "takes more only means that check is stricter than it needs to be.",
        _liquid_clustering_takes_four_keys,
    ),
    Probe(
        "CLUSTER BY AUTO is accepted and reads back",
        "https://docs.databricks.com/aws/en/delta/clustering#automatic-liquid-clustering",
        "`cluster_auto: true` needs predictive optimization on the workspace. "
        "Without it, that spec can't be applied here — use explicit keys.",
        _cluster_by_auto_is_accepted,
    ),
    Probe(
        "a clustered table can be partitioned after CLUSTER BY NONE",
        "https://docs.databricks.com/aws/en/tables/partitions",
        "The move from clustering to partitioning is planned as `CLUSTER BY "
        "NONE` and then a rebuild. If that route is closed here, the move "
        "can't be made at all.",
        _a_clustered_table_can_be_partitioned_after_cluster_by_none,
    ),
    Probe(
        "a seed's INSERT OVERWRITE with a column list is accepted",
        "https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-dml-insert-into",
        "A seed is loaded with one `INSERT OVERWRITE … (columns) VALUES …`. It "
        "is the documented grammar, and this is what settles it on a runtime.",
        _a_seed_loads_and_reads_back,
    ),
    Probe(
        "UNDROP brings back a dropped table",
        "https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-undrop-table",
        "The hint deltaplan prints under a `DROP TABLE` step. Where it doesn't "
        "hold — a schema with no recovery period — the hint is wrong.",
        _undrop_brings_back_a_table,
        keeps_dropped=True,
    ),
    Probe(
        "the warehouse runs in ANSI mode",
        "https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-ansi-compliance",
        "A rewrite counts the rows a conversion would lose. Outside ANSI mode a "
        "bad cast becomes NULL instead of failing, and that count means nothing.",
        _warehouses_run_in_ansi_mode,
    ),
    Probe(
        "a missing catalog is an error, not an empty schema",
        "https://docs.databricks.com/aws/en/sql/language-manual/information-schema/schemata",
        "deltaplan creates schemas and never catalogs. If a missing catalog read "
        "as empty, a typo in one would plan every table as new.",
        _a_missing_catalog_fails_introspection,
    ),
    Probe(
        "a grant on the schema isn't a grant on its tables",
        "https://docs.databricks.com/aws/en/sql/language-manual/information-schema/table_privileges",
        "`table_privileges` lists inherited grants too. If deltaplan read one as "
        "the table's own, a strict spec would plan to revoke it — and couldn't.",
        _a_grant_on_the_schema_is_not_the_tables,
    ),
    Probe(
        "materialized views and streaming tables are left alone",
        "https://docs.databricks.com/aws/en/sql/language-manual/information-schema/tables",
        "Both are someone else's to manage, and each keeps its data in a table "
        "Databricks made. If deltaplan read those as tables of the schema, a "
        "strict schema would plan to drop them.",
        _materialized_views_and_streaming_tables_are_left_alone,
        slow=True,
    ),
)


def run(bench: Bench, probes: Sequence[Probe] = PROBES) -> Iterator[Result]:
    """Run each probe in turn, yielding what it found.

    A probe that says the workspace does something else — `Disagrees`, or a
    failed assertion — is a `differed`; anything else that stops it is an
    `unknown`, because a privilege or a stopped warehouse is not an answer
    about behaviour. Nothing raises: the results are the report.
    """
    for index, probe in enumerate(probes, start=1):
        this = replace(bench, prefix=f"p{index:02d}_")
        try:
            probe.check(this)
        except (Disagrees, AssertionError) as error:
            yield Result(probe, "differed", str(error) or error.__class__.__name__)
        except Exception as error:  # noqa: BLE001 - whatever the workspace raised
            yield Result(probe, "unknown", with_advice(str(error)))
        else:
            yield Result(probe, "held")


def chosen(*, slow: bool = False, keeps_dropped: bool = True) -> tuple[Probe, ...]:
    """The probes to run: all of them, minus the ones a caller leaves out.

    `slow` takes minutes rather than seconds (it starts a Databricks pipeline);
    `keeps_dropped` needs a schema that holds the metastore's table quota for
    its recovery period.
    """
    return tuple(
        probe
        for probe in PROBES
        if (slow or not probe.slow) and (keeps_dropped or not probe.keeps_dropped)
    )


def scratch_name() -> str:
    """A name for a scratch schema, unique enough to be nobody else's."""
    return f"deltaplan_verify_{uuid.uuid4().hex[:8]}"


@contextmanager
def scratch(
    runner: SqlRunner, where: str, *, keeps_dropped: bool = False, keep: bool = False
) -> Iterator[str]:
    """A schema for probes to work in, dropped with everything in it.

    `where` is either the `catalog.schema` to make — which must not already
    exist, because this drops what it made — or just a catalog, in which case
    the name is deltaplan's own.

    Unless `keeps_dropped`, the schema is told to keep nothing it drops: a
    dropped table still counts against the metastore's table quota for its
    recovery period, and nothing a probe makes is worth a week of that.
    https://docs.databricks.com/aws/en/data-governance/unity-catalog/resource-quotas
    """
    catalog, _, name = where.partition(".")
    if "." in name:
        raise DeltaplanError(f"a schema is named catalog.schema, not {where!r}")
    if not name:
        name = scratch_name()
    elif _exists(runner, catalog, name):
        raise DeltaplanError(
            f"{catalog}.{name} already exists, and verify drops the schema it "
            "makes. Name one that doesn't exist, or pass just the catalog."
        )
    full = f"{catalog}.{name}"
    runner.query(f"CREATE SCHEMA {quote_qualified(full)}")
    try:
        if not keeps_dropped:
            # Not every workspace has the setting; one that hasn't keeps its
            # dropped tables, which costs quota but settles nothing else.
            with suppress(Exception):
                runner.query(
                    f"ALTER SCHEMA {quote_qualified(full)} SET RETAIN DROPPED TO 0 HOURS"
                )
        yield full
    finally:
        if not keep:
            runner.query(f"DROP SCHEMA {quote_qualified(full)} CASCADE")


def _exists(runner: SqlRunner, catalog: str, name: str) -> bool:
    return bool(
        runner.query(
            f"SELECT schema_name FROM {quote_ident(catalog)}.information_schema.schemata "
            f"WHERE schema_name = {quote_literal(name)}"
        )
    )
