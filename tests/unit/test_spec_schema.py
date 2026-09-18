"""The editor's JSON Schemas agree with the loader.

Each object's keys come from the loader's own key sets (a mismatch fails at
build time). These tests hold the rest: every example and everything `import`
writes validates; what the loader refuses, the schema refuses; the odd
spellings the loader accepts, the schema accepts too.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft7Validator

from deltaplan import spec_schema
from deltaplan.loader import dump_spec, load_spec
from deltaplan.model.table import Table

ROOT = Path(__file__).parents[2]
ID = [{"name": "id", "type": "int"}]
SPEC = Draft7Validator(spec_schema.spec_schema())
PROJECT = Draft7Validator(spec_schema.project_schema())


def errors(validator: Any, document: Any) -> list[str]:
    return [error.message for error in validator.iter_errors(document)]


def test_the_schemas_are_valid_json_schema() -> None:
    Draft7Validator.check_schema(spec_schema.spec_schema())
    Draft7Validator.check_schema(spec_schema.project_schema())


@pytest.mark.parametrize(
    "path", sorted((ROOT / "examples").rglob("*.yml")), ids=lambda p: p.name
)
def test_every_example_validates(path: Path) -> None:
    document = yaml.safe_load(path.read_text())
    validator = PROJECT if path.name == "deltaplan.yml" else SPEC
    if path.name == "databricks.yml":
        pytest.skip("a bundle, not deltaplan's")
    assert errors(validator, document) == []


def test_everything_import_writes_validates() -> None:
    from dataclasses import replace

    from deltaplan.model.table import RowFilter
    from deltaplan.model.types import Field, Identity, Mask, Primitive

    table = load_spec(ROOT / "tests" / "fixtures" / "everything.sql")
    assert isinstance(table, Table)
    governed = replace(
        table,
        columns=(
            *table.columns,
            Field(
                "email",
                Primitive("string"),
                tags=(("pii", "email"),),
                mask=Mask("main.s.mask_email", ("customer_id",)),
            ),
            Field("n", Primitive("bigint"), identity=Identity(False, 5, 2)),
        ),
        row_filter=RowFilter("main.s.by_region", ("customer_id",)),
    )
    document = yaml.safe_load(dump_spec(governed, catalog_variable="catalog"))
    assert errors(SPEC, document) == []


@pytest.mark.parametrize(
    ("spec", "problem"),
    [
        (
            {"table": "c.s.t", "columns": [{"name": "id", "type": "int"}], "colour": 1},
            "colour",
        ),
        (
            {
                "table": "c.s.t",
                "columns": [{"name": "id", "type": "int", "nulable": False}],
            },
            "nulable",
        ),
        (
            {
                "table": "c.s.t",
                "columns": [{"name": "id", "type": "int", "nullable": "no"}],
            },
            "boolean",
        ),
        ({"table": "c.s.t", "columns": [{"name": "id"}]}, "type"),
        ({"table": "c.s.t"}, "columns"),
        ({"table": "c.s.t", "columns": []}, "columns"),
        ({"table": "c.s.t", "columns": ID, "cluster_by": "id"}, "cluster_by"),
        ({"table": "c.s.t", "columns": ID, "properties": {"a": True}}, "string"),
        (
            {
                "table": "c.s.t",
                "columns": ID,
                "grants": [{"principal": "x", "privileges": ["EXECUTE"]}],
            },
            "EXECUTE",
        ),
        (
            {
                "function": "c.s.f",
                "returns": "int",
                "body": "1",
                "grants": [{"principal": "x", "privileges": ["SELECT"]}],
            },
            "SELECT",
        ),
        (
            {
                "table": "c.s.t",
                "columns": ID,
                "constraints": [{"check": {"expression": "a > 0"}}],
            },
            "name",
        ),
        ({"view": "c.s.v"}, "query"),
    ],
)
def test_what_the_loader_refuses_the_schema_refuses(
    tmp_path: Path, spec: dict[str, Any], problem: str
) -> None:
    from deltaplan.loader import SpecError

    assert errors(SPEC, spec), f"the schema accepted {spec}, despite its {problem}"
    path = tmp_path / "s.yml"
    path.write_text(yaml.safe_dump(spec))
    with pytest.raises(SpecError):
        load_spec(path)


@pytest.mark.parametrize(
    "spec",
    [
        {"table": "c.s.t", "columns": ID, "cluster_by": "AUTO"},
        {
            "table": "c.s.t",
            "columns": ID,
            "grants": [{"principal": "x", "privileges": ["select", "apply_tag"]}],
        },
        {
            "table": "c.s.t",
            "columns": [{"name": "id", "type": "bigint", "identity": "Always"}],
        },
        {
            "table": "c.s.t",
            "columns": [
                {
                    "name": "id",
                    "type": "bigint",
                    "identity": {"generated": "by default", "start": 5},
                }
            ],
        },
        {
            "table": "c.s.t",
            "columns": [
                {
                    "name": "a",
                    "type": {"array": {"element": "int", "contains_null": False}},
                }
            ],
        },
        {
            "table": "c.s.t",
            "columns": [
                {
                    "name": "m",
                    "type": {
                        "map": {
                            "key": "string",
                            "value": {"struct": [{"name": "x", "type": "int"}]},
                        }
                    },
                }
            ],
        },
    ],
)
def test_what_the_loader_accepts_the_schema_accepts(
    tmp_path: Path, spec: dict[str, Any]
) -> None:
    path = tmp_path / "s.yml"
    path.write_text(yaml.safe_dump(spec))
    load_spec(path)
    assert errors(SPEC, spec) == []


def test_the_published_schemas_are_current() -> None:
    for name, schema in (
        ("spec.json", spec_schema.spec_schema()),
        ("project.json", spec_schema.project_schema()),
    ):
        published = json.loads((ROOT / "docs" / "schema" / name).read_text())
        assert published == schema, (
            f"docs/schema/{name} is out of date: run "
            "`uv run python -m deltaplan.spec_schema docs/schema`"
        )
