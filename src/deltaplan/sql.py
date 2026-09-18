"""Identifier and literal quoting.

Every identifier that reaches a SQL statement goes through `quote_ident()` — there
is no other way to put a name into SQL in this codebase.

Databricks quotes identifiers with backticks and escapes an embedded backtick by
doubling it; string literals use single quotes, escaped by doubling.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-identifiers
"""

import re

# Unquoted identifiers Databricks accepts as-is, and which we therefore leave
# bare inside type strings for readability (never inside statements).
_BARE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def quote_ident(name: str) -> str:
    """Backtick-quote a single identifier. Always quotes — never conditionally."""
    if not name:
        raise ValueError("identifier cannot be empty")
    return "`" + name.replace("`", "``") + "`"


def quote_qualified(name: str) -> str:
    """Quote a dotted name (`catalog.schema.table`) part by part.

    A part that already carries backticks is taken as pre-quoted and left alone, so
    a spec may write `` cat.`odd.name` `` for a name that contains a dot.
    """
    if not name:
        raise ValueError("qualified name cannot be empty")
    parts = _split_qualified(name)
    return ".".join(quote_ident(part) for part in parts)


def _split_qualified(name: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    index = 0
    while index < len(name):
        char = name[index]
        if char == "`":
            if in_quotes and index + 1 < len(name) and name[index + 1] == "`":
                current.append("`")
                index += 2
                continue
            in_quotes = not in_quotes
        elif char == "." and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    if in_quotes:
        raise ValueError(f"unbalanced backticks in name: {name!r}")
    if any(not part for part in parts):
        raise ValueError(f"empty part in qualified name: {name!r}")
    return parts


def maybe_quote_ident(name: str) -> str:
    """Quote only when needed. For *type strings*, where readability matters.

    Statements never use this — they use `quote_ident()`. A rendered type string is
    compared and round-tripped, so gratuitous backticks would be noise.
    """
    if _BARE_IDENT.fullmatch(name):
        return name
    return quote_ident(name)


def quote_literal(value: str) -> str:
    """Single-quote a string literal, doubling embedded quotes."""
    return "'" + value.replace("'", "''") + "'"
