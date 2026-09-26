"""Replaying what the workspace answered, with no credentials.

Two halves. The first holds the machinery: what a recording keeps, what a replay
gives back, and that a statement the transcript hasn't got fails loudly rather
than passing quietly. The second replays every transcript in
`tests/transcripts/` through the probe it was recorded for — so a recorded
assumption keeps being checked between live runs, which is the whole point.

A transcript only says what was true when it was recorded. That is one thing more
than the fake warehouse can say, and one less than the live suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deltaplan.introspect import IntrospectionError, Introspector
from deltaplan.probes import PROBES, Bench, run
from fake_warehouse import FakeWarehouse
from transcript import (
    BARE,
    CATALOG,
    PRINCIPAL,
    SCHEMA,
    Exchange,
    Recorder,
    Replay,
    Transcript,
    TranscriptMiss,
    masking,
    normalise,
)

#: Where a live run writes them, and where a replay looks.
TRANSCRIPTS = Path(__file__).parent.parent / "transcripts"

#: The schemas and principal a replay works in. Any names will do — the recording
#: masked its own, and these go in their place. `KEPT_HERE` stands where the
#: recording's recoverable schema was, for the one probe about UNDROP.
SCHEMA_HERE = "main.replayed"
KEPT_HERE = "main.replayed_kept"
PRINCIPAL_HERE = "account users"


Row = dict[str, str | None]


class Answering:
    """A runner with canned answers, to record from."""

    def __init__(self, answers: dict[str, list[Row] | Exception]) -> None:
        self.answers = answers
        self.warehouse_id = "w1"

    def query(self, statement: str) -> tuple[Row, ...]:
        answer = self.answers[" ".join(statement.split())]
        if isinstance(answer, Exception):
            raise answer
        return tuple(answer)


# ---------------------------------------------------------------------------
# the machinery
# ---------------------------------------------------------------------------


def test_a_recording_keeps_the_answers_and_masks_the_scratch_schema() -> None:
    live = Answering(
        {
            "CREATE TABLE `main`.`deltaplan_it_abc123`.`r` (id INT)": [],
            "SELECT tag_name FROM main.deltaplan_it_abc123.r": [{"tag_name": "domain"}],
        }
    )
    recorder = Recorder(
        live, "a replace keeps tags", mask=masking("main.deltaplan_it_abc123")
    )
    recorder.query("CREATE TABLE `main`.`deltaplan_it_abc123`.`r` (id INT)")
    recorder.query("SELECT tag_name FROM main.deltaplan_it_abc123.r")

    written = recorder.transcript(runtime="2026.20")
    assert written.runtime == "2026.20"
    assert written.warehouse == "w1"
    assert [one.sql for one in written.exchanges] == [
        f"CREATE TABLE `{CATALOG}`.`{BARE}`.`r` (id INT)",
        f"SELECT tag_name FROM {SCHEMA}.r",
    ]
    assert written.exchanges[1].rows == ({"tag_name": "domain"},)
    assert written.filename == "a-replace-keeps-tags.json"


def test_a_recording_keeps_the_error_and_lets_it_through() -> None:
    """A refusal is an answer — several probes are about one — and a recorder
    doesn't swallow it."""
    live = Answering({"ALTER TABLE t ADD COLUMN k BIGINT": IntrospectionError("[X] no")})
    recorder = Recorder(live, "generated columns can't be added")
    with pytest.raises(IntrospectionError, match=r"\[X\] no"):
        recorder.query("ALTER TABLE t ADD COLUMN k BIGINT")
    assert recorder.transcript().exchanges[0].error == "[X] no"


def test_a_transcript_is_a_file_a_person_can_read(tmp_path: Path) -> None:
    written = Transcript(
        about="the warehouse runs in ANSI mode",
        recorded="2026-09-27",
        runtime="2026.20",
        principal=PRINCIPAL_HERE,
        exchanges=(Exchange("SET ANSI_MODE", rows=({"value": "true"},)),),
    )
    path = tmp_path / written.filename
    written.save(path)
    assert '"about"' in path.read_text(encoding="utf-8")
    assert Transcript.load(path) == written


