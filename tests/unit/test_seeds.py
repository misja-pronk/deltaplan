"""Seeds: the reference data a table is loaded with, kept in the repo.

The rest of deltaplan is about a table's shape. A seed is about its content,
and it only fits because the content is small, declared, and wholly deltaplan's:
the file is the truth, applying one replaces what is there, and what the plan
compares is a digest the loaded table carries — never the rows themselves.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-dml-insert-into
"""

from dataclasses import replace
from pathlib import Path

import pytest

from deltaplan.introspect import Introspector
from deltaplan.loader import SpecError, load_spec
from deltaplan.model.plan import Plan
from deltaplan.model.table import MANAGED_PROPERTY, SEED_PROPERTY, Table
from deltaplan.planning import plan_tables
from deltaplan.render.labels import describe
from fake_warehouse import FakeWarehouse
from helpers import col, run, table

NAME = "main.reference.countries"
MANAGED = ((MANAGED_PROPERTY, "true"),)
SPEC = """\
table: main.reference.countries
columns:
  - {name: code, type: string, nullable: false}
  - {name: name, type: string}
  - {name: population, type: bigint}
  - {name: eu, type: boolean}
"""


def spec_with(tmp_path: Path, seed: str, columns: str = "") -> Table:
    path = tmp_path / "countries.yml"
    path.write_text((columns or SPEC) + seed)
    loaded = load_spec(path)
    assert isinstance(loaded, Table)
    return loaded


CSV = "code,name,population,eu\nNL,Netherlands,17800000,true\nNO,Norway,5500000,false\n"


def test_a_csv_beside_the_spec_is_read(tmp_path: Path) -> None:
    (tmp_path / "countries.csv").write_text(CSV)
    loaded = spec_with(tmp_path, "seed: countries.csv\n")
    assert loaded.seed is not None
    assert loaded.seed.columns == ("code", "name", "population", "eu")
    assert len(loaded.seed) == 2
    assert loaded.seed.source == "countries.csv"


def test_rows_written_out_here_are_the_same_seed(tmp_path: Path) -> None:
    """The digest is over the values, so where they live doesn't matter."""
    (tmp_path / "countries.csv").write_text(CSV)
    from_file = spec_with(tmp_path, "seed: countries.csv\n")
    inline = spec_with(
        tmp_path,
        "seed:\n"
        "  - {code: NL, name: Netherlands, population: 17800000, eu: true}\n"
        "  - {code: NO, name: Norway, population: 5500000, eu: false}\n",
    )
    assert from_file.seed is not None and inline.seed is not None
    assert from_file.seed.digest == inline.seed.digest


def test_an_empty_cell_is_null_and_changes_the_digest(tmp_path: Path) -> None:
    (tmp_path / "countries.csv").write_text(CSV)
    full = spec_with(tmp_path, "seed: countries.csv\n")
    (tmp_path / "countries.csv").write_text(CSV.replace("Norway", ""))
    sparse = spec_with(tmp_path, "seed: countries.csv\n")
    assert sparse.seed is not None and full.seed is not None
    assert sparse.seed.rows[1][1] is None
    assert sparse.seed.digest != full.seed.digest


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        ("seed:\n  - {continent: Europe}\n", "no column for"),
        ("seed:\n  - {code: NL, population: lots}\n", "can't hold"),
        ("seed: nowhere.csv\n", "cannot read the seed"),
    ],
)
def test_what_a_seed_may_not_say(tmp_path: Path, seed: str, message: str) -> None:
    with pytest.raises(SpecError, match=message):
        spec_with(tmp_path, seed)


def test_a_seed_is_reference_data_not_a_dataset(tmp_path: Path) -> None:
    rows = "".join(f"C{n},Country {n},1,true\n" for n in range(1001))
    (tmp_path / "countries.csv").write_text("code,name,population,eu\n" + rows)
    with pytest.raises(SpecError, match="at most 1000 rows"):
        spec_with(tmp_path, "seed: countries.csv\n")


def test_a_seed_only_writes_plain_values(tmp_path: Path) -> None:
    columns = (
        "table: main.reference.countries\n"
        "columns:\n"
        "  - {name: code, type: string}\n"
        '  - {name: borders, type: "array<string>"}\n'
    )
    with pytest.raises(SpecError, match="only writes plain values"):
        spec_with(tmp_path, "seed:\n  - {borders: NL}\n", columns=columns)


def planned(desired: Table, fake: FakeWarehouse) -> Plan:
    return plan_tables([desired], Introspector(fake), target="t", tool_version="0")


