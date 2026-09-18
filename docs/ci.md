# In CI

deltaplan ships as a GitHub Action. It runs `plan` or `drift`, puts the result in the
job summary, and — on a pull request — posts it as a comment, updating its own comment
on every push instead of adding another.

## Credentials

The action talks to your workspace the way the CLI does, through the Databricks SDK's
unified auth. Give the job the environment variables, from repository or environment
secrets:

```yaml
env:
  DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}
  DATABRICKS_TOKEN: ${{ secrets.DATABRICKS_TOKEN }}
  DATABRICKS_WAREHOUSE_ID: ${{ secrets.DATABRICKS_WAREHOUSE_ID }}
```

A service principal with OAuth (`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`)
works the same way, and is the better choice for anything that applies.

## Plan on every pull request

```yaml
name: deltaplan
on:
  pull_request:
    paths: ["tables/**", "deltaplan.yml"]

permissions:
  contents: read
  pull-requests: write   # to comment

jobs:
  plan:
    runs-on: ubuntu-latest
    env:
      DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}
      DATABRICKS_TOKEN: ${{ secrets.DATABRICKS_TOKEN }}
      DATABRICKS_WAREHOUSE_ID: ${{ secrets.DATABRICKS_WAREHOUSE_ID }}
    steps:
      - uses: actions/checkout@v4
      - id: plan
        uses: misja-pronk/deltaplan@v0
        with:
          target: prod
      - uses: actions/upload-artifact@v4
        with:
          name: plan
          path: ${{ steps.plan.outputs.plan-file }}
```

The comment shows the summary, an alert for anything destructive, expensive or
impossible, the changes per table, the numbered steps with their risk, and the SQL
folded away underneath. It is rendered *from the plan file*, so it shows exactly what
`apply` of that file would run.

## Apply on merge

Review the plan on the pull request; apply it when it merges. A protected
[environment](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)
adds a human approval in between.

```yaml
name: deltaplan apply
on:
  push:
    branches: [main]
    paths: ["tables/**", "deltaplan.yml"]

jobs:
  apply:
    runs-on: ubuntu-latest
    environment: prod        # required reviewers go here
    env:
      DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}
      DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID }}
      DATABRICKS_CLIENT_SECRET: ${{ secrets.DATABRICKS_CLIENT_SECRET }}
      DATABRICKS_WAREHOUSE_ID: ${{ secrets.DATABRICKS_WAREHOUSE_ID }}
    steps:
      - uses: actions/checkout@v4
      - id: plan
        uses: misja-pronk/deltaplan@v0
        with:
          target: prod
          comment: false
      - if: steps.plan.outputs.has-changes == 'true'
        uses: astral-sh/setup-uv@v6
      - if: steps.plan.outputs.has-changes == 'true'
        env:
          PLAN_FILE: ${{ steps.plan.outputs.plan-file }}
        run: uvx deltaplan apply "$PLAN_FILE"
```

The plan is made fresh at merge time, so it is always against the tables as they are
now; `apply` refuses it anyway if they move between the two steps. Add
`--allow-destructive` only if you mean it — without it, a plan that drops anything stops
before running a single statement.

!!! note "Before the first PyPI release"
    `uvx deltaplan` needs deltaplan on PyPI. Until then, install from the repository:
    `uvx --from git+https://github.com/misja-pronk/deltaplan deltaplan apply …`.

## Catch drift nightly

`drift` asks whether `apply` would do anything. A hand edit in the catalog, a table
dropped outside deltaplan, a spec merged but never applied — all show up. Unmanaged
objects don't: deltaplan never claimed them.

```yaml
name: deltaplan drift
on:
  schedule:
    - cron: "0 6 * * 1-5"
  workflow_dispatch:

jobs:
  drift:
    runs-on: ubuntu-latest
    env:
      DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}
      DATABRICKS_TOKEN: ${{ secrets.DATABRICKS_TOKEN }}
      DATABRICKS_WAREHOUSE_ID: ${{ secrets.DATABRICKS_WAREHOUSE_ID }}
    steps:
      - uses: actions/checkout@v4
      - uses: misja-pronk/deltaplan@v0
        with:
          command: drift
          target: prod
```

The job fails when there is drift, with the details in the job summary. Set
`fail-on-drift: false` to report without failing, and branch on the `has-changes`
output instead.

On the command line, `deltaplan drift` exits `0` when live tables match their specs,
`2` when they have drifted, and `1` when something went wrong — the convention
`terraform plan -detailed-exitcode` uses.

## Inputs and outputs

| Input | Default | |
|---|---|---|
| `target` | *(required)* | The target in `deltaplan.yml`. |
| `command` | `plan` | `plan` or `drift`. |
| `config` | `deltaplan.yml` | Relative to `working-directory`. |
| `working-directory` | `.` | Where the project lives. |
| `clone` | `false` | `plan` only: `SHALLOW CLONE` before risky steps. |
| `comment` | `true` | Comment on the pull request, if there is one. |
| `fail-on-drift` | `true` | `drift` only: fail the job on drift. |
| `github-token` | `github.token` | Needs `pull-requests: write` to comment. |

| Output | |
|---|---|
| `has-changes` | `true` if `apply` would do something (or, for `drift`, if there is drift). |
| `plan-file` | The plan as JSON, for `deltaplan apply`. Empty for `drift`. |
| `markdown-file` | The rendered comment. |
