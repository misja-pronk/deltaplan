# YAML and SQL specs

A spec can be written two ways, and a project can mix them freely: each file is one
table, view or function.

- **YAML** (`.yml`, `.yaml`) is deltaplan's own format. It can say everything
  deltaplan manages — including its hints, like `renamed_from` and `using`, which have
  no SQL spelling. [Writing a spec](spec.md) covers it.
- **SQL** (`.sql`) is a `CREATE TABLE`, `CREATE VIEW` or `CREATE FUNCTION` statement,
  in Databricks SQL, optionally followed by `ALTER … SET TAGS` and `GRANT` statements
  about the same object.

Both are read into the same model, so a SQL spec and the YAML spec that says the same
thing plan identically. `deltaplan import -f sql` writes SQL specs for what's already
there.

```sql
CREATE TABLE ${catalog}.sales.customers (
  id         BIGINT NOT NULL COMMENT 'Surrogate key',
  name       STRING,
  created_at TIMESTAMP,
  CONSTRAINT customers_pk PRIMARY KEY (id)
)
COMMENT 'One row per customer'
CLUSTER BY (id);

ALTER TABLE ${catalog}.sales.customers SET TAGS ('domain' = 'sales');
GRANT SELECT ON TABLE ${catalog}.sales.customers TO `analysts`;
```

## How SQL specs are read

SQL specs are parsed with [sqlglot](https://github.com/tobymao/sqlglot)'s Databricks
dialect, and the rule is strict: **what sqlglot parses into structure, a SQL spec can
use; what it can't, a SQL spec can't** — even when Databricks itself accepts it. Such a
spec is refused with the line it's on, and the feature is written in YAML instead. As
sqlglot learns more of Databricks SQL, this list grows.

- The statement is a declaration, never run as written: `CREATE`, `CREATE OR REPLACE`
  and `IF NOT EXISTS` all mean the same thing. deltaplan plans its own statements from
  the difference with the live object, as for YAML.
- `${catalog}` and friends work as in YAML, and so does the bundle spelling
  `${var.catalog}`.
- A view's query and a function's body are kept exactly as written. Column-level
  expressions — `CHECK`, `GENERATED ALWAYS AS`, `DEFAULT` — are taken the way sqlglot
  renders them, which may differ from what you wrote in spacing and case.
- A `CHECK` constraint needs a name: `CONSTRAINT positive_amount CHECK (amount > 0)`.

## What each format supports

<!-- features:start -->
| | Feature | YAML | SQL | Notes |
|---|---|:---:|:---:|---|
| Tables | Columns and types, nested included | ✓ | ✓ | struct, array, map, decimal, char/varchar, timestamp_ntz, variant |
|  | NOT NULL, on nested fields too | ✓ | ✓ |  |
|  | Column comments | ✓ | ✓ |  |
|  | Table comment | ✓ | ✓ |  |
|  | Liquid clustering keys | ✓ | ✓ |  |
|  | Automatic liquid clustering | ✓ | ✓ | `cluster_by: auto` in YAML |
|  | Table properties | ✓ | ✓ |  |
|  | Primary key | ✓ | ✓ |  |
|  | Foreign keys | ✓ | ✓ |  |
|  | CHECK constraints | ✓ | ✓ | named: `CONSTRAINT <name> CHECK (…)` |
|  | Identity columns | ✓ | ✓ |  |
|  | Generated columns | ✓ | ✓ |  |
|  | Column defaults | ✓ | ✓ |  |
|  | Table tags | ✓ | ✓ | an `ALTER TABLE … SET TAGS` after the CREATE |
|  | Grants | ✓ | ✓ | `GRANT` statements after the CREATE |
|  | Column tags | ✓ | — | sqlglot passes `ALTER COLUMN … SET TAGS` through as unparsed text |
|  | Column masks | ✓ | — | sqlglot can't parse `MASK` |
|  | Row filters | ✓ | — | sqlglot passes `WITH ROW FILTER` through as unparsed text |
|  | Column renames (`renamed_from`) | ✓ | — | a deltaplan hint; SQL has no way to say it |
|  | Table renames (`renamed_from`) | ✓ | — | a deltaplan hint; SQL has no way to say it |
|  | Conversions and backfills (`using`) | ✓ | — | a deltaplan hint; SQL has no way to say it |
|  | Hooks | ✓ | — | deltaplan's own; SQL has no way to say it |
|  | Partitioning | — | — | not modelled: reported on live tables, never managed |
| Schemas | Schemas: comment and grants | ✓ | ✓ | never dropped |
|  | Schema tags | ✓ | — | sqlglot passes `ALTER SCHEMA … SET TAGS` through as unparsed text |
| Views | Views: query, comment, properties | ✓ | ✓ | the query is kept exactly as written |
|  | View tags and grants | ✓ | ✓ |  |
| Functions | SQL functions: parameters, return type, body, comment | ✓ | ✓ | the body is kept exactly as written |
|  | Function grants | ✓ | ✓ |  |
<!-- features:end -->

Every row with a ✓ under SQL is a test that loads a SQL spec using it; every — is a
test that such a spec is refused.
