"""Offline tests stay offline.

deltaplan asks the Databricks CLI what a bundle resolves to. A machine with the
CLI installed would answer differently from one without it — and from CI — so
the unit suite hides it, and the tests that want one put a stub on PATH
themselves. The live suite, which has both a CLI and a workspace, doesn't.
"""

import pytest

from helpers import path_without


@pytest.fixture(autouse=True)
def _without_the_databricks_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", path_without("databricks"))
