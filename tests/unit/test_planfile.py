"""A plan survives the trip through a file.

`apply` reads what `plan` wrote, so the two directions are tested together: a
plan that doesn't come back identical is a plan that would be applied wrong.
"""

import json

import pytest

from deltaplan.model.plan import Plan
from deltaplan.model.table import Check, PrimaryKey
from deltaplan.model.types import Decimal, Field, Primitive
from deltaplan.render.json import PLAN_FORMAT_VERSION, PlanFileError, dumps, loads
from helpers import col, plan_against, table

NAME = "main.sales.orders"

LIVE = table(
    col("order_id", "bigint", nullable=False),
    col("amount", "decimal(10,2)"),
    col("cust_id", "string"),
    col("address", "struct<street:string,old_zip:string>"),
    col("lines", "array<struct<sku:string,qty:int>>"),
    col("legacy_flag", "boolean"),
    name=NAME,
    comment="Order facts",
    constraints=(Check("positive", "amount > 0"),),
)

DESIRED = table(
    col("order_id", "bigint", nullable=False, comment="Surrogate key"),
    col("amount", "decimal(18,2)"),
    col("customer_ref", "string", renamed_from="cust_id"),
    col("address", "struct<street:string,old_zip:string,zip:string>"),
    col("lines", "array<struct<sku:string,qty:bigint>>"),
    name=NAME,
    comment="Order facts, one row per order",
    cluster_by=("order_id",),
    properties=(("delta.enableChangeDataFeed", "true"),),
    tags=(("domain", "sales"),),
    constraints=(
        PrimaryKey(("order_id",), "orders_pk"),
        Check("positive", "amount >= 0"),
    ),
)


def round_trip(plan: Plan) -> Plan:
    return loads(dumps(plan))


def test_a_busy_plan_round_trips() -> None:
    _, plan = plan_against(DESIRED, LIVE, size_bytes=442_381_631_488)
    assert plan.steps, "this plan should not be empty"
    assert round_trip(plan) == plan


def test_a_create_plan_round_trips() -> None:
    _, plan = plan_against(DESIRED)
    assert round_trip(plan) == plan


def test_an_empty_plan_round_trips() -> None:
    # Tables with nothing to do still belong in the file: `apply` recomputes the
    # fingerprint over exactly the tables that went into it.
    _, plan = plan_against(LIVE, LIVE)
    assert plan.steps == ()
    restored = round_trip(plan)
    assert restored == plan
    assert [d.table for d in restored.diffs] == [NAME]


def test_values_come_back_as_the_objects_they_were() -> None:
    _, plan = plan_against(DESIRED, LIVE)
    restored = round_trip(plan)
    by_kind = {(c.kind, c.path): c for c in restored.changes}

    widened = by_kind[("change_type", "amount")]
    assert widened.before == Decimal(10, 2)
    assert widened.after == Decimal(18, 2)

    added = by_kind[("add_column", "address.zip")]
    assert added.after == Field("zip", Primitive("string"))

    rename = by_kind[("rename_column", "customer_ref")]
    assert rename.before == "cust_id" and rename.after == "customer_ref"

    assert by_kind[("set_cluster_by", "")].after == ("order_id",)
    assert by_kind[("set_property", "delta.enableChangeDataFeed")].after == "true"

    constraints = [c for c in restored.changes if c.kind == "add_constraint"]
    assert PrimaryKey(("order_id",), "orders_pk") in [c.after for c in constraints]
    assert Check("positive", "amount >= 0") in [c.after for c in constraints]


def test_a_created_table_round_trips_whole() -> None:
    _, plan = plan_against(DESIRED)
    change = round_trip(plan).changes[0]
    assert change.kind == "create_table"
    assert change.after == DESIRED


def test_steps_keep_everything_apply_needs() -> None:
    _, plan = plan_against(DESIRED, LIVE)
    restored = round_trip(plan)
    assert restored.steps == plan.steps
    first = restored.steps[0]
    assert first.risk in {"meta", "feature", "rewrite", "destructive"}
    assert first.change >= 0, "the link back to its change survives"


def test_the_file_is_readable_without_deltaplan() -> None:
    _, plan = plan_against(DESIRED, LIVE)
    document = json.loads(dumps(plan))
    assert document["format_version"] == PLAN_FORMAT_VERSION
    types = [
        change["after"]["type"]
        for change in document["tables"][0]["changes"]
        if change["kind"] == "add_column"
    ]
    assert "string" in types, "types are plain Databricks type strings"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "not valid JSON"),
        ("[]", "a plan file is a JSON object"),
        ('{"format_version": 99}', "plan format version"),
        ('{"format_version": 1}', "malformed plan file"),
    ],
)
def test_unreadable_plan_files(text: str, message: str) -> None:
    with pytest.raises(PlanFileError, match=message):
        loads(text)
