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
specs: [tables]                  # files or directories, relative to this file
history_schema: main.deltaplan   # where `apply` keeps its history (milestone 2)

targets:
  dev:
    vars:
      catalog: dev
    warehouse_id: abc123def456   # optional; falls back to $DATABRICKS_WAREHOUSE_ID
  prod:
    vars:
      catalog: prod
    mode: additive               # additive (default) | strict
```

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
