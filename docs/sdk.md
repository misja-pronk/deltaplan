# As a library

deltaplan is a command you run, and a library you call. The command is the
library's first customer: every `deltaplan` subcommand is argument parsing and
rendering around the functions on this page, which is how they stay honest.

Reach for this when deltaplan is a step inside something larger — a deployment
task that plans, shows the plan its own way, asks its own question and applies
it; a notebook; a policy check that refuses a plan with a `destructive` step in
it.

```sh
pip install deltaplan            # no extras: the library is the package
```

Everything below is exported from `deltaplan` itself. Names reached through a
submodule are the implementation, and move without notice.

<hr class="dp-rule">

## The seven steps

Every host does these, in this order.

```python
import deltaplan

# 1. the project
project = deltaplan.Project.find()                 # or .load("deltaplan.yml")

# 2. a target, resolved
target = project.resolve(project.default)          # asks the Databricks CLI
                                                   # about a bundle, if there is one

# 3. the specs, with every diagnostic
specs = project.load_specs(target)
if specs.errors:
    for problem in specs.errors:
        print(problem)                             # file:line:column, and what
    raise SystemExit(1)

# 4. a connection
conn = deltaplan.Connection.from_target(target)

# 5. a plan
plan = deltaplan.plan(project, target, conn, specs=specs)
print(plan.summary)                                # Plan: 1 add, 2 change, …

# 6. apply it
if not plan.empty:
    run = deltaplan.apply(plan, conn, project=project, target=target)
    print(run.ok, run.ran, run.failed_step)

# 7. or just ask whether anything moved
if not deltaplan.drift(project, target, conn).empty:
    print("the workspace no longer matches the specs")
```

## A bundle you have already resolved

A host that just deployed a Databricks Asset Bundle holds what
`databricks bundle validate -o json -t <target>` printed. Hand that over and
**no subprocess runs**:

```python
target = project.resolve(project.target("prod"), bundle_config=config)
```

Without it, deltaplan asks the CLI itself — `DATABRICKS_CLI_PATH` first, then
`PATH` (`deltaplan.find_cli()`). If the CLI is there and fails, that is a
`BundleError` carrying **its** words: a bundle that doesn't resolve has no names
to plan against. On a machine with no CLI at all, the bundle file stands in and
what only the CLI could have settled stays unknown, with the reason.

!!! tip "`databricks` is a shim?"
    On a machine that manages tools with shims, the `databricks` on `PATH` can
    be `mise`, which the Databricks SDK's own `databricks-cli` authentication
    then can't use. Point `DATABRICKS_CLI_PATH` at the real binary; deltaplan
    and the SDK both follow it.

## Connecting

```python
conn = deltaplan.Connection.from_target(target)            # the usual way
conn = deltaplan.Connection(client=my_client, warehouse_id="abc123")
conn = deltaplan.Connection(profile="dev", warehouse_id="abc123")
conn = deltaplan.Connection(runner=my_runner)              # already runs SQL
```

A client you pass is the client deltaplan uses; it never makes a second one.
The warehouse is settled in one order, documented once: what you passed, then
the target's `warehouse_id`, then `DATABRICKS_WAREHOUSE_ID`, then the warehouse
a bundle's `lookup:` names. A failure here is `NotConnected`.

## Reading a plan

A `Plan` is a frozen object, not a string to parse:

```python
plan.empty, plan.is_destructive, plan.highest_risk
plan.summary.add, plan.summary.change, plan.summary.destroy, plan.summary.steps
plan.unmanaged            # live objects no spec describes
plan.orphaned             # deltaplan's own, whose spec is gone
plan.not_managed          # what `manage:` hands to another tool

for diff in plan.diffs:
    diff.table, diff.kind, diff.action      # "table" / "view" / … , "create" / …
    for step in diff.steps:
        step.id, step.title, step.risk, step.sql, step.est_bytes, step.warnings
```

Render it the way you need: `render_plan(plan, console)` for a Rich console,
`plan_text(plan)` for a plain string, `render_markdown(plan)` for a pull-request
comment, `plan_to_json(plan)` / `plan_from_json(text)` for a file or a queue —
a plan made here can be applied somewhere else.

## Applying

```python
run = deltaplan.apply(
    plan, conn,
    project=project, target=target,        # for where the run is recorded
    allow_destructive=False,
    observer=lambda step, status, note: print(step.id, status),
)
```

`apply` records every run in the project's `history_schema`; pass
`history=deltaplan.MemoryHistory()` in a test, or a store of your own. It takes
a lock per target, skips steps already true of the live table, and resumes a run
that stopped.

Before you ask a person to confirm, `deltaplan.is_stale(plan, conn)` says
whether the world has moved under the plan.

## Importing what already exists

```python
found = deltaplan.import_schema(conn, "main.sales", manage=project.manage)
for spec in found:
    print(spec.filename, spec.relation.name)
    write_somewhere(spec.text)
for name, reason in found.skipped:
    print("left alone:", name, reason)
```

Nothing is written: where the specs go is yours. Owners are left out, as is
anything `manage:` hands to another tool.

## Errors

Every error deltaplan raises on purpose descends from `DeltaplanError`, so one
`except` reports and a subclass reacts:

| | when | what a host does |
|---|---|---|
| `SpecErrors` | specs that can't be read — **all** of them, at once | show the list |
| `BundleError` | the bundle doesn't resolve | show what the CLI said |
| `NotConnected` | no workspace, or no warehouse | check the profile or the id |
| `PlanningError` | the specs can't be planned together | show it |
| `IntrospectionError` | the workspace can't be read | retry, or show it |
| `StalePlan` | the world moved; `.tables` names it | plan again |
| `DestructiveRefused` | it would destroy something; `.tables` names it | ask a person |
| `ExecutionError` | anything else that stops a run | show it |
| `NoHistory` | nowhere to record the run | set `history_schema` |

A step that fails while it is running doesn't raise: the result says which one
(`run.failed_step`, `run.error`), so the rest of the run stays on record.
