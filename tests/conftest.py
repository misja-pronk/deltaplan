"""Shared test plumbing.

Golden plans live in `tests/snapshots/`, not next to each test module, so the
differ's and the planner's snapshots sit together and are easy to review in a
diff. Refresh them with `uv run pytest --snapshot-update`.
"""

from pathlib import Path

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.amber import AmberSnapshotExtension


class GoldenSnapshots(AmberSnapshotExtension):
    # An absolute path wins over syrupy's per-module `__snapshots__` default,
    # because it joins this onto the test's directory.
    snapshot_dirname = Path(__file__).parent / "snapshots"


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    return snapshot.use_extension(GoldenSnapshots)