def test_a_replay_gives_back_what_was_recorded() -> None:
    written = Transcript(
        about="anything",
        recorded="2026-09-27",
        exchanges=(
            Exchange(f"SELECT id FROM {SCHEMA}.r", rows=({"id": "1"},)),
            Exchange(f"ALTER TABLE {SCHEMA}.r SET TAGS ('a' = 'b')", rows=()),
            Exchange("SELECT CAST('abc' AS INT) AS x", error="[CAST_INVALID_INPUT] no"),
        ),
    )
    replay = Replay(written, mask=masking(SCHEMA_HERE))
    assert replay.query(f"SELECT id\nFROM   {SCHEMA_HERE}.r") == ({"id": "1"},)
    assert replay.query(f"ALTER TABLE {SCHEMA_HERE}.r SET TAGS ('a' = 'b')") == ()
    with pytest.raises(IntrospectionError, match="CAST_INVALID_INPUT"):
        replay.query("SELECT CAST('abc' AS INT) AS x")


def test_a_statement_the_transcript_hasnt_got_fails_loudly() -> None:
    """The same discipline as `FakeSqlError`: a recording that no longer covers
    what deltaplan sends has stopped being evidence about it."""
    replay = Replay(Transcript(about="empty", recorded="2026-09-27"))
    with pytest.raises(TranscriptMiss, match="SELECT 1 AS whatever"):
        replay.query("SELECT 1 AS whatever")


def test_a_statement_asked_twice_gets_both_answers_in_order() -> None:
    """A read before and after a change gives the two answers it really gave —
    and once the recording runs out, the last one stands."""
    written = Transcript(
        about="anything",
        recorded="2026-09-27",
        exchanges=(
            Exchange("SELECT id FROM t", rows=({"id": "1"},)),
            Exchange("SELECT id FROM t", rows=({"id": "2"},)),
        ),
    )
    replay = Replay(written)
    assert [replay.query("SELECT id FROM t") for _ in range(3)] == [
        ({"id": "1"},),
        ({"id": "2"},),
        ({"id": "2"},),
    ]


def test_masking_covers_every_way_a_name_is_written() -> None:
    """Whole and in parts, quoted and bare — and nothing that merely contains
    one of them."""
    mask = masking("main.deltaplan_it_abc123", principal="them")
    assert normalise("SELECT * FROM `main`.`deltaplan_it_abc123`.`t`", mask) == (
        f"SELECT * FROM `{CATALOG}`.`{BARE}`.`t`"
    )
    assert normalise("SELECT * FROM main.deltaplan_it_abc123.t", mask) == (
        f"SELECT * FROM {SCHEMA}.t"
    )
    assert normalise("WHERE schema_name = 'deltaplan_it_abc123'", mask) == (
        f"WHERE schema_name = '{BARE}'"
    )
    assert normalise("GRANT SELECT ON t TO `them`", mask) == (
        f"GRANT SELECT ON t TO `{PRINCIPAL}`"
    )
    assert normalise("COMMENT ON TABLE t IS 'maintenance'", mask) == (
        "COMMENT ON TABLE t IS 'maintenance'"
    ), "a catalog called main is not every word starting with it"


def test_a_second_schema_is_masked_too() -> None:
    """The one probe about `UNDROP` works in a schema that keeps what it drops.
    Its name is new on every run as well, so a recording that kept it would never
    match anything on replay."""
    recording = masking("main.it_abc", "main.it_kept", principal="them")
    replaying = masking("main.here", "main.here_kept", principal="us")
    statement = "UNDROP TABLE `main`.`it_kept`.`u` -- from main.it_abc, granted to `them`"
    theirs = (
        statement.replace("it_kept", "here_kept")
        .replace("it_abc", "here")
        .replace("them", "us")
    )
    assert normalise(statement, recording) == normalise(theirs, replaying)
    assert "<other-bare>" in normalise(statement, recording)


