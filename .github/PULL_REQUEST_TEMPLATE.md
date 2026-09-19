<!-- Thanks for contributing to deltaplan! -->

## What & why

<!-- What does this change for someone using deltaplan, and why? Link any related issue (#123). -->

## Checklist

- [ ] `mise run check` passes (lint, format, types, unit tests)
- [ ] New behaviour has a test; changed plans have refreshed snapshots
- [ ] Anything a user sees in the terminal changed: `mise run screens`, and the docs say so
- [ ] `differ.py` / `planner.py` stayed pure — no I/O, SDK, clock or environment
- [ ] Any new SQL goes through `quote_ident()`; no destructive step outside the
      `destructive` risk class
- [ ] Any assumption about Databricks behaviour is backed by a live test + docs link, or
      marked `TODO(verify)`
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
