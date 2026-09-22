"""deltaplan: declarative plan and apply for Databricks tables.

Two ways in, the same code underneath. The command line is the one most people
meet:

```sh
deltaplan plan
deltaplan apply
```

And this package is the other, for a program that runs deltaplan as part of
something larger — a deployment task that plans, shows the plan its own way,
and applies it:

```python
import deltaplan

project = deltaplan.Project.find()
target = project.resolve(project.target("prod"))
```

Everything a host needs is exported here, and only what is exported here is
meant to be relied on. Names from `deltaplan.<module>` that this list doesn't
mention are the implementation, and move without notice.

Errors all descend from `DeltaplanError`, so one `except` reports any failure
and a subclass reacts to a particular one. Rendering a plan is separate from
making one: `render_plan` for a terminal, `render_markdown` for a pull request,
`plan_to_json` for a file or a queue.
"""

from __future__ import annotations

from deltaplan.api import NoHistory, apply, drift, is_stale, plan, validate
from deltaplan.bundle import Bundle, BundleError, BundleTarget
from deltaplan.connect import Connection, NotConnected
from deltaplan.errors import DeltaplanError
from deltaplan.executor import (
    DestructiveRefused,
    ExecutionError,
    ExecutionResult,
    Executor,
    StalePlan,
)
from deltaplan.history import DeltaHistory, HistoryStore, MemoryHistory
from deltaplan.introspect import IntrospectionError, Introspector, WarehouseRunner
from deltaplan.loader import (
    Diagnostic,
    LoadedSpec,
    Project,
    SpecError,
    SpecErrors,
    Specs,
    Target,
    dump_spec,
    load_project,
    load_spec,
    load_specs,
    spec_files,
    validate_spec,
)
from deltaplan.manage import MANAGEABLE, Manage
from deltaplan.model.function import Function
from deltaplan.model.plan import Plan, Risk, Step, Summary, TableDiff, TableFacts
from deltaplan.model.schema import Schema
from deltaplan.model.table import (
    Check,
    ForeignKey,
    Grant,
    PrimaryKey,
    RowFilter,
    Table,
)
from deltaplan.model.types import Column, Field, Mask
from deltaplan.model.view import Relation, View
from deltaplan.model.volume import Volume
from deltaplan.planning import PlanningError, plan_tables
from deltaplan.render.json import PlanFileError
from deltaplan.render.json import dumps as plan_to_json
from deltaplan.render.json import loads as plan_from_json
from deltaplan.render.markdown import render_markdown
from deltaplan.render.rich import plan_text, render_plan

__all__ = [
    "__version__",
    "apply",
    "Bundle",
    "BundleError",
    "BundleTarget",
    "Check",
    "Column",
    "Connection",
    "DeltaHistory",
    "DeltaplanError",
    "DestructiveRefused",
    "Diagnostic",
    "drift",
    "dump_spec",
    "ExecutionError",
    "ExecutionResult",
    "Executor",
    "Field",
    "ForeignKey",
    "Function",
    "Grant",
    "HistoryStore",
    "IntrospectionError",
    "Introspector",
    "is_stale",
    "load_project",
    "load_spec",
    "load_specs",
    "LoadedSpec",
    "Manage",
    "MANAGEABLE",
    "Mask",
    "MemoryHistory",
    "NoHistory",
    "NotConnected",
    "plan",
    "Plan",
    "plan_from_json",
    "plan_tables",
    "plan_text",
    "plan_to_json",
    "PlanFileError",
    "PlanningError",
    "PrimaryKey",
    "Project",
    "Relation",
    "render_markdown",
    "render_plan",
    "Risk",
    "RowFilter",
    "Schema",
    "spec_files",
    "SpecError",
    "SpecErrors",
    "Specs",
    "StalePlan",
    "Step",
    "Summary",
    "Table",
    "TableDiff",
    "TableFacts",
    "Target",
    "validate",
    "validate_spec",
    "View",
    "Volume",
    "WarehouseRunner",
]


def __getattr__(name: str) -> str:
    """`deltaplan.__version__`, read from the installed distribution."""
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("deltaplan")
        except PackageNotFoundError:  # pragma: no cover - running from a checkout
            return "0.0.0"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
