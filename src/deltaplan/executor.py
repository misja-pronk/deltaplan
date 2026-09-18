"""Running a plan.

DDL is not transactional across statements, so this makes no rollback promise.
It makes narrower ones, and they are what the design asks for:

* **Nothing runs from a stale plan.** A fresh run recomputes the state
  fingerprint over exactly the tables the plan was built from, and refuses if the
  world has moved.
* **Steps are idempotent.** Before each one the executor asks whether the change
  it implements is already true of the live table (`differ.is_applied`). That is
  the design's precheck, asked of the model rather than of a bespoke query —
  reusing code that is tested rather than adding assumptions that aren't.
* **A failed run resumes.** Every step's outcome is recorded, so the next
  `apply` of the same plan picks up where it stopped instead of starting over.
* **One run at a time.** A lock row, taken with a conditional update and
  confirmed by reading it back, with a TTL so a dead run can't block forever.
* **A restore point before anything destructive.** The table's Delta version is
  recorded first, so `RESTORE` is one command.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from deltaplan.differ import is_applied
from deltaplan.history import (
    DEFAULT_LOCK_MINUTES,
    HistoryStore,
    Status,
    StepOutcome,
)
from deltaplan.introspect import Introspector, SqlRunner
from deltaplan.model.change import Change
from deltaplan.model.plan import Plan, Step, fingerprint
from deltaplan.model.view import Relation

#: Risk classes whose steps get a restore point recorded before they run.
RECORD_VERSION_FOR = frozenset({"destructive", "rewrite"})


class ExecutionError(Exception):
    """A refusal: nothing ran, and the reason is in the message."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What one `apply` did."""

    run_id: str
    status: Status
    ran: tuple[int, ...] = ()
    skipped: tuple[int, ...] = ()
    failed: int | None = None
    error: str | None = None
    resumed: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


