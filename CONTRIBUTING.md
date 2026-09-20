# Contributing to deltaplan

Thanks for your interest! Issues and pull requests are very welcome.

`deltaplan` is alpha: every milestone in the design is built and checked against a live
workspace, and the spec format may still change. [`docs/DESIGN.md`](docs/DESIGN.md) is the
source of truth — if the code and the design disagree, that is a bug in one of them, so
please say which.

## How changes land

`main` is protected: every change is a pull request, merged once its checks pass.

- **`ci`** — lint, types, the unit tests on Python 3.11–3.14 and macOS, the docs in
  strict mode, the wheel, and the workflows. These are required.
- **`integration`** — the live suite, against a real workspace, on every pull request
  and nightly. It takes about 40 minutes and isn't required, so read it before merging
  anything that changes what deltaplan sends to Databricks.

Run `mise run check` before pushing; it is the `ci` gate minus the matrix. If you
changed anything a user sees in the terminal, `mise run screens` remakes the docs'
pictures, and the check fails until you do.

## Toolchain

deltaplan uses [`mise`](https://mise.jdx.dev) to pin tools and the all-Astral
stack — [`uv`](https://docs.astral.sh/uv/) (env / deps / run),
[`ruff`](https://docs.astral.sh/ruff/) (lint + format), and
[`ty`](https://docs.astral.sh/ty/) (type check).

```sh
mise install   # installs the pinned Python + uv (optional but recommended)
uv sync        # creates .venv and installs deps + dev tools
```

## Day-to-day

```sh
uv run deltaplan                  # run the CLI
uv run pytest tests/unit          # fast tests, no workspace needed
uv run ruff check . && uv run ruff format .   # lint + format
uv run ty check                   # type check
```

`mise run check` runs the whole gate (lint, format check, types, unit tests) in one
go; `mise tasks` lists the rest.

All of these run in CI on every push/PR — please make sure they're green before
opening a PR. New behaviour should come with a test.

## Architecture

```
spec (YAML) ─┐
             ├─> differ ─> changes ─> planner ─> plan (JSON) ─> renderer
live (UC) ───┘                                        │
                                                      └─> executor ─> history
```

The dependency rule is simple: **the middle of the pipeline does no I/O.**

- **`model/`** — frozen, slotted stdlib dataclasses holding tuples, so everything is
  hashable. No Pydantic, no SDK.
- **`loader.py` / `introspect.py`** — the only places that read YAML or talk to a
  workspace. Validation happens at this edge and nowhere else.
- **`differ.py` / `planner.py`** — pure functions: no I/O, no SDK imports, no clock,
  no environment. They must be unit-testable without a workspace.
- **`render/`** — rich / markdown / json views of the same `Plan` object, sharing
  their wording through `render/labels.py`.
- **`planning.py`** — specs and a warehouse in, a plan out: the pipeline `plan`,
  `drift` and the GitHub Action share.
- **`executor.py`** / **`history.py`** — the only places that run SQL that changes
  anything.
- **`action.yml`** + **`action/`** — the GitHub Action, at the repo root so
  `uses: misja-pronk/deltaplan@v0` finds it.

House rules worth repeating:

- Never build SQL by concatenating unquoted identifiers — there is one
  `quote_ident()` helper, and it is used everywhere.
- Anything not modelled on a live table is reported as **unmanaged** and never
  diffed away. Only tables deltaplan created can be drop candidates.
- No destructive step without the `destructive` risk class.
- Every Databricks behaviour assumption gets a test and a link to the docs in the
  test docstring. If a behaviour is unclear, add a `TODO(verify)` and say so — don't
  guess.

## Tests

Three layers, each honest about what it proves:

1. **Unit tests** (`tests/unit/`) — the pure middle of the pipeline, with golden plans
   in `tests/snapshots/`.
2. **Convergence against a fake warehouse** (`tests/fake_warehouse.py`) — an in-memory
   catalog that interprets the statements the planner generates, so `plan → apply →
   re-plan is empty` can be asserted offline, for every kind of change. It proves our
   SQL means what our changes mean; it cannot prove Databricks accepts it.
3. **Integration tests** (`tests/integration/`) — marked `@pytest.mark.integration`,
   skipped without credentials, run nightly against a real workspace in an ephemeral
   schema. The only source of truth about Databricks.

Every discovered Databricks limitation becomes a test in layer 3, with a link to the
documentation in its docstring. There is a fuller description in
[docs/testing.md](docs/testing.md).

## Commits & PRs

- Small, PR-sized commits with [conventional commit](https://www.conventionalcommits.org/)
  messages (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- Describe the *why*, not just the *what*.
- By contributing you agree your work is licensed under the project's
  [Apache-2.0 License](LICENSE).

## Releasing

A release is a version. `pyproject.toml` says what it is, and merging the bump is what
ships it.

In a pull request, bump the version and move the changelog notes under it:

```sh
uv version 0.1.0a7          # or: uv version --bump patch / minor / major
```

In `CHANGELOG.md`, rename `## [Unreleased]` to `## [0.1.0a7] - <date>` and start a fresh,
empty `## [Unreleased]` above it. Merge it once `ci` passes — that is the whole release.

The [`release`](.github/workflows/release.yml) workflow watches `pyproject.toml` on
`main`. When it finds a version that isn't tagged and isn't on PyPI, it runs the gate on
that commit, publishes to **PyPI** (Trusted Publishing — no API token), and creates the
GitHub release with the changelog's notes, which is what makes the `v0.1.0a7` tag. A
version with `a`, `b` or `rc` is marked a pre-release. The Action's `v0` tag moves to
every 0.x release, so `uses: misja-pronk/deltaplan@v0` follows the newest.

Nothing else triggers it, so a `pyproject.toml` change that isn't a bump — a dependency,
a ruff rule — costs one run that says "nothing to release" and stops. A bump without
changelog notes under that version fails instead of publishing something unexplained.

If a release fails halfway, fix what broke and run the workflow again from the Actions
tab: whatever already landed (the PyPI file, the tag) makes it stop rather than repeat
itself.

## Previewing the docs

```sh
uv run --group docs mkdocs serve
```
