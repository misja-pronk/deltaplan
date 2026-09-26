"""Drift, back into the spec.

The loop these close: `drift` says a table was changed by hand, `adopt` writes
that change into the spec file, and `drift` is quiet again — with a git diff to
review in between. So most of these assert two things at once: that the file now
says what is live, and that *only* what deltaplan would have planned changed in
it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.adopt import CannotAdopt, adopt
from deltaplan.cli import app
from deltaplan.connect import Connection
from deltaplan.loader import LoadedSpec, load_spec_text
from deltaplan.model.table import MANAGED_PROPERTY, Grant, Seed, Table
from deltaplan.model.view import View
from fake_warehouse import FakeWarehouse
from helpers import col, table

runner = CliRunner()

CONFIG = """\
version: 1
specs: [tables]
targets:
  dev:
    default: true
    vars: {catalog: main}
    warehouse_id: abc123
"""

SPEC = """\
# Orders, from the ingest pipeline
table: ${catalog}.sales.orders
comment: Order facts

columns:
  - name: id            # the surrogate key
    type: bigint
    nullable: false
  - {name: placed, type: date}
tags:
  domain: sales
"""

#: The table that spec describes, as the workspace would hold it — deltaplan's
#: own, so a plan is about drift rather than about claiming it.
IN_SYNC = replace(
    table(
        col("id", "bigint", nullable=False),
        col("placed", "date"),
        name="main.sales.orders",
        comment="Order facts",
        tags=(("domain", "sales"),),
    ),
    properties=((MANAGED_PROPERTY, "true"),),
)


def at(tmp_path: Path, spec: str = SPEC, name: str = "tables/orders.yml") -> Path:
    """A project with one spec in it, and the path of that spec."""
    (tmp_path / "deltaplan.yml").write_text(CONFIG)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(spec)
    return path


def run(
    tmp_path: Path,
    live: Table | View,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
) -> tuple[str, int]:
    """`deltaplan adopt` against a workspace holding `live`."""
    fake = FakeWarehouse.of(live)
    monkeypatch.setattr(cli, "_connect", lambda *_a, **_k: Connection(runner=fake))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["adopt", *args])
    return result.output, result.exit_code


def drifted(tmp_path: Path, live: Table | View, monkeypatch: pytest.MonkeyPatch) -> int:
    """What `deltaplan drift` says now: 0 in sync, 2 drifted."""
    fake = FakeWarehouse.of(live)
    monkeypatch.setattr(cli, "_connect", lambda *_a, **_k: Connection(runner=fake))
    monkeypatch.chdir(tmp_path)
    return runner.invoke(app, ["drift"]).exit_code


# ---------------------------------------------------------------------------
# what lands in the file
# ---------------------------------------------------------------------------


def test_a_hand_added_column_lands_in_the_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = at(tmp_path)
    live = replace(
        IN_SYNC,
        columns=(*IN_SYNC.columns, col("region", "string", comment="ISO code")),
    )
    assert drifted(tmp_path, live, monkeypatch) == 2, "drift sees it first"
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    assert "+ columns: region string" in output
    after = path.read_text()
    assert "region" in after
    assert after.startswith("# Orders, from the ingest pipeline\n"), "its comment stayed"
    assert "${catalog}" in after, "its variable stayed"
    assert "# the surrogate key" in after
    assert drifted(tmp_path, live, monkeypatch) == 0, "and drift is quiet"


def test_a_widened_type_and_a_new_comment_are_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = at(tmp_path)
    live = replace(
        IN_SYNC,
        comment="Order facts, per region",
        columns=(col("id", "bigint", nullable=False), col("placed", "timestamp")),
    )
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    assert "~ comment: Order facts → Order facts, per region" in output
    assert "~ columns.placed.type: date → timestamp" in output
    assert "{name: placed, type: timestamp}" in path.read_text()
    assert drifted(tmp_path, live, monkeypatch) == 0


def test_a_column_the_workspace_no_longer_has_leaves_the_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live is the truth here: a column someone dropped by hand stops being
    declared, rather than being planned back into existence."""
    path = at(tmp_path)
    live = replace(IN_SYNC, columns=(col("id", "bigint", nullable=False),))
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    assert "- columns: placed" in output
    assert "placed" not in path.read_text()
    assert drifted(tmp_path, live, monkeypatch) == 0


def test_the_hints_only_a_file_can_carry_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = SPEC.replace(
        "  - {name: placed, type: date}\n",
        "  - name: placed\n    type: date\n    renamed_from: ordered_at\n"
        "    using: CAST(ordered_at AS DATE)\n",
    )
    path = at(tmp_path, spec)
    live = replace(IN_SYNC, columns=(*IN_SYNC.columns, col("region", "string")))
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    after = path.read_text()
    assert "renamed_from: ordered_at" in after
    assert "using: CAST(ordered_at AS DATE)" in after


def test_a_tag_the_spec_never_claimed_is_left_unmanaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adopting drift is not the moment to start managing something new: a live
    tag the file doesn't mention stays exactly as unmanaged as it was."""
    path = at(tmp_path)
    live = replace(
        IN_SYNC,
        tags=(("domain", "sales"), ("owner", "crm")),
        columns=(*IN_SYNC.columns, col("region", "string")),
    )
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    assert "owner" not in path.read_text()
    assert "owner" not in output


def test_a_tag_the_spec_declares_takes_the_live_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = at(tmp_path)
    live = replace(IN_SYNC, tags=(("domain", "orders"),))
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    assert "~ tags.domain: sales → orders" in output
    assert "domain: orders" in path.read_text()
    assert drifted(tmp_path, live, monkeypatch) == 0


