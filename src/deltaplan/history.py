"""Run history and the apply lock.

There is no state file — Unity Catalog is the state — so what `apply` did lives
in Delta tables too, in a schema you configure (`history_schema` in
`deltaplan.yml`). Three tables:

* `runs`  — one row per `apply`, with its status.
* `steps` — one row per step, with the SQL, the outcome, and the Delta version
  the table was on before anything risky.
* `lock`  — one row per target, so two applies can't fight over the same tables.

The store is a protocol with two implementations: `DeltaHistory`, which is the
real one, and `MemoryHistory`, which lets the executor be tested offline. The
generated SQL is asserted verbatim in the unit tests and exercised for real by
the integration suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias

from deltaplan.introspect import SqlRunner
from deltaplan.sql import quote_ident, quote_literal, quote_qualified

Status: TypeAlias = Literal["running", "succeeded", "failed", "skipped"]

#: Statuses that mean a step needs no further attention on a resume.
DONE: frozenset[str] = frozenset({"succeeded", "skipped"})

DEFAULT_LOCK_MINUTES = 60


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What happened to one step."""

    step_id: int
    table: str
    sql: str | None
    status: Status
    error: str | None = None
    delta_version_before: int | None = None


class HistoryStore(Protocol):
    """Where a run records itself, and where a resume reads from."""

    def ensure(self) -> None:
        """Create the history tables if they aren't there yet."""

    def start_run(
        self, run_id: str, plan_hash: str, target: str, tool_version: str
    ) -> None: ...

    def finish_run(self, run_id: str, status: Status) -> None: ...

    def resumable_run(self, plan_hash: str, target: str) -> str | None:
        """The id of a run of this plan that hasn't succeeded, if there is one.

        Both a failed run and one that died mid-flight are resumable: the point
        of recording every step is that the next `apply` continues rather than
        starting over.
        """

    def finished_steps(self, run_id: str) -> frozenset[int]: ...

    def record_step(self, run_id: str, outcome: StepOutcome) -> None: ...

    def acquire_lock(self, target: str, run_id: str, minutes: int) -> bool: ...

    def renew_lock(self, target: str, run_id: str, minutes: int) -> bool:
        """Push the lock's expiry out again. False if this run no longer holds it."""

    def release_lock(self, target: str, run_id: str) -> None: ...

    def lock_holder(self, target: str) -> str | None: ...

    def force_unlock(self, target: str) -> str | None:
        """Release the lock whoever holds it, and say who that was."""


# ---------------------------------------------------------------------------
# the real one
# ---------------------------------------------------------------------------

RUNS_COLUMNS = (
    ("run_id", "STRING"),
    ("plan_hash", "STRING"),
    ("target", "STRING"),
    ("user", "STRING"),
    ("tool_version", "STRING"),
    ("status", "STRING"),
    ("started_at", "TIMESTAMP"),
    ("ended_at", "TIMESTAMP"),
)

STEPS_COLUMNS = (
    ("run_id", "STRING"),
    ("step_id", "BIGINT"),
    ("table_name", "STRING"),
    ("sql", "STRING"),
    ("status", "STRING"),
    ("started_at", "TIMESTAMP"),
    ("ended_at", "TIMESTAMP"),
    ("error", "STRING"),
    ("delta_version_before", "BIGINT"),
)

LOCK_COLUMNS = (
    ("id", "STRING"),
    ("holder", "STRING"),
    ("acquired_at", "TIMESTAMP"),
    ("expires_at", "TIMESTAMP"),
)