@dataclass(slots=True)
class Executor:
    """Runs a plan's steps, recording everything it does."""

    runner: SqlRunner
    introspector: Introspector
    history: HistoryStore
    lock_minutes: int = DEFAULT_LOCK_MINUTES
    #: Overridable so tests get a run id they can assert on.
    new_run_id: Callable[[], str] = field(default=lambda: uuid.uuid4().hex[:12])
    #: Called as each step resolves, so a caller can show progress live rather
    #: than after the fact.
    observer: Callable[[Step, Status, str | None], None] | None = None
    _live: dict[str, Relation | None] = field(default_factory=dict)

    # -- the run -----------------------------------------------------------
    def apply(self, plan: Plan, *, allow_destructive: bool = False) -> ExecutionResult:
        self._refuse_unrunnable(plan, allow_destructive=allow_destructive)
        self.history.ensure()

        plan_hash = plan_identity(plan)
        resumed = self.history.resumable_run(plan_hash, plan.target)
        run_id = resumed or self.new_run_id()

        self._live = self.introspector.tables([diff.table for diff in plan.diffs])
        if resumed is None:
            self._refuse_stale(plan)

        if not self.history.acquire_lock(plan.target, run_id, self.lock_minutes):
            holder = self.history.lock_holder(plan.target)
            raise ExecutionError(
                f"target {plan.target!r} is locked by run {holder}. Wait for it to "
                "finish, or release it with `deltaplan force-unlock`."
            )

        try:
            if resumed is None:
                self.history.start_run(run_id, plan_hash, plan.target, plan.tool_version)
            result = self._run_steps(plan, run_id, resumed=resumed is not None)
            self.history.finish_run(run_id, result.status)
            return result
        finally:
            self.history.release_lock(plan.target, run_id)

    def _run_steps(self, plan: Plan, run_id: str, *, resumed: bool) -> ExecutionResult:
        already = self.history.finished_steps(run_id) if resumed else frozenset()
        changes = plan.changes
        ran: list[int] = []
        skipped: list[int] = []

        for step in plan.steps:
            if step.id in already:
                skipped.append(step.id)
                self._observe(step, "skipped", "done in an earlier run")
                continue
            change = changes[step.change] if 0 <= step.change < len(changes) else None
            if change is not None and self._already_done(change):
                skipped.append(step.id)
                self.history.record_step(
                    run_id, StepOutcome(step.id, step.table, step.sql, "skipped")
                )
                self._observe(step, "skipped", "already applied")
                continue

            failure = self._run_step(step, run_id)
            self._observe(step, "failed" if failure else "succeeded", failure)
            if failure is not None:
                return ExecutionResult(
                    run_id=run_id,
                    status="failed",
                    ran=tuple(ran),
                    skipped=tuple(skipped),
                    failed=step.id,
                    error=failure,
                    resumed=resumed,
                )
            ran.append(step.id)

        return ExecutionResult(
            run_id=run_id,
            status="succeeded",
            ran=tuple(ran),
            skipped=tuple(skipped),
            resumed=resumed,
        )

    def _run_step(self, step: Step, run_id: str) -> str | None:
        """Run one step. Returns the error, or None when it worked."""
        version = self._restore_point(step)
        blocked = self._blocked(step)
        if blocked is not None:
            self.history.record_step(
                run_id,
                StepOutcome(step.id, step.table, step.sql, "failed", blocked, version),
            )
            return blocked

        try:
            if step.sql is not None:
                self.runner.query(step.sql)
            self._confirm(step)
        except Exception as error:  # noqa: BLE001 - whatever the warehouse raised
            message = f"{type(error).__name__}: {error}"
            self.history.record_step(
                run_id,
                StepOutcome(step.id, step.table, step.sql, "failed", message, version),
            )
            return message

        self.history.record_step(
            run_id,
            StepOutcome(step.id, step.table, step.sql, "succeeded", None, version),
        )
        return None

    def _observe(self, step: Step, status: Status, note: str | None) -> None:
        if self.observer is not None:
            self.observer(step, status, note)

    # -- checks ------------------------------------------------------------
    def _already_done(self, change: Change) -> bool:
        return is_applied(change, self._live.get(change.table))

    def _blocked(self, step: Step) -> str | None:
        """Ask the step's precheck whether a precondition stops it."""
        if step.precheck is None:
            return None
        rows = self.runner.query(step.precheck)
        if not rows or not _is_true(next(iter(rows[0].values()), None)):
            return None
        reason = step.refusal or "; ".join(step.warnings) or "a precondition is not met"
        return f"refused before running: {reason}"

    def _confirm(self, step: Step) -> None:
        if step.postcheck is None:
            return
        rows = self.runner.query(step.postcheck)
        if not rows or not _is_true(next(iter(rows[0].values()), None)):
            raise ExecutionError(
                "the statement ran but the postcheck says it didn't take"
            )

    def _restore_point(self, step: Step) -> int | None:
        if step.risk not in RECORD_VERSION_FOR:
            return None
        try:
            return self.introspector.latest_version(step.table)
        except Exception:  # noqa: BLE001 - a missing restore point must not stop a run
            return None

    def _refuse_unrunnable(self, plan: Plan, *, allow_destructive: bool) -> None:
        missing = [step for step in plan.steps if step.sql is None]
        if missing:
            described = "\n".join(
                f"  {step.id}. {step.title} on {step.table}"
                + (f" — {step.note}" if step.note else "")
                for step in missing
            )
            raise ExecutionError(
                f"this plan has {len(missing)} step(s) deltaplan can't run:\n{described}"
            )
        destructive = [step for step in plan.steps if step.risk == "destructive"]
        if destructive and not allow_destructive:
            titles = ", ".join(
                f"{step.id}. {step.title} on {step.table}" for step in destructive
            )
            raise ExecutionError(
                f"this plan destroys something ({titles}). Re-run with "
                "--allow-destructive if that is what you want."
            )

    def _refuse_stale(self, plan: Plan) -> None:
        current = fingerprint(self._live[diff.table] for diff in plan.diffs)
        if current != plan.state_fingerprint:
            raise ExecutionError(
                "the live tables have changed since this plan was made "
                f"(fingerprint {current}, plan says {plan.state_fingerprint}). "
                "Run `deltaplan plan` again and review the new plan."
            )


def plan_identity(plan: Plan) -> str:
    """What makes a plan *this* plan: its specs, against that live state.

    Two runs of the same plan share it, which is how a resume finds its run; any
    edit to a spec or any drift in the catalog produces a different one.
    """
    return f"{plan.spec_hash}:{plan.state_fingerprint}"


def _is_true(value: object) -> bool:
    """The API returns booleans as the strings `true` / `false`."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"
