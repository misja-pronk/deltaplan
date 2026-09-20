# Testing without a workspace

deltaplan generates SQL for a system most contributors can't run on a laptop, and
that CI shouldn't need credentials to check. The suite is built in three layers, and
each one is honest about what it can and cannot prove.

```sh
uv run pytest tests/unit        # layers 1 and 2 — fast, offline, every PR
uv run pytest -m integration    # layer 3 — real workspace, nightly
```

<hr class="dp-rule">

## 1. Unit tests

The type parser, loader, differ and planner are pure functions, so they are tested the
ordinary way: inputs in, values out, with golden plans in `tests/snapshots/` for
anything shaped like a document.

This is where most of the tests live, and it is only possible because the middle of the
pipeline does no I/O.

## 2. Convergence against a fake warehouse

`tests/fake_warehouse.py` is an in-memory Unity Catalog. It holds `Table` models,
answers the introspector's `information_schema` and `DESCRIBE` queries by rendering
them into the row shapes the real API returns, and **interprets the statements the
planner generates** by mutating those models.

That closes the loop offline:

```python
plan = build_plan(diff(desired, live))  # what we would do
run(plan, fake)  # do it
assert diff(desired, live_now(fake)) == ()  # nothing left to do
```

`tests/unit/test_convergence.py` makes that assertion for every kind of change —
creates, nested adds, renames, widenings, drops, constraints, reordering — and
`tests/unit/test_executor.py` uses the same fake to test skip, resume, failure,
locking and the destructive gate.

**What this proves.** That every statement deltaplan emits says what the change it came
from meant; that a plan closes the diff it was built from; that `is_applied` — the
executor's idempotency check — agrees with the differ; and that the executor's own
machinery behaves. In milliseconds, with no credentials.

!!! warning "What it does not prove"
    The fake implements *deltaplan's* reading of the Databricks manual. If we have
    misread it, the fake misreads it the same way and the tests still pass. It cannot
    tell you that Databricks accepts a statement, that a widening is permitted, or that
    a nested rename works.

    Anything the fake doesn't recognise raises `FakeSqlError` rather than passing
    quietly, so a new statement shape can't slip through untested — but a *wrong*
    statement the fake also gets wrong will sail through. That is what layer 3 is for.

## 3. Integration tests

`tests/integration/` is the only source of truth about Databricks. Each test creates an
ephemeral schema, does its work, and drops it. They are marked `@pytest.mark.integration`
and skip themselves without credentials, so forks and laptops stay green, and they run
nightly in CI.

They assert the things nothing else can:

- a table created from a spec reads back as that spec;
- plan → apply → re-plan is empty, against the real thing;
- `DROP COLUMN` really is refused until column mapping is on;
- every widening `widens()` claims is one Databricks accepts;
- the history and lock SQL — `MERGE`, `INTERVAL`, `current_user()` — is valid;
- **dogfooding**: `tests/messy_schema.py` builds a schema the way a real team ends up
  with one — years of `ALTER`s, masks, a Python UDF, legacy partitioning, awkward names —
  and `test_live_dogfood.py` imports it, requires a plan of nothing but ownership
  claims, adopts it, and changes it. When you meet a real-world table deltaplan
  misreads, add its shape to the messy schema.

Every Databricks behaviour deltaplan relies on should have a test here and a link to the
documentation in its docstring. Where a behaviour is assumed but unverified, the code
says `TODO(verify)` rather than pretending.

### Running the live suite

Nothing in deltaplan has been verified against a real workspace until this has run. Every
`TODO(verify)` in the source names an assumption one of these tests settles.

You need:

- a **catalog you can write to** — every test creates a schema called
  `deltaplan_it_<random>` in it and drops it, with everything inside, when it finishes;
- a **SQL warehouse** — the tests run a few dozen small statements; a 2X-Small
  serverless warehouse is plenty;
- a principal allowed to `CREATE SCHEMA` in that catalog and `CREATE FUNCTION` in its
  schemas (the mask test creates a masking function);
- optionally a principal to grant to — `account users` by default.

```sh
databricks auth login --host https://<workspace> --profile deltaplan-test

DATABRICKS_CONFIG_PROFILE=deltaplan-test \
DATABRICKS_WAREHOUSE_ID=<warehouse id> \
DELTAPLAN_TEST_CATALOG=<scratch catalog> \
DELTAPLAN_TEST_PRINCIPAL="account users" \
  uv run pytest -m integration -v
```

A failure here is the point of the suite: an assumption about Databricks was wrong. Fix
the code, keep the test, and remove the `TODO(verify)` it settled — and if the fake
warehouse agreed with the wrong assumption, fix the fake too, so the offline suite stops
agreeing with it.

### Asking one question of the workspace

A single live question doesn't need the whole forty-minute suite. Push a branch with the
test on it and run the suite against it, filtered:

```sh
gh workflow run integration.yml --ref my-branch -f k=the_test_name
```

That's the honest way to settle a Databricks behaviour before building on it: probe it,
read the answer, then write the code and the test that keeps it.

### When the metastore says it is full

`QUOTA_EXCEEDED.UC_RESOURCE_QUOTA_EXCEEDED` — *"Cannot create 1 Table(s) ... (estimated
count: 523, limit: 500)"* — usually isn't. Unity Catalog
[counts tables as they are created](https://docs.databricks.com/aws/en/data-governance/unity-catalog/resource-quotas)
and catches up with deletions later, so a day of live runs leaves the count far above
what is really there.

The suite handles it: before anything runs it drops `deltaplan_it_*` schemas older than
half an hour (what a cancelled CI run leaves behind), then reads the quota, which is what
asks Unity Catalog to recount. If the count is still at the limit it skips the suite
rather than failing every test in it — the recount lands within about half an hour, and
the next run goes through.

## The docs' pictures

Every terminal on the docs site is deltaplan's own output. `tests/screens.py` writes a
small project for each scene, applies its "before" specs through the CLI into the fake
warehouse, runs the command the page shows, and records what the CLI printed as an SVG.
The spec files a page quotes are written alongside, so the YAML next to a plan is the
YAML that made it.

`test_screens.py` fails when a picture no longer matches what the CLI prints. After
changing anything a user sees, remake them and look at the diff:

```sh
mise run screens
```

A new feature earns a scene in the [feature gallery](features.md): add it to
`tests/screens.py` and a section to `docs/features.md`.

## Where a new test goes

| You changed | Test it in |
|---|---|
| The type parser, loader, differ, planner | `tests/unit/`, with a snapshot if it shapes a plan |
| The SQL a step generates | `tests/unit/test_convergence.py` — teach the fake the statement |
| The executor, history, locking | `tests/unit/test_executor.py` with `MemoryHistory` |
| An assumption about what Databricks does | `tests/integration/`, with the docs link |
| Anything a user sees in the terminal | a scene in `tests/screens.py`, then `mise run screens` |
