# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Column masks and row filters**, handled as security controls: set or
  replaced when the spec declares them, never removed because a spec is silent,
  inline in `CREATE TABLE` so a new table is never unprotected, refused up front
  if the function is missing, and never rewritten — the staging copy would hold
  possibly unmasked data.
- A step's precheck now carries its own `refusal`, so a refused step says
  exactly why ("the masking function … does not exist", "ssn still has NULLs").
- **Grants** (`grants:` on a table). A principal the spec names gets exactly
  the privileges listed — granted or revoked to match, each revoke with a
  warning and its undo; principals it doesn't name are left alone. Privileges
  are checked against a known list, because as keywords they can't be quoted.
  A rewrite puts back grants to principals the spec doesn't name.
- **Column tags** (`tags:` on a column), additive like table tags. A rewrite
  puts back the table and column tags the spec doesn't declare, so rebuilding a
  table never diffs away what deltaplan doesn't manage.

## [0.5.0 — milestone 4]

### Added

- **Milestone 4 (CI) is complete.**
- `--format md`: the plan as a pull-request comment — summary, GitHub alerts for
  anything destructive, expensive or impossible, a `diff` block per table so
  additions and removals are coloured, the numbered steps with their risk, and
  the SQL folded away. Falls back to leaving out the SQL, then to a summary
  table, when a plan is too long for a comment.
- `deltaplan show plan.json`: render a saved plan in any format without a
  warehouse — exactly what `apply` of that file would run.
- `deltaplan drift`: exits 0 in sync, 2 on drift, 1 on error. Drift is anything
  `apply` would do; unmanaged objects are not drift.
- A GitHub Action (`uses: misja-pronk/deltaplan@v0`) that runs `plan` or
  `drift`, writes the job summary, and comments on the pull request — updating
  its own comment rather than adding one per push. Inputs reach its script
  through the environment, never by interpolation, and a test holds it to that.
- CI lints the workflows with actionlint; releases move the major-version tag
  the Action is used by.

## [0.4.0 — ownership and strict schemas]

### Added

- **Ownership is claimed.** A spec for a table deltaplan didn't create plans a
  visible `CLAIM ownership` step that marks it managed — which is how an
  `import`ed table is handed over on its first apply.
- **Strict schemas.** A managed table whose spec was deleted is dropped in a
  strict schema (destructive, so `--allow-destructive` applies, with `UNDROP`
  as the way back) and kept — but listed — in an additive one. Tables deltaplan
  didn't create are never touched in either mode.
- `schemas:` in `deltaplan.yml` sets the mode per schema, as the design
  specifies; the target's `mode` is the default.
- `history_schema` and `schemas:` keys may use target variables
  (`${catalog}.deltaplan`), so one project file serves every catalog.
- `deltaplan plan --clone` adds a `SHALLOW CLONE` of each table before the first
  step that could lose its data.
- `planning.py`: the specs-to-plan pipeline, out of the CLI, so `plan`, `drift`
  and the GitHub Action share it.

### Changed

- The plan summary counts destroyed tables; it was hard-coded to zero.

## [0.3.0 — milestone 3]

### Added

- **Milestone 3 (rewrites) is complete**: a table that can't be patched is
  rebuilt, and `apply` runs it.
- A rewrite stages the converted data beside the table, **replaces** the table
  from that staging table (keeping its identity and Delta history, so the
  recorded restore point means something, and with no window where the table is
  empty), puts back what a query result can't carry — `NOT NULL`, comments, tags,
  constraints — with ordinary `ALTER`s, and drops the staging table.
- deltaplan writes the conversion where it honestly can: a cast between scalars,
  `named_struct` matched **by name** rather than by position, `transform` over an
  array of structs, and `CAST(NULL AS …)` for a column that didn't exist.
- `using:` on a column — a SQL expression over the live table — for conversions
  deltaplan won't invent: a struct becoming an array, a map whose shape moved, or
  any change that needs a decision rather than a cast.
- The plan file now carries both sides of each diff, so it records what was
  compared and a rewrite knows what it is rebuilding into.

### Changed

- `apply` no longer refuses plans containing rewrites. It still refuses any plan
  with a step deltaplan couldn't generate, naming the step and what it needs.

### Fixed

- Table-level changes (properties, tags) were rendered one level too deep, as
  though they were nested inside a column.

## [0.2.0 — milestone 2]

### Added

- **Milestone 2 (apply) is complete**: `deltaplan apply plan.json` and
  `deltaplan force-unlock`.
- Executor with the design's four promises: a fresh run refuses a stale plan
  (recomputed state fingerprint), steps are skipped when the change they
  implement is already true of the live table, a failed run resumes from the
  history table instead of starting over, and a lock row per target keeps two
  applies apart. A restore point is recorded before every destructive step.
- Run history in Delta tables (`runs`, `steps`, `lock`) in the schema named by
  `history_schema`, created on first use.
- The plan file is now read as well as written, so `apply` consumes exactly what
  `plan` produced — asserted by a round-trip test.
- A fake warehouse (`tests/fake_warehouse.py`) that interprets deltaplan's own
  SQL against in-memory models, so `plan → apply → re-plan is empty` is asserted
  offline for every kind of change. See [docs/testing.md](docs/testing.md).

### Fixed

- The table features deltaplan enables itself as prerequisites
  (`delta.columnMapping.mode`, `delta.enableTypeWidening`) are no longer reported
  back as unmanaged properties after an apply.

## [0.1.0 — milestone 1]

### Added

- **Milestone 1 (read-only) is complete**: `validate`, `import` and `plan`.
- Type tree and parser for Databricks type strings, including nested
  struct/array/map, decimals, backticked field names, and `not null` / `comment`
  inside structs.
- YAML loader with `${var}` substitution per target, both type notations, and
  errors that carry file, line and column — including for unknown keys.
- A `deltaplan.yml` project file: where specs live, and what each target
  substitutes.
- Pure differ: recursive diff at Databricks' nested paths, declared renames via
  `renamed_from`, kind-change detection, and opt-in column-order diffing.
- Pure planner: changes become ordered steps classified `meta` / `feature` /
  `rewrite` / `destructive`, with column mapping and type widening inserted as
  their own prerequisite steps, and a conservative widening matrix.
- Renderers: the terminal layout from the design document, and JSON for
  `-o plan.json`.
- Introspection of live Unity Catalog state through `information_schema` and
  `DESCRIBE DETAIL`, plus a live integration suite that asserts the Databricks
  behaviour the planner relies on.
- Project scaffold: uv + hatchling packaging (src layout, Apache-2.0), mise tasks,
  ruff + ty configuration, pytest with a `unit` / `integration` split, and CI for
  lint, types, tests, docs and the built wheel.
- `docs/DESIGN.md` as the source of truth, plus a mkdocs-material site published to
  GitHub Pages.
- A `deltaplan version` command, so the packaging is testable end to end.
