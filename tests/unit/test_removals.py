"""Removing a tag or a property: `tags: {pii: null}`.

Leaving a key out of a spec only stops managing it — deltaplan can't tell that
from someone else's tag — so `null` is the one way a spec says "this must not be
there". Verified live (2026-09-19): UNSET TAGS on tables, columns, views, schemas
and volumes, and UNSET TBLPROPERTIES on tables and views; removing a key that
isn't there is a no-op rather than an error, so a resumed apply is safe.
https://docs.databricks.com/aws/en/database-objects/tags
"""

from dataclasses import replace
from pathlib import Path

import pytest

from deltaplan.differ import diff, is_applied, unmanaged
from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, load_spec
from deltaplan.model.change import Change
from deltaplan.model.plan import Plan
from deltaplan.model.schema import Schema
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.model.types import Field, Primitive
from deltaplan.model.view import Relation, View
from deltaplan.model.volume import Volume
from deltaplan.planning import plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.rich import plan_text
from fake_warehouse import FakeWarehouse
from helpers import col, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)


def planned(specs: list[Relation], fake: FakeWarehouse) -> Plan:
    return plan_tables(specs, Introspector(fake), target="t", tool_version="0")


def converge(specs: list[Relation], fake: FakeWarehouse) -> Plan:
    plan = planned(specs, fake)
    run(plan, fake)
    assert planned(specs, fake).empty, "re-planning after apply must be empty"
    return plan


def spec(tmp_path: Path, text: str) -> Relation:
    path = tmp_path / "spec.yml"
    path.write_text(text)
    return load_spec(path)


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------


def test_null_removes_and_a_value_sets(tmp_path: Path) -> None:
    loaded = spec(
        tmp_path,
        "table: main.sales.orders\n"
        "tags: {domain: sales, pii: null, legacy: ~}\n"
        "properties: {team: crm, 'delta.enableChangeDataFeed': null}\n"
        "columns:\n"
        "  - {name: email, type: string, tags: {pii: null, owner: crm}}\n",
    )
    assert isinstance(loaded, Table)
    assert loaded.tags == (("domain", "sales"),)
    assert loaded.removed_tags == ("pii", "legacy")
    assert loaded.properties == (("team", "crm"),)
    assert loaded.removed_properties == ("delta.enableChangeDataFeed",)
    assert loaded.columns[0].tags == (("owner", "crm"),)
    assert loaded.columns[0].removed_tags == ("pii",)


def test_a_removal_is_written_back_as_null(tmp_path: Path) -> None:
    from deltaplan.loader import dump_spec

    loaded = spec(
        tmp_path,
        "table: main.sales.orders\ntags: {pii: null}\nproperties: {team: null}\n"
        "columns:\n  - {name: email, type: string, tags: {pii: null}}\n",
    )
    (tmp_path / "again.yml").write_text(dump_spec(loaded))
    again = load_spec(tmp_path / "again.yml")
    assert isinstance(again, Table) and isinstance(loaded, Table)
    assert (again.removed_tags, again.removed_properties) == (("pii",), ("team",))
    assert again.columns[0].removed_tags == ("pii",)


def test_an_empty_value_is_not_a_removal(tmp_path: Path) -> None:
    """A line typed halfway must not delete a tag."""
    with pytest.raises(SpecError, match="has no value — give one, or write `null`"):
        spec(tmp_path, "table: t\ntags:\n  pii:\ncolumns:\n  - {name: a, type: int}\n")


def test_the_ownership_marker_cannot_be_removed(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="deltaplan.managed can't be removed"):
        spec(
            tmp_path,
            "table: t\nproperties: {deltaplan.managed: null}\n"
            "columns:\n  - {name: a, type: int}\n",
        )


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("view: main.sales.v\nquery: SELECT 1\ntags: {pii: null}\n", View),
        ("schema: main.sales\ntags: {pii: null}\n", Schema),
        ("volume: main.sales.landing\ntags: {pii: null}\n", Volume),
    ],
)
def test_every_kind_with_tags_can_remove_one(
    tmp_path: Path, text: str, kind: type
) -> None:
    loaded = spec(tmp_path, text)
    assert isinstance(loaded, kind)
    assert loaded.removed_tags == ("pii",)


# ---------------------------------------------------------------------------
# the plan, run against the fake
# ---------------------------------------------------------------------------

