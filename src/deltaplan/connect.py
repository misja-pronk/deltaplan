"""How deltaplan reaches a workspace, in one place.

A plan is made from two things: the specs, and what is live. This is the
second — a Databricks workspace client and the SQL warehouse to run statements
on. The rules for finding them were spread through the command line; they live
here now, so a program embedding deltaplan gets the same ones without copying
them.

The warehouse is settled in this order, first answer wins:

1. what the caller passed;
2. `warehouse_id` on the target (`deltaplan.yml`, or a bundle variable);
3. `DATABRICKS_WAREHOUSE_ID` in the environment;
4. the warehouse a bundle's `warehouse_id: {lookup: {warehouse: …}}` names,
   looked up by name once connected.

The client follows the Databricks SDK's unified authentication, so whatever
works for the Databricks CLI works here: a profile, a host and a token, OAuth.
https://docs.databricks.com/aws/en/dev-tools/auth/unified-auth
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from deltaplan.errors import DeltaplanError
from deltaplan.introspect import Introspector, SqlRunner, WarehouseRunner
from deltaplan.manage import EVERYTHING, Manage

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from deltaplan.loader import Target


class NotConnected(DeltaplanError):
    """deltaplan couldn't reach a workspace, or couldn't tell which warehouse.

    The message says which of the two it was and what was tried, so a host can
    show it without adding anything.
    """


class Connection:
    """A workspace client and the warehouse to run statements on.

    Build one from a resolved target — the usual way, since a target already
    knows its profile, host and warehouse — or hand over a client you already
    have, which deltaplan then uses rather than making a second one:

    ```python
    conn = deltaplan.Connection.from_target(resolved)
    conn = deltaplan.Connection(client=my_client, warehouse_id="abc123")
    conn = deltaplan.Connection(profile="dev", warehouse_id="abc123")
    conn = deltaplan.Connection(runner=my_runner)     # already runs statements
    ```

    Raises `NotConnected` when there is no client to be made, or no warehouse
    to be found.
    """

    def __init__(
        self,
        *,
        client: WorkspaceClient | None = None,
        runner: SqlRunner | None = None,
        profile: str | None = None,
        host: str | None = None,
        warehouse_id: str | None = None,
        warehouse_name: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if runner is not None:
            # Something that already runs statements — another tool's pooled
            # connection, or a test's in-memory warehouse. Taken as it is.
            self.client = client  # type: ignore[assignment]
            self._runner: SqlRunner | None = runner
            self.warehouse_id = warehouse_id or "given"
            return
        self._runner = None
        self.client: WorkspaceClient | None = (
            client if client is not None else _client(profile, host)
        )
        chosen = warehouse_id or (environ or os.environ).get("DATABRICKS_WAREHOUSE_ID")
        if not chosen and warehouse_name:
            chosen = _by_name(self.client, warehouse_name)
        if not chosen:
            raise NotConnected(
                "no SQL warehouse: pass warehouse_id, set it on the target, or "
                "export DATABRICKS_WAREHOUSE_ID."
            )
        self.warehouse_id: str = chosen

    @classmethod
    def from_target(
        cls,
        target: Target,
        *,
        client: WorkspaceClient | None = None,
        profile: str | None = None,
        warehouse_id: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Connection:
        """Connect the way this target says to.

        `profile` and `warehouse_id` override what the target carries — that is
        what a `--profile` or `--warehouse-id` flag is for. A bundle's warehouse
        lookup is done here, once there is a client to do it with.
        """
        return cls(
            client=client,
            profile=profile or target.profile,
            host=None if (profile or target.profile) else target.host,
            warehouse_id=warehouse_id or target.warehouse_id,
            warehouse_name=target.warehouse_lookup,
            environ=environ,
        )

    @property
    def runner(self) -> SqlRunner:
        """The statement runner deltaplan sends SQL through."""
        if self._runner is not None:
            return self._runner
        if self.client is None:  # pragma: no cover - the constructor sees to this
            raise NotConnected("this connection has neither a client nor a runner")
        return WarehouseRunner(self.client, self.warehouse_id)

    def introspector(
        self, manage: Manage = EVERYTHING, parallel: int = 8
    ) -> Introspector:
        """A reader of live state, told what this project manages."""
        return Introspector(self.runner, manage, parallel=parallel)

    def __repr__(self) -> str:
        return f"Connection(warehouse_id={self.warehouse_id!r})"


def _client(profile: str | None, host: str | None) -> WorkspaceClient:
    from databricks.sdk import WorkspaceClient

    try:
        if profile:
            return WorkspaceClient(profile=profile)
        if host:
            return WorkspaceClient(host=host)
        return WorkspaceClient()
    except Exception as error:  # the SDK raises ValueError for most config problems
        where = (
            f"profile {profile!r}"
            if profile
            else f"host {host}"
            if host
            else "the environment or the DEFAULT profile"
        )
        raise NotConnected(
            f"can't connect to a Databricks workspace using {where}: {error}"
        ) from error


def _by_name(client: WorkspaceClient, name: str) -> str:
    """The id of the SQL warehouse a bundle's `warehouse_id` lookup names."""
    found = [w.id for w in client.warehouses.list() if w.name == name and w.id]
    if len(found) != 1:
        problem = "no SQL warehouse" if not found else "more than one SQL warehouse"
        raise NotConnected(
            f"the bundle looks up the warehouse by name, and there is {problem} "
            f"called {name!r}. Set warehouse_id on the target instead."
        )
    return found[0]
