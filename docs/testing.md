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
- the history and lock SQL — `MERGE`, `INTERVAL`, `current_user()` — is valid.

Every Databricks behaviour deltaplan relies on should have a test here and a link to the
documentation in its docstring. Where a behaviour is assumed but unverified, the code
says `TODO(verify)` rather than pretending.

## Where a new test goes

| You changed | Test it in |
|---|---|
| The type parser, loader, differ, planner | `tests/unit/`, with a snapshot if it shapes a plan |
| The SQL a step generates | `tests/unit/test_convergence.py` — teach the fake the statement |
| The executor, history, locking | `tests/unit/test_executor.py` with `MemoryHistory` |
| An assumption about what Databricks does | `tests/integration/`, with the docs link |
