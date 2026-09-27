"""A warehouse that is starting is waited for, not failed on.

A stopped SQL warehouse starts on the first request — and until it has started,
it answers that request with *could not be processed by the warehouse*, which is
the same sentence a warehouse that will never start gives. The difference is
what the workspace says the warehouse is doing. While it says STARTING (or
STOPPED, since the refused request is what starts it), the runner asks again;
the moment it says RUNNING and still refuses, or the wait runs out, the refusal
is reported as it arrived.

No statement ran, so asking again is safe for a write as much as for a read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest

from deltaplan.introspect import IntrospectionError, Progress, WarehouseRunner

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

REFUSED = "The request could not be processed by the warehouse."


class _Response:
    def __init__(self) -> None:
        from databricks.sdk.service.sql import StatementState

        self.statement_id = "01ef-abc"
        self.status = type(
            "Status", (), {"state": StatementState.SUCCEEDED, "error": None}
        )()
        columns = [type("Column", (), {"name": "n"})()]
        self.manifest = type(
            "Manifest", (), {"schema": type("Schema", (), {"columns": columns})()}
        )()
        self.result = type(
            "Result", (), {"data_array": [["1"]], "next_chunk_index": None}
        )()


@dataclass
class StartingClient:
    """Refuses `refusals` requests, then takes one; says `states` in turn."""

    refusals: int
    states: list[str] = field(default_factory=lambda: ["STARTING"])
    said: str = REFUSED
    #: A workspace that can't say what the warehouse is doing.
    cannot_say: bool = False
    requests: int = 0
    looked: int = 0

    @property
    def statement_execution(self) -> Any:
        return self

    @property
    def warehouses(self) -> Any:
        return self

    def execute_statement(self, **_kwargs: object) -> _Response:
        self.requests += 1
        if self.requests <= self.refusals:
            raise RuntimeError(self.said)
        return _Response()

    def get(self, _warehouse_id: str) -> Any:
        from databricks.sdk.service.sql import State

        self.looked += 1
        if self.cannot_say:
            raise RuntimeError("no such warehouse")
        name = self.states[min(self.looked, len(self.states)) - 1]
        return type("Warehouse", (), {"state": State(name), "name": "Starter"})()


def runner(client: StartingClient, **rest: Any) -> WarehouseRunner:
    return WarehouseRunner(
        cast("WorkspaceClient", client),
        "w1",
        poll_seconds=0,
        start_poll_seconds=0,
        **rest,
    )


def test_a_starting_warehouse_is_asked_again_until_it_takes_the_request() -> None:
    client = StartingClient(refusals=3)
    assert runner(client).query("SELECT 1 AS n") == ({"n": "1"},)
    assert client.requests == 4
    assert client.looked == 3, "the state was checked before every retry"


def test_a_stopped_warehouse_counts_as_starting() -> None:
    """The refused request is what starts it."""
    client = StartingClient(refusals=1, states=["STOPPED"])
    assert runner(client).query("SELECT 1 AS n") == ({"n": "1"},)


def test_a_running_warehouse_that_refuses_is_refusing() -> None:
    """The same sentence from a warehouse that has started is not a wait: a
    person should hear it at once, with what deltaplan knows about it."""
    client = StartingClient(refusals=1, states=["RUNNING"])
    with pytest.raises(IntrospectionError, match="could not be processed") as raised:
        runner(client).query("SELECT 1")
    assert client.requests == 1
    assert "serverless warehouse" in str(raised.value), "the advice is still there"


def test_any_other_refusal_is_not_retried() -> None:
    client = StartingClient(refusals=1, said="PERMISSION_DENIED: no")
    with pytest.raises(IntrospectionError, match="PERMISSION_DENIED"):
        runner(client).query("SELECT 1")
    assert client.requests == 1
    assert client.looked == 0


def test_the_wait_has_a_bound_and_says_so() -> None:
    client = StartingClient(refusals=1_000)
    with pytest.raises(IntrospectionError, match="waited .* for warehouse w1 to start"):
        runner(client, start_wait_seconds=0).query("SELECT 1")
    assert client.requests == 1, "a bound of nothing gives up at the first refusal"


def test_a_workspace_that_cannot_say_leaves_the_refusal_as_it_is() -> None:
    client = StartingClient(refusals=1, cannot_say=True)
    with pytest.raises(IntrospectionError, match="could not be processed"):
        runner(client).query("SELECT 1")


def test_whoever_is_watching_hears_what_the_wait_is_for() -> None:
    client = StartingClient(refusals=2)
    heard: list[Progress] = []
    runner(client, heartbeat=heard.append).query("SELECT 1 AS n")
    assert [beat.waiting for beat in heard] == ["warehouse starting"] * 2
    assert heard[0].statement == "SELECT 1 AS n"
    assert heard[0].statement_id == "", "nothing has run yet"
