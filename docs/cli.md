# Commands

```
deltaplan validate            # spec lint, no connection needed
deltaplan import <schema>     # live tables -> YAML specs
deltaplan plan -t <target> [-o plan.json] [--format rich|md|json]
deltaplan apply plan.json [--allow-destructive]
deltaplan drift -t <target>   # exit code != 0 on drift, for CI
deltaplan force-unlock
```

!!! warning "Pre-alpha"
    `validate`, `import` and `plan` work today. `apply`, `drift` and `force-unlock`
    are designed but not built yet, and nothing writes to a workspace.

Every command takes `--config` to point at a `deltaplan.yml`, and `-t/--target` to pick
the target whose variables are substituted. `plan` and `import` also take
`--warehouse-id`, which otherwise comes from the target or `$DATABRICKS_WAREHOUSE_ID`.

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

## `import`

```sh
deltaplan import main.sales -o tables -t dev
```

Generates specs from tables that already exist, so adoption doesn't start with a blank
file. If the target has a variable whose value is that catalog, the generated spec uses
`${catalog}` instead of the literal name, so it fits every target. Views and non-Delta
tables are reported and skipped. Imported tables are marked managed on their first apply.

## `plan`

Reads live state, diffs it against the specs, and prints the plan. `--format`:

- `rich` — the terminal view: a tree of nested changes with numbered, risk-labelled steps.
- `md` — Markdown, for posting as a pull-request comment. *(Milestone 4.)*
- `json` — the plan object itself. `-o plan.json` writes it out for `apply` to consume.

All of them render the same plan object, so the review and the artefact can't disagree.
`--check-order` additionally diffs column order, which is off by default because a
reordered spec is usually an edit to the file rather than an intent to move columns.

Tables in the schema that no spec describes are listed at the bottom as unmanaged, and
never touched.

## `apply`

Executes a plan file. Each step is idempotent (precheck → SQL → postcheck → history
row), so an interrupted run resumes from the history table rather than starting over.

Before it runs anything, `apply` recomputes the state fingerprint and refuses a plan
that no longer matches the live tables — a plan reviewed yesterday can't quietly do
something else today.

`--allow-destructive` is required for any step in the `destructive` class.

## `drift`

Plans and exits non-zero if anything differs. Point a scheduled CI job at it to find
out when someone edits a table by hand.

## `force-unlock`

`apply` takes a lock so two runs can't fight over the same tables. If a run dies hard,
this releases it.
