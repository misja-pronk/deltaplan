<!-- Thanks for contributing to deltaplan! -->

## What & why

<!-- What does this change, and why? Link any related issue (#123). -->

## Checklist

- [ ] `uv run pytest tests/unit` passes
- [ ] `uv run ruff check .` and `uv run ruff format .` are clean
- [ ] `uv run ty check` passes
- [ ] New behaviour has a test; changed plans have refreshed snapshots
- [ ] `differ.py` / `planner.py` stayed pure — no I/O, SDK, clock or environment
- [ ] Any new SQL goes through `quote_ident()`; no destructive step outside the
      `destructive` risk class
- [ ] Any assumption about Databricks behaviour is backed by a test + docs link, or
      marked `TODO(verify)`
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
