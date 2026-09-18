# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
