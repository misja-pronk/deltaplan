# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Backfills.** `using:` on a column being added fills the existing rows
  (`UPDATE … WHERE col IS NULL`) before `SET NOT NULL` — so a NOT NULL column
  can be added to a table with data. Without it, the plan warns and says what to
  add.
- **Table hooks** (`hooks: {before, after}`), the design's simple pre/post SQL
  hooks: run as written around a table's changes, only when it has some.
- **Schemas are created when a spec needs them**, once each, just before the
  first table or view in them — so a fresh target plans from nothing. Catalogs
  are never created, and schemas are never dropped. The history schema is
  created the same way.

- **A target can name its workspace**: `profile:` on a target picks a
  `~/.databrickscfg` profile, and `--profile` overrides it on every command that
  connects — dev and prod are usually different workspaces.
- A runbook for the live test suite in the testing guide.
- The terminal plan ends with the same warnings the pull-request comment
  raises: that it destroys something, or has steps `apply` will refuse.

### Fixed

- **A rewrite could drop a column without `--allow-destructive`.** A rewrite
  copies only the columns the spec lists, so a column the spec also removed went
  with it — inside a step classed `rewrite`, which the flag doesn't gate. The
  step that replaces the table is now `destructive` whenever the rewrite drops a
  column or field, and names it.
- **A lossy conversion could NULL values silently.** The staging step now
  checks that every row arrived and no converted column gained NULLs, and stops
  the run — before the original table is touched — if either did.
- **A rewrite dropped properties and constraints nobody declared** — a
  retention setting such as `delta.logRetentionDuration`, a CHECK or primary key
  someone else added. The replacement now carries them across, as it already
  did tags and grants; a view replace carries its properties the same way.
- **Partitioning, identity and generated columns, and column defaults went
  unnoticed** — and a rewrite would have dropped them (an identity column coming
  back as a plain BIGINT). They are now read from the catalog, reported as not
  modelled, and a table that has any is never rewritten.
- **`import` wrote properties Delta maintains itself** — among them
  `delta.columnMapping.maxColumnId`, which every later plan would then have set
  back to a stale value as columns were added. Import now writes intent only,
  `validate` refuses a spec that declares a Delta-maintained property, and
  `delta.feature.*` flags are no longer reported as unmanaged.
- A failed postcheck reports what it means, not a generic message.
- The live suite skipped entirely unless `DATABRICKS_HOST` was set, so anyone
  authenticating with a profile would never have run it. It now accepts any
  source the Databricks SDK does, and says why when it skips.
- Failing to connect to a workspace is a message naming the profile, not a
  traceback.

- **A differently-cased name could drop a live table.** A spec naming
  `main.sales.Orders` against the live `orders`, in a strict schema, planned a
  no-op create and a DROP of the real table. Names are now compared the way Unity
  Catalog compares them: object names in lower case, column and field names
  ignoring case.
- **The apply lock could expire under a long run.** It is now renewed before
  every step, and a run that finds it has lost the lock stops rather than carry
  on beside a second one.
- A precheck whose own query fails is recorded as a failed step, instead of
  escaping as a traceback; warehouse errors during `apply` and `force-unlock` are
  reported as messages.

### Added

- **Milestone 5 (governance) is complete.**
- **Views.** A spec with `view:` and a `query:` describes a view. The query is
  what is compared — whitespace aside — and a change replaces the view, with
  its tags and grants put back as they were, and the old definition as undo.
  Views are planned after tables and after the views they read; a cycle is an
  error. Views can be claimed, dropped in a strict schema, and `import`ed.
- A table is never turned into a view or the reverse; planning stops instead.
- Materialized views and streaming tables are recognised and skipped — they
  report their storage as Delta, and would otherwise have been treated as
  tables.
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
