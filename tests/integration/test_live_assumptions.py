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

**Recording what the workspace answers.** With `DELTAPLAN_RECORD` set to a
directory, every probe that holds also writes a transcript there: the statements
it sent and the answers that came back. `tests/unit/test_transcripts.py` then
replays them offline, with no credentials, so an assumption keeps being checked
between live runs.

    DELTAPLAN_RECORD=tests/transcripts \\
      uv run pytest -m integration tests/integration/test_live_assumptions.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deltaplan.introspect import Introspector, WarehouseRunner
from deltaplan.probes import PROBES, Bench, Probe, run
from transcript import Recorder, masking

pytestmark = pytest.mark.integration

#: A principal to grant to. Every account has `account users`; a workspace that
#: doesn't can name another.
PRINCIPAL = os.environ.get("DELTAPLAN_TEST_PRINCIPAL", "account users")

#: Where to write transcripts, if anywhere.
RECORD = os.environ.get("DELTAPLAN_RECORD")


@pytest.fixture(scope="session")
def runtime(runner: WarehouseRunner) -> str | None:
    """What the warehouse says it is, for the transcripts to carry.

    Asked once, off the record, and only when something is being recorded: a
    transcript without a runtime version says less than one with it, and a
    workspace that won't answer shouldn't fail a run over metadata.
    """
    if not RECORD:
        return None
    try:
        [row] = runner.query("SELECT current_version().dbsql_version AS version")
    except Exception as error:  # noqa: BLE001 - metadata, not an assertion
        print(f"could not read the warehouse version: {error}")
        return None
    return row["version"]


@pytest.mark.parametrize("probe", PROBES, ids=lambda probe: probe.name)
def test_the_assumption_holds(
    probe: Probe,
    runner: WarehouseRunner,
    introspector: Introspector,
    schema: str,
    runtime: str | None,
    request: pytest.FixtureRequest,
) -> None:
    recorder = (
        Recorder(runner, probe.name, mask=masking(schema, principal=PRINCIPAL))
        if RECORD
        else None
    )
    used = recorder or runner

    def recoverable() -> str:
        """The schema that keeps what it drops — made only when a probe asks.

        It holds the metastore's table quota for its recovery period, so only the
        one probe about UNDROP gets one. Its name is new on every run too, so a
        recording has to mask it as well: the recorder is told about it here,
        which is before any statement names it.
        """
        kept = str(request.getfixturevalue("recoverable_schema"))
        if recorder is not None:
            recorder.mask = masking(schema, kept, principal=PRINCIPAL)
        return kept

    bench = Bench(
        runner=used,
        # While recording, one query at a time: parallel reads interleave
        # differently on every run, and a transcript should be reviewable.
        introspector=Introspector(used, parallel=1) if recorder else introspector,
        schema=schema,
        principal=PRINCIPAL,
        recoverable=recoverable,
    )
    [result] = list(run(bench, [probe]))
    if recorder is not None and result.held:
        # Only a probe that held is evidence about Databricks; one that didn't is
        # something to fix in the code, and a transcript of it would assert the
        # mistake.
        written = recorder.transcript(runtime=runtime)
        path = Path(RECORD or ".") / written.filename
        written.save(path)
        print(f"recorded {len(written.exchanges)} statements to {path}")
    assert result.held, (
        f"{result.outcome}: {result.detail}\n\n{probe.matters}\n{probe.docs}"
    )
