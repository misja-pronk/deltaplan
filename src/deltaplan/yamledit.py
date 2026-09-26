"""Editing a YAML file instead of rewriting it.

`adopt` has to make a spec file say what is live without disturbing anything
else in it: the comments someone wrote, the `${catalog}` they used instead of a
name, their quoting, their key order. Re-dumping the model loses all of that —
the file comes back canonical, and the diff someone has to review is the whole
file.

So this edits the text. A file is parsed to YAML's own node tree, which carries
the exact character span of every key, value and list item; the wanted document
is walked alongside it, and only what differs becomes an edit. Everything else
stays byte for byte as it was.

Two rules make it safe to use on a file someone cares about:

- **A value that already reads right is never touched.** `rendered` applies
  whatever substitution the reader would do, so `${catalog}.sales.orders` and
  `prod.sales.orders` compare equal and the variable survives.
- **Nothing here decides what the file should say.** It takes a document and
  makes the file hold it. Whether that document is right is the caller's
  business — and `adopt` checks its own work by reading the result back.

List items are matched by their `name`, which is how every list in a spec is
keyed (columns, parameters, grants by principal); a list of plain scalars is
replaced whole when it differs, because there is nothing to match on.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

#: How a caller resolves what the file says before comparing it — the variable
#: substitution the loader would do. The identity by default.
Rendered = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class _Edit:
    """A span of the source, and what goes there instead."""

    start: int
    end: int
    text: str


def merge_into(
    source: str,
    wanted: Mapping[str, object],
    *,
    rendered: Rendered = str,
) -> str:
    """The file, edited until reading it gives `wanted` — and not edited further.

    Raises `ValueError` if the file isn't a YAML mapping, which is the only
    shape a spec has.
    """
    node = yaml.compose(source)
    if node is None:
        return yaml.safe_dump(dict(wanted), sort_keys=False, width=100)
    if not isinstance(node, MappingNode):
        raise ValueError("a spec file is a mapping of keys to values")
    edits: list[_Edit] = []
    _mapping(source, node, wanted, edits, rendered)
    return _applied(source, edits)


def _applied(source: str, edits: Sequence[_Edit]) -> str:
    """Every edit, from the end of the file backwards so the spans stay valid."""
    text = source
    for edit in sorted(edits, key=lambda one: one.start, reverse=True):
        text = text[: edit.start] + edit.text + text[edit.end :]
    return text


# ---------------------------------------------------------------------------
# walking the two together
# ---------------------------------------------------------------------------


def _mapping(
    source: str,
    node: MappingNode,
    wanted: Mapping[str, object],
    edits: list[_Edit],
    rendered: Rendered,
) -> None:
    present = {str(key.value): (key, value) for key, value in node.value}
    for key, value in wanted.items():
        if key in present:
            _value(source, present[key][1], value, edits, rendered)
        else:
            edits.append(_added_key(source, node, key, value, wanted, present))
    for key, (key_node, value_node) in present.items():
        if key not in wanted:
            edits.append(_removed(source, node, key_node, value_node))


def _value(
    source: str,
    node: Node,
    wanted: object,
    edits: list[_Edit],
    rendered: Rendered,
) -> None:
    if _plain(node, rendered) == wanted:
        return  # the file already says this, however it spells it
    if isinstance(wanted, Mapping) and isinstance(node, MappingNode):
        _mapping(source, node, wanted, edits, rendered)
        return
    if (
        isinstance(wanted, Sequence)
        and not isinstance(wanted, str)
        and isinstance(node, SequenceNode)
    ):
        _sequence(source, node, list(wanted), edits, rendered)
        return
    edits.append(_replacement(source, node, wanted))


def _sequence(
    source: str,
    node: SequenceNode,
    wanted: list[object],
    edits: list[_Edit],
    rendered: Rendered,
) -> None:
    """Items with a `name` are matched by it; anything else is replaced whole."""
    named = [one for one in wanted if isinstance(one, Mapping) and "name" in one]
    items = [item for item in node.value if isinstance(item, MappingNode)]
    if len(named) != len(wanted) or len(items) != len(node.value):
        # Nothing to match on, or a list that mixes shapes: it is one value.
        edits.append(_replacement(source, node, wanted))
        return
    present: dict[str, MappingNode] = {}
    for item in items:
        for key, value in item.value:
            if str(key.value) == "name" and isinstance(value, ScalarNode):
                present[rendered(value.value)] = item
    for one in named:
        name = str(one["name"])
        if name in present:
            _mapping(source, present[name], one, edits, rendered)
        else:
            edits.append(_added_item(source, node, one))
    keeping = {str(one["name"]) for one in named}
    for name, item in present.items():
        if name not in keeping:
            edits.append(_removed_item(source, node, item))


def _plain(node: Node, rendered: Rendered) -> object:
    """What reading this part of the file gives, as plain Python.

    So that a subtree the file already holds is recognised whatever its
    spelling — quoted or bare, flow or block, `${catalog}` or the name it
    stands for — and left alone.
    """
    if isinstance(node, MappingNode):
        return {str(key.value): _plain(value, rendered) for key, value in node.value}
    if isinstance(node, SequenceNode):
        return [_plain(item, rendered) for item in node.value]
    if node.tag == "tag:yaml.org,2002:str":
        return rendered(node.value)
    try:
        return yaml.safe_load(node.value)
    except yaml.YAMLError:  # pragma: no cover - a scalar YAML can't read back
        return node.value


# ---------------------------------------------------------------------------
# writing into the text
# ---------------------------------------------------------------------------


def _replacement(source: str, node: Node, wanted: object) -> _Edit:
    """A value the file spells differently, written the way the file writes it.

    A block collection is replaced with a block, indented to sit where the old
    one sat; a `|` block — a view's query, a function's body — stays a `|`
    block; anything else goes on its line.
    """
    if (
        isinstance(node, ScalarNode)
        and node.style in {"|", ">"}
        and isinstance(wanted, str)
    ):
        return _block_scalar(source, node, wanted)
    if isinstance(node, MappingNode | SequenceNode) and not node.flow_style:
        written = _block(wanted, node.start_mark.column).lstrip(" ").rstrip("\n")
        return _Edit(node.start_mark.index, _last(node), written)
    return _Edit(node.start_mark.index, node.end_mark.index, _inline(wanted))


def _block_scalar(source: str, node: ScalarNode, wanted: str) -> _Edit:
    """A `|` block, rewritten at the indentation the old one used.

    SQL is read by people; a query that arrived as a block goes back as one
    rather than as a quoted line with `\\n` in it.
    """
    end = _last(node)
    newline = source.find("\n", node.start_mark.index)
    indent = node.start_mark.column + 2
    if 0 <= newline < end:
        line = source[newline + 1 : end]
        found = len(line) - len(line.lstrip(" "))
        indent = found or indent
    pad = " " * indent
    body = "".join(f"{pad}{one}\n" for one in wanted.strip("\n").splitlines())
    return _Edit(node.start_mark.index, end, f"{node.style}\n{body}".rstrip("\n"))


def _inline(value: object) -> str:
    """One value, as YAML writes it on the line it is on.

    Dumped as a mapping value and then unprefixed, so the quoting is the one a
    block file uses: `Order facts, per region` stays bare, where dumping it
    inside a flow list would quote it for the comma's sake.
    """
    dumped = yaml.safe_dump(
        {"value": _dumpable(value)},
        default_flow_style=not isinstance(value, str),
        sort_keys=False,
        width=1 << 20,
    ).strip()
    if dumped.startswith("{"):
        dumped = dumped.removeprefix("{").removesuffix("}").strip()
    return dumped.removeprefix("value:").strip()


def _block(value: object, indent: int) -> str:
    """One value, as YAML writes it over lines, indented to sit where it goes."""
    dumped = yaml.safe_dump(
        _dumpable(value), sort_keys=False, default_flow_style=False, width=100
    )
    pad = " " * indent
    return "".join(f"{pad}{line}\n" for line in dumped.rstrip("\n").splitlines())


def _dumpable(value: object) -> object:
    """The same value in plain types.

    A caller's document may carry a `str` subclass that means something to its
    own writer — the loader marks a query as one, to write it as a `|` block.
    YAML's safe dumper refuses what it doesn't know, and this is not the place
    that decides how a query is written.
    """
    if isinstance(value, str):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _dumpable(one) for key, one in value.items()}
    if isinstance(value, Sequence):
        return [_dumpable(one) for one in value]
    return value


def _added_key(
    source: str,
    node: MappingNode,
    key: str,
    value: object,
    wanted: Mapping[str, object],
    present: Mapping[str, tuple[Node, Node]],
) -> _Edit:
    """A key the file hasn't got, written where the document has it.

    After the nearest key that comes before it in the document and is in the
    file already — so a new `comment` lands under `table:` rather than at the
    bottom, and the diff is one line where it belongs.
    """
    if node.flow_style:
        close = source.rindex("}", node.start_mark.index, node.end_mark.index)
        separator = "" if not node.value else ", "
        return _Edit(close, close, f"{separator}{key}: {_inline(value)}")
    indent = node.value[0][0].start_mark.column if node.value else 0
    anchor = _anchor(wanted, present, key)
    at, prefix = _after_line(source, _last(anchor if anchor is not None else node))
    return _Edit(at, at, prefix + _block({key: value}, indent))


def _anchor(
    wanted: Mapping[str, object],
    present: Mapping[str, tuple[Node, Node]],
    key: str,
) -> Node | None:
    """The value node a new key goes after: the last one before it in both."""
    before: Node | None = None
    for candidate in wanted:
        if candidate == key:
            break
        if candidate in present:
            before = present[candidate][1]
    return before


def _added_item(source: str, node: SequenceNode, value: Mapping[str, object]) -> _Edit:
    """An item the list hasn't got, written after the last one it has."""
    if node.flow_style:
        close = source.rindex("]", node.start_mark.index, node.end_mark.index)
        separator = "" if not node.value else ", "
        return _Edit(close, close, f"{separator}{_inline(dict(value))}")
    # A block sequence's own start mark is its first `-`, which is the column
    # every item of it sits at.
    indent = node.start_mark.column
    last = node.value[-1] if node.value else None
    flow = isinstance(last, MappingNode) and last.flow_style
    at, prefix = _after_line(source, _last(node))
    written = (
        f"{' ' * indent}- {_inline(dict(value))}\n"
        if flow
        else _block([dict(value)], indent)
    )
    return _Edit(at, at, prefix + written)


