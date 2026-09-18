"""How much `plan` reads, and how fast.

Each table costs a DESCRIBE DETAIL and a SHOW CREATE TABLE, at about a second
apiece on a warehouse — a 300-table schema took minutes, one query after
another. So only tables a spec describes are read in full; the rest get a light
read (enough to list them and see whether they are deltaplan's); and the
per-table queries run several at a time.
"""

import threading
import time
from dataclasses import dataclass, field, replace

from deltaplan.executor import Executor
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector, Row
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.model.types import Field, Identity, Primitive
from deltaplan.planning import plan_tables
from fake_warehouse import FakeWarehouse
from helpers import col, table

MANAGED = ((MANAGED_PROPERTY, "true"),)
ORDERS = table(col("id", "bigint"), name="main.sales.orders", properties=MANAGED)
THEIRS = table(col("id", "bigint"), name="main.sales.theirs")
ORPHAN = Table(
    "main.sales.old_orders",
    (Field("id", Primitive("bigint"), identity=Identity()),),
    properties=MANAGED,
)


def shown(fake: FakeWarehouse) -> set[str]:
    """Which tables got a SHOW CREATE TABLE."""
    return {
        s.split(".")[-1].strip("`")
        for s in fake.statements
        if s.startswith("SHOW CREATE TABLE")
    }


def planned(fake: FakeWarehouse, specs: list[Table], *, strict: bool = False) -> Plan:
    return plan_tables(
        specs,
        Introspector(fake),
        target="t",
        tool_version="0",
        mode_for=lambda _schema: "strict" if strict else "additive",
    )


def test_only_tables_with_a_spec_are_read_in_full() -> None:
    fake = FakeWarehouse.of(ORDERS, THEIRS, ORPHAN)
    plan = planned(fake, [ORDERS])
    assert shown(fake) == {"orders"}
    assert plan.unmanaged_tables == ("main.sales.theirs",)
    assert plan.orphaned_tables == ("main.sales.old_orders",)
    details = [s for s in fake.statements if s.startswith("DESCRIBE DETAIL")]
    assert len(details) == 3, "every table still gets its light read"


def test_a_strict_orphan_is_read_in_full_before_its_drop() -> None:
    """Apply reads a drop target again, in full, to check nothing moved; the
    plan must have seen it the same way, or every drop would look stale."""
    fake = FakeWarehouse.of(ORDERS, THEIRS, ORPHAN)
    plan = planned(fake, [ORDERS], strict=True)
    assert shown(fake) == {"orders", "old_orders"}
    [drop] = [d for d in plan.diffs if d.table == ORPHAN.name]
    assert drop.live == ORPHAN, "complete, identity and all"

    result = Executor(
        runner=fake,
        introspector=Introspector(fake),
        history=MemoryHistory(),
        new_run_id=lambda: "run1",
    ).apply(plan, allow_destructive=True)
    assert result.ok, result.error
    assert ORPHAN.name not in fake.tables


def test_a_rename_source_is_read_in_full() -> None:
    fake = FakeWarehouse.of(ORPHAN)
    renamed = replace(
        table(col("id", "bigint"), name="main.sales.orders"), renamed_from=ORPHAN.name
    )
    planned(fake, [renamed])
    assert shown(fake) == {"old_orders"}


def test_apply_reads_only_its_own_tables_in_full() -> None:
    fake = FakeWarehouse.of(ORDERS, THEIRS)
    wider = table(*ORDERS.columns, col("note", "string"), name=ORDERS.name)
    plan = planned(fake, [wider])
    fake.statements.clear()
    Executor(
        runner=fake,
        introspector=Introspector(fake),
        history=MemoryHistory(),
        new_run_id=lambda: "run1",
    ).apply(plan)
    assert shown(fake) == {"orders"}


def test_a_light_read_can_be_completed() -> None:
    introspector = Introspector(FakeWarehouse.of(ORPHAN))
    light = introspector.schema("main", "sales", full=[]).get(ORPHAN.name)
    assert light is not None and not light.definition_read
    assert light.table.columns[0].identity is None, "only SHOW CREATE TABLE says"
    full = introspector.complete(light)
    assert full.definition_read and full.table == ORPHAN


@dataclass
class Slow:
    """A runner whose per-table reads take a moment, counting how many overlap."""

    inner: FakeWarehouse
    in_flight: int = 0
    most: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def query(self, statement: str) -> tuple[Row, ...]:
        if not statement.startswith(("DESCRIBE DETAIL", "SHOW CREATE TABLE")):
            return self.inner.query(statement)
        with self.lock:
            self.in_flight += 1
            self.most = max(self.most, self.in_flight)
        try:
            time.sleep(0.02)
            return self.inner.query(statement)
        finally:
            with self.lock:
                self.in_flight -= 1


def test_per_table_reads_run_several_at_a_time() -> None:
    tables = [
        table(col("id", "bigint"), name=f"main.sales.t{i}", properties=MANAGED)
        for i in range(8)
    ]
    together, alone = Slow(FakeWarehouse.of(*tables)), Slow(FakeWarehouse.of(*tables))
    parallel = Introspector(together, parallel=4).schema("main", "sales")
    serial = Introspector(alone, parallel=1).schema("main", "sales")
    assert 1 < together.most <= 4
    assert alone.most == 1
    assert parallel == serial, "the same answer either way"
