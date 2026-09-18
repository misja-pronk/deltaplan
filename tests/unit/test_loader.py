"""Specs load into the model, and bad specs fail with a file:line:column."""

from pathlib import Path

import pytest

from deltaplan.loader import (
    SpecError,
    load_project,
    load_specs,
    load_table,
    spec_files,
    validate_table,
)
from deltaplan.model.table import Check, PrimaryKey, Table
from deltaplan.model.types import Array, Field, Map, Primitive, Struct, render_type

SPEC = """
table: ${catalog}.sales.orders
comment: Order facts
cluster_by: [order_date]
tags: {domain: sales}
properties:
  delta.enableChangeDataFeed: "true"
columns:
  - name: order_id
    type: bigint
    nullable: false
  - name: order_date
    type: date
  - name: customer_ref
    type: string
    renamed_from: cust_id
  - name: address
    type:
      struct:
        - {name: street, type: string}
        - {name: zip, type: string, comment: Postal code}
constraints:
  - primary_key: [order_id]
"""


def write(tmp_path: Path, text: str, name: str = "orders.yml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_loads_the_design_documents_example(tmp_path: Path) -> None:
    table = load_table(write(tmp_path, SPEC), {"catalog": "main"})
    assert table.name == "main.sales.orders"
    assert table.comment == "Order facts"
    assert table.cluster_by == ("order_date",)
    assert table.tags == (("domain", "sales"),)
    assert table.properties == (("delta.enableChangeDataFeed", "true"),)
    assert table.column_names == ("order_id", "order_date", "customer_ref", "address")
    assert table.column("order_id") == Field(
        "order_id", Primitive("bigint"), nullable=False
    )
    customer_ref = table.column("customer_ref")
    assert customer_ref is not None and customer_ref.renamed_from == "cust_id"
    assert table.constraints == (PrimaryKey(("order_id",)),)
    assert table.schema == "main.sales"
    assert table.short_name == "orders"


def test_both_type_notations_agree(tmp_path: Path) -> None:
    as_string = load_table(
        write(
            tmp_path,
            "table: c.s.t\ncolumns:\n"
            "  - {name: a, type: 'struct<street:string,zip:string>'}\n",
        )
    )
    as_yaml = load_table(
        write(
            tmp_path,
            """
table: c.s.t
columns:
  - name: a
    type:
      struct:
        - {name: street, type: string}
        - {name: zip, type: string}
""",
            name="nested.yml",
        )
    )
    assert as_string.columns == as_yaml.columns


def test_nested_yaml_arrays_and_maps(tmp_path: Path) -> None:
    table = load_table(
        write(
            tmp_path,
            """
table: c.s.t
columns:
  - name: lines
    type:
      array:
        element:
          struct:
            - {name: sku, type: string, renamed_from: item_code}
  - name: by_code
    type:
      map:
        key: string
        value: int
  - name: shorthand
    type:
      array: int
""",
        )
    )
    lines = table.column("lines")
    assert lines is not None
    assert isinstance(lines.type, Array)
    assert isinstance(lines.type.element, Struct)
    assert lines.type.element.fields[0].renamed_from == "item_code"
    by_code = table.column("by_code")
    assert by_code is not None
    assert by_code.type == Map(Primitive("string"), Primitive("int"))
    shorthand = table.column("shorthand")
    assert shorthand is not None
    assert shorthand.type == Array(Primitive("int"))


def test_comments_and_nullability_survive_into_the_type(tmp_path: Path) -> None:
    table = load_table(
        write(
            tmp_path,
            """
table: c.s.t
columns:
  - name: a
    type:
      struct:
        - {name: zip, type: string, nullable: false, comment: "Postal code, unformatted"}
""",
        )
    )
    column = table.column("a")
    assert column is not None
    assert (
        render_type(column.type)
        == "struct<zip:string not null comment 'Postal code, unformatted'>"
    )


def test_constraints(tmp_path: Path) -> None:
    table = load_table(
        write(
            tmp_path,
            """
table: c.s.t
columns:
  - {name: id, type: bigint, nullable: false}
constraints:
  - primary_key: {columns: [id], name: orders_pk}
  - check: {name: positive, expression: "id > 0"}
""",
        )
    )
    assert table.constraints == (
        PrimaryKey(("id",), "orders_pk"),
        Check("positive", "id > 0"),
    )


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (
            "columns: [{name: a, type: int}]\n",
            "needs a 'table', 'view' or 'function' key",
        ),
        ("table: c.s.t\n", "needs a 'columns' key"),
        ("table: c.s.t\ncolumns: []\n", "at least one column"),
        (
            "table: c.s.t\ncolumns: [{name: a, type: int}]\nnope: 1\n",
            "unknown key 'nope'",
        ),
        (
            "table: c.s.t\ncolumns: [{name: a, type: int, nulable: true}]\n",
            "unknown key 'nulable'",
        ),
        ("table: c.s.t\ncolumns: [{type: int}]\n", "needs a 'name' key"),
        ("table: c.s.t\ncolumns: [{name: a}]\n", "needs a 'type' key"),
        ("table: c.s.t\ncolumns: [{name: a, type: strin g}]\n", "unexpected trailing"),
        (
            "table: c.s.t\ncolumns: [{name: a, type: int, nullable: yep}]\n",
            "must be true or false",
        ),
        (
            "table: c.s.t\nproperties: {delta.enableChangeDataFeed: true}\n"
            "columns: [{name: a, type: int}]\n",
            "must be a string",
        ),
        (
            "table: c.s.t\ncolumns: [{name: a, type: {struct: [], array: []}}]\n",
            "exactly one of struct, array or map",
        ),
        (
            "table: c.s.t\ncolumns: [{name: a, type: int}]\n"
            "constraints: [{foreign_key: [a]}]\n",
            "a foreign key must be a mapping",
        ),
        (
            "table: c.s.t\ncolumns: [{name: a, type: int}]\n"
            "constraints: [{unique: [a]}]\n",
            "unknown constraint 'unique'",
        ),
        (
            "table: c.s.t\ncolumns: [{name: a, type: int}]\ntable: c.s.u\n",
            "duplicate key",
        ),
        ("", "spec file is empty"),
        ("table: [1, 2]\ncolumns: []\n", "must be a single value"),
    ],
)
def test_spec_errors(tmp_path: Path, spec: str, message: str) -> None:
    with pytest.raises(SpecError, match=message):
        load_table(write(tmp_path, spec))


