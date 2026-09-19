"""Schemas as specs: their comment, tags and grants.

A schema is created — with its comment — when a spec declares it, and its tags
and grants are kept in line with the spec, per principal as for tables. Like a
function, a schema carries no marker to say deltaplan made it, so one is never
dropped, strict or not; a schema without a spec is only ever created bare, when
a table needs it.

In SQL, sqlglot parses `CREATE SCHEMA … COMMENT` and `GRANT … ON SCHEMA`, but
reads `ALTER SCHEMA … SET TAGS` as an opaque command (checked 2026-09-18) — so a
SQL spec can give a schema its comment and grants, and its tags need YAML.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-schema
"""

from __future__ import annotations

from dataclasses import dataclass, field

from deltaplan.model.table import Grant, Securable, sort_governance


@dataclass(frozen=True, slots=True)
class Schema(Securable):
    """A schema: `catalog.schema`."""

    name: str
    comment: str | None = None
    tags: tuple[tuple[str, str], ...] = ()
    grants: tuple[Grant, ...] = ()
    # A schema spec takes none; the field exists so it shares Securable's code.
    properties: tuple[tuple[str, str], ...] = ()
    #: What the spec says must not be there: `tags: {pii: null}`. Spec-only —
    #: a live object never has any — so they take no part in comparing.
    removed_properties: tuple[str, ...] = field(default=(), compare=False)
    removed_tags: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        sort_governance(self)

    @property
    def schema(self) -> str:
        """Itself — the `catalog.schema` a table's `schema` would name."""
        return self.name

    @property
    def managed(self) -> bool:
        """Never: nothing records that deltaplan made a schema."""
        return False
