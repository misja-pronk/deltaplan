"""`manage:` — what deltaplan looks after here, and what belongs elsewhere.

A team whose policy framework owns grants, and whose catalogue writes the tags
its ABAC rules read, doesn't want a second tool writing them. Handing one over
means the key is refused in a spec, left out of the editors' schema, never
written by `import`, never asked of the workspace, and so never in a plan.

What it does *not* mean is that deltaplan forgets they exist: it still has to
know, or a rewrite would destroy someone else's work. That is the last test
here.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, dump_spec, load_project, load_spec, load_specs
from deltaplan.manage import MANAGEABLE, Manage, strip
from deltaplan.model.table import MANAGED_PROPERTY, Grant, Table
from deltaplan.model.types import Mask
from deltaplan.planning import plan_tables
from deltaplan.render.json import dumps, loads
from deltaplan.render.markdown import render_markdown
from deltaplan.render.rich import plan_text
from deltaplan.spec_schema import spec_schema
from fake_warehouse import FakeWarehouse
from helpers import col, run, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)


def project(tmp_path: Path, manage: str = "") -> Path:
    (tmp_path / "tables").mkdir(exist_ok=True)
    path = tmp_path / "deltaplan.yml"
    path.write_text(
        "specs: [tables]\ntargets:\n  dev:\n"
        "    default: true\n    vars: {catalog: main}\n" + manage
    )
    return path


def spec(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tables" / "orders.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


COLUMNS = "columns:\n  - {name: id, type: bigint}\n"


def test_everything_is_managed_unless_a_project_says_otherwise(tmp_path: Path) -> None:
    loaded = load_project(project(tmp_path))
    assert loaded.manage.elsewhere == ()
    assert loaded.manage.manages("grants")


def test_a_handed_over_key_is_refused_in_a_spec(tmp_path: Path) -> None:
    path = spec(
        tmp_path,
        f"table: main.sales.orders\n{COLUMNS}"
        "grants:\n  - {principal: analysts, privileges: [SELECT]}\n",
    )
    loaded = load_project(project(tmp_path, "manage:\n  grants: false\n"))
    with pytest.raises(SpecError) as raised:
        load_specs(loaded, loaded.target("dev"))
    message = str(raised.value)
    assert "isn't deltaplan's in this project" in message
    assert "manage.grants: false" in message
    assert str(path.name) in message, "the error says which file"


def test_the_same_spec_is_fine_where_grants_are_managed(tmp_path: Path) -> None:
    spec(
        tmp_path,
        f"table: main.sales.orders\n{COLUMNS}"
        "grants:\n  - {principal: analysts, privileges: [SELECT]}\n",
    )
    loaded = load_project(project(tmp_path))
    [only] = load_specs(loaded, loaded.target("dev"))
    assert isinstance(only.table, Table)
    assert only.table.grants[0].principal == "analysts"


@pytest.mark.parametrize("aspect", sorted(MANAGEABLE))
def test_every_handed_over_aspect_leaves_the_editors_schema(aspect: str) -> None:
    """What `validate` refuses, an editor must not offer."""
    full, lean = offered(Manage()), offered(Manage((aspect,)))
    for key in MANAGEABLE[aspect]:
        assert key in full, f"{key} should be offered when everything is managed"
        assert key not in lean, f"{key} should be gone when {aspect} is elsewhere"


def offered(manage: Manage) -> set[str]:
    """Every spec key the schema offers, at the table and at a column."""
    schema = spec_schema(manage)
    keys: set[str] = set()
    for node in (schema, *schema.get("definitions", {}).values()):
        for part in (node, *node.get("oneOf", []), *node.get("allOf", [])):
            if isinstance(part, dict) and part.get("type") == "object":
                keys |= set(part.get("properties", {}))
    return keys


def test_an_unknown_aspect_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="can't hand over 'abac'"):
        load_project(project(tmp_path, "manage:\n  abac: false\n"))


def test_the_plan_says_what_it_could_not_have_changed(tmp_path: Path) -> None:
    fake = FakeWarehouse()
    fake.schemas.add("main.sales")
    desired = table(col("id", "bigint"), name=NAME)
    manage = Manage(("grants", "tags"))
    plan = plan_tables(
        [desired],
        Introspector(fake, manage),
        target="dev",
        tool_version="0",
        manage=manage,
    )
    assert plan.not_managed == ("grants", "tags")
    assert "grants and tags are managed elsewhere" in plan_text(plan)
    assert "are managed elsewhere" in render_markdown(plan)
    assert loads(dumps(plan)).not_managed == ("grants", "tags")


def test_the_workspace_is_not_even_asked_about_grants() -> None:
    """A query that never runs can't put someone else's grants in a plan."""
    live = replace(
        table(col("id", "bigint"), name=NAME, properties=MANAGED),
        grants=(Grant("analysts", ("SELECT",)),),
    )
    fake = FakeWarehouse.of(live)
    asked = Introspector(fake).schema("main", "sales")
    handed_over = Introspector(fake, Manage(("grants",))).schema("main", "sales")
    assert asked.tables[0].table.grants, "the fake does hold the grant"
    assert handed_over.tables[0].table.grants == ()


def test_import_writes_no_key_the_project_would_refuse() -> None:
    live = replace(
        table(col("id", "bigint"), name=NAME, properties=MANAGED),
        grants=(Grant("analysts", ("SELECT",)),),
        tags=(("domain", "sales"),),
        owner="someone@example.com",
    )
    written = dump_spec(live, manage=Manage(("grants", "owner", "tags", "properties")))
    assert "grants:" not in written
    assert "owner:" not in written
    assert "tags:" not in written
    # And what it wrote is a spec that project accepts.
    assert isinstance(load_spec_text(written), Table)


def load_spec_text(text: str, tmp: Path | None = None) -> object:
    import tempfile

    directory = tmp or Path(tempfile.mkdtemp())
    path = directory / "orders.yml"
    path.write_text(text)
    return load_spec(path)


def test_handing_something_over_does_not_make_deltaplan_blind() -> None:
    """The rewrite refusal rests on knowing a table is masked. Still true."""
    masked = replace(
        table(col("id", "bigint"), name=NAME, properties=MANAGED),
        columns=(replace(col("id", "bigint"), mask=Mask("main.sales.hide")),),
    )
    assert masked.protected, "a masked table protects itself"
    # Handing masks over only stops deltaplan declaring them in a spec…
    written = strip(masked, Manage(("masks",)))
    assert isinstance(written, Table)
    assert written.protected is False
    # …while what it reads from the workspace still carries them.
    fake = FakeWarehouse.of(masked)
    live = Introspector(fake, Manage(("masks",))).schema("main", "sales")
    assert live.tables[0].table.protected, "a live masked table is still masked"


def test_handing_properties_over_keeps_the_ownership_marker() -> None:
    """The marker that says a table is deltaplan's is a property itself.

    Handing `properties` to another tool must not hand that over too, or
    deltaplan would lose track of which tables are its to manage.
    """
    manage = Manage(("properties",))
    fake = FakeWarehouse()
    fake.schemas.add("main.sales")
    desired = table(col("id", "bigint"), name=NAME)
    plan = plan_tables(
        [desired],
        Introspector(fake, manage),
        target="dev",
        tool_version="0",
        manage=manage,
    )
    [step] = plan.steps
    assert MANAGED_PROPERTY in (step.sql or ""), "a new table still says it is ours"
    run(plan, fake)
    assert fake.tables[NAME].properties_map()[MANAGED_PROPERTY] == "true"
    # And a table made by someone else is still claimed, not silently adopted.
    fake.tables["main.sales.theirs"] = replace(
        fake.tables[NAME], name="main.sales.theirs", properties=()
    )
    theirs = table(col("id", "bigint"), name="main.sales.theirs")
    claim = plan_tables(
        [theirs],
        Introspector(fake, manage),
        target="dev",
        tool_version="0",
        manage=manage,
    )
    assert [change.kind for change in claim.changes] == ["claim_table"]
