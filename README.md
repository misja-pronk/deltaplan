# deltaplan △

**Declarative `plan` / `apply` for Databricks SQL tables.**
Describe your Unity Catalog tables in YAML, diff that against the live catalog,
review a plan that knows which Delta changes are free and which rewrite 400 GB —
then apply it.

[![ci](https://github.com/misja-pronk/deltaplan/actions/workflows/ci.yml/badge.svg)](https://github.com/misja-pronk/deltaplan/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-deltaplan-1f9e9a.svg)](https://misja-pronk.github.io/deltaplan/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

> **Status: pre-alpha.** Milestone 1 (read-only: `validate`, `import`, `plan`) is under
> construction. Nothing here writes to a workspace yet.

**[Read the docs →](https://misja-pronk.github.io/deltaplan/)** — the spec format, the
commands, and the safety model. [`docs/DESIGN.md`](docs/DESIGN.md) is the source of truth.

## The spec

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

## The plan

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

## Why

- **Delta-aware.** Metadata-only, needs-a-table-feature, and full-rewrite are different
  things, and the plan says which one you're about to do — before you do it.
- **Safe by default.** Only tables deltaplan created are ever drop candidates;
  everything else is reported as unmanaged and left untouched. Destructive steps need
  `--allow-destructive`, and a stale plan is refused.
- **No state file.** Unity Catalog is the state.
- **Nested types are first class.** Struct, array and map fields diff by path
  (`address.element.zip`), including renames and per-field comments.
- **Reviewable.** The plan is a data structure; the terminal, Markdown (for PR comments)
  and JSON renderers all read the same object.

## Commands

```sh
deltaplan validate            # spec lint, no connection needed
deltaplan import <schema>     # live tables -> YAML specs
deltaplan plan -t <target> [-o plan.json] [--format rich|md|json]
deltaplan apply plan.json [--allow-destructive]
deltaplan drift -t <target>   # exit code != 0 on drift, for CI
deltaplan force-unlock
```

## Development

deltaplan uses [mise](https://mise.jdx.dev) + the Astral stack
([uv](https://docs.astral.sh/uv/), [ruff](https://docs.astral.sh/ruff/),
[ty](https://docs.astral.sh/ty/)).

```sh
mise install     # pinned Python + uv
uv sync          # .venv with deps and dev tools

mise run check   # lint + format check + types + unit tests
mise run test    # uv run pytest tests/unit
mise run docs    # preview the docs at localhost:8000
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the architecture and the house rules.

## License

Apache-2.0 — see [LICENSE](LICENSE).