def test_errors_carry_file_line_and_column(tmp_path: Path) -> None:
    path = write(tmp_path, "table: c.s.t\ncolumns:\n  - name: a\n    typo: int\n")
    with pytest.raises(SpecError) as raised:
        load_table(path)
    assert raised.value.loc.file == path
    assert raised.value.loc.line == 4
    assert raised.value.loc.column == 5
    assert str(path) in str(raised.value)


def test_undefined_variable_points_at_its_line(tmp_path: Path) -> None:
    path = write(tmp_path, "table: ${catalog}.s.t\ncolumns: [{name: a, type: int}]\n")
    with pytest.raises(SpecError, match=r"undefined variable \$\{catalog\}") as raised:
        load_table(path, {"other": "x"})
    assert raised.value.loc.line == 1


def test_invalid_yaml_reports_its_position(tmp_path: Path) -> None:
    path = write(tmp_path, "table: c.s.t\ncolumns: [\n")
    with pytest.raises(SpecError) as raised:
        load_table(path)
    assert raised.value.loc.file == path


# ---------------------------------------------------------------------------
# linting
# ---------------------------------------------------------------------------


def messages(table: Table) -> list[str]:
    return [f"{d.severity}: {d.message}" for d in validate_table(table, "spec.yml")]


def test_clean_spec_has_nothing_to_say(tmp_path: Path) -> None:
    assert (
        validate_table(load_table(write(tmp_path, SPEC), {"catalog": "main"}), "s") == ()
    )


def test_lints_names_columns_and_keys(tmp_path: Path) -> None:
    table = load_table(
        write(
            tmp_path,
            """
table: sales.orders
cluster_by: [nope]
columns:
  - {name: id, type: bigint}
  - {name: id, type: bigint}
  - {name: weird, type: geograhpy}
constraints:
  - primary_key: [id]
""",
        )
    )
    said = messages(table)
    assert any("must be catalog.schema.table" in m for m in said)
    assert any("duplicate column 'id'" in m for m in said)
    assert any("cluster_by column 'nope' is not in the spec" in m for m in said)
    assert any("must be declared nullable: false" in m for m in said)
    assert any("warning" in m and "unknown type 'geograhpy'" in m for m in said)


def test_lints_contradictory_rename(tmp_path: Path) -> None:
    table = load_table(
        write(
            tmp_path,
            """
table: c.s.t
columns:
  - {name: cust_id, type: string}
  - {name: customer_ref, type: string, renamed_from: cust_id}
""",
        )
    )
    assert any("is itself a column in this spec" in m for m in messages(table))


