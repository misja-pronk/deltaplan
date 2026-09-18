# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project scaffold: uv + hatchling packaging (src layout, Apache-2.0), mise tasks,
  ruff + ty configuration, pytest with a `unit` / `integration` split, and CI for
  lint, types, tests, docs and the built wheel.
- `docs/DESIGN.md` as the source of truth, plus a mkdocs-material site published to
  GitHub Pages.
- A `deltaplan version` command, so the packaging is testable end to end.
