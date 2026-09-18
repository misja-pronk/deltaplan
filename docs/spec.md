# Writing a spec

A deltaplan project is a `deltaplan.yml` and a directory of specs — one file per
table, view or function, describing the state you want rather than the statements to
get there. This page covers YAML specs; a spec can also be a `CREATE` statement in a
`.sql` file — see [YAML and SQL specs](formats.md) for what each can say.

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

## The project file

`deltaplan.yml` says where the specs are and what each target substitutes into them.
Commands find it by walking up from the working directory, or you can point at one with
`--config`.

```yaml
version: 1
specs: [tables]                        # files or directories, relative to this file
history_schema: ${catalog}.deltaplan   # where `apply` keeps its run history

targets:
  dev:
    vars:
      catalog: dev
    profile: dev                       # optional ~/.databrickscfg profile
    warehouse_id: abc123def456         # optional; falls back to $DATABRICKS_WAREHOUSE_ID
  prod:
    vars:
      catalog: prod
    mode: additive                     # the default for every schema: additive | strict

schemas:                               # per-schema overrides of the target's mode
  ${catalog}.sales: strict
```

A schema that doesn't exist yet is created — once, just before the first table or view
that needs it — so a fresh target plans from nothing. deltaplan creates schemas but never
catalogs, and never drops a schema.

