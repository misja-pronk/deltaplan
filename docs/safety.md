# Safety model

A schema tool is only useful if you trust it near production. deltaplan's defaults are
built around that.

## Ownership: it only touches what it made

There is no state file — Unity Catalog is the state — so ownership is recorded on the
tables themselves.

- Tables deltaplan creates get the property `deltaplan.managed = true`.
- **Only managed tables can ever become drop candidates.**
- Anything else in the schema is reported as **unmanaged** and left untouched.
- Writing a spec for a table someone else created is the decision to manage it, so
  the plan **claims** it — a visible `CLAIM ownership` step that sets the marker. This
  is how `import` hands a table over: import writes the specs, and the first apply
  claims the tables.

```
sales.orders   ~ update
  + ownership — deltaplan manages this table from now on
    1. CLAIM ownership              [meta]
```

### Additive and strict schemas

A table deltaplan created whose spec has since been deleted is **orphaned**. What
happens to it is up to the schema's mode:

- **`additive`** (the default) — it stays. The plan lists it, so a deleted spec is never
  silent, but nothing is dropped.
- **`strict`** — it is dropped. That is a `destructive` step, so `apply` refuses it
  without `--allow-destructive`, and the plan carries `UNDROP TABLE` as the way back.

```
sales.retired   - destroy  (12 GB)
  - 4 columns — its spec is gone and the schema is strict
    1. DROP TABLE                   [destructive]
```

