"""The docs' terminal pictures are the CLI's real output, and stay that way.

`tests/screens.py` makes them by running the CLI; this runs it again and fails
when anything a user sees has changed without the pictures being remade:

    uv run python tests/screens.py docs/assets/screens
"""

from pathlib import Path

import screens

COMMITTED = Path(__file__).parents[2] / "docs" / "assets" / "screens"


def test_the_docs_pictures_match_what_the_cli_prints(tmp_path: Path) -> None:
    made = {path.name: path for path in screens.make(tmp_path)}
    committed = {path.name: path for path in COMMITTED.iterdir()}
    assert sorted(made) == sorted(committed), "remake the pictures (see the docstring)"
    stale = [
        name
        for name, path in made.items()
        if path.read_bytes() != committed[name].read_bytes()
    ]
    assert stale == [], f"out of date, remake them (see the docstring): {stale}"
