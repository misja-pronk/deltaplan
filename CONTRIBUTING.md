# Contributing to deltaplan

Thanks for your interest! Issues and pull requests are very welcome.

`deltaplan` is pre-alpha: the read-only milestone (`validate`, `import`, `plan`) is
still being built. [`docs/DESIGN.md`](docs/DESIGN.md) is the source of truth — if the
code and the design disagree, that is a bug in one of them, so please say which.

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

Releases are **version-driven**: the `version` in `pyproject.toml` is the single
source of truth, and merging a bump to `main` ships it. No manual tagging.

1. On a branch, bump the version:

   ```sh
   uv version --bump patch   # or: minor / major — edits pyproject.toml + uv.lock
   ```

2. In `CHANGELOG.md`, rename the `## [Unreleased]` heading to `## [X.Y.Z]` (the
   new version) and start a fresh, empty `## [Unreleased]` above it. Those notes
   become the GitHub release body.
3. Open a PR. When it merges to `main`, the [`release`](.github/workflows/release.yml)
   workflow builds the wheel + sdist, publishes to **PyPI** (via Trusted Publishing —
   no API token), and creates the **`vX.Y.Z`** tag and GitHub release.

A merge that doesn't change the version is a no-op, and a version that's already
tagged or already on PyPI is skipped — so the workflow is safe to re-run.

> **One-time setup.** Releasing is deliberately dormant until deltaplan is public.
> Two things switch it on: a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/)
> for repository `misja-pronk/deltaplan`, workflow `release.yml`, environment `pypi`
> (at <https://pypi.org/manage/account/publishing/>), and the repository variable
> `RELEASE_ENABLED=true`.

## Previewing the docs

```sh
uv run --group docs mkdocs serve
```