@dataclass(slots=True)
class DeltaHistory:
    """History in Delta tables, reached through a SQL warehouse."""

    runner: SqlRunner
    schema: str

    # -- setup -------------------------------------------------------------
    def ensure(self) -> None:
        self.runner.query(f"CREATE SCHEMA IF NOT EXISTS {quote_qualified(self.schema)}")
        for name, columns in (
            ("runs", RUNS_COLUMNS),
            ("steps", STEPS_COLUMNS),
            ("lock", LOCK_COLUMNS),
        ):
            self.runner.query(self._create_sql(name, columns))

    def _create_sql(self, name: str, columns: tuple[tuple[str, str], ...]) -> str:
        body = ",\n".join(f"  {quote_ident(c)} {t}" for c, t in columns)
        return f"CREATE TABLE IF NOT EXISTS {self._table(name)} (\n{body}\n) USING DELTA"

    def _table(self, name: str) -> str:
        return quote_qualified(f"{self.schema}.{name}")

    # -- runs --------------------------------------------------------------
    def start_run(
        self, run_id: str, plan_hash: str, target: str, tool_version: str
    ) -> None:
        # `current_user()` rather than anything client-side: the warehouse knows
        # who actually ran the statement.
        self.runner.query(
            f"INSERT INTO {self._table('runs')} SELECT "
            f"{quote_literal(run_id)}, {quote_literal(plan_hash)}, "
            f"{quote_literal(target)}, current_user(), "
            f"{quote_literal(tool_version)}, 'running', current_timestamp(), "
            "CAST(NULL AS TIMESTAMP)"
        )

    def finish_run(self, run_id: str, status: Status) -> None:
        self.runner.query(
            f"UPDATE {self._table('runs')} SET status = {quote_literal(status)}, "
            f"ended_at = current_timestamp() WHERE run_id = {quote_literal(run_id)}"
        )

    def resumable_run(self, plan_hash: str, target: str) -> str | None:
        rows = self.runner.query(
            f"SELECT run_id FROM {self._table('runs')} "
            f"WHERE plan_hash = {quote_literal(plan_hash)} "
            f"AND target = {quote_literal(target)} AND status <> 'succeeded' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        return str(rows[0]["run_id"]) if rows and rows[0].get("run_id") else None

    def finished_steps(self, run_id: str) -> frozenset[int]:
        rows = self.runner.query(
            f"SELECT step_id FROM {self._table('steps')} "
            f"WHERE run_id = {quote_literal(run_id)} "
            "AND status IN ('succeeded', 'skipped')"
        )
        return frozenset(int(row["step_id"] or 0) for row in rows)

    def record_step(self, run_id: str, outcome: StepOutcome) -> None:
        version = outcome.delta_version_before
        self.runner.query(
            f"INSERT INTO {self._table('steps')} SELECT "
            f"{quote_literal(run_id)}, {outcome.step_id}, "
            f"{quote_literal(outcome.table)}, {_literal_or_null(outcome.sql)}, "
            f"{quote_literal(outcome.status)}, current_timestamp(), "
            f"current_timestamp(), {_literal_or_null(outcome.error)}, "
            f"{version if version is not None else 'CAST(NULL AS BIGINT)'}"
        )

    # -- lock --------------------------------------------------------------
    def acquire_lock(self, target: str, run_id: str, minutes: int) -> bool:
        """Take the lock if it is free or expired, then read back who holds it.

        The conditional `UPDATE` is the claim; the read-back is the confirmation,
        because it doesn't depend on how a warehouse reports affected rows.
        """
        # `held` rather than `lock` as the alias: LOCK is a keyword in enough
        # dialects to be worth avoiding.
        # TODO(verify): MERGE and the INTERVAL literal against a live warehouse.
        self.runner.query(
            f"MERGE INTO {self._table('lock')} AS held "
            f"USING (SELECT {quote_literal(target)} AS id) AS candidate "
            "ON held.id = candidate.id "
            "WHEN NOT MATCHED THEN INSERT (id, holder) VALUES (candidate.id, NULL)"
        )
        self.runner.query(
            f"UPDATE {self._table('lock')} SET holder = {quote_literal(run_id)}, "
            "acquired_at = current_timestamp(), "
            f"expires_at = current_timestamp() + INTERVAL {int(minutes)} MINUTES "
            f"WHERE id = {quote_literal(target)} "
            "AND (holder IS NULL OR expires_at < current_timestamp())"
        )
        return self.lock_holder(target) == run_id

    def renew_lock(self, target: str, run_id: str, minutes: int) -> bool:
        self.runner.query(
            f"UPDATE {self._table('lock')} "
            f"SET expires_at = current_timestamp() + INTERVAL {int(minutes)} MINUTES "
            f"WHERE id = {quote_literal(target)} AND holder = {quote_literal(run_id)}"
        )
        return self.lock_holder(target) == run_id

    def release_lock(self, target: str, run_id: str) -> None:
        self.runner.query(
            f"UPDATE {self._table('lock')} SET holder = NULL, "
            "acquired_at = CAST(NULL AS TIMESTAMP), "
            "expires_at = CAST(NULL AS TIMESTAMP) "
            f"WHERE id = {quote_literal(target)} AND holder = {quote_literal(run_id)}"
        )

    def lock_holder(self, target: str) -> str | None:
        rows = self.runner.query(
            f"SELECT holder FROM {self._table('lock')} WHERE id = {quote_literal(target)}"
        )
        return str(rows[0]["holder"]) if rows and rows[0].get("holder") else None

    def force_unlock(self, target: str) -> str | None:
        holder = self.lock_holder(target)
        if holder is not None:
            self.release_lock(target, holder)
        return holder


def _literal_or_null(value: str | None) -> str:
    return quote_literal(value) if value is not None else "CAST(NULL AS STRING)"


# ---------------------------------------------------------------------------
# the offline one
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MemoryHistory:
    """The same contract, in memory. What the executor's unit tests run against."""

    runs: dict[str, dict[str, str]] = field(default_factory=dict)
    steps: dict[str, list[StepOutcome]] = field(default_factory=dict)
    locks: dict[str, str] = field(default_factory=dict)
    created: bool = False

    def ensure(self) -> None:
        self.created = True

    def start_run(
        self, run_id: str, plan_hash: str, target: str, tool_version: str
    ) -> None:
        self.runs[run_id] = {
            "plan_hash": plan_hash,
            "target": target,
            "user": "memory",
            "tool_version": tool_version,
            "status": "running",
        }
        self.steps.setdefault(run_id, [])

    def finish_run(self, run_id: str, status: Status) -> None:
        self.runs[run_id]["status"] = status

    def resumable_run(self, plan_hash: str, target: str) -> str | None:
        for run_id, run in reversed(list(self.runs.items())):
            if (
                run["plan_hash"] == plan_hash
                and run["target"] == target
                and run["status"] != "succeeded"
            ):
                return run_id
        return None

    def finished_steps(self, run_id: str) -> frozenset[int]:
        return frozenset(
            outcome.step_id
            for outcome in self.steps.get(run_id, ())
            if outcome.status in DONE
        )

    def record_step(self, run_id: str, outcome: StepOutcome) -> None:
        self.steps.setdefault(run_id, []).append(outcome)

    def acquire_lock(self, target: str, run_id: str, minutes: int) -> bool:
        del minutes  # nothing expires in a test
        holder = self.locks.get(target)
        if holder is not None and holder != run_id:
            return False
        self.locks[target] = run_id
        return True

    def renew_lock(self, target: str, run_id: str, minutes: int) -> bool:
        del minutes
        return self.locks.get(target) == run_id

    def release_lock(self, target: str, run_id: str) -> None:
        if self.locks.get(target) == run_id:
            del self.locks[target]

    def lock_holder(self, target: str) -> str | None:
        return self.locks.get(target)

    def force_unlock(self, target: str) -> str | None:
        return self.locks.pop(target, None)
