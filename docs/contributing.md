# Contributing

deltaplan uses an all-[Astral](https://astral.sh) toolchain, version-managed by
[mise](https://mise.jdx.dev). Reach for exactly these four tools.

| Tool | Role | Provided by |
|------|------|-------------|
| **mise** | Provisions Python 3.14 + uv | `mise install` |
| **uv** | Env, deps, and command runner (`.venv`) | mise |
| **ruff** | Lint **and** format | `uv run ruff` |
| **ty** | Type checking | `uv run ty` |

!!! warning "Do not use other tools"
    Never use system `pip` / `python` / `virtualenv`, or `poetry`, `pipenv`, `conda`,
    `black`, `flake8`, `isort`, or `mypy`. uv replaces pip/virtualenv; ruff replaces
    black/flake8/isort; ty replaces mypy.

## Commands

```sh
mise install            # one-time: install Python + uv per mise.toml
uv sync                 # create/refresh .venv from pyproject + uv.lock

uv run deltaplan        # run the CLI
uv run pytest tests/unit          # fast tests, no workspace needed
uv run pytest -m integration      # needs DATABRICKS_HOST / token / warehouse id
uv run ruff check .     # lint
uv run ruff format .    # format
uv run ty check         # type check
```

`mise run check` runs the whole gate in one go; `mise tasks` lists the rest.

## Before committing

All of these must pass:

```sh
uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest tests/unit
```

## The rules that matter

The [design](DESIGN.md) is the source of truth; if the code disagrees with it, say so
rather than picking one silently. Beyond that:

- **`differ.py` and `planner.py` are pure** — no I/O, no SDK imports, no clock, no
  environment. Everything outside the loader, introspector and executor must be
  unit-testable without a workspace.
- **The domain model is frozen, slotted stdlib dataclasses holding tuples**, so it stays
  hashable. Pydantic or msgspec live in the loader and nowhere else.
- **All SQL goes through `quote_ident()`.** No identifier is ever concatenated raw.
- **Nothing unmodelled is diffed away.** Unknown features on a live table are reported
  as unmanaged.
- **No destructive step outside the `destructive` risk class.**
- **Every Databricks behaviour assumption gets a test** and a documentation link in the
  test docstring. If you're unsure, add a `TODO(verify)` — don't guess.

## Tests

- `tests/unit/` — differ and planner against golden plan snapshots (syrupy). Fast,
  offline, and the bar for every PR.
- `tests/integration/` — marked `@pytest.mark.integration`, skipped without credentials,
  and run nightly against a real workspace in an ephemeral schema.
- Every discovered Databricks limitation becomes a test.

## Commits & PRs

Small, PR-sized commits with [conventional commit](https://www.conventionalcommits.org/)
messages. Describe the *why*. New behaviour comes with a test; changed plans come with
refreshed snapshots.

Full details, including the release process, are in
[CONTRIBUTING.md](https://github.com/misja-pronk/deltaplan/blob/main/CONTRIBUTING.md).

## Previewing these docs

```sh
uv run --group docs mkdocs serve
```
