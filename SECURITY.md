# Security Policy

## How deltaplan handles your workspace

- **No credentials are stored.** Authentication is delegated entirely to the
  Databricks SDK's unified auth (`~/.databrickscfg` profiles, OAuth token cache, or
  `DATABRICKS_*` environment variables). deltaplan never writes a token anywhere.
- **No state file.** Unity Catalog is the state. There is no local artefact holding
  a copy of your schema, and nothing to leak or drift.
- **Plans are inert.** `deltaplan plan` only reads (`information_schema`,
  `DESCRIBE TABLE EXTENDED`, `DESCRIBE DETAIL`). Every statement that changes
  anything is executed by `apply`, from a reviewed plan, and nowhere else.
- **Identifiers are always quoted.** SQL is never assembled by concatenating raw
  identifiers; one `quote_ident()` helper handles every name that reaches a
  statement.
- **Safe by default.** Only tables deltaplan created (`deltaplan.managed = true`) can
  ever be drop candidates; everything else is reported as unmanaged and left
  untouched. Destructive steps require `--allow-destructive`, and `apply` refuses a
  stale plan whose state fingerprint no longer matches the live tables.

Plans and the history tables record the SQL that was run, including column names and
table comments. Treat plan JSON as you would a schema dump — it can contain
business-sensitive names, though never data.

## Supported versions

The latest released version on PyPI is supported. Please upgrade before reporting an
issue.

## Reporting a vulnerability

Please report security issues **privately**:

- Open a [GitHub Security Advisory](https://github.com/misja-pronk/deltaplan/security/advisories/new), or
- email **misja@prorexconsultancy.nl**.

Do not open a public issue for security reports. You'll get an acknowledgement as
soon as possible, and we'll coordinate a fix and disclosure with you.