def test_a_declared_tag_the_workspace_lost_stops_being_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = at(tmp_path)
    live = replace(IN_SYNC, tags=())
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    assert "domain" not in path.read_text()
    assert drifted(tmp_path, live, monkeypatch) == 0


def test_a_grant_is_adopted_only_for_a_principal_the_spec_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = SPEC + "grants:\n  - principal: analysts\n    privileges: [SELECT]\n"
    path = at(tmp_path, spec)
    live = replace(
        IN_SYNC,
        grants=(
            Grant("analysts", ("SELECT", "MODIFY")),
            Grant("everyone", ("SELECT",)),
        ),
    )
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    after = path.read_text()
    assert "MODIFY" in after, "the privileges of a principal the spec names"
    assert "everyone" not in after, "and nobody else's"


# ---------------------------------------------------------------------------
# what it won't do
# ---------------------------------------------------------------------------


def test_a_seed_stays_the_files_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seed's rows live in the repo. The workspace can't tell a file what they
    should be, so adopt keeps them — and says the plan still has a load in it."""
    spec = (
        "table: ${catalog}.sales.currencies\n"
        "columns:\n"
        "  - {name: code, type: string}\n"
        "seed:\n"
        "  - {code: EUR}\n"
        "  - {code: USD}\n"
    )
    path = at(tmp_path, spec, "tables/currencies.yml")
    live = replace(
        table(col("code", "string"), name="main.sales.currencies"),
        properties=((MANAGED_PROPERTY, "true"),),
    )
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    assert "Still planned: load_seed" in output
    after = path.read_text()
    assert "{code: EUR}" in after and "{code: USD}" in after


def test_a_sql_spec_is_refused_with_the_reason(tmp_path: Path) -> None:
    path = tmp_path / "orders.sql"
    path.write_text("CREATE TABLE main.sales.orders (id BIGINT)")
    spec = LoadedSpec(path, IN_SYNC)
    with pytest.raises(CannotAdopt, match="SQL spec"):
        adopt(spec, IN_SYNC)


def test_a_live_object_of_another_kind_is_refused(tmp_path: Path) -> None:
    path = at(tmp_path)
    spec = LoadedSpec(path, load_spec_text(SPEC, path, {"catalog": "main"}))
    live = View("main.sales.orders", "SELECT 1 AS id")
    with pytest.raises(CannotAdopt, match="table .* and a view"):
        adopt(spec, live)


def test_a_spec_with_nothing_live_is_left_for_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at(tmp_path)
    empty = replace(table(col("x", "int"), name="main.sales.other"))
    output, code = run(tmp_path, empty, monkeypatch)
    assert code == 0
    assert "already says what is live" in output


def test_a_spec_that_already_says_it_is_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = at(tmp_path)
    before = path.read_text()
    output, code = run(tmp_path, IN_SYNC, monkeypatch)
    assert code == 0
    assert "already says what is live" in output
    assert path.read_text() == before


def test_a_name_that_matches_no_spec_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at(tmp_path)
    output, code = run(tmp_path, IN_SYNC, monkeypatch, "invoices")
    assert code == 1
    assert "matches no spec" in output


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = at(tmp_path)
    before = path.read_text()
    live = replace(IN_SYNC, columns=(*IN_SYNC.columns, col("region", "string")))
    output, code = run(tmp_path, live, monkeypatch, "--dry-run")
    assert code == 0, output
    assert "+ columns: region string" in output
    assert "Nothing written" in output
    assert path.read_text() == before


def test_one_name_adopts_one_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = at(tmp_path)
    other = tmp_path / "tables" / "invoices.yml"
    other.write_text(
        "table: ${catalog}.sales.invoices\ncolumns:\n  - {name: id, type: int}\n"
    )
    live = replace(IN_SYNC, columns=(*IN_SYNC.columns, col("region", "string")))
    output, code = run(tmp_path, live, monkeypatch, "orders")
    assert code == 0, output
    assert "orders.yml" in output
    assert "invoices" not in output
    assert "region" in path.read_text()


def test_the_new_text_can_be_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at(tmp_path)
    live = replace(IN_SYNC, columns=(*IN_SYNC.columns, col("region", "string")))
    output, _ = run(tmp_path, live, monkeypatch, "--diff", "--dry-run")
    assert "table: ${catalog}.sales.orders" in output


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


def test_a_views_query_is_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = (
        "# the view the dashboards read\n"
        "view: ${catalog}.sales.recent\n"
        "query: |\n"
        "  SELECT id FROM main.sales.orders\n"
    )
    path = at(tmp_path, spec, "tables/recent.yml")
    live = View(
        "main.sales.recent", "SELECT id, placed FROM main.sales.orders", comment=None
    )
    output, code = run(tmp_path, live, monkeypatch)
    assert code == 0, output
    after = path.read_text()
    assert "SELECT id, placed FROM main.sales.orders" in after
    assert after.startswith("# the view the dashboards read\n")


def test_the_seed_a_spec_keeps_is_the_one_it_had() -> None:
    """The model rule underneath: a seed is carried across, never read back from
    a workspace that doesn't hold one."""
    spec = replace(
        table(col("code", "string"), name="main.sales.currencies"),
        seed=Seed(columns=("code",), rows=(("EUR",),)),
    )
    live = table(col("code", "string"), name="main.sales.currencies")
    from deltaplan.adopt import _adopted

    adopted = _adopted(spec, live)
    assert isinstance(adopted, Table)
    assert adopted.seed == spec.seed
