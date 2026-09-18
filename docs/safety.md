# Safety model

A schema tool is only useful if you trust it near production. deltaplan's defaults are
built around that.

## Ownership: it only touches what it made

There is no state file — Unity Catalog is the state — so ownership is recorded on the
tables themselves.

- Tables deltaplan creates get the property `deltaplan.managed = true`.
- **Only managed tables can ever become drop candidates.**
- Anything else in the schema is reported as **unmanaged** and left untouched.
- `import` adopts existing tables deliberately: it generates specs, and marks them
  managed on the first apply.

Per schema you choose a mode: `additive` (never drop — the default) or `strict`.

The same rule applies within a table. A table feature or property deltaplan doesn't
model is shown as *"unmanaged feature, left untouched"* — never diffed away just
because the spec is silent about it.

## Risk classes

Every step in a plan carries a class, and the class decides what happens:

| Class | Examples | Behaviour |
|---|---|---|
| `meta` | add column, comment, tags, properties, constraints, add nested field | Runs directly |
| `feature` | rename/drop column → column mapping; `int`→`bigint` → type widening | The planner inserts a `SET TBLPROPERTIES` step first, and warns about streaming readers |
| `rewrite` | incompatible type change, kind change (struct→array), partitioning | `CREATE OR REPLACE TABLE … AS SELECT`; shows the table size and records a restore point |
| `destructive` | drop column, drop table | Refused unless you pass `--allow-destructive` |

Prerequisites are steps, not side effects: if a rename needs column mapping, you see
"enable columnMapping" as its own numbered line with its own warning.

## When something goes wrong

DDL is not transactional across statements, so deltaplan makes no rollback promise. It
makes narrower ones instead:

- **Idempotent steps.** Each has a precheck and a postcheck, so re-running is safe.
- **Resume, don't restart.** `apply` picks up from the history table.
- **A restore point before every rewrite.** The Delta version is recorded first
  (`delta_version_before`), so `RESTORE` is one command. A `SHALLOW CLONE` is optional.
- **No stale applies.** The state fingerprint is recomputed at apply time; if the world
  moved since the plan was made, deltaplan stops.
- **One run at a time.** A lock table (with a TTL, and `force-unlock` if a run dies)
  keeps two applies off the same tables.

## Honest about Databricks

Delta's rules for nested fields, type widening and column mapping are specific, and they
change. deltaplan's policy is that every behaviour it relies on has a test and a link to
the documentation behind it — and where a behaviour is unverified, it is marked as such
rather than guessed at.
