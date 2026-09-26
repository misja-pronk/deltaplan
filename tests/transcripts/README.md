# Transcripts

What the workspace actually answered, kept — one file per assumption
(`deltaplan.probes.PROBES`), each carrying the date it was recorded and the
runtime that answered.

`tests/unit/test_transcripts.py` replays every file here through the probe it was
recorded for, offline and with no credentials. So an assumption keeps being
checked between live runs, against answers Databricks really gave rather than
against deltaplan's reading of the manual.

## Recording them

One live run, with `DELTAPLAN_RECORD` pointing here:

```sh
DATABRICKS_CONFIG_PROFILE=deltaplan-test \
DATABRICKS_WAREHOUSE_ID=<warehouse id> \
DELTAPLAN_TEST_CATALOG=<scratch catalog> \
DELTAPLAN_RECORD=tests/transcripts \
  uv run pytest -m integration tests/integration/test_live_assumptions.py
```

Only a probe that **held** is written: a probe that didn't is something to fix in
the code, and a transcript of it would assert the mistake.

Re-record after a runtime upgrade, or whenever a replay starts failing. The diff
of a transcript is the diff of what Databricks answers — worth reading.

## What is in them

The statements deltaplan sent, in order, and the rows or the error that came
back. What is masked is only what is new on every run: the scratch schema
(`<schema>`, and its parts `<catalog>` and `<bare>`, because statements name them
separately), the second schema the `UNDROP` probe asks for (`<other>`), and the
principal a grant names (`<principal>`). Nothing else is touched — the types as
the catalog spells them, the error classes, the privilege names are all as they
arrived.

A transcript says what was true **when it was recorded**. That is one thing more
than `tests/fake_warehouse.py` can say, and one less than the live suite.
