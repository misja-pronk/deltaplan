# deltaplan — design

Working name: `deltaplan`. Declarative, Terraform-style `plan` / `apply` for Databricks SQL (Unity Catalog, Delta) tables.

## Goals

- Desired state in YAML → diff against live Unity Catalog → reviewable plan → safe apply.
- Rich plan output: per table, per column, nested struct changes as a tree, numbered steps, risk labels, size/cost hints.
- Delta-aware planning: knows which changes are metadata-only, which need a table feature, which need a rewrite.
- Safe by default: never touches what it does not manage, never destroys without an explicit flag.
- Free, Python-native, Apache-2.0. Fits next to Databricks Asset Bundles and CI.

## Non-goals (v1)

- Views, grants, masks, row filters, volumes, functions (later milestones).
- Data backfills beyond simple pre/post SQL hooks.
- ~~Parsing SQL DDL as the source of truth.~~ **Changed 2026-09-18, by the owner:**
  a spec may be a `.sql` `CREATE` statement, parsed with sqlglot into the same model
  as YAML. SQL specs support exactly what sqlglot parses into structure; YAML stays
  the complete format. See `docs/formats.md` and `deltaplan.features`.
- Non-Delta formats.

## Pipeline

```
spec (YAML) ─┐
             ├─> differ ─> changes ─> planner ─> plan (JSON) ─> renderer
live (UC) ───┘                                        │
                                                      └─> executor ─> history
```

1. **Loader** — YAML → frozen dataclasses. Validation at this edge only (Pydantic `TypeAdapter` or msgspec). Variables per target (`${catalog}`).
2. **Introspector** — live state from `information_schema`, `DESCRIBE TABLE EXTENDED`, `DESCRIBE DETAIL` into the same dataclasses. Type strings are parsed into the type tree.
3. **Differ** — pure function `(desired, actual) -> list[Change]`.
4. **Planner** — pure function `list[Change] -> Plan`. Expands changes into ordered steps, inserts prerequisite steps, classifies risk, resolves dependencies.
5. **Renderer** — Rich CLI, Markdown (PR comments), JSON. All from the same plan object.
6. **Executor** — runs steps on a SQL warehouse (Statement Execution API via `databricks-sdk`). Precheck → SQL → postcheck → history row.

Differ and planner do no I/O. Everything outside loader, introspector and executor must be unit-testable without a workspace.

## Domain model

Frozen, slotted dataclasses. Tuples, not lists, so everything is hashable.

```python
DataType = Primitive | Decimal | Array | Map | Struct

Primitive(name)
Decimal(precision, scale)
Array(element, contains_null=True)
Map(key, value)
Struct(fields: tuple[Field, ...])
Field(name, type, nullable=True, comment=None, renamed_from=None)  # hints: compare=False

Column  = Field at top level
Table(name, columns, comment, cluster_by, properties, tags, constraints)
```

- **Change** (semantic, rendered): `path` (e.g. `address.element.zip`), `kind`, `before`, `after`.
- **Step** (executable): `id`, `sql`, `risk`, `precheck`, `postcheck`, `est_bytes`, `undo_hint`.
- **Plan**: tool version, target, spec hash, state fingerprint, changes, steps.

Paths follow Databricks nested syntax: struct `a.b`, array `a.element.b`, map `m.key` / `m.value`.

## Spec format

```yaml
table: ${catalog}.sales.orders
comment: Order facts
cluster_by: [order_date]
tags: {domain: sales}
properties:
  delta.enableChangeDataFeed: "true"
columns:
  - name: order_id
    type: bigint
    nullable: false
  - name: customer_ref
    type: string
    renamed_from: cust_id
  - name: address
    type:
      struct:
        - {name: street, type: string}
        - {name: zip, type: string}
constraints:
  - primary_key: [order_id]
```

- Types accepted as string (`struct<street:string,zip:string>`) or nested YAML. Nested form allows per-field comments and `renamed_from`.
- `renamed_from` is ignored once the old name is gone and the new one exists; `validate` warns that it can be removed.
- Unknown keys are an error.

## Ownership

There is no state file; Unity Catalog is the state.

