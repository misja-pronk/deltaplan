"""The project as a host program meets it: load, resolve, read the specs.

These are the first three steps of the sequence in issue #17 — every host does
them, in this order, before it can plan. What they must not need is anything
private: no `_plan_for`, no reaching into the CLI, and no subprocess when the
host already holds the bundle's resolved configuration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import deltaplan
from deltaplan.loader import Project, SpecErrors, Specs

BUNDLE = """\
bundle:
  name: shop
resources:
  schemas:
    sales:
      catalog_name: main
      name: sales
targets:
  dev:
    default: true
    mode: development
"""

PROJECT = "specs: [tables]\nbundle: databricks.yml\n"
SPEC = "table: main.sales.orders\ncolumns:\n  - {name: id, type: bigint}\n"


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_a_project_loads_from_a_path_or_finds_itself(tmp_path: Path) -> None:
    write(tmp_path, "deltaplan.yml", "specs: [tables]\ntargets:\n  dev: {}\n")
    write(tmp_path, "tables/orders.yml", SPEC)
    by_path = Project.load(tmp_path / "deltaplan.yml")
    found = Project.find(tmp_path / "tables")
    assert by_path.root == found.root == tmp_path
    assert by_path.default.name == "dev", "the only target is the default"


def test_a_project_without_a_bundle_resolves_to_itself(tmp_path: Path) -> None:
    write(tmp_path, "deltaplan.yml", "specs: [tables]\ntargets:\n  dev: {}\n")
    project = Project.load(tmp_path / "deltaplan.yml")
    target = project.target("dev")
    assert project.resolve(target) is target


def test_a_resolved_bundle_is_taken_as_given(tmp_path: Path, monkeypatch) -> None:
    """A host that just deployed holds this mapping: no CLI runs."""
    write(tmp_path, "databricks.yml", BUNDLE)
    write(tmp_path, "deltaplan.yml", PROJECT)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    project = Project.load(tmp_path / "deltaplan.yml")
    config = {
        "bundle": {"name": "shop", "target": "dev"},
        "variables": {"catalog": {"value": "main"}, "warehouse_id": {"value": "abc123"}},
        "workspace": {"host": "https://example.cloud.databricks.com"},
        "resources": {
            "schemas": {"sales": {"catalog_name": "main", "name": "dev_jane_sales"}}
        },
    }
    resolved = project.resolve(project.target("dev"), bundle_config=config)
    assert resolved.warehouse_id == "abc123"
    assert resolved.host == "https://example.cloud.databricks.com"
    assert resolved.variables_map()["resources.schemas.sales.name"] == "dev_jane_sales"


def test_the_cli_is_asked_when_the_host_has_nothing(tmp_path: Path, monkeypatch) -> None:
    answer = json.dumps(
        {
            "bundle": {"name": "shop", "target": "dev"},
            "variables": {"catalog": {"value": "main"}},
            "resources": {
                "schemas": {"sales": {"catalog_name": "main", "name": "dev_jane_sales"}}
            },
        }
    )
    binary = write(
        tmp_path, "bin/databricks", f"#!/bin/bash\ncat <<'JSON'\n{answer}\nJSON\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{os.environ['PATH']}")
    write(tmp_path, "databricks.yml", BUNDLE)
    write(tmp_path, "deltaplan.yml", PROJECT)
    project = Project.load(tmp_path / "deltaplan.yml")
    resolved = project.resolve(project.target("dev"))
    assert resolved.variables_map()["resources.schemas.sales.name"] == "dev_jane_sales"


def test_a_failing_cli_says_what_it_said(tmp_path: Path, monkeypatch) -> None:
    """Its words, not ours: 'install the Databricks CLI' helps nobody who has."""
    binary = write(
        tmp_path,
        "bin/databricks",
        "#!/bin/bash\n>&2 echo 'Error: two profiles match this host'\nexit 1\n",
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{os.environ['PATH']}")
    write(tmp_path, "databricks.yml", BUNDLE)
    write(tmp_path, "deltaplan.yml", PROJECT)
    write(
        tmp_path,
        "tables/orders.yml",
        "table: main.${resources.schemas.sales.name}.orders\n"
        "columns:\n  - {name: id, type: bigint}\n",
    )
    project = Project.load(tmp_path / "deltaplan.yml")
    resolved = project.resolve(project.target("dev"))
    with pytest.raises(SpecErrors) as raised:
        project.load_specs(resolved)
    message = str(raised.value)
    assert "two profiles match this host" in message
    assert "install the Databricks CLI" not in message


def test_every_unreadable_spec_is_reported_at_once(tmp_path: Path) -> None:
    write(tmp_path, "deltaplan.yml", "specs: [tables]\ntargets:\n  dev: {}\n")
    write(tmp_path, "tables/a.yml", "table: main.sales.a\ncolumns:\n  - {name: id}\n")
    write(tmp_path, "tables/b.yml", "table: main.sales.b\nwat: true\n")
    write(tmp_path, "tables/c.yml", SPEC)
    project = Project.load(tmp_path / "deltaplan.yml")
    with pytest.raises(SpecErrors) as raised:
        project.load_specs(project.target("dev"))
    assert len(raised.value.errors) == 2, "both, not the first"
    assert "a.yml" in str(raised.value) and "b.yml" in str(raised.value)


def test_specs_carry_their_diagnostics(tmp_path: Path) -> None:
    """A spec that parses but is wrong is a judgement, not a refusal to read."""
    write(tmp_path, "deltaplan.yml", "specs: [tables]\ntargets:\n  dev: {}\n")
    write(
        tmp_path,
        "tables/orders.yml",
        "table: main.sales.orders\ncolumns:\n  - {name: id, type: bigint}\n"
        "constraints:\n  - primary_key: [id]\n",
    )
    project = Project.load(tmp_path / "deltaplan.yml")
    specs = project.load_specs(project.target("dev"))
    assert isinstance(specs, Specs)
    assert len(specs) == 1
    assert specs.relations[0].name == "main.sales.orders"
    # A primary key on a nullable column is an error a host must be able to see.
    assert [d.severity for d in specs.errors] == ["error"]
    assert specs.errors[0].message


def test_the_sdk_exports_what_these_tests_used() -> None:
    for name in ("Project", "Specs", "SpecErrors", "Bundle"):
        assert name in deltaplan.__all__, f"{name} should be public"