def _last(node: Node) -> int:
    """Where the text of this node really ends.

    A block collection's own end mark is where the *next* token starts — the
    line after it, or the key that follows — so the end of a collection is the
    end of its last leaf, found by looking.
    """
    if isinstance(node, MappingNode | SequenceNode) and node.flow_style:
        # A flow collection ends at its own bracket, which is the mark.
        return node.end_mark.index
    if isinstance(node, MappingNode):
        return max(
            (max(_last(key), _last(value)) for key, value in node.value),
            default=node.end_mark.index,
        )
    if isinstance(node, SequenceNode):
        return max((_last(item) for item in node.value), default=node.end_mark.index)
    return node.end_mark.index


def _removed(source: str, node: MappingNode, key_node: Node, value_node: Node) -> _Edit:
    """A key the file has and the document hasn't."""
    if node.flow_style:
        start = key_node.start_mark.index
        end = value_node.end_mark.index
        # Take the comma with it, whichever side it is on.
        while end < node.end_mark.index and source[end] in ", ":
            end += 1
        while start > node.start_mark.index and source[start - 1] in ", ":
            start -= 1
            if source[start] == ",":
                break
        return _Edit(start, end, "")
    return _Edit(
        _line_start(source, key_node.start_mark.index),
        _line_end(source, _last(value_node)),
        "",
    )


def _removed_item(source: str, node: SequenceNode, item: MappingNode) -> _Edit:
    if node.flow_style:
        start, end = item.start_mark.index, item.end_mark.index
        while end < node.end_mark.index and source[end] in ", ":
            end += 1
        return _Edit(start, end, "")
    # A block item starts at its `-`, which sits before the mapping's first key.
    start = _line_start(source, item.start_mark.index)
    dash = source.rindex("-", start, item.start_mark.index + 1)
    return _Edit(_line_start(source, dash), _line_end(source, _last(item)), "")


def _line_start(source: str, index: int) -> int:
    return source.rfind("\n", 0, index) + 1


def _after_line(source: str, index: int) -> tuple[int, str]:
    """Where to write a new line, and what to write before it.

    A file that doesn't end in a newline needs one before anything is added to
    it, or the addition lands on the end of the last line.
    """
    at = _line_end(source, index)
    return at, "" if at < len(source) or source.endswith("\n") else "\n"


def _line_end(source: str, index: int) -> int:
    """Just past the newline the value's last line ends with.

    A node's end mark stops at its last character, so a trailing comment on that
    line stays with the value it comments on.
    """
    found = source.find("\n", index)
    return len(source) if found == -1 else found + 1