The mode is per schema, with the target's `mode` as the default — see
[the project file](spec.md#the-project-file). Strict never reaches a table deltaplan
didn't create: an unmanaged table is left alone in every mode.

Access follows the same line. A principal a spec names gets exactly the privileges it
lists; any principal a spec doesn't name is left alone. A revoke is planned like
anything else — visible, numbered, with a warning and its undo.

The same rule applies within a table. A table feature or property deltaplan doesn't
model is shown as *"unmanaged feature, left untouched"* — never diffed away just
because the spec is silent about it. Partitioning a spec doesn't mention is left as it is,
and a rewrite for another reason keeps it. Identity and generated columns are modelled,
but a table with one is never rewritten: a rewrite rebuilds the table from a query, and
they would come back as plain columns.

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

## Without a history schema

`apply` records every run in three Delta tables — `runs`, `steps` and `lock` — in the
`history_schema` a project names. A project that names none applies anyway. deltaplan's
own state is on the tables themselves (the ownership marker, a seed's digest), so the
history adds three things on top, and this is what each costs to give up:

| With a history schema | Without one |
|---|---|
| **A lock per target**: one `apply` at a time | None. Whatever runs deltaplan has to be the only thing running it — a deploy pipeline usually already is. `force-unlock` says there is nothing to unlock. |
| **Resume**: an interrupted run continues from its recorded steps | Plan again. Every step is checked against the live table before it runs, so the new plan simply doesn't contain what is already true. |
| **A restore point** before a risky step, in a table | Still taken, and printed — in the apply output and on `run.restore_points`. `RESTORE TABLE … TO VERSION AS OF` is still one command; the number is in the log rather than in a table. |
| **An audit**: who ran what, when | Not deltaplan's. Your git history, your CI run, and Delta's own table history know. |

Everything else is unchanged: a stale plan is still refused, a destructive step still
needs `--allow-destructive`, and a second `apply` of the same plan is still refused
because the world it described has moved.

```yaml
# deltaplan.yml — with no history_schema, nothing is written outside your tables
specs: [tables]
targets:
  prod: {catalog: prod}
```

## What isn't deltaplan's

A project can hand part of a table to the tool that already owns it —
[`manage:`](spec.md#what-deltaplan-manages) in `deltaplan.yml`. That line is drawn where
specs are read, so nothing handed over can reach a plan by any route.

It cuts one way only. deltaplan stops *declaring* grants, tags or masks; it doesn't stop
*knowing* about them, because knowing is what keeps it from destroying them. A table with
a column mask still refuses to be rebuilt. A renamed column's tags are still put back
after a rewrite. What another tool set stays exactly as that tool left it.

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
   untouched. This is the expensive step, and it is safe to repeat. It is then
   **checked**: the staged copy must have every row, and no converted column may have
   gained NULLs. A `CAST` that can't convert a value errors in ANSI mode but quietly
   yields NULL without it — so a lossy conversion stops the run here, with the original
   table as it was and the staged copy kept to inspect.
2. The table is **replaced** from that staging table — not dropped and recreated. The
   table keeps its identity and its Delta history, which is what makes the recorded
   restore point worth having, and the swap is a single statement, so readers never see
   an empty table.
3. What the replace loses is put back with ordinary `ALTER`s: `NOT NULL`, the
   constraints, and the comment of any column whose values were converted. A replace
   keeps the table's tags, its grants and its owner, and each column's tags under the
   same name (all verified against a live workspace), so the plan doesn't pretend to
   set them again — but a *renamed* column's tags stay behind on the old name, and
   those it does put back. That includes what the spec doesn't declare: properties
   someone else set (a retention setting, say), their tags, their constraints and their
   grants all survive. Rebuilding a table never diffs away what deltaplan doesn't
   manage.
4. The staging table is dropped.

### A rewrite that converts nothing writes once

Plenty of rewrites don't change a single value: new partitioning, the move to liquid
clustering, a rename, a dropped column. There is nothing a staged copy could catch, so
there isn't one — the table reads itself and is replaced in the same statement (Databricks
allows that; verified against a live workspace), and its data is written once instead of
twice:

```
  ↻ rewrite
    1. REPLACE TABLE                [rewrite]  (2.1 TB)
         · the table is rebuilt from itself in one statement, so its data is written once
```

Everything else is unchanged: the table keeps its identity and history, `apply` records
the version before the step, and the plan's undo hint is the `RESTORE TABLE` that goes
with it. Staging is kept for the one thing it was made for — a conversion that could
quietly turn values into NULL.

deltaplan writes the conversion itself where it honestly can: a cast between scalars, a
`named_struct` rebuilt **by name** (never by position, which would quietly move one
field's values into another), and a `transform` over an array of structs. Where it
can't — a struct becoming an array, a map whose shape moved — it says so and asks for a
[`using:` expression](spec.md#rewrites-and-using) instead of inventing something.

!!! danger "A rewrite that drops a column is destructive"
    A rewrite copies the columns the spec lists and nothing else. If the spec also
    removes a column, the step that replaces the table drops it — so that step is
    classed `destructive`, names what it drops, and `apply` refuses it without
    `--allow-destructive`.

## When something goes wrong

DDL is not transactional across statements, so deltaplan makes no rollback promise. It
makes narrower ones instead:

- **Idempotent steps.** Before each step, deltaplan asks whether the change it implements
  is already true of the live table, and skips it if so — so a repeated run is a no-op
  rather than an error.
- **Resume, don't restart.** Every step's outcome goes to the history table, and the next
  `apply` of the same plan continues from where it stopped.
- **A restore point before every rewrite.** The Delta version is recorded first
  (`delta_version_before`), so `RESTORE` is one command.
- **A clone, if you want one.** `deltaplan plan --clone` adds a `SHALLOW CLONE` of each
  table just before the first step that could lose its data — a copy of the table as
  it was that you can query side by side with the new one. A shallow clone copies no
  data; it points at the table's current files, so it lasts until a `VACUUM` removes
  them.
- **No stale applies.** The state fingerprint is recomputed at apply time; if the world
  moved since the plan was made, deltaplan stops.
- **One run at a time.** A lock table (with a TTL, and `force-unlock` if a run dies)
  keeps two applies off the same tables.

## Honest about Databricks

Delta's rules for nested fields, type widening and column mapping are specific, and they
change. deltaplan's policy is that every behaviour it relies on has a test and a link to
the documentation behind it — and where a behaviour is unverified, it is marked as such
rather than guessed at.
