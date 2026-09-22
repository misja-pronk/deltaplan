# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0a1] - 2026-09-22

deltaplan is a library as well as a command, and the command is the library's
first customer.

### Added

- **A public SDK: `import deltaplan`.** A program that runs deltaplan as part
  of something larger — a deployment task, a notebook, a policy check — now has
  names it can rely on, `__all__`, and a page of its own
  (**[As a library](https://misja-pronk.github.io/deltaplan/sdk/)**). The seven
  steps every host takes: `Project.find()` / `Project.load()`,
  `project.resolve(target, bundle_config=…)`, `project.load_specs(target)`,
  `Connection.from_target(target)`, `deltaplan.plan(…)`,
  `deltaplan.apply(…)`, and `deltaplan.drift(…)`. With
  `deltaplan.validate(…)`, `deltaplan.import_schema(…)`,
  `deltaplan.is_stale(plan, conn)` and `deltaplan.find_cli()` beside them.
- **Errors with one root.** Everything deltaplan raises on purpose is a
  `DeltaplanError`, so one `except` reports a failure and a subclass reacts to
  a particular one. Two are new because they are the two a host acts on:
  `StalePlan` and `DestructiveRefused`, each naming the tables it is about.
  `SpecErrors` carries *every* unreadable spec, not the first.
- **A plan can be talked about without walking it.** `plan.is_destructive`,
  `plan.unmanaged`, `plan.orphaned`, and per table `diff.kind`, `diff.action`,
  `diff.steps`, `diff.risk`, `diff.warnings` — so nothing has to read a class
  name to learn that a diff is about a view. A plan written to JSON and read
  back keeps all of it.
- **A bundle the caller already resolved.** `project.resolve(target,
  bundle_config=…)` takes what `databricks bundle validate -o json` printed and
  runs no subprocess. `Bundle.from_resolved(mapping)` is the entry.
- **`manage: comments`.** Descriptions can be handed to the tool that owns
  them, like grants and tags. A comment a spec doesn't mention normally means
  *remove it*, so handing them over also stops deltaplan comparing them.

### Changed

- **A bundle that doesn't resolve is an error.** When the Databricks CLI is
  installed and fails, deltaplan stops and shows what it said — "two profiles
  match this host" — instead of falling back to the bundle file and planning
  against names a deploy would never use. With no CLI at all, the file stands
  in as before. The CLI is looked for the way the Databricks SDK looks for it:
  `DATABRICKS_CLI_PATH` first, then `PATH`.
- **The command line is the SDK's first customer.** `cli.py` is argument
  parsing, rendering and exit codes around the same public functions; it holds
  no logic the library lacks, and imports nothing private. What it prints is
  unchanged, down to the pictures in the documentation.

### Fixed

- **A volume no longer stands in for a table that shares its name.** Asked
  about a table it didn't have, a live schema handed back the volume of that
  name, so a plan to create the table refused itself as stale with nothing
  having changed.

## [0.1.0a10] - 2026-09-22

Say what deltaplan looks after, and what belongs to the tool that already owns
it.

### Added

- **`manage:` — what deltaplan looks after here, and what belongs to another
  tool.** Teams often already have something that owns part of a table: a policy
  framework that sets grants, a catalogue that writes the tags an ABAC rule
  reads. `manage: {grants: false, tags: false}` in `deltaplan.yml` hands those
  over: the key is refused in a spec where you write it, left out of the
  editors' JSON Schema, never written by `import`, and never in a plan — and for
  grants the workspace isn't even asked. `grants`, `tags`, `owner`,
  `properties`, `masks` and `row_filters` can be handed over; a table's shape
  can't. What is handed over is still *read* where not reading it would destroy
  it: a masked table still refuses a rewrite, and a renamed column's tags are
  still put back after one. A plan says what it could not have changed, in the
  terminal and in the pull-request comment.

## [0.1.0a9] - 2026-09-20

A bundle resolves the way a deploy does, and rebuilding a table costs one pass
over its data instead of two.

### Changed

- **A bundle is resolved by the Databricks CLI, not by deltaplan.** For any
  target with a `bundle:`, deltaplan runs
  `databricks bundle validate -o json -t <target>` once per command and uses
  the answer: every `${var.…}` filled in, every `lookup:` run against the
  workspace, and every object under the name a deploy would give it — which
  for `mode: development` is `dev_jane_sales`, and under
  `presets.name_prefix: team_` is `teamsales`. Lookups other than the
  warehouse and `${workspace.current_user.…}` therefore work now, where they
  used to be *unknown*. Reading the bundle file stays as the fallback for when
  the CLI isn't installed or has no credentials — it resolves nothing without
  them — and it still says *unknown* with a reason rather than guessing.
  `deltaplan.yml` keeps the last word either way. Bundles also have a guide of
  their own now: **With an Asset Bundle** in the docs.
- **A rewrite that converts nothing writes the data once.** Rebuilding a table
  staged the whole thing and then replaced it from the staging copy — two full
  writes, even for changes that touch no value. Databricks allows a table to
  read itself and be replaced in the same statement (verified live), so new
  partitioning, the move to liquid clustering, a rename or a dropped column is
  now a single `REPLACE TABLE`: half the writes, and one step in the plan
  instead of three. A conversion — a cast, or a `using:` expression — still
  stages, because that is the one thing a rewrite can get quietly wrong, and
  staging is what lets it be checked while the original is still there.

## [0.1.0a8] - 2026-09-20

deltaplan reads the context an Asset Bundle already holds, and leaves what the
bundle declares to the bundle.

### Added

- **A bundle's catalogs, schemas and volumes are read as context.** Teams keep
  the schema itself in `databricks.yml`; deltaplan now reads those resources
  (from the bundle and its included files, per target), lets a spec name one
  the way the bundle does — `${resources.schemas.sales.name}` — and leaves the
  object itself to the bundle: it won't create or manage it, a spec for one is
  an error, `import` writes no spec for it, and a table whose schema hasn't
  been deployed yet says "run `databricks bundle deploy` first" instead of
  creating it.

## [0.1.0a7] - 2026-09-20

A rewrite's plan says what it will really do, and nothing more.

### Changed

- **A rewrite plans what it actually has to do.** A replace keeps the table's
  tags, grants and owner, and a column's tags — verified live — so the plan no
  longer lists steps to set them again; the tour's rewrite went from twelve
  steps to nine. What a replace does lose is still put back: `NOT NULL`, the
  constraints, a converted column's comment, and a renamed column's tags,
  which stay behind on the old name. A live test asserts nothing is lost,
  including the tags and grants the spec doesn't name.

## [0.1.0a6] - 2026-09-19

What an engineer replacing a setup notebook needs: `deltaplan apply` in one
go, a first `import` that sets up the project, owners, partitioning, and
removing a tag or property. The GitHub Action applies too. The first release
cut from a tag, after the live suite passed on its pull request.

### Added

- **The GitHub Action applies**: `command: apply` plans, puts the plan in the
  job summary and runs exactly that plan, with the deltaplan of the action's
  own version; `allow-destructive: true` lets it drop. `@v0` now follows the
  newest 0.x release, alphas included — it didn't exist before.
- **A first `import` writes `deltaplan.yml`**: in a directory without a
  project, `deltaplan import main.crm` also writes the project file — one
  target, `dev`, whose catalog is the one imported from, so the specs say
  `${catalog}` — and `plan` and `apply` work straight after. With `-o` it
  adds nothing. A **Get started** page walks exactly this.
- **Partitioning**: `partitioned_by: [day]`, in YAML and SQL specs. Left out,
  a table's partitioning stays as it is, so no plan rewrites a partitioned
  table by surprise; `[]` says none. Moving to liquid clustering — take
  `partitioned_by` out, add `cluster_by` — is planned as a rewrite that keeps
  every row. A rewrite for any other reason keeps the table's partitions,
  where it used to refuse to rewrite a partitioned table at all. Verified
  live, both ways.
- **Owners**: `owner: data-eng` on tables, views, functions, schemas and
  volumes. Only an owner a spec names is enforced, always as the object's
  last step. A replaced view or function belongs to whoever replaced it, so
  deltaplan puts the owner back. `import` leaves owners out. Verified live.
- **`deltaplan apply` without a plan file**: plans, shows the plan and asks
  before running it — spec to table in one command. `--yes` skips the
  question; a closed stdin counts as no. A saved plan still runs as before,
  for CI.
- **`--select`** on `plan` and `apply`: `orders`, `sales.orders` or `sales.*`.
  A selection plans only what it names, so it never drops a table it left
  out, even in a strict schema.
- **Removing a tag or property**: `tags: {pii: null}` in a spec means it must
  not be there, and plans `UNSET TAGS` / `UNSET TBLPROPERTIES` with the undo.
  Leaving a key out still only stops managing it. Tables, columns, views,
  schemas and volumes; YAML only. Verified live.

### Changed

- **Releases are cut by pushing a tag** (`v0.1.0a7`) that matches the version
  in `pyproject.toml`; the workflow checks it, runs the gate, and publishes.

## [0.1.0a5] - 2026-09-19

Every assumption deltaplan makes about Databricks that a workspace can check is
now checked by a live test, and the docs have a tour and a feature gallery with
the CLI's real output. The live suite passes 53 of 53.

### Fixed

Found by settling the `TODO(verify)` list against a live workspace:

- **`plan --clone` left a backup a strict schema would drop.** A shallow clone
  copies the table's properties, ownership marker included, so the next plan
  saw the backup as a managed table whose spec was gone and planned
  `drop_table`. The clone is now made with `deltaplan.managed = 'false'`.
- **Two integer-to-decimal widenings Delta refuses were planned in place.**
  Delta widens `tinyint`, `smallint` and `int` only to a decimal with at least
  10 integer digits, and `bigint` to at least 20 — so `tinyint` to
  `decimal(5,0)` and `bigint` to `decimal(19,0)` failed at apply. They are
  rewrites now.
- **A materialized view's storage table was read as an ordinary table**, so
  `import` wrote a spec for it. `__materialization_…` tables are skipped now,
  like the view itself.

Found by writing the docs' tour, which runs the real CLI:

- **`plan -o plan.json` wrote the text view** in the default format, so the
  documented `plan -o plan.json` then `apply plan.json` failed. The file is the
  plan object now; the terminal still shows the plan.
- **`apply` never showed a step's risk class**: `[meta]` is Rich markup and was
  swallowed. So was any lowercase bracket in an error — a type written
  `array[int]` was reported as `array`.
- **The PR comment broke on undo hints**: the backticks around quoted names
  ended the inline code early.
- The plan showed steps out of order when a grant or a hook came after a
  column's changes, put a claim under a column called `deltaplan`, and wrote
  `1 steps`; step 10 sat a column right of step 9. All fixed.
- Paths in messages are relative to where you ran the command.

### Added

- **A tour and a feature gallery** in the docs, with the CLI's real output at
  every step — generated by `tests/screens.py`, and kept current by a test.
- `deltaplan --version`.

### Changed

- **A nested field's `NOT NULL` is an `ALTER`**, set or dropped in place, rather
  than a rewrite; and a rewrite puts it back afterwards instead of refusing.
- **A map key widens in place**, like any other field.
- Every assumption the plans rest on that could be checked live now has a live
  test (`tests/integration/test_live_assumptions.py`); two `TODO(verify)`s are
  left — host-only auth, and `CLUSTER BY AUTO` on a workspace without predictive
  optimization — which this workspace can't settle.

## [0.1.0a4] - 2026-09-19

Fixes found by dogfooding: a schema built by hand the way real ones end up,
imported, adopted and changed on a live workspace. The live suite passes
25 of 25.

### Fixed

Found by dogfooding — importing a messy, hand-built schema and planning it:

- **A column named after a reserved word broke reading the table's definition.**
  Databricks prints `select STRING` in `SHOW CREATE TABLE` without backticks;
  such names are quoted before parsing. The table's identity, generated and
  default columns were silently missing from its imported spec.
- **Changing a column a CHECK uses failed at apply.** Delta refuses to change
  the type of, rename or drop such a column. The CHECK is now dropped first and
  put back after, as the spec has it. A change a generated column blocks is
  refused with the reason, since a generated column can't be made again.
- **A name with a space (or `,;{}()=`) failed at apply**: it needs column
  mapping, which is now switched on — in `CREATE TABLE`, before adding such a
  column, and on a rewrite's staging table.
- `validate` rejects `NOT NULL` inside an array or map (Delta refuses it), and a
  CHECK or generated column that uses a column the spec doesn't have.

### Added

- A live dogfooding test: a messy schema built by hand is imported, adopted and
  changed, and every plan along the way must be what it should be.

## [0.1.0a3] - 2026-09-18

Schemas and managed volumes as specs, and a `plan` that reads only what it
needs, several queries at a time. Verified against a live workspace: 24 of 24.

### Added

- **Schemas as specs** (`schema:`): a schema's comment, tags and grants. A
  declared schema is created with its comment before the tables in it; its tags
  and grants are brought in line, per principal. A spec only adds — a comment
  it doesn't give isn't cleared, and tags and grants it doesn't name are
  reported. Never dropped. `import` writes `_schema.yml`; SQL specs can say a
  schema's comment and grants, not its tags. Schema privileges verified live.
- **Managed volumes** (`volume:`): comment, tags and grants, created and kept in
  line, never dropped (that would delete their files). External volumes are
  listed and left alone. YAML only. Volume privileges verified live.

### Changed

- **`plan` reads less, and in parallel.** Only tables a spec describes get the full
  read (`DESCRIBE DETAIL` and `SHOW CREATE TABLE`); the rest of a schema gets
  `DESCRIBE DETAIL` alone — unless a strict schema is about to drop it. Per-table
  queries run eight at a time (`--parallel` on `plan`, `drift` and `import`).
  `apply` reads only its own tables in full.

## [0.1.0a2] - 2026-09-18

The first release run against a real workspace. That found six bugs in
0.1.0a1 — most seriously, `apply` couldn't create a table — all fixed below,
and SQL specs arrive alongside YAML.

### Added

- **Editor support.** JSON Schemas for YAML specs and `deltaplan.yml`, published
  with the docs and printed by `deltaplan schema`, give completion and inline
  errors in any editor with a YAML language server. `import` writes the
  `$schema` line into each spec. Built from the loader's own key sets and held
  to them by tests, so the editor and `validate` agree.
- **SQL specs.** A `.sql` file holding a `CREATE TABLE`, `CREATE VIEW` or `CREATE
  FUNCTION` — optionally followed by `ALTER … SET TAGS` and `GRANT` for the same
  object — is a spec, read with sqlglot into the same model as YAML. SQL specs
  support what sqlglot parses into structure; what it can't (column masks, row
  filters, column tags today) is refused with its line and a pointer to YAML.
  View queries and function bodies are kept exactly as written. A project can mix
  both formats.
- **`import --format sql`** writes SQL specs, and YAML for a table SQL can't
  describe (column tags, masks, row filters), saying which. A foreign key into
  the imported catalog now goes behind `${catalog}` too, in both formats.
- **A supported-features list** for YAML and SQL (`docs/formats.md`), generated
  from `deltaplan.features` and proven row by row by the tests.

- **`cluster_by: auto`** — automatic liquid clustering. Only whether it is on is
  compared: the keys are Databricks' choice. Naming keys turns it off.
- **The `timestampNtz` feature is enabled first** when a `timestamp_ntz` column is
  added (nested too) or a column is widened to one; `ALTER TABLE` fails without
  it, found live.

### Fixed

- **Every `CREATE TABLE` failed on a real warehouse**: its postcheck was a
  `DESCRIBE`, whose first value is a column name, not true/false. It's gone; the
  fake now refuses `DESCRIBE TABLE` so such a check can't pass offline again.
- **Masks and row filters couldn't be read**: introspection asked
  `information_schema` for columns that don't exist.
- Table features are read from `DESCRIBE DETAIL`'s `tableFeatures`, where
  Databricks lists them. The `allowColumnDefaults` step was planned again on every
  table that already had it.
- Plan files keep whether the schema exists, and the table's features.
- **A second read through the same introspector returned the first one's state**
  — it cached `DESCRIBE DETAIL` for its whole life. With plan and apply sharing
  one, apply's staleness check could never see a change. Caches now last one read.
- **Apostrophes were silently dropped** from every comment, tag and property
  deltaplan wrote. It escaped quotes by doubling them, and Databricks reads
  `'It''s'` as two literals joined: `Its`. Literals are backslash-escaped now
  (`'It\'s'`), as Databricks expects and writes them back.
- **Identity, generated and default columns were never read back**, nor NOT NULL
  and comments inside structs: `information_schema.columns` doesn't report them
  on a live workspace. Introspection now reads each table's `SHOW CREATE TABLE`
  (parsed with sqlglot) for them. A definition it can't read is reported, and
  keeps the table from being rewritten.
- **Expressions compare by meaning.** Checks, generations and defaults are
  canonicalised with sqlglot before comparing, so the catalog's
  `( CAST(placed_at AS DATE) )` matches a spec's `cast(placed_at as date)`.
- **CHECK constraints were never read back**, so every plan re-added them. Delta
  keeps them as `delta.constraints.<name>` properties, not in
  `information_schema`; they're read from there, and no longer reported as
  unmanaged properties.
- **Less noise about properties nobody set.** Unity Catalog's own bookkeeping
  (`io.unitycatalog.*`, row tracking's hidden column names, `*.internal`) is
  never reported or imported; the platform's defaults for new tables aren't
  either, while they hold the default value.

## [0.1.0a1] - 2026-09-18

The first public release: an alpha. Everything in the design is built and tested
offline, against a fake warehouse that interprets deltaplan's own SQL. The live
suite has only just started running against a real workspace — its first run
found a wrong assumption about `information_schema`, fixed here — so expect more
of those before 0.1.0.

### Added

- **Table renames**: `renamed_from:` on a table plans `ALTER TABLE … RENAME TO`
  as its first step, and the table's other changes follow under the new name.
  The old name is never treated as an orphan, so a strict schema renames rather
  than drops. `apply` checks for staleness under the name the table was read by.
- **Asset Bundles.** `bundle: databricks.yml` in `deltaplan.yml` takes the
  targets from the bundle: names, the default, each target's workspace, and
  its variables (defaults, overrides, `BUNDLE_VAR_*`, `${var.…}` and
  `${bundle.target}` references, `include:` files). A `warehouse_id` lookup is
  resolved by name once connected. Variables that only a workspace could
  resolve are reported, with the reason, when a spec uses one.
- Specs may write a variable as `${var.name}`, as bundles do.
- A target can be marked `default: true`; `-t` is then optional.
- **SQL functions** (`function:` specs): parameters, return type, body,
  comment and `EXECUTE` grants. Created before the tables and views that call
  them, replaced when their definition changes (grants put back), never
  dropped. Introspected from `information_schema.routines` and imported.
- **Foreign keys** (`foreign_key:` constraints), introspected, diffed, planned
  and imported. They are planned after every table, so the table they reference
  exists first, and matched by what they mean rather than only by name.
- Plan files now read back a table's hooks; they were written but dropped on
  the way in.
- **Identity, generated and default columns** (`identity:`, `generated:`,
  `default:`). `CREATE TABLE` has all three. Defaults can be set, changed and
  dropped later, with the `allowColumnDefaults` feature enabled first as its own
  step; identity and generated columns exist only from creation, so adding or
  changing one on an existing table is a step deltaplan won't run, with the
  reason. Rewrites carry defaults; tables with identity or generated columns
  are never rewritten. Introspected and imported, so an imported table
  re-creates faithfully.
- `plan` and `drift` note a `renamed_from` hint that has done its job and can
  be deleted. (The design puts this in `validate`, which can't see the live
  table.) A table with only notes still reads "No changes".
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

### Fixed

- A new view was headed `~ update` and counted as a change rather than an add.
- `plan -o plan.txt` also printed the whole plan to stdout.
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

### Before the first release

deltaplan was built in milestones before anything was published. Their
numbers were internal and never released; what each added is kept here.

#### Milestone 4

##### Added

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

#### Ownership and strict schemas

##### Added

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

##### Changed

- The plan summary counts destroyed tables; it was hard-coded to zero.

#### Milestone 3

##### Added

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

##### Changed

- `apply` no longer refuses plans containing rewrites. It still refuses any plan
  with a step deltaplan couldn't generate, naming the step and what it needs.

##### Fixed

- Table-level changes (properties, tags) were rendered one level too deep, as
  though they were nested inside a column.

#### Milestone 2

##### Added

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

##### Fixed

- The table features deltaplan enables itself as prerequisites
  (`delta.columnMapping.mode`, `delta.enableTypeWidening`) are no longer reported
  back as unmanaged properties after an apply.

#### Milestone 1

##### Added

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
