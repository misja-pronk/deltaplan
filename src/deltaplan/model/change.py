"""What differs between two tables.

A `Change` is *semantic*: it says what is different, not how to fix it. Turning
changes into ordered, risk-classified SQL is the planner's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from deltaplan.model.function import Function
from deltaplan.model.schema import Schema
from deltaplan.model.table import Constraint, RowFilter, Table
from deltaplan.model.types import DataType, Field, Identity, Mask
from deltaplan.model.view import View
from deltaplan.model.volume import Volume

ChangeKind: TypeAlias = Literal[
    "create_table",
    "drop_table",
    "claim_table",
    "rename_table",
    "create_schema",
    "set_schema_comment",
    "create_volume",
    "set_volume_comment",
    "create_view",
    "replace_view",
    "create_function",
    "replace_function",
    "set_table_comment",
    "set_cluster_by",
    "set_property",
    "set_tag",
    "set_column_tag",
    "set_mask",
    "set_row_filter",
    "set_default",
    "set_identity",
    "set_generated",
    "add_column",
    "drop_column",
    "rename_column",
    "change_type",
    "set_nullable",
    "set_comment",
    "reorder_columns",
    "add_constraint",
    "drop_constraint",
    "grant",
    "revoke",
]

#: Kinds that bring a table, view or function into being.
CREATE_KINDS: frozenset[str] = frozenset(
    {"create_table", "create_view", "create_function", "create_schema", "create_volume"}
)

#: Kinds whose `path` names something other than a column — a property key, a
#: tag key, a principal.
TABLE_LEVEL_KINDS: frozenset[str] = frozenset(
    {"set_property", "set_tag", "grant", "revoke", "set_row_filter", "rename_table"}
)

#: Whatever a change is about. Every member is hashable, so changes are too.
ChangeValue: TypeAlias = (
    str
    | bool
    | None
    | tuple[str, ...]
    | DataType
    | Field
    | Constraint
    | Table
    | View
    | Function
    | Schema
    | Volume
    | Mask
    | RowFilter
    | Identity
)


@dataclass(frozen=True, slots=True)
class Change:
    """One semantic difference, at one path.

    `path` addresses the thing that changed, in Databricks' nested syntax:
    `amount`, `address.zip`, `lines.element.sku`, `by_code.value.n`. For
    table-level changes it is the empty string, except for properties and tags,
    where it is the key.

    For a rename, `path` is where the column *ends up*, `before` is the old name
    and `after` the new one — so a rename groups with the other changes to that
    column rather than orphaning itself under the old name.
    """

    table: str
    kind: ChangeKind
    path: str = ""
    before: ChangeValue = None
    after: ChangeValue = None

    @property
    def column(self) -> str:
        """The top-level column this change belongs to, or "" for table-level."""
        if self.kind in TABLE_LEVEL_KINDS:
            return ""
        return self.path.split(".")[0] if self.path else ""

    @property
    def parent_path(self) -> str:
        """The path of the enclosing field, or "" at the top level."""
        return self.path.rsplit(".", 1)[0] if "." in self.path else ""

    @property
    def leaf(self) -> str:
        """The last segment of the path."""
        return self.path.rsplit(".", 1)[-1] if self.path else ""

    @property
    def nested(self) -> bool:
        return "." in self.path
