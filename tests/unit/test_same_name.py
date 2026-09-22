"""A volume and a table may share a name; deltaplan must not mix them up.

Tables, views, functions and volumes don't all share a namespace in Unity
Catalog, so `main.sales.landing` can be a table *and* a volume. Found by a host
integrating deltaplan: the executor looked a planned table up, was handed the
volume that shared its name, and refused the plan as stale — nothing had
changed at all.
https://docs.databricks.com/aws/en/volumes/
"""

from __future__ import annotations

from deltaplan import api
from deltaplan.connect import Connection
from deltaplan.executor import stale_tables
from deltaplan.history import MemoryHistory
from deltaplan.introspect import Introspector
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.model.volume import Volume
from deltaplan.planning import plan_tables
from fake_warehouse import FakeWarehouse
from helpers import col, table

NAME = "main.sales.landing"
MANAGED = ((MANAGED_PROPERTY, "true"),)


def with_volume() -> FakeWarehouse:
    """A volume called `landing`, and no table by that name — yet."""
    fake = FakeWarehouse()
    fake.schemas.add("main.sales")
    fake.volumes[NAME] = Volume(NAME, comment="files, not rows")
    return fake


def both() -> FakeWarehouse:
    """A table and a volume, under one name."""
    fake = FakeWarehouse.of(table(col("id", "bigint"), name=NAME, properties=MANAGED))
    fake.volumes[NAME] = Volume(NAME, comment="files, not rows")
    return fake


def test_a_lookup_answers_about_the_kind_it_was_asked_for() -> None:
    live = Introspector(both()).schema("main", "sales")
    assert isinstance(live.relation(NAME, "table"), Table)
    assert isinstance(live.relation(NAME, "volume"), Volume)


def test_a_table_that_isnt_there_is_not_the_volume_that_is() -> None:
    """The bug: asked about a table it doesn't have, the schema handed back the
    volume of the same name, and a plan to create the table looked stale."""
    live = Introspector(with_volume()).schema("main", "sales")
    assert live.relation(NAME, "table") is None
    assert isinstance(live.relation(NAME, "volume"), Volume)


def test_a_plan_to_create_the_table_is_not_stale(
    fake: FakeWarehouse | None = None,
) -> None:
    warehouse = fake or with_volume()
    desired = table(col("id", "bigint"), name=NAME)
    plan = plan_tables([desired], Introspector(warehouse), target="dev", tool_version="0")
    assert not plan.empty, "the table has to be created"
    assert stale_tables(plan, Introspector(warehouse)) == (), "nothing has moved"


def test_and_it_applies() -> None:
    warehouse = with_volume()
    desired = table(col("id", "bigint"), name=NAME)
    plan = plan_tables([desired], Introspector(warehouse), target="dev", tool_version="0")
    result = api.apply(plan, Connection(runner=warehouse), history=MemoryHistory())
    assert result.ok, result.error
    assert NAME in warehouse.tables
    assert NAME in warehouse.volumes, "the volume is left exactly where it was"