def test_lints_nested_duplicate_fields(tmp_path: Path) -> None:
    table = load_table(
        write(
            tmp_path,
            """
table: c.s.t
columns:
  - name: a
    type:
      struct:
        - {name: x, type: int}
        - {name: x, type: int}
""",
        )
    )
    assert any("a: duplicate field 'x'" in m for m in messages(table))


# ---------------------------------------------------------------------------
# project config
# ---------------------------------------------------------------------------

CONFIG = """
version: 1
specs: [tables]
history_schema: main.deltaplan
targets:
  dev:
    vars: {catalog: dev_catalog}
  prod:
    vars: {catalog: prod}
    warehouse_id: abc123
    mode: strict
"""


def test_project_and_targets(tmp_path: Path) -> None:
    (tmp_path / "deltaplan.yml").write_text(CONFIG)
    tables = tmp_path / "tables"
    tables.mkdir()
    (tables / "orders.yml").write_text(SPEC)

    project = load_project(tmp_path / "deltaplan.yml")
    assert project.history_schema == "main.deltaplan"
    assert spec_files(project) == (tables / "orders.yml",)

    dev = project.target("dev")
    assert dev.mode == "additive" and dev.warehouse_id is None
    prod = project.target("prod")
    assert prod.mode == "strict" and prod.warehouse_id == "abc123"

    loaded = load_specs(project, prod)
    assert [spec.table.name for spec in loaded] == ["prod.sales.orders"]

    with pytest.raises(KeyError, match="unknown target 'staging'"):
        project.target("staging")


def test_bad_mode_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "deltaplan.yml").write_text("targets:\n  dev:\n    mode: yolo\n")
    with pytest.raises(SpecError, match="mode must be 'additive' or 'strict'"):
        load_project(tmp_path / "deltaplan.yml")


def test_using_is_read_as_a_hint(tmp_path: Path) -> None:
    table = load_table(
        write(
            tmp_path,
            """
table: c.s.t
columns:
  - name: amount
    type: string
    using: "format_number(amount, 2)"
""",
        )
    )
    amount = table.column("amount")
    assert amount is not None
    assert amount.using == "format_number(amount, 2)"
    # It is a hint about how to get there, not part of the state, so it doesn't
    # make the column look different from the one it describes.
    assert amount == Field("amount", Primitive("string"))
    assert validate_table(table, "spec.yml") == ()


def test_using_on_a_nested_field_is_an_error(tmp_path: Path) -> None:
    table = load_table(
        write(
            tmp_path,
            """
table: c.s.t
columns:
  - name: a
    type:
      struct:
        - {name: b, type: string, using: "1"}
""",
        )
    )
    assert any("`using` applies to whole columns only" in m for m in messages(table))


def test_modes_are_per_schema_and_resolve_per_target(tmp_path: Path) -> None:
    (tmp_path / "deltaplan.yml").write_text(
        """
history_schema: ${catalog}.deltaplan
targets:
  dev:
    vars: {catalog: dev}
  prod:
    vars: {catalog: prod}
    mode: strict
schemas:
  ${catalog}.sales: strict
  ${catalog}.archive: additive
"""
    )
    project = load_project(tmp_path / "deltaplan.yml")
    dev, prod = project.target("dev"), project.target("prod")

    assert project.mode_for(dev, "dev.sales") == "strict"
    assert project.mode_for(dev, "dev.other") == "additive", "the target's default"
    assert project.mode_for(prod, "prod.archive") == "additive", "the schema wins"
    assert project.mode_for(prod, "prod.other") == "strict"

    assert project.history_schema_for(dev) == "dev.deltaplan"
    assert project.history_schema_for(prod) == "prod.deltaplan"


def test_a_schema_mode_must_name_catalog_and_schema(tmp_path: Path) -> None:
    (tmp_path / "deltaplan.yml").write_text("schemas:\n  sales: strict\n")
    with pytest.raises(SpecError, match="keyed catalog.schema"):
        load_project(tmp_path / "deltaplan.yml")


def test_a_history_schema_variable_the_target_lacks(tmp_path: Path) -> None:
    (tmp_path / "deltaplan.yml").write_text(
        "history_schema: ${catalog}.deltaplan\ntargets:\n  dev: {}\n"
    )
    project = load_project(tmp_path / "deltaplan.yml")
    with pytest.raises(KeyError, match="undefined variable"):
        project.history_schema_for(project.target("dev"))
