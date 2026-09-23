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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from deltaplan.differ import is_applied
from deltaplan.errors import DeltaplanError
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


class ExecutionError(DeltaplanError):
    """A refusal: nothing ran, and the reason is in the message."""


class DestructiveRefused(ExecutionError):
    """The plan destroys something, and nobody said that was alright.

    `tables` names them, so a host can ask a person about those tables rather
    than about a plan. Pass `allow_destructive=True` to go ahead.
    """

    def __init__(self, message: str, tables: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.tables = tables


class StalePlan(ExecutionError):
    """The world moved between the plan and the apply.

    `tables` names the ones that changed. A host's answer is almost always to
    plan again and show the new plan, not to retry this one.
    """

    def __init__(self, message: str, tables: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.tables = tables


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
    #: `(table, version)` for every step that took a restore point before it
    #: ran. With no history schema this is the only place they are kept, so a
    #: `RESTORE TABLE … TO VERSION AS OF` is still one command away.
    restore_points: tuple[tuple[str, int], ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every step ran, or was already true of the live table."""
        return self.status == "succeeded"

    @property
    def failed_step(self) -> int | None:
        """The number of the step that stopped the run, if one did."""
        return self.failed


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
    #: `(table, version)` for every restore point this run took.
    _restore_points: list[tuple[str, int]] = field(default_factory=list)

    # -- the run -----------------------------------------------------------
    def apply(self, plan: Plan, *, allow_destructive: bool = False) -> ExecutionResult:
        self._refuse_unrunnable(plan, allow_destructive=allow_destructive)
        self.history.ensure()

        plan_hash = plan_identity(plan)
        resumed = self.history.resumable_run(plan_hash, plan.target)
        run_id = resumed or self.new_run_id()

        self._live = self.introspector.tables(_read_from(plan), _kinds(plan))
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

            # A lock has a TTL so a dead run can't hold it forever — which means a
            # long run has to keep it alive, or a second apply could start
            # halfway through this one.
            if not self.history.renew_lock(plan.target, run_id, self.lock_minutes):
                lost = (
                    f"lost the lock on {plan.target} before step {step.id}: it expired "
                    "or was released. Stopped here; run `deltaplan apply` again to "
                    "resume once nothing else is running."
                )
                self._observe(step, "failed", lost)
                return ExecutionResult(
                    run_id=run_id,
                    status="failed",
                    ran=tuple(ran),
                    skipped=tuple(skipped),
                    failed=step.id,
                    error=lost,
                    resumed=resumed,
                    restore_points=tuple(self._restore_points),
                )
            failure = self._run_step(step, run_id)
            self._observe(
                step,
                "failed" if failure else "succeeded",
                failure or _taken(self._restore_points, step),
            )
            if failure is not None:
                return ExecutionResult(
                    run_id=run_id,
                    status="failed",
                    ran=tuple(ran),
                    skipped=tuple(skipped),
                    failed=step.id,
                    error=failure,
                    resumed=resumed,
                    restore_points=tuple(self._restore_points),
                )
            ran.append(step.id)

        return ExecutionResult(
            run_id=run_id,
            status="succeeded",
            ran=tuple(ran),
            skipped=tuple(skipped),
            resumed=resumed,
            restore_points=tuple(self._restore_points),
        )

    def _run_step(self, step: Step, run_id: str) -> str | None:
        """Run one step. Returns the error, or None when it worked."""
        version = self._restore_point(step)
        if version is not None:
            # Kept on the run as well as in the history: without a history
            # schema this is where a restore point lives.
            self._restore_points.append((step.table, version))
        try:
            blocked = self._blocked(step)
        except Exception as error:  # noqa: BLE001 - the precheck's own query failed
            blocked = f"the precheck could not run: {type(error).__name__}: {error}"
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
        except ExecutionError as error:
            message = str(error)  # deltaplan's own explanation, already worded
            self.history.record_step(
                run_id,
                StepOutcome(step.id, step.table, step.sql, "failed", message, version),
            )
            return message
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
                step.failure or "the statement ran but the postcheck says it didn't take"
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
                f"this plan has {len(missing)} step{'' if len(missing) == 1 else 's'} "
                f"deltaplan can't run:\n{described}"
            )
        destructive = [step for step in plan.steps if step.risk == "destructive"]
        if destructive and not allow_destructive:
            titles = ", ".join(
                f"{step.id}. {step.title} on {step.table}" for step in destructive
            )
            raise DestructiveRefused(
                f"this plan destroys something ({titles}). Allow it explicitly if "
                "that is what you want.",
                tuple(dict.fromkeys(step.table for step in destructive)),
            )

    def _refuse_stale(self, plan: Plan) -> None:
        current = fingerprint(self._live.get(name) for name in _read_from(plan))
        if current == plan.state_fingerprint:
            return
        moved = _moved(plan, self._live)
        named = f" ({', '.join(moved)})" if moved else ""
        raise StalePlan(
            f"the live tables have changed since this plan was made{named}. "
            "Plan again and review the new plan.",
            moved,
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


def stale_tables(plan: Plan, introspector: Introspector) -> tuple[str, ...]:
    """The tables that have moved since this plan was made, read fresh.

    Empty means the plan still describes the world it was made in, which is
    what `apply` insists on. Asking first lets a host plan again rather than
    put a stale plan to a person.
    """
    return _moved(plan, introspector.tables(_read_from(plan), _kinds(plan)))


def _moved(plan: Plan, live: Mapping[str, Relation | None]) -> tuple[str, ...]:
    """Which tables differ from the live state the plan was built against.

    The plan carries that state, so a name is better than a pair of hashes
    nobody can act on.
    """
    return tuple(
        diff.table
        for diff in plan.diffs
        if fingerprint([diff.live])
        != fingerprint([live.get(diff.live.name if diff.live else diff.table)])
    )


def _taken(points: list[tuple[str, int]], step: Step) -> str | None:
    """The restore point this step took, as a note for whoever is watching."""
    for table, version in reversed(points):
        if table == step.table:
            return f"restore point: {table} version {version}"
    return None


def _kinds(plan: Plan) -> dict[str, str]:
    """What each name in the plan was planned as, by the name it was read from."""
    return {
        (diff.live.name if diff.live is not None else diff.table): diff.kind
        for diff in plan.diffs
    }


def _read_from(plan: Plan) -> list[str]:
    """The name each table's live state was read from: its own, or — for a table
    being renamed — the one it has until the rename runs."""
    return [
        diff.live.name if diff.live is not None else diff.table for diff in plan.diffs
    ]