`history_schema` and the `schemas:` keys may use the target's variables, like a spec
can, so one project file serves every catalog. What the modes mean is in the
[safety model](safety.md#additive-and-strict-schemas).

There is a runnable example of exactly this layout in
[`examples/`](https://github.com/misja-pronk/deltaplan/tree/main/examples).

Without `-t`, a command uses the only target, or the one marked `default: true`.

### Next to an Asset Bundle

A project that already has a [Databricks Asset Bundle](https://docs.databricks.com/aws/en/dev-tools/bundles/)
doesn't need to list its targets twice. Name the bundle, and its targets become
deltaplan's:

```yaml
specs: [tables]
bundle: databricks.yml                 # relative to this file

targets:                               # optional: only what a bundle has no word for
  prod:
    mode: strict
    warehouse_id: abc123def456
```

From the bundle deltaplan takes each target's name, the `default: true` one, its
`workspace` (`profile`, or `host`), and the bundle's variables as that target resolves
them: defaults, the target's overrides, `BUNDLE_VAR_<name>` from the environment, and
references to `${var.…}`, `${bundle.target}` and `${bundle.name}`. Files listed under
`include:` are read too.

- **Specs can spell a variable either way**: `${catalog}` or the bundle's
  `${var.catalog}`.
- **deltaplan.yml has the last word.** Its targets add `mode` and `warehouse_id`, and
  their `vars` and `profile` override the bundle's. A target there that the bundle
  doesn't have is an error — it's almost certainly a typo.
- **The warehouse** is the target's `warehouse_id`, else the bundle's `warehouse_id`
  variable. If that variable is a lookup (`lookup: {warehouse: "Starter Warehouse"}`,
  as the `default-sql` template writes it), deltaplan finds the warehouse by name once
  connected.
- **Some variables need a workspace**: other lookups, complex variables, and anything
  using `${workspace.current_user.short_name}`. deltaplan doesn't guess them. A spec
  that uses one fails and says why; give the value under the target's `vars` instead.
- A bundle's own `mode: development | production` is about jobs and pipelines and has
  nothing to do with deltaplan's `additive | strict`, so it's ignored.

## Variables

`${catalog}` and friends come from the target's `vars`, so one spec serves dev, staging
and prod. A variable that isn't defined for the target is an error pointing at the line
that used it — never an empty string.

Unknown keys are an error too: a typo in a property name should fail the lint, not
silently do nothing. Every message carries the file, line and column it came from.

## Types

A type can be written two ways.

=== "As a string"

    ```yaml
    - name: address
      type: struct<street:string,zip:string>
    ```

=== "As nested YAML"

    ```yaml
    - name: address
      type:
        struct:
          - {name: street, type: string, comment: House number included}
          - {name: zip, type: string}
    ```

Both parse to the same type tree. The nested form is the one to reach for when fields
need their own comments or a `renamed_from`.

Arrays and maps take the same nested form, which is what you need to put a comment or a
`renamed_from` on a field inside a collection:

```yaml
- name: line_items
  type:
    array:
      element:
        struct:
          - {name: sku, type: string, renamed_from: item_code}
          - {name: quantity, type: int}

- name: by_code
  type:
    map:
      key: string
      value: int
```

Children are addressed with Databricks' own path syntax — `a.b` inside a struct, `a.element.b` inside an array, `m.key` /
`m.value` inside a map. Those paths are what you see in a plan:

```
  ~ address
    + zip STRING
    3. ADD COLUMN address.zip     [meta]
```

## Clustering

```yaml
cluster_by: [order_date, region]   # liquid clustering on these keys
cluster_by: auto                   # automatic: Databricks picks the keys
```

Leave `cluster_by` out for no clustering: a clustered table is then set to
`CLUSTER BY NONE`. Changing keys applies to data written from then on; run `OPTIMIZE`
to recluster what's there.

With `auto`, the keys the table shows are Databricks' choice and can change, so
deltaplan checks only that automatic clustering is on — it never diffs the keys, and
`import` writes `auto` rather than the keys it happened to find. Naming keys turns
automatic clustering off. It needs predictive optimization on the table; see
[automatic liquid clustering](https://docs.databricks.com/aws/en/delta/clustering#automatic-liquid-clustering).

## Renames

Columns are matched by name, so a rename would otherwise look like a drop plus an add —
data loss. `renamed_from` says what actually happened:

```yaml
- name: customer_ref
  type: string
  renamed_from: cust_id
```

deltaplan plans a `RENAME COLUMN` (enabling column mapping first, if the table doesn't
have it). Once the old name is gone and the new one exists, the hint is inert, and
`plan` notes that it can be removed. (It's `plan` rather than `validate` that says so,
because telling needs the live table.)

!!! warning "Column mapping is not free"
    Enabling `columnMapping` on a table breaks existing streaming readers. The plan
    labels that step `[feature]` and warns before you apply it.

A table can be renamed the same way. Without the hint, a new name looks like a new
table and the old one like an orphan — in a strict schema, an empty table created and
the full one dropped.

```yaml
table: ${catalog}.sales.orders
renamed_from: order_facts          # or ${catalog}.sales.order_facts
```

- **The rename is the table's first step**, before its hooks and any rewrite, so
  everything after it uses the new name. It warns that whatever reads the old name —
  views, jobs, dashboards — stops finding it.
- **Within the schema only.** Unity Catalog doesn't move a table between schemas with
  a rename, so `validate` rejects a `renamed_from` in another one.
- **The old name is never an orphan** while a spec names it in `renamed_from`, so a
  strict schema renames rather than drops. If both names exist, nothing is renamed and
  the plan says so.
- Once the rename has run, the hint is inert and `plan` notes that it can be removed.

## Column tags

```yaml
- name: email
  type: string
  tags: {pii: email, owner: crm}
```

Tags go on columns, not on fields inside them. Like table tags they are additive: the
tags in the spec are set, and tags someone else put on the column are listed as
unmanaged and left alone — including across a rewrite, which puts them back after
rebuilding the table.

## Identity, generated and default columns

```yaml
columns:
  - name: order_id
    type: bigint
    identity: always            # or by_default, or {generated: by_default, start: 100, increment: 1}
  - name: order_ts
    type: timestamp
  - name: order_date
    type: date
    generated: CAST(order_ts AS DATE)
  - name: status
    type: string
    default: "'new'"            # a SQL expression — note the quotes inside the quotes
```

A column takes one of the three, and they go on columns, not fields inside them.
Databricks treats them differently, and so does the plan:

- **A default** can be set, changed or dropped at any time. The first default on a table
  needs the `allowColumnDefaults` table feature, which the plan enables first, as its own
  step. A default applies to rows written from then on.
- **An identity or generated column** exists only from the moment the table is created.
  `CREATE TABLE` includes it; adding one to an existing table, or changing or removing
  one, is a step deltaplan won't run — the plan says so, and why.
- A rewrite carries defaults across. A table with an identity or generated column is
  never rewritten: the rebuilt table would have plain columns in their place.

An identity column must be `bigint`. All three are modelled, so a spec that leaves one out
means the column has none — as a missing comment means no comment. `import` writes them,
so an imported spec plans nothing.

## Adding a NOT NULL column to a table with data

A new column arrives empty in every existing row, so `NOT NULL` can't hold until those
rows are filled. `using:` — the same hint a rewrite reads — says how:

```yaml
- name: region
  type: string
  nullable: false
  using: "coalesce(country_region, 'unknown')"
```

The plan adds the column, fills it with
`UPDATE … SET region = <using> WHERE region IS NULL`, then sets `NOT NULL`. The fill
rewrites the files holding the rows it touches, so it is classed `rewrite` and a restore
point is recorded first. It is safe to repeat. Without `using:`, the plan still adds the
column, but warns that `SET NOT NULL` will fail.

## Hooks

```yaml
hooks:
  before: DELETE FROM ${catalog}.sales.orders WHERE order_id IS NULL
  after: OPTIMIZE ${catalog}.sales.orders
```

SQL to run around a table's changes, for what a spec can't say. Hooks run only when the
table has changes in the plan — they are for the change, not for every apply — and
`before` runs ahead of the table's first step, `after` behind its last. deltaplan runs
them as written and can't tell what they do, so the plan shows them with that warning.

## Column masks and row filters

```yaml
columns:
  - name: ssn
    type: string
    mask: ${catalog}.security.mask_ssn
  - name: email
    type: string
    mask:
      function: ${catalog}.security.mask_email
      using_columns: [region]

row_filter:
  function: ${catalog}.security.by_region
  columns: [region]
```

The functions are SQL UDFs named in full (`catalog.schema.function`) — created by hand,
or declared in a [function spec](#functions) so they're created in the same plan, before
the tables that use them. deltaplan treats what they protect as security controls:

- **It only adds or replaces them.** A mask or filter in the spec is set, or replaced if
  it names a different function. One the spec doesn't mention is listed as unmanaged
  and left in place — deltaplan will not remove a security control because a spec is
  silent about it. Remove one by hand, deliberately.
- **A new table never exists unprotected.** Masks and the filter are part of its
  `CREATE TABLE`, not added afterwards.
- **A missing function is caught first.** Setting a mask or filter on an existing table
  checks the function exists before running, so a typo is refused rather than
  half-applied.
- **A protected table is not rewritten.** A rewrite stages a copy of the data, and that
  copy holds whatever the applying principal can see — possibly unmasked — in a table
  without the protection. deltaplan plans that as a step it won't run, and says why.

See [row filters and column masks](https://docs.databricks.com/aws/en/tables/row-and-column-filters)
for how to write the functions.

## Grants

```yaml
grants:
  - principal: analysts
    privileges: [SELECT]
  - principal: etl@example.com
    privileges: [SELECT, MODIFY]
```

A principal the spec names has **exactly** those privileges on the table: missing ones
are granted, extra ones revoked — and the plan says so, with the `GRANT` that would
undo each revoke. Principals the spec doesn't name are someone else's business: they are
listed as unmanaged and never touched. Grants inherited from the schema or catalog
aren't the table's, and are ignored.

Privileges are `SELECT`, `MODIFY`, `APPLY TAG`, `MANAGE` and `ALL PRIVILEGES`. Anything
else is an error at load time: privileges are SQL keywords, not names, so they can't be
quoted — they are checked instead.

## Rewrites and `using`

Some changes can't be made in place: a column whose type can't be widened, a struct that
becomes an array, a map whose shape moves. deltaplan plans those as a
[rewrite](safety.md#what-a-rewrite-actually-does) — the table is rebuilt from a query
over itself — and writes the conversion where it honestly can:

| Change | What deltaplan writes |
|---|---|
| Between scalars | `CAST(amount AS STRING)` |
| Inside a struct | `named_struct('street', address.street, …)`, matched **by name** |
| Inside an array of structs | `transform(lines, x -> named_struct(…))` |
| A column that didn't exist | `CAST(NULL AS TIMESTAMP)` |
| A renamed column or field | read from the old name, written to the new one |

Where it can't — a struct becoming an array, a map's key or value type moving, or any
conversion that needs a decision rather than a cast — it refuses and names the column.
Tell it what to do with `using:`, a SQL expression evaluated against the *live* table:

```yaml
- name: amount
  type: string
  using: "format_number(amount, 2)"

- name: address
  type: string
  using: "concat_ws(' ', address.street, address.zip)"
```

`using` is a hint, like `renamed_from`: it describes how to get from the old table to the
new one, so it takes no part in comparisons and is only read when a rewrite actually
happens. It applies to whole columns — build nested values inside the expression rather
than putting `using` on a nested field.

!!! tip "A cast is not always what you mean"
    deltaplan writes the obvious cast. If you want different semantics — a date parsed
    with a format, a rounding rule, a default instead of NULL — write it with `using`
    and the plan will show exactly what will run.

## Views

A view spec has a `view:` key instead of `table:`, and a query instead of columns — a
view's columns are whatever its query returns.

```yaml
view: ${catalog}.sales.big_orders
comment: Orders over 1000
tags: {domain: sales}
grants:
  - {principal: analysts, privileges: [SELECT]}
query: |
  SELECT order_id, order_date, amount
  FROM ${catalog}.sales.orders
  WHERE amount > 1000
```

- **The query is what is compared.** Whitespace and a trailing semicolon don't count;
  anything else does. A changed query or comment is planned as a `REPLACE VIEW`, with
  the old definition as its undo.
- **Governance survives a replace.** Tags and grants are put back as they were right
  after the replace, then the spec's own changes to them are applied.
- **Views come after tables**, and after any view their query reads — so a view over a
  table created in the same plan, or over another view, just works. Views that read each
  other in a cycle are an error.
- **A table is never turned into a view**, or the other way round. If the catalog has a
  table where the spec says view, planning stops and says so.
- `import` writes view specs too, with the query as the catalog holds it — catalog names
  and all, since rewriting names inside SQL isn't something to do by text search.

## Schemas

A schema spec gives a schema its comment, tags and grants:

```yaml
schema: ${catalog}.sales
comment: Sales data
tags: {domain: sales}
grants:
  - {principal: analysts, privileges: [USE SCHEMA, SELECT]}
  - {principal: engineers, privileges: [USE SCHEMA, CREATE TABLE, MODIFY]}
```

- **A declared schema is created with its comment**, then tagged and granted — before
  any table in it, which then finds it made. Without a spec, a schema is still created
  bare when a table needs it.
- **A schema is shared ground, so a spec only adds.** A comment is set only when the
  spec gives one; tags and grants the spec doesn't name are reported and left alone.
  Grants to a principal the spec does name are made to match it exactly.
- **A schema is never dropped**, strict mode or not.
- Schema privileges: `USE SCHEMA`, `SELECT`, `MODIFY`, `EXECUTE`, `REFRESH`, `APPLY TAG`,
  `MANAGE`, `ALL PRIVILEGES`, `CREATE TABLE` (views too), `CREATE FUNCTION`,
  `CREATE VOLUME`, `CREATE MATERIALIZED VIEW`, `CREATE MODEL`, `READ VOLUME` and
  `WRITE VOLUME`.
- `import` writes the schema's spec as `_schema.yml` when it has a comment, tags or
  grants. In SQL, `CREATE SCHEMA … COMMENT` and `GRANT … ON SCHEMA` work; tags need YAML.

## Functions

A function spec has a `function:` key, its parameters, what it returns, and a body — the
expression after `RETURN`.

```yaml
function: ${catalog}.security.mask_email
comment: Hide emails from everyone outside pii
parameters:
  - {name: email, type: string}
returns: string
grants:
  - {principal: analysts, privileges: [EXECUTE]}
body: |
  CASE WHEN is_account_group_member('pii') THEN email ELSE '***' END
```

- **SQL functions only.** Python UDFs aren't modelled; `import` skips them and `plan`
  leaves them alone.
- **Parameters, return type, body and comment are its shape.** Whitespace and a trailing
  semicolon in the body don't count; any other change is planned as a
  `REPLACE FUNCTION`, with the old definition as its undo. A replace takes effect for
  every mask, row filter and view that calls the function, from the moment it runs — the
  plan warns about exactly that.
- **Grants survive a replace**, put back as they were, and are otherwise managed per
  principal as for [tables](#grants). Function privileges are `EXECUTE`, `MANAGE` and
  `ALL PRIVILEGES`.
- **Functions come first**: before tables, so a mask or row filter can call one created
  in the same plan, and before views that call them. A function that calls another
  comes after it; a cycle is an error.
- **A function is never dropped.** It carries no ownership marker, so nothing shows
  deltaplan created it; one without a spec is left alone, in strict schemas too.
- Unity Catalog lets a function share a table's name. deltaplan doesn't: plans are keyed
  by name, so planning stops and asks you to rename one.

## Constraints

```yaml
constraints:
  - primary_key: [order_id]                       # or: {columns: [...], name: ...}
  - check: {name: positive_amount, expression: "amount > 0"}
```

```yaml
constraints:
  - foreign_key:
      columns: [customer_id]
      references: ${catalog}.sales.customers
      referenced_columns: [customer_id]
      name: orders_customer_fk          # optional
```

Primary and foreign keys in Unity Catalog are informational, and a primary key's columns
must be declared `nullable: false` — `validate` says so if they aren't. A foreign key must
point at the referenced table's primary key. Foreign keys are planned last, after every
table, so the table they reference always exists first. One is matched by what it means —
columns, table, referenced columns — so an unnamed key in the spec is satisfied by the
same key under any name; name it only if the name matters. Expressions — checks,
generated columns, defaults — are compared by what they say, not how they're spelled:
both sides are parsed and written back in one canonical form, so `cast(Placed_At as
date)` in a spec matches the catalog's `( CAST(placed_at AS DATE) )`.

## What deltaplan leaves alone

Properties, tags and constraints that exist on the live table but aren't in the spec are
reported as **unmanaged** and never diffed away — deltaplan can't tell "I stopped
managing this" from "someone else owns this", so it doesn't guess. The same goes for
tables in the schema that no spec describes, and for views and non-Delta tables.
