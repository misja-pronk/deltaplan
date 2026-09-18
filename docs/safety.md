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
| `rewrite` | incompatible type change, kind change (struct→array), a map's shape | The table is rebuilt from a query; shows the table size and records a restore point |
| `destructive` | drop column, drop table | Refused unless you pass `--allow-destructive` |

Prerequisites are steps, not side effects: if a rename needs column mapping, you see
"enable columnMapping" as its own numbered line with its own warning.

## What a rewrite actually does

A table that needs a rewrite is rebuilt rather than patched, so its plan is a sequence
rather than one statement per change:

```
  ↻ rewrite
    1. STAGE rewritten data         [rewrite]  (412 GB)
    2. REPLACE TABLE                [rewrite]  (412 GB)
    3. SET NOT NULL                 [meta]
    4. COMMENT ON COLUMN            [meta]
    5. DROP staging                 [meta]
```

1. The converted data is written to a staging table beside the original, which is left
   untouched. This is the expensive step, and it is safe to repeat.
2. The table is **replaced** from that staging table — not dropped and recreated. The
   table keeps its identity and its Delta history, which is what makes the recorded
   restore point worth having, and the swap is a single statement, so readers never see
   an empty table.
3. A query result carries names, types and an order and nothing else, so what it can't
   carry — `NOT NULL`, comments, tags, constraints — is put back with ordinary `ALTER`s.
4. The staging table is dropped.

deltaplan writes the conversion itself where it honestly can: a cast between scalars, a
`named_struct` rebuilt **by name** (never by position, which would quietly move one
field's values into another), and a `transform` over an array of structs. Where it
can't — a struct becoming an array, a map whose shape moved — it says so and asks for a
[`using:` expression](spec.md#rewrites-and-using) instead of inventing something.

!!! warning "One thing a rewrite cannot do"
    It cannot make a field *inside a struct* `NOT NULL`, because the new table is built
    from a query and a query result has no required nested fields. deltaplan refuses the
    plan rather than silently dropping the constraint.

## When something goes wrong

DDL is not transactional across statements, so deltaplan makes no rollback promise. It
makes narrower ones instead:

- **Idempotent steps.** Before each step, deltaplan asks whether the change it implements
  is already true of the live table, and skips it if so — so a repeated run is a no-op
  rather than an error.
- **Resume, don't restart.** Every step's outcome goes to the history table, and the next
  `apply` of the same plan continues from where it stopped.
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