- Tables created by the tool get the property `deltaplan.managed = true`.
- Only managed tables can ever become drop candidates.
- Anything else is reported as **unmanaged** and left untouched.
- Per-schema mode: `additive` (never drop, default) or `strict`.
- `import` generates specs from existing tables and marks them managed on first apply.
- Features seen on a live table that the model does not cover are shown as "unmanaged feature, left untouched" — never diffed away.

## Step classification

| Class | Examples | Behaviour |
|---|---|---|
| `meta` | add column, comment, tags, properties, constraints, add nested field | Runs directly |
| `feature` | rename/drop column → column mapping; int→bigint → type widening | Planner inserts `SET TBLPROPERTIES` step; warns about streaming readers |
| `rewrite` | incompatible type change, kind change (struct→array), partitioning | `CREATE OR REPLACE TABLE … AS SELECT`; shows table size; records restore point |
| `destructive` | drop column, drop table | Requires `--allow-destructive` |

Nested-field rules to verify against current Databricks docs and encode as tests: add nested field (meta), rename/drop nested (feature), widen nested (feature), reorder (meta, opt-in diff), `SET NOT NULL` on nested (unsupported → rewrite or error), map key change (rewrite).

> **Verified live (2026-09-19), two of these differ:** `SET NOT NULL` and `DROP NOT NULL` on a struct's field are ordinary `ALTER`s (meta), and a map key widens in place like any other field — only a key change that isn't a widening is a rewrite. `tests/integration/test_live_assumptions.py` and `test_live_round_trip.py` hold the evidence.

## Failure model

DDL is not transactional across statements. No rollback promise.

- Every step is idempotent via precheck/postcheck.
- `apply` resumes from the history table.
- Before any rewrite: record Delta version (`delta_version_before`) so `RESTORE` is one command. Optional `SHALLOW CLONE`.
- Stale plan protection: `apply` recomputes the state fingerprint and refuses if it differs.

## History and locking

Delta tables in a dedicated schema (configurable).

- `runs`: run_id, plan_hash, target, user, tool_version, status, started_at, ended_at
- `steps`: run_id, step_id, table, sql, status, started_at, ended_at, error, delta_version_before
- `lock`: conditional `UPDATE … WHERE holder IS NULL`, check affected rows. TTL + `force-unlock`.

## CLI

```
deltaplan validate            # spec lint, no connection needed
deltaplan import <schema>     # live tables -> YAML specs
deltaplan plan -t <target> [-o plan.json] [--format rich|md|json]
deltaplan apply plan.json [--allow-destructive]
deltaplan drift -t <target>   # exit code != 0 on drift, for CI
deltaplan force-unlock
```

## Plan output (target look)

```
sales.orders   ~ update  (412 GB)
  ~ amount  DECIMAL(10,2) → (18,2)
    1. enable typeWidening        [feature]
    2. ALTER COLUMN TYPE          [meta]
  ~ address
    + zip STRING
    3. ADD COLUMN address.zip     [meta]
  → customer_ref (was cust_id)
    4. enable columnMapping       [feature]
       ⚠ breaks streaming readers
    5. RENAME COLUMN              [meta]
  - legacy_flag
    6. DROP COLUMN                [destructive]

Plan: 0 add, 1 change, 0 destroy · 6 steps · 0 rewrites · 1 warning
```

## Testing

- Unit: differ and planner with golden plan snapshots. No workspace needed.
- Integration: real workspace, ephemeral schema per run, nightly in CI. Marked `@pytest.mark.integration`, skipped without credentials.
- Every discovered Databricks limitation becomes a test.

## Milestones

1. **Read-only**: domain model, type parser, loader, introspector, differ, planner (classification only), Rich renderer, `validate`, `import`, `plan`.
2. **Apply (meta)**: executor, history, locking, fingerprint check, resume.
3. **Feature + rewrite**: prerequisite steps, rewrites, restore points, `--allow-destructive`.
4. **CI**: Markdown renderer, GitHub Action, `drift`.
5. **Governance**: tags on columns, masks, row filters, grants, views.

Open-source after milestone 1.

## Stack

Python ≥ 3.11 · uv · src layout · typer + rich · databricks-sdk · PyYAML · pytest (+ syrupy for snapshots) · ruff · pyright · GitHub Actions · Apache-2.0.
