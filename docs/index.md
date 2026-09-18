# deltaplan

Declarative, Terraform-style `plan` / `apply` for Databricks SQL tables — Unity Catalog and Delta.

!!! warning "Pre-alpha"
    `validate`, `import`, `plan`, `apply` and `force-unlock` work, including
    rewrites. `drift` and the governance milestone are still to come.
    [DESIGN.md](DESIGN.md) is the source of truth for the rest.

Describe the tables you want in YAML, diff that against live Unity Catalog, review a
plan, then apply it. deltaplan knows which Delta changes are metadata-only, which need
a table feature enabled first, and which force a rewrite — and it says so before it
touches anything.

<hr class="dp-rule">

## Highlights

- **A plan you can actually read** — per table, per column, nested struct changes as a
  tree, numbered steps, risk labels, and size hints on anything that rewrites.
- **Delta-aware planning** — metadata-only vs. table-feature vs. rewrite is a
  classification the planner makes explicit, not a surprise at apply time.
- **Safe by default** — only tables deltaplan created can ever be drop candidates.
  Everything else is reported as unmanaged and left alone; destructive steps need
  `--allow-destructive`.
- **No state file** — Unity Catalog *is* the state. Nothing to sync, nothing to corrupt.
- **Nested types are first class** — struct, array and map fields diff by path
  (`address.element.zip`), with per-field comments and renames.
- **Built for CI** — a `drift` command with a non-zero exit code, a Markdown renderer
  for PR comments, and JSON for anything else.
- **Fits your stack** — Python-native, Apache-2.0, and happy next to Databricks Asset
  Bundles.

## A spec

```yaml
table: ${catalog}.sales.orders
comment: Order facts
cluster_by: [order_date]
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
```

## The plan it produces

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

## Next steps

- [Installation](installation.md) — install with uvx, uv tool, or pipx.
- [Writing a spec](spec.md) — the YAML format, types, and renames.
- [Commands](cli.md) — `validate`, `import`, `plan`, `apply`, `drift`.
- [Safety model](safety.md) — ownership, risk classes, and what deltaplan refuses to do.
