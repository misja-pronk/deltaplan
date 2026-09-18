# CLAUDE.md

`deltaplan`: declarative plan/apply for Databricks SQL tables. Read `docs/DESIGN.md` first; it is the source of truth. If code and design disagree, flag it instead of silently picking one.

## Rules

- Differ and planner are pure: no I/O, no SDK imports, no clock, no env.
- Domain model: frozen, slotted stdlib dataclasses with tuples. Pydantic/msgspec only in the loader.
- Never generate SQL by string-concatenating unquoted identifiers. One `quote_ident()` helper, used everywhere.
- Anything not modelled on a live table is reported as unmanaged, never diffed away.
- No destructive step without the `destructive` risk class.
- Every Databricks behaviour assumption gets a test and a link to the docs in the test docstring. If unsure about a behaviour, say so and add a `TODO(verify)` — do not guess.
- Small PR-sized commits, conventional commit messages.

## Layout

```
src/deltaplan/
  model/        types.py, table.py, change.py, plan.py
  typeparser.py
  loader.py
  introspect.py
  differ.py
  planner.py
  render/       rich.py, markdown.py, json.py
  executor.py   (milestone 2)
  cli.py
tests/
  unit/
  integration/
  snapshots/
docs/           DESIGN.md (source of truth) + the mkdocs site
examples/
```

## Commands

Tooling is mise + the Astral stack (uv, ruff, ty) — same as `isolinear`. Never use
pip/virtualenv, black/flake8/isort, or mypy.

```
mise install                   # pinned Python + uv
uv sync
uv run pytest tests/unit
uv run pytest -m integration   # needs DATABRICKS_HOST / token / warehouse id
uv run ruff check . && uv run ruff format --check .
uv run ty check
```

`mise run check` is the full gate (lint + format check + types + unit tests);
`mise tasks` lists the rest. Docs: `mise run docs` (mkdocs-material, published to
Pages). Releases are version-driven from `pyproject.toml` — see CONTRIBUTING.md.

## Milestone 1 (read-only) — done

All eight items are built, lint/type/test clean, with golden plans in
`tests/snapshots/` and a live suite in `tests/integration/`. Deliberate
departures from this document, each explained in the commit that made it:
`ty` instead of pyright; a hand-written validator in the loader rather than
Pydantic/msgspec, so errors carry file:line:column; `deltaplan.yml` invented for
the project/target config; nested YAML types extended from struct to array and
map; `sql.py` added for `quote_ident()`; rewrites classified but not generated (milestone 3); the Markdown
renderer deferred to milestone 4.

**Milestone 2 (apply) is done too**: `executor.py`, `history.py` (the design's
runs/steps/lock tables), `apply` and `force-unlock`. Two more departures worth
knowing: the design's per-step precheck/postcheck queries are replaced by
`differ.is_applied()`, which asks the *model* whether a change is already true
of the live table — it reuses tested code instead of inventing SQL we cannot
verify (`precheck` survives as a precondition guard, e.g. SET NOT NULL); and
`history.py` is a new module the layout above doesn't list, because putting the
history tables inside `executor.py` would have made one file do two jobs.

Testing without a workspace: `tests/fake_warehouse.py` is an in-memory catalog
that interprets deltaplan's own SQL, so plan → apply → re-plan can be asserted
offline for every change kind. It proves our SQL matches our intent; only
`tests/integration/` proves Databricks agrees. Read `docs/testing.md` before
adding a test, and keep the fake's `FakeSqlError` loud — a statement shape it
doesn't know must fail, not pass.

**Milestone 3 (rewrites) is done.** A table with a rewrite-class change is
rebuilt whole rather than patched: stage the converted data, REPLACE the table
from the staging table (identity and history kept, no empty window), put back
what a query result can't carry with ordinary ALTERs, drop the staging table.
Two departures worth knowing: the design's single `CREATE OR REPLACE TABLE …
AS SELECT` is staged in two statements, because a self-referencing RTAS is
unverified (TODO(verify) in `replace_table_sql`) and staging makes the expensive
step repeatable; and `using:` is a new spec hint for conversions deltaplan won't
invent. A rewrite cannot set NOT NULL on a nested field — it refuses rather than
dropping the constraint.

**The rest of the design's safety model is in too**: ownership claims (a spec
for someone else's table plans a visible `claim_table`), strict schemas (managed
tables whose spec is gone become `drop_table`, destructive), and
`plan --clone` for a SHALLOW CLONE before risky steps. The specs-to-plan
pipeline lives in `planning.py` (a module the layout above doesn't list),
because `plan`, `drift` and the Action all need it. One departure: the design
says the mode is per schema; `deltaplan.yml` has a per-schema `schemas:` map
*and* keeps the target's `mode` as the default for unlisted schemas.

**Milestone 4 (CI) is done**: `render/markdown.py`, `show`, `drift` (exit 0/2/1),
and a composite GitHub Action — `action.yml` at the repo root, with its comment
script in `action/upsert_comment.py` (stdlib only, tested against a fake API).
Change labels are shared by both renderers in `render/labels.py`. The Action
must never interpolate `${{ }}` into a `run:` script — `test_action.py`
enforces it.

**Milestone 5 (governance) is done**: column tags, grants (per principal),
column masks and row filters (additive, never removed, never rewritten), and
views (`model/view.py`; `Relation = Table | View`; tables and views share
`Securable`). Every milestone in DESIGN.md is built.

Since then, beyond the design: schema creation, hooks and backfills, identity /
generated / default columns, foreign keys, SQL functions (`model/function.py`;
`Relation = Table | View | Function`) and Asset Bundle targets (`bundle.py`, a
new module the layout doesn't list: it reads someone else's YAML leniently,
which `loader.py`'s strict validator shouldn't). External tables are out of
scope for now — managed tables only.

What remains is verification, not construction: nothing has run against a real
workspace. Every `TODO(verify)` in `src/` names an assumption the live suite in
`tests/integration/` is written to settle — run it (`uv run pytest -m
integration`) before trusting any of this with production tables.

1. ~~Scaffold: `pyproject.toml` (uv, src layout, Apache-2.0), ruff, ty, pytest, GitHub Actions for lint + unit tests, README stub, move `DESIGN.md` to `docs/`.~~ **Done.**
2. `model/types.py` + `typeparser.py`: type tree and parser for Databricks type strings incl. nested struct/array/map, decimal, backticked field names, `NOT NULL` and comments inside structs. Round-trip tests.
3. `model/table.py` + `loader.py`: YAML → model, both type notations, `${var}` substitution, strict unknown-key errors with file/line paths.
4. `differ.py`: recursive diff with paths, `renamed_from` handling, kind-change detection. Snapshot tests.
5. `planner.py`: changes → steps with risk classes and prerequisite steps (column mapping, type widening). SQL generation. Snapshot tests.
6. `render/rich.py`: the plan layout from the design doc, nested changes as a tree.
7. `introspect.py`: live state via `databricks-sdk` Statement Execution API. Integration tests with ephemeral schema.
8. `cli.py`: `validate`, `import`, `plan`.

Stop after each numbered item and summarise what was built and what was assumed.
