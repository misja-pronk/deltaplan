"""Identifier and literal quoting.

Every identifier that reaches a SQL statement goes through `quote_ident()` — there
is no other way to put a name into SQL in this codebase.

Databricks quotes identifiers with backticks and escapes an embedded backtick by
doubling it; string literals use single quotes, escaped by doubling.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-identifiers
"""

import functools
import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

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
    """Single-quote a string literal, escaping with backslashes.

    Not by doubling quotes: Databricks reads `'It''s'` as two adjacent literals
    and joins them into `Its` — verified live. `'It\\'s'` is `It's`.
    https://docs.databricks.com/aws/en/sql/language-manual/data-types/string-type
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@functools.lru_cache(maxsize=4096)
def normalise_expression(expression: str) -> str:
    """An expression in one canonical spelling, so both sides of a diff compare
    by what they say rather than how they were typed.

    The catalog echoes expressions back its own way — a CHECK wrapped in
    parentheses, a generation as `( CAST(placed_at AS DATE) )` (verified live).
    So the expression is parsed with sqlglot and written back in its canonical
    form, with unquoted identifiers in lower case since Databricks ignores their
    case: `cast(Placed_At as date)` and `( CAST(placed_at AS DATE) )` are the
    same expression. String literals keep their case. When sqlglot can't parse
    it, whitespace runs are collapsed and outer parentheses stripped instead.
    """
    text = _strip_outer(" ".join(expression.split()))
    logger = logging.getLogger("sqlglot")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        parsed = sqlglot.parse_one(text, read="databricks")
    except (ParseError, TokenError):
        return text
    finally:
        logger.setLevel(level)
    if parsed is None or isinstance(parsed, exp.Command):
        return text
    canonical = normalize_identifiers(parsed, dialect="databricks")
    return _strip_outer(canonical.sql(dialect="databricks"))


#: Characters Delta refuses in a column name unless the table has name-based
#: column mapping — the list its error gives (DELTA_INVALID_CHARACTERS_IN_COLUMN_NAMES,
#: seen live 2026-09-19).
MAPPING_ONLY_NAME_CHARS = frozenset(" ,;{}()\n\t=")


def needs_name_mapping(name: str) -> bool:
    """Whether a column or field name can only exist under column mapping."""
    return any(char in MAPPING_ONLY_NAME_CHARS for char in name)


@functools.lru_cache(maxsize=4096)
def referenced_columns(expression: str) -> frozenset[str]:
    """The top-level column names an expression uses, lower-cased.

    Parsed with sqlglot. When it can't be parsed, every word counts — erring
    towards "this might depend on that" is the safe direction: at worst a CHECK
    is dropped and put back that didn't need to be.
    """
    logger = logging.getLogger("sqlglot")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        parsed = sqlglot.parse_one(expression, read="databricks")
    except (ParseError, TokenError):
        parsed = None
    finally:
        logger.setLevel(level)
    if parsed is None or isinstance(parsed, exp.Command):
        quoted = re.findall(r"`([^`]+)`", expression)
        bare = re.findall(r"\w+", re.sub(r"`[^`]*`", " ", expression))
        return frozenset(word.lower() for word in (*quoted, *bare))
    # `shipping.zip` parses as table `shipping`, column `zip`; in a table's own
    # expression it is the field zip of the column shipping — the first part.
    return frozenset(
        column.parts[0].name.lower() for column in parsed.find_all(exp.Column)
    )


def _strip_outer(text: str) -> str:
    while text.startswith("(") and text.endswith(")") and _outer_parens_wrap(text):
        text = text[1:-1].strip()
    return text


def _outer_parens_wrap(text: str) -> bool:
    """Do the first and last parentheses pair with each other?

    `(a > 0) AND (b > 0)` starts and ends with a parenthesis but is not wrapped
    in one. (Parentheses inside string literals are not accounted for.)
    """
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index == len(text) - 1
    return False


#: Table privileges in Unity Catalog. They are keywords, so they can't be quoted
#: like identifiers — which is why a privilege is checked against this list
#: before it can reach a statement.
#: https://docs.databricks.com/aws/en/data-governance/unity-catalog/manage-privileges/privileges
TABLE_PRIVILEGES: frozenset[str] = frozenset(
    {"ALL PRIVILEGES", "APPLY TAG", "MANAGE", "MODIFY", "SELECT"}
)
#: What can be granted on a function.
FUNCTION_PRIVILEGES: frozenset[str] = frozenset({"ALL PRIVILEGES", "EXECUTE", "MANAGE"})
#: What can be granted on a schema — each one granted on a live workspace
#: (2026-09-18). Refused there: CREATE VIEW (CREATE TABLE covers views), BROWSE
#: (a catalog's), EXTERNAL USE SCHEMA and bare CREATE.
SCHEMA_PRIVILEGES: frozenset[str] = frozenset(
    {
        "ALL PRIVILEGES",
        "APPLY TAG",
        "CREATE FUNCTION",
        "CREATE MATERIALIZED VIEW",
        "CREATE MODEL",
        "CREATE TABLE",
        "CREATE VOLUME",
        "EXECUTE",
        "MANAGE",
        "MODIFY",
        "READ VOLUME",
        "REFRESH",
        "SELECT",
        "USE SCHEMA",
        "WRITE VOLUME",
    }
)
#: Every privilege deltaplan will put in a statement. The loader checks each
#: grant against its object's own list; this is the last line, at SQL time.
#: What can be granted on a volume — verified live (2026-09-18); SELECT and
#: BROWSE are refused there.
VOLUME_PRIVILEGES: frozenset[str] = frozenset(
    {"ALL PRIVILEGES", "APPLY TAG", "MANAGE", "READ VOLUME", "WRITE VOLUME"}
)
KNOWN_PRIVILEGES: frozenset[str] = (
    TABLE_PRIVILEGES | FUNCTION_PRIVILEGES | SCHEMA_PRIVILEGES | VOLUME_PRIVILEGES
)


def normalise_privilege(privilege: str) -> str:
    """`select` -> `SELECT`, `all_privileges` -> `ALL PRIVILEGES`."""
    return " ".join(privilege.replace("_", " ").upper().split())


def privilege_sql(privilege: str, allowed: frozenset[str] = KNOWN_PRIVILEGES) -> str:
    """A privilege, fit for a GRANT or REVOKE. Anything not allowed is refused."""
    normalised = normalise_privilege(privilege)
    if normalised not in allowed:
        known = ", ".join(sorted(allowed))
        raise ValueError(f"unknown privilege {privilege!r} (known: {known})")
    return normalised


def same_principal(a: str, b: str) -> bool:
    """Unity Catalog stores a user's email lower-cased (verified live), so a
    principal is compared without regard to case."""
    return a.casefold() == b.casefold()
