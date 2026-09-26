"""What the workspace actually answered, kept.

`fake_warehouse.py` proves that deltaplan's SQL matches deltaplan's *reading* of
the manual. Where that reading is wrong the fake is wrong in the same direction,
and the offline suite agrees with the mistake. The live suite is the only answer
to that — and it costs a workspace, forty minutes, and some weeks it can't run at
all.

A transcript is one live run written down: every statement deltaplan sent, and the
rows or the error that came back. Replayed, the same assertions run offline
against answers Databricks really gave. That is one thing more than the fake can
say, and one less than the live suite: a transcript says what was true **when it
was recorded**, on that runtime, on that day.

Recording one, against a workspace (see `docs/testing.md`):

    DELTAPLAN_RECORD=tests/transcripts \\
      uv run pytest -m integration tests/integration/test_live_assumptions.py

Replaying: `tests/unit/test_transcripts.py` runs every transcript in
`tests/transcripts/`, so a recording is checked from the moment it lands, with no
credentials.

Two rules keep a transcript honest:

- **A statement the transcript hasn't got fails**, loudly, with the statement in
  the message — the same discipline as `FakeSqlError`. A recording that no longer
  covers what deltaplan sends is a failing test, not a silent pass.
- **Only what is incidental is masked**: the schemas a run makes, whose names are
  new every time, and the principal a grant names (`masking()`). Everything
  else — the rows, the error classes, the types as the catalog spells them — is
  kept as it came.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from deltaplan.introspect import Row, SqlRunner

#: What a recording puts in place of the names that are new on every run: the
#: scratch schema — whole, and its two parts, because statements name them
#: separately — the second schema the UNDROP probe asks for, and the principal a
#: grant names. A replay puts its own back.
SCHEMA = "<schema>"
CATALOG = "<catalog>"
BARE = "<bare>"
OTHER = "<other>"
OTHER_BARE = "<other-bare>"
PRINCIPAL = "<principal>"


def masking(*schemas: str, principal: str = "") -> dict[str, str]:
    """The names that are new on every run, and what stands in for each.

    Every schema is masked whole *and* in parts, because a statement names them
    separately as often as together — a quoted three-part name, but also
    `WHERE schema_name = 'schema'`. The first schema is the run's own; a second
    is the one the `UNDROP` probe asks for, which keeps what it drops.

    A recording and the replay of it must build this the same way, from their own
    names, or the two never match.
    """
    named: dict[str, str] = {}
    for index, full in enumerate(name for name in schemas if name):
        catalog, _, bare = full.partition(".")
        named.setdefault(full, SCHEMA if index == 0 else OTHER)
        named.setdefault(bare, BARE if index == 0 else OTHER_BARE)
        named.setdefault(catalog, CATALOG)
    if principal:
        named.setdefault(principal, PRINCIPAL)
    return named


class TranscriptMiss(Exception):
    """The transcript has no answer for this statement.

    Loud on purpose: a recording that doesn't cover what deltaplan now sends has
    stopped being evidence about it.
    """


@dataclass(frozen=True, slots=True)
class Exchange:
    """One statement and what came back: rows, or the error."""

    sql: str
    rows: tuple[Row, ...] | None = None
    error: str | None = None

    def as_json(self) -> dict[str, object]:
        body: dict[str, object] = {"sql": self.sql}
        if self.error is not None:
            body["error"] = self.error
        else:
            body["rows"] = [dict(row) for row in self.rows or ()]
        return body

    @classmethod
    def from_json(cls, body: dict[str, object]) -> Exchange:
        rows = body.get("rows")
        return cls(
            sql=str(body["sql"]),
            rows=tuple(rows) if isinstance(rows, list) else None,  # type: ignore[arg-type]
            error=str(body["error"]) if body.get("error") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class Transcript:
    """One live run, as a file a person can read and review the diff of."""

    about: str
    recorded: str
    exchanges: tuple[Exchange, ...] = ()
    #: What the workspace said it was, when it was asked.
    runtime: str | None = None
    warehouse: str | None = None
    #: The principal the run granted to, so a replay can name the same one.
    principal: str | None = None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "about": self.about,
            "recorded": self.recorded,
            "runtime": self.runtime,
            "warehouse": self.warehouse,
            "principal": self.principal,
            "statements": [exchange.as_json() for exchange in self.exchanges],
        }
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Transcript:
        body = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            about=body["about"],
            recorded=body["recorded"],
            exchanges=tuple(
                Exchange.from_json(one) for one in body.get("statements", ())
            ),
            runtime=body.get("runtime"),
            warehouse=body.get("warehouse"),
            principal=body.get("principal"),
        )

    @property
    def filename(self) -> str:
        """`a replace keeps tags` -> `a-replace-keeps-tags.json`."""
        keep = [c if c.isalnum() else "-" for c in self.about.lower()]
        slug = "".join(keep)
        while "--" in slug:
            slug = slug.replace("--", "-")
        return f"{slug.strip('-')}.json"


def normalise(statement: str, mask: Mapping[str, str] | None = None) -> str:
    """One statement as a transcript keys it.

    Whitespace collapsed — deltaplan writes some statements over several lines,
    and a recording shouldn't depend on where they wrap — and every name in
    `mask` (from `masking()`) replaced by what stands in for it.

    Whole words only, so a catalog called `main` doesn't turn `maintenance` into
    a placeholder; longest name first, so `main.place` wins over either of its
    parts; and one pass, so a placeholder is never masked again — a schema called
    `schema` would otherwise turn `<schema>` into `<<bare>>`.
    """
    text = " ".join(statement.split())
    named = dict(mask or {})
    if not named:
        return text
    order = sorted(named, key=len, reverse=True)
    pattern = re.compile("|".join(rf"\b{re.escape(name)}\b" for name in order))
    return pattern.sub(lambda found: named[found.group(0)], text)


@dataclass
class Recorder:
    """A `SqlRunner` that runs statements and writes down the answers.

    Wraps the real runner rather than replacing it: a recording is a live run
    that also left a record, so anything it says is something the workspace said.
    """

    inner: SqlRunner
    about: str
    #: What `masking()` built from this run's own names. A recorder may be handed
    #: a name mid-run — the `UNDROP` probe's second schema is only made if that
    #: probe runs — and statements are masked as they go, so it takes effect.
    mask: dict[str, str] = field(default_factory=dict)
    exchanges: list[Exchange] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def client(self) -> object:
        """Whatever the wrapped runner talks to — a recorder adds nothing."""
        return getattr(self.inner, "client", None)

    def query(self, statement: str) -> tuple[Row, ...]:
        keyed = normalise(statement, self.mask)
        try:
            rows = self.inner.query(statement)
        except Exception as error:
            with self._lock:
                self.exchanges.append(Exchange(keyed, error=str(error)))
            raise
        with self._lock:
            self.exchanges.append(Exchange(keyed, rows=rows))
        return rows

    def transcript(self, *, runtime: str | None = None) -> Transcript:
        return Transcript(
            about=self.about,
            recorded=datetime.now(UTC).date().isoformat(),
            exchanges=tuple(self.exchanges),
            runtime=runtime,
            warehouse=self._warehouse(),
            principal=next(
                (name for name, stands in self.mask.items() if stands == PRINCIPAL),
                None,
            ),
        )

    def _warehouse(self) -> str | None:
        return getattr(self.inner, "warehouse_id", None)


@dataclass
class Replay:
    """A `SqlRunner` that answers from a transcript, and refuses to guess.

    Answers come back in the order they were recorded for each statement, so a
    query asked before and after a change gives the two answers it really gave.
    """

    transcript: Transcript
    #: What `masking()` builds from the names *this* replay uses, where the
    #: recording had its own. Built the same way, or the two never match.
    mask: dict[str, str] = field(default_factory=dict)
    #: Statements that have been answered, in order — what a test can assert on.
    asked: list[str] = field(default_factory=list)
    _left: dict[str, list[Exchange]] = field(default_factory=dict)
    client: None = None

    def __post_init__(self) -> None:
        for exchange in self.transcript.exchanges:
            self._left.setdefault(exchange.sql, []).append(exchange)

    def query(self, statement: str) -> tuple[Row, ...]:
        from deltaplan.introspect import IntrospectionError

        keyed = normalise(statement, self.mask)
        self.asked.append(keyed)
        waiting = self._left.get(keyed)
        if not waiting:
            recorded = "recorded" if self.transcript.exchanges else "empty"
            raise TranscriptMiss(
                f"the transcript {self.transcript.about!r} ({recorded} "
                f"{self.transcript.recorded}) has no answer for:\n  {keyed}\n"
                "Re-record it: see docs/testing.md."
            )
        # The last answer stands for every later ask of the same statement: a
        # transcript is evidence, not a script, and running out of a repeated
        # read should not be what fails a test.
        exchange = waiting.pop(0) if len(waiting) > 1 else waiting[0]
        if exchange.error is not None:
            raise IntrospectionError(exchange.error)
        return tuple(exchange.rows or ())
