# Installation

deltaplan is a Python 3.11+ CLI, published on [PyPI](https://pypi.org/project/deltaplan/).

!!! warning "Alpha"
    Every release so far is a pre-release (`0.1.0a1`, …), which installers skip unless
    asked — hence the flags below. Try it on a dev catalog before a production one.

=== "uv tool"

    ```sh
    uv tool install --prerelease allow deltaplan
    deltaplan version
    ```

=== "uvx"

    ```sh
    uvx --prerelease allow deltaplan plan -t dev
    ```

=== "pipx"

    ```sh
    pipx install --pip-args=--pre deltaplan
    deltaplan version
    ```

=== "pip"

    ```sh
    pip install --pre deltaplan
    ```

## From source

```sh
git clone https://github.com/misja-pronk/deltaplan
cd deltaplan
uv sync
uv run deltaplan version
```

## Connecting to a workspace

deltaplan delegates authentication to the Databricks SDK's unified auth, so anything
that works for the Databricks CLI works here.

**Per target, with profiles** — the usual setup, since dev and prod tend to be
different workspaces. Log in once per workspace with the Databricks CLI, then name the
profile and a SQL warehouse on each target:

```sh
databricks auth login --host https://adb-1111.1.azuredatabricks.net --profile dev
databricks auth login --host https://adb-2222.2.azuredatabricks.net --profile prod
```

```yaml
targets:
  dev:
    vars: {catalog: dev}
    profile: dev
    warehouse_id: abc123def456
  prod:
    vars: {catalog: prod}
    profile: prod
    warehouse_id: 789ghi012jkl
```

`--profile` on any command overrides the target's.

**From the environment** — what CI does. With no profile set anywhere, the SDK's
defaults apply:

```sh
export DATABRICKS_HOST="https://adb-1234567890.1.azuredatabricks.net"
export DATABRICKS_TOKEN="dapi..."
export DATABRICKS_WAREHOUSE_ID="abc123def456"
```

Statements run on a SQL warehouse via the Statement Execution API, so a warehouse id —
from the target, `--warehouse-id`, or `DATABRICKS_WAREHOUSE_ID` — is required for
everything except `validate` and `show`.

!!! tip "`validate` needs nothing"
    `deltaplan validate` is a pure spec lint — no credentials, no network. It is the
    right thing to run in a pre-commit hook.