def seeded(tmp_path: Path) -> Table:
    (tmp_path / "countries.csv").write_text(CSV)
    return spec_with(tmp_path, "seed: countries.csv\n")


def test_a_new_table_is_created_then_loaded(tmp_path: Path) -> None:
    fake = FakeWarehouse()
    fake.schemas.add("main.reference")
    desired = seeded(tmp_path)
    plan = planned(desired, fake)
    assert [step.title for step in plan.steps] == [
        "CREATE TABLE countries",
        "LOAD SEED",
        "RECORD SEED",
    ]
    load = plan.steps[1]
    assert load.risk == "rewrite", "nothing is destroyed: the table was empty"
    assert "2 rows from countries.csv" in (load.note or "")
    assert "INSERT OVERWRITE" in (load.sql or "")
    # Values are literals of their column's type, quoted where they must be.
    assert "('NL', 'Netherlands', 17800000, TRUE)" in (load.sql or "")


def test_loading_over_rows_that_are_there_is_destructive(tmp_path: Path) -> None:
    live = table(
        col("code", "string", nullable=False),
        col("name", "string"),
        col("population", "bigint"),
        col("eu", "boolean"),
        name=NAME,
        properties=MANAGED,
    )
    fake = FakeWarehouse.of(live)
    fake.sizes[NAME] = 4096
    plan = planned(seeded(tmp_path), fake)
    [load] = [step for step in plan.steps if step.title == "LOAD SEED"]
    assert load.risk == "destructive"
    assert load.warnings and "whole content" in load.warnings[0]
    assert load.undo_hint, "a restore point before it runs"


def test_a_loaded_table_says_what_it_holds_and_stays_quiet(tmp_path: Path) -> None:
    """Convergence: the digest the load records is what the next plan reads."""
    fake = FakeWarehouse()
    fake.schemas.add("main.reference")
    desired = seeded(tmp_path)
    assert desired.seed is not None
    run(planned(desired, fake), fake)
    loaded = fake.tables[NAME].properties_map()[SEED_PROPERTY]
    assert loaded == desired.seed.digest
    assert planned(desired, fake).empty


def test_changing_a_value_loads_it_again(tmp_path: Path) -> None:
    fake = FakeWarehouse()
    fake.schemas.add("main.reference")
    run(planned(seeded(tmp_path), fake), fake)
    (tmp_path / "countries.csv").write_text(CSV.replace("5500000", "5600000"))
    changed = spec_with(tmp_path, "seed: countries.csv\n")
    plan = planned(changed, fake)
    assert [step.title for step in plan.steps] == ["LOAD SEED", "RECORD SEED"]
    assert "seed 2 rows from countries.csv" in {
        describe(change)[1] for change in plan.changes
    }


def test_a_spec_without_a_seed_leaves_the_rows_alone(tmp_path: Path) -> None:
    """Taking a seed out of a spec doesn't empty the table: it stops managing it."""
    fake = FakeWarehouse()
    fake.schemas.add("main.reference")
    run(planned(seeded(tmp_path), fake), fake)
    without = replace(seeded(tmp_path), seed=None)
    assert planned(without, fake).empty


def test_the_statement_matches_the_documented_grammar(tmp_path: Path) -> None:
    """`INSERT OVERWRITE [TABLE] name [ ( columns ) | BY NAME ] query`.

    A live workspace is what settles whether Databricks takes a statement, and
    the seed one waits on a suite that can't run today. Two things can be said
    without one: that the shape is the documented grammar, with `VALUES` as the
    query, and that a Databricks parser reads it.
    https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-dml-insert-into
    """
    import sqlglot

    fake = FakeWarehouse()
    fake.schemas.add("main.reference")
    plan = planned(seeded(tmp_path), fake)
    [load] = [step for step in plan.steps if step.title == "LOAD SEED"]
    statement = load.sql or ""
    assert statement.startswith(
        "INSERT OVERWRITE `main`.`reference`.`countries` "
        "(`code`, `name`, `population`, `eu`)\nVALUES\n"
    )
    parsed = sqlglot.parse_one(statement, dialect="databricks")
    assert parsed.key == "insert"
    assert parsed.args.get("overwrite") is True
    # And round-tripping it through the parser doesn't change what it says.
    again = sqlglot.parse_one(parsed.sql(dialect="databricks"), dialect="databricks")
    assert again.sql(dialect="databricks") == parsed.sql(dialect="databricks")
