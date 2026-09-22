"""One base class, so a host can catch deltaplan and nothing else.

Every error deltaplan raises on purpose is a `DeltaplanError`. A program
embedding it catches that to report a failure, and a subclass when it wants to
react to one — a stale plan is worth re-planning, a refused destructive step is
worth asking a person about, an unreadable spec is neither.

The classes themselves stay in the module that raises them, where their
docstrings sit next to the code that knows why: `SpecError` in the loader,
`ExecutionError` in the executor. This is only the root they share.
"""

from __future__ import annotations


class DeltaplanError(Exception):
    """Something deltaplan refuses to do, with the reason in the message.

    Messages are complete sentences and name the object they are about, so a
    host can show one without adding context of its own.
    """
