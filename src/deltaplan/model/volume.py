"""Managed volumes: their comment, tags and grants.

A volume holds files, and dropping a managed one deletes them — and, like a
function or a schema, a volume carries nothing that could record that
deltaplan made it. So volumes are created and kept in line, never dropped.
External volumes (with a LOCATION) are out of scope: reported, left alone.

YAML only: sqlglot reads CREATE VOLUME and GRANT … ON VOLUME as opaque
commands (checked 2026-09-18).
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-volume
"""

from __future__ import annotations

from dataclasses import dataclass

from deltaplan.model.table import Grant, Securable, sort_governance


@dataclass(frozen=True, slots=True)
class Volume(Securable):
    """A managed volume: `catalog.schema.volume`."""

    name: str
    comment: str | None = None
    tags: tuple[tuple[str, str], ...] = ()
    grants: tuple[Grant, ...] = ()
    # A volume spec takes none; the field exists so it shares Securable's code.
    properties: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        sort_governance(self)

    @property
    def managed(self) -> bool:
        """Never: nothing records that deltaplan made a volume."""
        return False
