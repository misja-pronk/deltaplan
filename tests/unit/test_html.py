"""A plan as a page, and the one-page server `deltaplan ui` runs.

The page is a third rendering of the same object — after the terminal and the
pull-request comment — and it must say the same things with the same words. What
it must *not* be is a second source of truth, a thing that fetches anything, or
a place to change something.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deltaplan import cli
from deltaplan.connect import Connection
from deltaplan.introspect import Introspector
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, Table
from deltaplan.planning import plan_tables
from deltaplan.render.html import render_html
from deltaplan.serve import page_server
from fake_warehouse import FakeWarehouse
from helpers import col, table

NAME = "main.sales.orders"
MANAGED = ((MANAGED_PROPERTY, "true"),)
LIVE = table(
    col("id", "bigint"),
    col("amount", "int"),
    col("legacy", "string"),
    name=NAME,
    properties=MANAGED,
)


def planned(desired: Table, fake: FakeWarehouse) -> Plan:
    return plan_tables([desired], Introspector(fake), target="dev", tool_version="0.2.0")


@pytest.fixture
def plan() -> Plan:
    fake = FakeWarehouse.of(LIVE)
    fake.sizes[NAME] = 442381631488
    desired = replace(
        table(col("id", "bigint"), col("amount", "string"), name=NAME),
        comment="Order facts",
    )
    return planned(desired, fake)


def test_the_page_stands_on_its_own(plan: Plan) -> None:
    page = render_html(plan)
    assert page.startswith("<!doctype html>")
    assert "<style>" in page and "<script>" in page
    assert "http://" not in page and "https://" not in page, (
        "nothing may be fetched: the page has to open from disk, offline"
    )
    assert "<form" not in page and "apply" not in page.lower().split("<script>")[0]


def test_it_says_what_the_terminal_says(plan: Plan) -> None:
    page = render_html(plan)
    assert str(plan.summary) in page
    assert "sales.orders" in page, "the table, without its catalog, as elsewhere"
    assert "412 GB" in page, "and its size in the same words"
    for step in plan.steps:
        assert step.title in page
        assert step.sql is None or step.sql.splitlines()[0] in page
    assert plan.target in page and plan.spec_hash in page


def test_every_risk_a_step_has_is_on_its_table(plan: Plan) -> None:
    page = render_html(plan)
    risks = sorted({step.risk for step in plan.steps})
    for risk in risks:
        assert f'class="risk risk-{risk}"' in page
    assert f'data-risks="{" ".join(risks)}"' in page, "so the filter can find it"


def test_anything_from_a_spec_is_escaped() -> None:
    """A comment, a column name or a SQL string can't become markup."""
    fake = FakeWarehouse.of(LIVE)
    desired = replace(
        table(col("id", "bigint"), col("amount", "int"), name=NAME),
        comment="<script>alert('x')</script>",
    )
    page = render_html(planned(desired, fake))
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page


def test_an_empty_plan_says_so(plan: Plan) -> None:
    fake = FakeWarehouse.of(LIVE)
    nothing = planned(replace(LIVE, properties=MANAGED), fake)
    assert nothing.empty
    assert "No changes. Live tables match your specs." in render_html(nothing)


def test_what_a_plan_reports_without_changing_is_on_the_page() -> None:
    fake = FakeWarehouse.of(LIVE, table(col("id", "bigint"), name="main.sales.theirs"))
    built = planned(replace(LIVE, comment="Orders"), fake)
    page = render_html(replace(built, not_managed=("grants", "tags")))
    assert "main.sales.theirs" in page, "an unmanaged table is reported"
    assert "grants and tags are managed elsewhere" in page


def test_the_server_hands_over_that_page_and_nothing_else(plan: Plan) -> None:
    server = page_server(render_html(plan))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/") as answer:  # noqa: S310 - our own server
            assert answer.status == 200
            assert answer.headers["Content-Type"] == "text/html; charset=utf-8"
            assert b"<!doctype html>" in answer.read()
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{base}/../etc/passwd")  # noqa: S310
        assert refused.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_cli_writes_a_page_with_o(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "deltaplan.yml"
    project.write_text(
        "specs: [tables]\ntargets:\n  dev:\n    default: true\n"
        "    vars: {catalog: main}\n"
    )
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "orders.yml").write_text(
        "table: ${catalog}.sales.orders\ncolumns:\n  - {name: id, type: bigint}\n"
    )
    fake = FakeWarehouse()
    fake.schemas.add("main.sales")
    monkeypatch.setattr(cli, "_connect", lambda *_a, **_k: Connection(runner=fake))
    out = tmp_path / "plan.html"
    result = CliRunner().invoke(
        cli.app,
        ["plan", "-c", str(project), "-f", "html", "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    page = out.read_text()
    assert page.startswith("<!doctype html>")
    assert "CREATE TABLE orders" in page
