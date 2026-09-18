# Writing a spec

A spec is one YAML file per table: the state you want, not the statements to get there.

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

## Variables

`${catalog}` and friends are substituted per target, so one spec serves dev, staging and
prod. Unknown keys are an error — a typo in a property name should fail the lint, not
silently do nothing.

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

Arrays and maps nest the same way, and their children are addressed with Databricks'
own path syntax — `a.b` inside a struct, `a.element.b` inside an array, `m.key` /
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
