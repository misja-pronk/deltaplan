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
    Milestone 1 ships `validate`, `import` and `plan`. `apply`, `drift` and
    `force-unlock` are designed but not built yet.

## `validate`

Lints specs offline: schema, unknown keys, type strings, and stale `renamed_from` hints.
No credentials, no network — ideal for a pre-commit hook or the fast lane of CI.

## `import`

Generates specs from tables that already exist, so adoption doesn't start with a blank
file. Imported tables are marked managed on their first apply.

## `plan`

Reads live state, diffs it against the specs, and prints the plan. `--format`:

- `rich` — the terminal view: a tree of nested changes with numbered, risk-labelled steps.
- `md` — Markdown, for posting as a pull-request comment.
- `json` — the plan object itself. `-o plan.json` writes it out for `apply` to consume.

All three render the same plan object, so the review and the artefact can't disagree.

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
