# Commands

```
deltaplan validate            # spec lint, no connection needed
deltaplan import <schema>     # live tables -> YAML specs
deltaplan plan -t <target> [-o plan.json] [--format rich|md|json]
deltaplan show plan.json [--format rich|md|json]
deltaplan apply plan.json [--allow-destructive]
deltaplan drift -t <target>   # exit code 2 on drift, for CI
deltaplan force-unlock
```

!!! warning "Pre-alpha"
    Every command here works offline against deltaplan's test warehouse. None has yet
    been run against a real workspace.

Every command takes `--config` to point at a `deltaplan.yml`, and `-t/--target` to pick
the target whose variables are substituted — without it, the only target or the one
marked `default: true` (in `deltaplan.yml` or the [bundle](spec.md#next-to-an-asset-bundle)).
`plan` and `import` also take `--warehouse-id`, which otherwise comes from the target
or `$DATABRICKS_WAREHOUSE_ID`.

## `validate`

```sh
deltaplan validate -t dev            # every spec in the project
deltaplan validate tables/orders.yml # just these
```

Lints specs offline: unknown keys, type strings, duplicate columns and nested fields,
`cluster_by` and primary-key columns that don't exist, nullable primary-key columns,
contradictory `renamed_from` hints, and type names that look misspelled. No credentials,
no network — ideal for a pre-commit hook or the fast lane of CI. Exits non-zero if
anything is wrong.

## `schema`

```sh
deltaplan schema spec       # the JSON Schema for YAML specs
deltaplan schema project    # ... and for deltaplan.yml
```

Prints the JSON Schema an editor uses for completion and inline errors, for the version
you have installed. See [editor support](editors.md).

## `import`

```sh
deltaplan import main.sales -o tables -t dev             # YAML specs
deltaplan import main.sales -o tables -t dev -f sql      # SQL specs
```

Generates specs from the tables, views and SQL functions that already exist, so adoption
doesn't start with a blank file. If the target has a variable whose value is that
catalog, the generated spec uses `${catalog}` instead of the literal name — foreign keys
into the same catalog too — so it fits every target. Non-Delta tables are reported and
skipped, and so are the platform's defaults and Unity Catalog's own bookkeeping
properties. An imported table is claimed as managed on its first apply.

`-f sql` writes `CREATE` statements. A table with column tags, masks or a row filter —
[what SQL specs can't say](formats.md#what-each-format-supports) — is written as YAML
instead, and the output says so.

## `plan`

Reads live state, diffs it against the specs, and prints the plan. `--format`:

- `rich` — the terminal view: a tree of nested changes with numbered, risk-labelled steps.
- `md` — Markdown, for a pull-request comment. The [GitHub Action](ci.md) posts it for
  you.
- `json` — the plan object itself. `-o plan.json` writes it out for `apply` to consume.

All of them render the same plan object, so the review and the artefact can't disagree.

Reading live state costs a query or two per table. Only tables a spec describes are read
in full; the rest of each schema gets a light read — enough to list it and tell whether
it's deltaplan's. `--parallel` (default 8) sets how many of those queries run at once;
`drift` and `import` take it too.
`--check-order` additionally diffs column order, which is off by default because a
reordered spec is usually an edit to the file rather than an intent to move columns.

`--clone` adds a `SHALLOW CLONE` of each table before the first step that could lose
its data — see the [safety model](safety.md#when-something-goes-wrong).

Tables in the schema that no spec describes are listed at the bottom as unmanaged, and
never touched.

## `show`

```sh
deltaplan show plan.json -f md
```

Renders a saved plan in any format, without a warehouse. What you see is what
`apply plan.json` would run — which is why the GitHub Action renders its comment this
way rather than planning twice.

## `apply`

```sh
deltaplan apply plan.json [--allow-destructive]
```

Runs a plan, printing each step as it resolves:

```
dev · 6 step(s) · highest risk destructive

  1. enable typeWidening          [feature]  ok
  2. ALTER COLUMN TYPE            [meta]     ok
  3. ADD COLUMN address.zip       [meta]     skipped (already applied)
  4. enable columnMapping         [feature]  ok
  5. RENAME COLUMN                [meta]     ok
  6. DROP COLUMN                  [destructive] ok

Applied 5 step(s), skipped 1 · run 3f9a2b1c4d5e
```

Four promises, and no others — DDL is not transactional across statements, so there is
no rollback:

- **Nothing runs from a stale plan.** Before a fresh run, `apply` re-reads every table
  the plan was built from and recomputes the state fingerprint. If anything moved, it
  refuses and tells you to plan again. (Which is also why applying the same file twice
  is refused: the second time, it *is* stale.)
- **Steps don't repeat themselves.** Before each step, deltaplan asks whether the change
  it implements is already true of the live table, and skips it if so.
- **A failed run resumes.** Every step's outcome is written to the history table, so
  running `deltaplan apply plan.json` again picks up from the step that failed instead
  of starting over. The fingerprint is not re-checked on a resume — of course the tables
  changed, the first half of the plan changed them.
- **One run at a time.** A lock row per target, taken with a conditional update and
  confirmed by reading it back. It has a one-hour TTL so a run that died can't hold it
  forever, and a live run renews it before every step — so a rewrite that takes longer
  than an hour keeps it. If a run ever finds it has lost the lock, it stops before the
  next step rather than carry on beside whoever took it.

`--allow-destructive` is required for any step in the `destructive` class; without it
`apply` refuses before running anything at all. A plan containing a step deltaplan
couldn't generate — a conversion it won't invent, a change a generated column
blocks — is refused the same way, naming the step and what it needs. A restore point —
the table's Delta version before the step — is recorded for every destructive step, so `RESTORE TABLE …
TO VERSION AS OF n` is one command.

### History

`apply` keeps three Delta tables in the schema named by `history_schema` in
`deltaplan.yml`, and creates the schema and the tables on first use:

| Table | One row per |
|---|---|
| `runs` | apply — with its plan hash, target, user, tool version and status |
| `steps` | step — with the SQL, the outcome, any error, and the Delta version before it |
| `lock` | target — held while a run is in flight |

## `drift`

```sh
deltaplan drift -t prod [-f rich|md|json] [-o file]
```

Asks whether `apply` would do anything, and exits accordingly:

| Exit code | Meaning |
|---|---|
| `0` | Live tables match their specs. |
| `2` | They have drifted — the plan is printed. |
| `1` | Something went wrong. |

Drift is a hand edit in the catalog, a table dropped outside deltaplan, a spec merged but
never applied. Unmanaged objects are not drift: deltaplan never claimed them. Point a
[scheduled workflow](ci.md#catch-drift-nightly) at it.

## `force-unlock`

```sh
deltaplan force-unlock -t prod
```

`apply` takes a lock so two runs can't fight over the same tables. If a run dies hard
without releasing it, this does — and tells you which run was holding it. The lock also
expires by itself after an hour.
