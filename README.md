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

> **Status: alpha.** Every milestone in the design is built — plan, apply (rewrites
> included), drift, the GitHub Action, and governance (tags, grants, masks, row filters,
> views, SQL functions). It is tested offline against a fake warehouse, and every
> assumption it makes about Databricks is checked by a live suite against a real
> workspace. Try it on a dev catalog before production.

```sh
uv tool install --prerelease allow deltaplan   # or: pip install --pre deltaplan
```

**[Take the tour →](https://misja-pronk.github.io/deltaplan/tour/)** — one project from
nothing to a reviewed pull request, every step shown — or browse the
[feature gallery](https://misja-pronk.github.io/deltaplan/features/). The
[docs](https://misja-pronk.github.io/deltaplan/) have the spec format, the commands, and
the safety model. [`docs/DESIGN.md`](docs/DESIGN.md) is the source of truth.

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

Or as SQL — the same model, read with [sqlglot](https://github.com/tobymao/sqlglot):

```sql
CREATE TABLE ${catalog}.sales.customers (
  customer_id BIGINT NOT NULL,
  name        STRING,
  CONSTRAINT customers_pk PRIMARY KEY (customer_id)
)
CLUSTER BY AUTO;
```

A project can mix both. SQL specs support what sqlglot can parse; YAML supports
everything — [the list](https://misja-pronk.github.io/deltaplan/formats/) says which.

## The plan

![A deltaplan plan: a rename, a widening, a backfilled NOT NULL column, a nested field, a CHECK and a grant, each with its numbered, risk-labelled steps](https://misja-pronk.github.io/deltaplan/assets/screens/tour-plan-change.svg)

## Why

- **Delta-aware.** Metadata-only, needs-a-table-feature, and full-rewrite are different
  things, and the plan says which one you're about to do — before you do it.
- **Safe by default.** Only tables deltaplan manages are ever drop candidates;
  everything else is reported as unmanaged and left untouched. Destructive steps need
  `--allow-destructive`, and a stale plan is refused.
- **No state file.** Unity Catalog is the state.
- **Nested types are first class.** Struct, array and map fields diff by path
  (`address.element.zip`), including renames and per-field comments.
- **Reviewable.** The plan is a data structure; the terminal, Markdown (for PR comments)
  and JSON renderers all read the same object.

## Commands

```sh
deltaplan validate -t dev             # spec lint, no connection needed
deltaplan import main.sales -o tables # live tables -> YAML specs
deltaplan plan -t dev [-o plan.json] [--format rich|md|json]
deltaplan show plan.json -f md        # render a saved plan, no warehouse needed
deltaplan apply plan.json [--allow-destructive]
deltaplan drift -t dev                # exit code 2 on drift, for CI
deltaplan force-unlock -t dev
```

## In CI

```yaml
- uses: misja-pronk/deltaplan@v0
  with:
    target: prod        # comments the plan on the pull request
```

Plan on pull requests, apply on merge, catch drift nightly — see
[the CI guide](https://misja-pronk.github.io/deltaplan/ci/).

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
