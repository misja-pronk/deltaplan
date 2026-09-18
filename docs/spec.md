# Writing a spec

A deltaplan project is a `deltaplan.yml` and a directory of specs — one YAML file
per table, describing the state you want rather than the statements to get there.

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

## Renames

Columns are matched by name, so a rename would otherwise look like a drop plus an add —
data loss. `renamed_from` says what actually happened:

```yaml
- name: customer_ref
  type: string
  renamed_from: cust_id
```

deltaplan plans a `RENAME COLUMN` (enabling column mapping first, if the table doesn't
have it). Once the old name is gone and the new one exists, the hint is inert;
`validate` will tell you it can be removed.

!!! warning "Column mapping is not free"
    Enabling `columnMapping` on a table breaks existing streaming readers. The plan
    labels that step `[feature]` and warns before you apply it.

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

The functions are ordinary SQL UDFs you create yourself, named in full
(`catalog.schema.function`). deltaplan treats what they protect as security controls:

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

## Constraints

```yaml
constraints:
  - primary_key: [order_id]                       # or: {columns: [...], name: ...}
  - check: {name: positive_amount, expression: "amount > 0"}
```

Primary keys in Unity Catalog are informational, and their columns must be declared
`nullable: false` — `validate` says so if they aren't. A check expression is compared
textually after stripping outer parentheses and collapsing whitespace, so write it the
way the catalog echoes it back.

!!! note "Foreign keys aren't modelled yet"
    A `foreign_key:` entry is rejected with an explicit error rather than silently
    ignored. They need cross-table ordering, which arrives with `apply`.

## What deltaplan leaves alone

Properties, tags and constraints that exist on the live table but aren't in the spec are
reported as **unmanaged** and never diffed away — deltaplan can't tell "I stopped
managing this" from "someone else owns this", so it doesn't guess. The same goes for
tables in the schema that no spec describes, and for views and non-Delta tables.
