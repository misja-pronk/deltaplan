"""What deltaplan manages here — and what belongs to another tool.

A table has more to it than its shape, and plenty of teams already have
something that owns the rest: a policy framework that sets grants, a catalogue
that writes the tags an ABAC rule reads. Two tools writing the same thing is
how a Monday morning starts with a table nobody recognises.

So `deltaplan.yml` can draw the line:

```yaml
manage:
  grants: false      # their policy framework owns these
  tags: false        # and the tags its rules read
```

Everything is managed unless it says otherwise. What is switched off is no
longer deltaplan's to *declare*: the key is refused in a spec, left out of the
editors' JSON Schema, and never written by `import` — so it can never appear in
a plan either.

Switched off is not the same as invisible. deltaplan keeps reading what it needs
to avoid destroying someone else's work: a masked table still refuses a rewrite,
and a renamed column's tags are still put back afterwards. It reads less only
where nothing depends on the answer.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from deltaplan.model.view import Relation

#: Any relation, kept as itself: a stripped table is a table.
_R = TypeVar("_R", bound="Relation")

#: What can be handed over, and the spec keys each one covers.
MANAGEABLE: dict[str, tuple[str, ...]] = {
    "comments": ("comment",),
    "grants": ("grants",),
    "masks": ("mask",),
    "owner": ("owner",),
    "properties": ("properties",),
    "row_filters": ("row_filter",),
    "tags": ("tags",),
}

#: The spec key -> what it belongs to, for the error a spec gets.
ASPECT_OF: dict[str, str] = {
    key: aspect for aspect, keys in MANAGEABLE.items() for key in keys
}


@dataclass(frozen=True, slots=True)
class Manage:
    """The line between deltaplan's business and someone else's."""

    #: Aspects this project has handed to another tool.
    elsewhere: tuple[str, ...] = ()

    def manages(self, aspect: str) -> bool:
        return aspect not in self.elsewhere

    def allows(self, key: str) -> bool:
        """Whether a spec may use this key at all."""
        aspect = ASPECT_OF.get(key)
        return aspect is None or self.manages(aspect)

    def keys(self, allowed: set[str]) -> set[str]:
        """`allowed`, without the keys this project doesn't manage."""
        if not self.elsewhere:
            return allowed
        return {key for key in allowed if self.allows(key)}

    def __bool__(self) -> bool:
        """True when something has been handed over."""
        return bool(self.elsewhere)


#: The default: a project where deltaplan manages everything it knows how to.
EVERYTHING = Manage()


def strip(relation: _R, manage: Manage) -> _R:
    """`relation` without what this project leaves to another tool.

    Used on both sides. On a spec it is what `import` writes; on the live
    object it is what the differ compares against, so a comment another tool
    owns is never diffed away merely because no spec mentions it. What the
    *rewrite* reads is the untouched live object, so it still puts back what a
    replace would lose.

    What deltaplan reads from a live table is one thing; what it writes into a
    spec is another. `import` reads a masked, tagged, granted table just as it
    always did — and then writes a spec that only says what this project
    manages, so the file it hands you is one `validate` accepts.
    """
    if not manage.elsewhere:
        return relation
    blank: dict[str, object] = {}
    if not manage.manages("comments"):
        blank["comment"] = None
    if not manage.manages("tags"):
        blank["tags"] = ()
    if not manage.manages("grants"):
        blank["grants"] = ()
    if not manage.manages("owner"):
        blank["owner"] = None
    if not manage.manages("properties"):
        blank["properties"] = ()
    if not manage.manages("row_filters"):
        blank["row_filter"] = None
    fields = {f.name for f in dataclasses.fields(relation)}
    stripped = dataclasses.replace(
        relation, **{k: v for k, v in blank.items() if k in fields}
    )
    columns = getattr(stripped, "columns", None)
    if columns is None:
        return stripped
    keep_tags, keep_masks = manage.manages("tags"), manage.manages("masks")
    keep_comments = manage.manages("comments")
    if keep_tags and keep_masks and keep_comments:
        return stripped
    return dataclasses.replace(
        stripped,
        columns=tuple(
            dataclasses.replace(
                column,
                **({} if keep_tags else {"tags": ()}),
                **({} if keep_masks else {"mask": None}),
                **({} if keep_comments else {"comment": None}),
            )
            for column in columns
        ),
    )