def test_a_recording_can_be_replayed() -> None:
    """The two halves fit: what a recorder writes is what a replay reads, with
    the scratch schema swapped for the one replaying."""
    live = Answering({f"SELECT id FROM {SCHEMA_HERE}.r": [{"id": "1"}]})
    recorder = Recorder(live, "anything", mask=masking(SCHEMA_HERE))
    recorder.query(f"SELECT id FROM {SCHEMA_HERE}.r")
    replay = Replay(recorder.transcript(), mask=masking("other.schema"))
    assert replay.query("SELECT id FROM other.schema.r") == ({"id": "1"},)


def test_a_probe_can_be_recorded_and_replayed_whole() -> None:
    """The whole path, with the fake warehouse standing in for a workspace.

    It proves the plumbing — that what a probe sends is what a recording keeps,
    and that a replay can answer it in a schema of another name — not the
    assumption: a transcript recorded from the fake says only what the fake
    says. Recording against Databricks is `DELTAPLAN_RECORD` on a live run.
    """
    [probe] = [one for one in PROBES if one.name.startswith("CLUSTER BY AUTO")]
    fake = FakeWarehouse()
    fake.schemas.add(SCHEMA_HERE)
    recorder = Recorder(
        fake, probe.name, mask=masking(SCHEMA_HERE, principal=PRINCIPAL_HERE)
    )
    live = Bench(
        runner=recorder,
        introspector=Introspector(recorder, parallel=1),
        schema=SCHEMA_HERE,
        principal=PRINCIPAL_HERE,
    )
    [result] = list(run(live, [probe]))
    assert result.held, result.detail
    written = recorder.transcript()
    assert written.exchanges

    elsewhere = "other.place"
    replay = Replay(written, mask=masking(elsewhere, principal=PRINCIPAL_HERE))
    offline = Bench(
        runner=replay,
        introspector=Introspector(replay, parallel=1),
        schema=elsewhere,
        principal=PRINCIPAL_HERE,
    )
    [again] = list(run(offline, [probe]))
    assert again.held, again.detail
    assert replay.asked, "and it really did answer from the transcript"


# ---------------------------------------------------------------------------
# the recordings themselves
# ---------------------------------------------------------------------------

RECORDED = sorted(TRANSCRIPTS.glob("*.json"))


@pytest.mark.skipif(
    not RECORDED,
    reason="no transcripts recorded yet — see tests/transcripts/README.md",
)
@pytest.mark.parametrize(
    "path", RECORDED or [None], ids=lambda path: path.stem if path else "none"
)
def test_the_probe_still_holds_against_what_the_workspace_answered(path: Path) -> None:
    """Every recording, through the probe it was recorded for.

    The probe runs exactly as the live suite ran it — one probe, so its objects
    are named the same — against the answers Databricks gave. A probe that no
    longer holds here means deltaplan's expectation changed; a `TranscriptMiss`
    means it now sends something the recording doesn't cover. Either way the
    transcript needs recording again, and until then the test says so.
    """
    written = Transcript.load(path)
    matching = [probe for probe in PROBES if probe.name == written.about]
    assert matching, (
        f"{path.name} was recorded for {written.about!r}, which is no longer a "
        "probe. Delete the transcript, or put the probe back."
    )
    principal = written.principal or PRINCIPAL_HERE
    replay = Replay(written, mask=masking(SCHEMA_HERE, KEPT_HERE, principal=principal))
    bench = Bench(
        runner=replay,
        introspector=Introspector(replay, parallel=1),
        schema=SCHEMA_HERE,
        principal=principal,
        # Where the recording's second schema was — the one that keeps what it
        # drops, which only the UNDROP probe asks for.
        recoverable=lambda: KEPT_HERE,
    )
    [result] = list(run(bench, matching))
    assert result.held, (
        f"{written.about} no longer holds against the answers recorded on "
        f"{written.recorded}"
        + (f" (runtime {written.runtime})" if written.runtime else "")
        + f":\n{result.detail}"
    )
