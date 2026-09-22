"""`import deltaplan` is the promise; everything else is how it is kept.

A host program embeds deltaplan through this surface, so these tests hold it
still: every exported name exists and says what it is, every deliberate error
descends from one root, and importing the package costs nothing it doesn't
have to.
"""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys
from typing import get_origin

import pytest

import deltaplan


def test_every_exported_name_is_there() -> None:
    missing = [name for name in deltaplan.__all__ if not hasattr(deltaplan, name)]
    assert missing == [], "exported but absent"


def test_the_list_is_sorted_so_a_diff_to_it_reads() -> None:
    assert deltaplan.__all__ == sorted(deltaplan.__all__, key=str.lower)


@pytest.mark.parametrize("name", [n for n in deltaplan.__all__ if not n.startswith("__")])
def test_every_exported_name_says_what_it_is(name: str) -> None:
    """A host reading `help(deltaplan.X)` should not meet silence."""
    thing = getattr(deltaplan, name)
    if get_origin(thing) is not None or not (isinstance(thing, type) or callable(thing)):
        return  # a constant or a type alias; the module docstring covers those
    assert (thing.__doc__ or "").strip(), f"{name} has no docstring"


ERRORS = [
    "BundleError",
    "DestructiveRefused",
    "ExecutionError",
    "IntrospectionError",
    "PlanFileError",
    "PlanningError",
    "SpecError",
    "StalePlan",
]


@pytest.mark.parametrize("name", ERRORS)
def test_one_except_catches_everything_deltaplan_refuses(name: str) -> None:
    assert issubclass(getattr(deltaplan, name), deltaplan.DeltaplanError)


def test_the_two_a_host_reacts_to_are_execution_errors() -> None:
    """A stale plan is worth planning again; a refused drop is worth asking."""
    for error in (deltaplan.StalePlan, deltaplan.DestructiveRefused):
        assert issubclass(error, deltaplan.ExecutionError)


def test_importing_deltaplan_does_not_import_the_cli() -> None:
    """A library import shouldn't pay for argument parsing and a console."""
    code = "import deltaplan, sys; print('deltaplan.cli' in sys.modules)"
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


def test_the_version_is_the_installed_one() -> None:
    assert deltaplan.__version__ == importlib.metadata.version("deltaplan")


def test_the_library_page_only_names_things_that_exist() -> None:
    """`docs/sdk.md` is the contract as a reader meets it.

    Every `deltaplan.X` it mentions has to be exported, or the page promises
    something the package doesn't have.
    """
    import re
    from pathlib import Path

    page = Path(__file__).resolve().parents[2] / "docs" / "sdk.md"
    # `deltaplan.yml` is the project file, not an attribute.
    text = page.read_text().replace("deltaplan.yml", "the project file")
    named = set(re.findall(r"\bdeltaplan\.([A-Za-z_][A-Za-z0-9_]*)", text))
    missing = sorted(name for name in named if name not in deltaplan.__all__)
    assert missing == [], f"docs/sdk.md names what deltaplan doesn't export: {missing}"