LIVE = table(
    Field("email", Primitive("string"), tags=(("owner", "crm"), ("pii", "email"))),
    name=NAME,
    properties=(*MANAGED, ("team", "crm"), ("retention", "30 days")),
    tags=(("domain", "sales"), ("pii", "true")),
)


def test_a_table_loses_what_the_spec_removes() -> None:
    desired = replace(
        table(
            Field(
                "email",
                Primitive("string"),
                tags=(("owner", "crm"),),
                removed_tags=("pii",),
            ),
            name=NAME,
            properties=(("team", "crm"),),
            tags=(("domain", "sales"),),
        ),
        removed_tags=("pii",),
        removed_properties=("retention",),
    )
    fake = FakeWarehouse.of(LIVE)
    plan = converge([desired], fake)
    assert [step.title for step in plan.steps] == [
        "UNSET TBLPROPERTIES",
        "UNSET TAGS",
        "UNSET COLUMN TAGS",
    ]
    after = fake.tables[NAME]
    assert dict(after.tags) == {"domain": "sales"}
    assert dict(after.columns[0].tags) == {"owner": "crm"}
    assert "retention" not in dict(after.properties)


def test_the_plan_says_what_goes_and_how_to_undo_it() -> None:
    desired = replace(
        table(col("email", "string"), name=NAME, properties=(("team", "crm"),)),
        tags=(("domain", "sales"),),
        removed_tags=("pii",),
    )
    plan = planned([desired], FakeWarehouse.of(LIVE))
    [step] = [s for s in plan.steps if s.title == "UNSET TAGS"]
    assert step.sql == "ALTER TABLE `main`.`sales`.`orders` UNSET TAGS ('pii')"
    assert step.undo_hint == (
        "ALTER TABLE `main`.`sales`.`orders` SET TAGS ('pii' = 'true')"
    )
    assert "- tag pii" in plan_text(plan)


def test_removing_what_is_not_there_plans_nothing() -> None:
    desired = replace(
        table(col("email", "string"), name=NAME, properties=(("team", "crm"),)),
        tags=(("domain", "sales"), ("pii", "true")),
        removed_tags=("gone",),
        removed_properties=("never_set",),
    )
    assert [c.kind for c in diff(desired, LIVE)] == []


def test_a_key_being_removed_is_not_reported_as_unmanaged() -> None:
    desired = replace(
        table(col("email", "string"), name=NAME, properties=(("team", "crm"),)),
        tags=(("domain", "sales"),),
        removed_tags=("pii",),
        removed_properties=("retention",),
    )
    found = unmanaged(desired, LIVE)
    assert "tag pii" not in found and "property retention" not in found


@pytest.mark.parametrize(
    ("live", "desired"),
    [
        (
            View("main.sales.v", "SELECT 1", tags=(("pii", "no"),)),
            View("main.sales.v", "SELECT 1", removed_tags=("pii",)),
        ),
        (
            View("main.sales.v", "SELECT 1", properties=(("team", "x"),)),
            View("main.sales.v", "SELECT 1", removed_properties=("team",)),
        ),
        (
            Schema("main.sales", tags=(("pii", "no"),)),
            Schema("main.sales", removed_tags=("pii",)),
        ),
        (
            Volume("main.sales.landing", tags=(("pii", "no"),)),
            Volume("main.sales.landing", removed_tags=("pii",)),
        ),
    ],
)
def test_views_schemas_and_volumes_lose_them_too(
    live: Relation, desired: Relation
) -> None:
    if isinstance(live, View):
        live = replace(live, properties=(*live.properties, *MANAGED))
    fake = FakeWarehouse.of(live)
    plan = converge([desired], fake)
    assert [s.title for s in plan.steps] in (["UNSET TAGS"], ["UNSET TBLPROPERTIES"])


def test_a_removal_survives_the_plan_file() -> None:
    desired = replace(
        table(
            Field("email", Primitive("string"), removed_tags=("pii",)),
            name=NAME,
            properties=(("team", "crm"),),
        ),
        tags=(("domain", "sales"), ("pii", "true")),
    )
    plan = planned([desired], FakeWarehouse.of(LIVE))
    assert loads(dumps(plan)) == plan


def test_a_resumed_removal_counts_as_done() -> None:
    change = Change(NAME, "unset_column_tag", "email", before=("pii", "email"))
    gone = replace(LIVE, columns=(col("email", "string"),))
    assert not is_applied(change, LIVE)
    assert is_applied(change, gone)
