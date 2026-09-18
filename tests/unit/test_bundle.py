"""Targets from a Databricks Asset Bundle — read offline, resolved as far as a
bundle can be without a workspace.

Precedence follows the bundle docs: `BUNDLE_VAR_<name>` beats the target's
override, which beats the variable's default.
https://docs.databricks.com/aws/en/dev-tools/bundles/variables
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.bundle import BundleError, read_bundle
from deltaplan.loader import SpecError, load_project, load_specs
from deltaplan.model.table import Table

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

runner = CliRunner()

BUNDLE = """\
bundle:
  name: sales

variables:
  catalog:
    description: Where the tables live
    default: dev
  schema:
    default: sales_${bundle.target}
  qualified:
    default: ${var.catalog}.${var.schema}
  warehouse_id:
    lookup:
      warehouse: Starter Warehouse
  owner:
    default: ${workspace.current_user.short_name}
  settings:
    type: complex
    default: {retries: 3}
  required:
    description: No default

workspace:
  host: https://adb-1.azuredatabricks.net

targets:
  dev:
    default: true
    mode: development
  prod:
    mode: production
    workspace:
      host: https://adb-2.azuredatabricks.net
      profile: prod
    variables:
      catalog: main
      warehouse_id: abc123
      required: yes-please
"""


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# reading the bundle
# ---------------------------------------------------------------------------


def test_targets_variables_and_workspaces(tmp_path: Path) -> None:
    bundle = read_bundle(write(tmp_path, "databricks.yml", BUNDLE))
    assert bundle.name == "sales"
    assert [t.name for t in bundle.targets] == ["dev", "prod"]
    assert bundle.default == "dev"

    dev, prod = bundle.targets
    assert dict(dev.variables) == {
        "catalog": "dev",
        "schema": "sales_dev",
        "qualified": "dev.sales_dev",
    }
    assert dev.host == "https://adb-1.azuredatabricks.net", "the top-level workspace"
    assert dev.profile is None
    assert dev.warehouse_lookup == "Starter Warehouse"

    assert dict(prod.variables) == {
        "catalog": "main",
        "schema": "sales_prod",
        "qualified": "main.sales_prod",
        "warehouse_id": "abc123",
        "required": "yes-please",
    }
    assert (prod.host, prod.profile) == ("https://adb-2.azuredatabricks.net", "prod")
    assert prod.warehouse_lookup is None, "a value, not a lookup, on prod"


def test_what_cannot_be_resolved_says_why(tmp_path: Path) -> None:
    dev = read_bundle(write(tmp_path, "databricks.yml", BUNDLE)).targets[0]
    reasons = dict(dev.unresolved)
    assert set(reasons) == {"warehouse_id", "owner", "settings", "required"}
    assert "lookup of a warehouse" in reasons["warehouse_id"]
    assert "${workspace.current_user.short_name}" in reasons["owner"]
    assert "complex" in reasons["settings"]
    assert "no default, and target 'dev' doesn't set it" in reasons["required"]


def test_an_unresolved_reference_explains_the_chain(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "databricks.yml",
        "variables:\n"
        "  who: {default: '${workspace.current_user.userName}'}\n"
        "  schema: {default: 'dev_${var.who}'}\n"
        "targets: {dev: {}}\n",
    )
    reasons = dict(read_bundle(path).targets[0].unresolved)
    assert reasons["schema"].startswith("uses ${var.who}, which uses ")


def test_a_reference_cycle_is_unresolved_not_a_crash(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "databricks.yml",
        "variables:\n"
        "  a: {default: '${var.b}'}\n"
        "  b: {default: '${var.a}'}\n"
        "targets: {dev: {}}\n",
    )
    reasons = dict(read_bundle(path).targets[0].unresolved)
    assert "refers to itself" in reasons["a"]


def test_the_environment_beats_the_target(tmp_path: Path) -> None:
    bundle = read_bundle(
        write(tmp_path, "databricks.yml", BUNDLE),
        {"BUNDLE_VAR_catalog": "ci", "BUNDLE_VAR_required": "set"},
    )
    prod = bundle.targets[1]
    assert dict(prod.variables)["catalog"] == "ci"
    assert dict(prod.variables)["qualified"] == "ci.sales_prod"
    assert "required" not in dict(bundle.targets[0].unresolved)


def test_a_target_override_can_be_written_as_a_default(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "databricks.yml",
        "variables: {catalog: {default: dev}}\n"
        "targets:\n"
        "  prod:\n"
        "    variables: {catalog: {default: main}}\n",
    )
    assert dict(read_bundle(path).targets[0].variables) == {"catalog": "main"}


def test_included_files_add_variables_and_targets(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "databricks.yml",
        "include: [resources/*.yml]\n"
        "variables: {catalog: {default: dev}}\n"
        "targets:\n"
        "  dev: {default: true}\n",
    )
    write(
        tmp_path,
        "resources/targets.yml",
        "variables: {schema: {default: sales}}\n"
        "targets:\n"
        "  dev: {workspace: {profile: dev}}\n"
        "  prod: {variables: {catalog: main}}\n",
    )
    bundle = read_bundle(path)
    dev, prod = bundle.targets
    assert dev.default and dev.profile == "dev", "keys from both files"
    assert dict(dev.variables) == {"catalog": "dev", "schema": "sales"}
    assert dict(prod.variables) == {"catalog": "main", "schema": "sales"}


def test_a_single_target_is_the_default(tmp_path: Path) -> None:
    path = write(tmp_path, "databricks.yml", "targets: {only: {}}\n")
    assert read_bundle(path).default == "only"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("bundle: {name: x}\n", "has no targets"),
        ("- a\n- b\n", "is not a mapping"),
        ("targets: [dev]\n", "targets should be a mapping"),
        ("targets: {dev: {}\n", "is not valid YAML"),
        ("include: resources\ntargets: {dev: {}}\n", "include should be a list"),
    ],
)
def test_a_bundle_deltaplan_cannot_read(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(BundleError, match=message):
        read_bundle(write(tmp_path, "databricks.yml", text))


# ---------------------------------------------------------------------------
# a deltaplan project on top of a bundle
# ---------------------------------------------------------------------------

SPEC = """\
table: ${var.catalog}.${schema}.orders
columns:
  - {name: id, type: bigint}
