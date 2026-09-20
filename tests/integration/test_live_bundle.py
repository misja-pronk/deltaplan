"""What the Databricks CLI resolves a bundle to — the thing deltaplan trusts.

deltaplan doesn't interpret a bundle where the CLI can answer for it: it asks
`databricks bundle validate -o json -t <target>` and takes what comes back.
This is the test that says the answer really carries what deltaplan reads out of
it — variables filled in, a `lookup:` run against the workspace, and the names a
deploy would use, which for a development target is not what the file says.

Needs the Databricks CLI on PATH as well as credentials; without it, it skips.
https://docs.databricks.com/aws/en/dev-tools/bundles/variables
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from deltaplan.bundle import resolve_target
from deltaplan.introspect import WarehouseRunner
from deltaplan.loader import as_deployed, load_project

pytestmark = pytest.mark.integration

BUNDLE = """\
bundle:
  name: deltaplan_it

variables:
  catalog:
    default: {catalog}
  team:
    default: sales
  warehouse_id:
    lookup:
      warehouse: {warehouse}

resources:
  schemas:
    sales:
      catalog_name: ${{var.catalog}}
      name: ${{var.team}}
  volumes:
    landing:
      catalog_name: ${{var.catalog}}
      schema_name: ${{resources.schemas.sales.name}}
      name: landing

targets:
  dev:
    default: true
    mode: development
  prefixed:
    presets:
      name_prefix: team_
"""


@pytest.fixture
def bundle(
    runner: WarehouseRunner, catalog: str, warehouse_id: str, tmp_path: Path
) -> Path:
    if shutil.which("databricks") is None:
        pytest.skip("the Databricks CLI is not on PATH")
    warehouse = runner.client.warehouses.get(warehouse_id)
    path = tmp_path / "databricks.yml"
    path.write_text(BUNDLE.format(catalog=catalog, warehouse=warehouse.name))
    return path


def test_the_cli_fills_in_variables_and_runs_the_lookup(
    bundle: Path, catalog: str, warehouse_id: str
) -> None:
    target = resolve_target(bundle, "dev")
    assert target is not None, "the CLI must answer for a workspace it is logged in to"
    variables = dict(target.variables)
    assert variables["catalog"] == catalog
    assert variables["team"] == "sales"
    assert variables["warehouse_id"] == warehouse_id, (
        "a lookup by warehouse name comes back as its id"
    )


def test_a_development_target_deploys_under_another_name(
    bundle: Path, catalog: str
) -> None:
    """`mode: development` makes `sales` into `dev_<user>_sales`. Reading the
    file would plan against `sales`, which is why the CLI is asked."""
    target = resolve_target(bundle, "dev")
    assert target is not None
    names = {resource.key: resource.full_name for resource in target.resources}
    schema = names["sales"]
    assert schema is not None
    assert schema.startswith(f"{catalog}.dev_") and schema.endswith("_sales"), schema
    # The CLI leaves `${resources…}` between resources to the deploy; deltaplan
    # resolves those from the names the CLI did settle.
    assert names["landing"] == f"{schema}.landing"


def test_a_name_prefix_drops_the_underscore(bundle: Path, catalog: str) -> None:
    """`name_prefix: team_` deploys `sales` as `teamsales` — the CLI's rule, not
    one deltaplan would have guessed."""
    target = resolve_target(bundle, "prefixed")
    assert target is not None
    names = {resource.key: resource.full_name for resource in target.resources}
    assert names["sales"] == f"{catalog}.teamsales"


def test_a_project_takes_the_warehouse_from_the_resolved_bundle(
    bundle: Path, warehouse_id: str
) -> None:
    """End to end: `deltaplan.yml` names the bundle and gets a warehouse."""
    (bundle.parent / "deltaplan.yml").write_text(
        "specs: [tables]\nbundle: databricks.yml\n"
    )
    project = load_project(bundle.parent / "deltaplan.yml")
    target = as_deployed(project, project.target("dev"))
    assert target.warehouse_id == warehouse_id
    assert target.warehouse_lookup is None, "nothing is left for deltaplan to look up"
