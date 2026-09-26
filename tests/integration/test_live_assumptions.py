"""What Databricks does that deltaplan's plans are built around.

The assumptions no longer live here: they are `deltaplan.probes.PROBES`, which
`deltaplan verify` runs in anyone's workspace, and which this runs in ours. One
list, so an assumption is written down once and can't drift between the tool and
the suite. Each probe names the Databricks page it rests on, and
`probe.matters` says what it costs where it doesn't hold — which is what this
prints when one fails.

They were first settled on 2026-09-19, and several turned out other than
assumed: a nested field's NOT NULL is an ordinary ALTER, a map key widens in
place, a shallow clone copies the ownership marker, and REPLACE keeps a table's
tags and grants while a replaced view or function loses them.

Each probe gets a schema of its own here — the fixture drops it, and keeps
nothing it drops — so one probe can never leave anything the next one reads.
"""

from __future__ import annotations

import os

import pytest

from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.probes import PROBES, Bench, Probe, run

pytestmark = pytest.mark.integration

#: A principal to grant to. Every account has `account users`; a workspace that
#: doesn't can name another.
PRINCIPAL = os.environ.get("DELTAPLAN_TEST_PRINCIPAL", "account users")


@pytest.mark.parametrize("probe", PROBES, ids=lambda probe: probe.name)
def test_the_assumption_holds(
    probe: Probe,
    runner: WarehouseRunner,
    introspector: Introspector,
    schema: str,
    request: pytest.FixtureRequest,
) -> None:
    bench = Bench(
        runner=runner,
        introspector=introspector,
        schema=schema,
        principal=PRINCIPAL,
        # Asked for by the one probe about UNDROP, and made only then: a schema
        # that keeps what it drops holds the metastore's table quota for a week.
        recoverable=lambda: str(request.getfixturevalue("recoverable_schema")),
    )
    [result] = list(run(bench, [probe]))
    assert result.held, (
        f"{result.outcome}: {result.detail}\n\n{probe.matters}\n{probe.docs}"
    )