"""


def bundle_project(tmp_path: Path, config: str = "") -> Path:
    write(tmp_path, "databricks.yml", BUNDLE)
    write(tmp_path, "tables/orders.yml", SPEC)
    return write(tmp_path, "deltaplan.yml", "bundle: databricks.yml\n" + config)


def test_the_bundle_supplies_the_targets(tmp_path: Path) -> None:
    project = load_project(bundle_project(tmp_path))
    assert [t.name for t in project.targets] == ["dev", "prod"]
    assert project.default_target == "dev"
    assert project.bundle == tmp_path / "databricks.yml"

    dev = project.target("dev")
    assert dev.warehouse_id is None and dev.warehouse_lookup == "Starter Warehouse"
    assert dev.host == "https://adb-1.azuredatabricks.net"
    assert dev.mode == "additive", "a bundle's mode means something else"

    prod = project.target("prod")
    assert prod.warehouse_id == "abc123", "from the bundle's warehouse_id variable"
    assert prod.profile == "prod"

    [loaded] = load_specs(project, dev)
    assert isinstance(loaded.table, Table)
    assert loaded.table.name == "dev.sales_dev.orders", "${var.x} works in a spec too"


def test_deltaplan_yml_adds_what_a_bundle_has_no_word_for(tmp_path: Path) -> None:
    project = load_project(
        bundle_project(
            tmp_path,
            "targets:\n"
            "  prod:\n"
            "    mode: strict\n"
            "    warehouse_id: override\n"
            "    profile: prod-admin\n"
            "    vars: {catalog: prod_main, owner: platform}\n",
        )
    )
    prod = project.target("prod")
    assert prod.mode == "strict"
    assert prod.warehouse_id == "override"
    assert prod.profile == "prod-admin"
    assert prod.variables_map()["catalog"] == "prod_main"
    assert prod.variables_map()["owner"] == "platform"
    assert "owner" not in prod.unresolved_map(), "set here, so no longer missing"
    assert prod.variables_map()["qualified"] == "main.sales_prod", (
        "bundle references resolve inside the bundle, before deltaplan's vars"
    )


def test_a_target_the_bundle_does_not_have_is_an_error(tmp_path: Path) -> None:
    path = bundle_project(tmp_path, "targets:\n  staging: {mode: strict}\n")
    with pytest.raises(SpecError, match="target 'staging' isn't in the bundle") as info:
        load_project(path)
    assert info.value.loc.line == 3


def test_a_broken_bundle_points_at_the_bundle_key(tmp_path: Path) -> None:
    write(tmp_path, "databricks.yml", "bundle: {name: x}\n")
    path = write(tmp_path, "deltaplan.yml", "specs: [tables]\nbundle: databricks.yml\n")
    with pytest.raises(SpecError, match="has no targets") as info:
        load_project(path)
    assert info.value.loc.line == 2


def test_a_spec_using_an_unresolved_variable_says_why(tmp_path: Path) -> None:
    project = load_project(bundle_project(tmp_path))
    write(
        tmp_path,
        "tables/owned.yml",
        "table: ${catalog}.${owner}.t\ncolumns: [{name: id, type: bigint}]\n",
    )
    with pytest.raises(SpecError, match=r"\$\{owner\} comes from the bundle but uses"):
        load_specs(project, project.target("dev"))


def test_the_environment_reaches_the_project(tmp_path: Path) -> None:
    project = load_project(bundle_project(tmp_path), {"BUNDLE_VAR_catalog": "ci"})
    assert project.target("dev").variables_map()["catalog"] == "ci"


def test_a_target_of_its_own_can_be_the_default(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "deltaplan.yml",
        "targets:\n  dev: {vars: {catalog: dev}}\n  prod: {default: true}\n",
    )
    assert load_project(path).default_target == "prod"
    write(
        tmp_path,
        "deltaplan.yml",
        "targets:\n  dev: {default: true}\n  prod: {default: true}\n",
    )
    with pytest.raises(SpecError, match="only one target can be the default"):
        load_project(path)


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


def test_validate_uses_the_bundle_default_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 0, result.output
    assert "1 spec(s) OK" in result.output


@dataclass
class _Warehouse:
    id: str | None
    name: str


class _Warehouses:
    def __init__(self, *warehouses: _Warehouse) -> None:
        self.warehouses = warehouses

    def list(self) -> tuple[_Warehouse, ...]:
        return self.warehouses


class _Client:
    """Just enough of a WorkspaceClient to list warehouses."""

    def __init__(self, *warehouses: _Warehouse) -> None:
        self.warehouses = _Warehouses(*warehouses)


def client(*warehouses: _Warehouse) -> "WorkspaceClient":
    return cast("WorkspaceClient", _Client(*warehouses))


def test_a_warehouse_lookup_is_resolved_by_name() -> None:
    found = client(_Warehouse("w1", "Other"), _Warehouse("w2", "Starter Warehouse"))
    assert cli._find_warehouse(found, "Starter Warehouse") == "w2"


@pytest.mark.parametrize(
    ("warehouses", "message"),
    [
        ((), "no SQL warehouse"),
        (
            (
                _Warehouse("w1", "Starter Warehouse"),
                _Warehouse("w2", "Starter Warehouse"),
            ),
            "more than one SQL warehouse",
        ),
    ],
)
def test_a_warehouse_lookup_that_does_not_find_one_warehouse(
    warehouses: tuple[_Warehouse, ...],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import typer

    with pytest.raises(typer.Exit):
        cli._find_warehouse(client(*warehouses), "Starter Warehouse")
    assert message in capsys.readouterr().err


def test_a_bundle_host_is_used_when_there_is_no_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[dict[str, str]] = []

    class FakeWorkspaceClient:
        def __init__(self, **kwargs: str) -> None:
            made.append(kwargs)

    import databricks.sdk

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", FakeWorkspaceClient)
    project = load_project(bundle_project(tmp_path))
    cli._client(project.target("dev"), None)
    cli._client(project.target("prod"), None)
    cli._client(project.target("dev"), "mine")
    assert made == [
        {"host": "https://adb-1.azuredatabricks.net"},
        {"profile": "prod"},
        {"profile": "mine"},
    ]
