# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
