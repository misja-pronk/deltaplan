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

## Milestone 1 (read-only) — do in this order

Item 1 is done. Note the one deliberate change: `ty`, not pyright.

1. ~~Scaffold: `pyproject.toml` (uv, src layout, Apache-2.0), ruff, ty, pytest, GitHub Actions for lint + unit tests, README stub, move `DESIGN.md` to `docs/`.~~ **Done.**
2. `model/types.py` + `typeparser.py`: type tree and parser for Databricks type strings incl. nested struct/array/map, decimal, backticked field names, `NOT NULL` and comments inside structs. Round-trip tests.
3. `model/table.py` + `loader.py`: YAML → model, both type notations, `${var}` substitution, strict unknown-key errors with file/line paths.
4. `differ.py`: recursive diff with paths, `renamed_from` handling, kind-change detection. Snapshot tests.
5. `planner.py`: changes → steps with risk classes and prerequisite steps (column mapping, type widening). SQL generation. Snapshot tests.
6. `render/rich.py`: the plan layout from the design doc, nested changes as a tree.
7. `introspect.py`: live state via `databricks-sdk` Statement Execution API. Integration tests with ephemeral schema.
8. `cli.py`: `validate`, `import`, `plan`.

Stop after each numbered item and summarise what was built and what was assumed.
