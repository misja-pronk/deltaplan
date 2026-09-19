"""SQL functions: what column masks and row filters call.

A function spec gives its parameters, return type and body. The body is what is
compared — like a view's query — and a change replaces the function.

Functions differ from tables and views in one way that matters: they carry no
properties, so there is nowhere to record that deltaplan created one. deltaplan
therefore creates and replaces functions but never drops them, strict schema or
not. Only SQL functions are modelled; Python UDFs are left alone.

https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-sql-function
"""

from __future__ import annotations

from dataclasses import dataclass

from deltaplan.model.table import Grant, Securable, sort_governance
from deltaplan.model.types import DataType


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    type: DataType


@dataclass(frozen=True, slots=True)
class Function(Securable):
    """A SQL function."""

    name: str
    parameters: tuple[Parameter, ...]
    returns: DataType
    body: str
    comment: str | None = None
    grants: tuple[Grant, ...] = ()
    # A function takes none of these; the fields exist so it shares Securable's code.
    properties: tuple[tuple[str, str], ...] = ()
    tags: tuple[tuple[str, str], ...] = ()
    removed_properties: tuple[str, ...] = ()
    removed_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        sort_governance(self)
        object.__setattr__(self, "body", self.body.strip())

    @property
    def managed(self) -> bool:
        """Never: a function has no properties to carry deltaplan's marker."""
        return False
