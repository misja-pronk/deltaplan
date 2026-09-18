"""Reading Databricks' own `SHOW CREATE TABLE` output.

Some of a column's definition is nowhere else. On a live workspace (verified
2026-09-18) `information_schema.columns` reports no identity, generation
expression or default — `is_identity` and `is_generated` say NO for columns that
are both — and `full_data_type` drops NOT NULL and comments inside structs;
`DESCRIBE TABLE` does too. `SHOW CREATE TABLE` has all of it, so introspection
reads the columns' details from there, parsed with sqlglot.

Before parsing, three things are cut from the statement, all verified in live
output: `COLLATE UTF8_BINARY`, which Databricks writes after every string,
top-level and nested — the default, so it carries nothing (any other collation
is reported, since deltaplan doesn't model collations); and a column's
`MASK … USING COLUMNS(…)` and the table's `WITH ROW FILTER … ON (…)`, which
sqlglot can't parse and introspection reads from information_schema anyway.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-aux-show-create-table
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

from deltaplan.model.types import DataType, Identity
from deltaplan.typeparser import TypeParseError, parse_type

DIALECT = "databricks"
DEFAULT_COLLATION = "UTF8_BINARY"


class DdlError(Exception):
    """sqlglot couldn't read a `SHOW CREATE TABLE` statement."""


@dataclass(frozen=True, slots=True)
class DdlColumn:
    """What `SHOW CREATE TABLE` says about one top-level column."""

    #: The full type, nested NOT NULL and comments included; None when it
    #: couldn't be parsed (a nested non-default collation).
    type: DataType | None
    identity: Identity | None = None
    generated: str | None = None
    default: str | None = None
    #: A collation other than the default — not modelled, so reported.
    collation: str | None = None


def read_columns(ddl: str) -> dict[str, DdlColumn]:
    """The columns of a `CREATE TABLE` statement, by name."""
    text = _without_noise(ddl)
    logger = logging.getLogger("sqlglot")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        tree = sqlglot.parse_one(text, read=DIALECT)
    except (ParseError, TokenError) as error:
        raise DdlError(str(error).splitlines()[0]) from error
    finally:
        logger.setLevel(level)
    schema = tree.this if isinstance(tree, exp.Create) else None
    if not isinstance(schema, exp.Schema):
        raise DdlError("not a CREATE TABLE with a column list")

    columns: dict[str, DdlColumn] = {}
    for definition in schema.expressions:
        if not isinstance(definition, exp.ColumnDef):
            continue
        kind = definition.args.get("kind")
        data_type: DataType | None = None
        if kind is not None:
            try:
                data_type = parse_type(kind.sql(dialect=DIALECT))
            except TypeParseError:
                data_type = None
        identity: Identity | None = None
        generated = default = collation = None
        for constraint in definition.args.get("constraints") or []:
            spec = constraint.args.get("kind")
            if isinstance(spec, exp.GeneratedAsIdentityColumnConstraint):
                identity = Identity(
                    always=bool(spec.this),
                    start=_integer(spec.args.get("start"), 1),
                    increment=_integer(spec.args.get("increment"), 1),
                )
            elif isinstance(spec, exp.ComputedColumnConstraint):
                generated = spec.this.sql(dialect=DIALECT)
            elif isinstance(spec, exp.DefaultColumnConstraint):
                default = spec.this.sql(dialect=DIALECT)
            elif isinstance(spec, exp.CollateColumnConstraint):
                collation = spec.this.sql(dialect=DIALECT) if spec.this else None
        if data_type is None and collation is None:
            # Unparseable only because of a collation inside a struct.
            collation = "a non-default collation on a nested field"
        columns[definition.name] = DdlColumn(
            data_type, identity, generated, default, collation
        )
    return columns


def _without_noise(ddl: str) -> str:
    """Cut the default collation, masks and the row filter — by token, so a
    string literal that happens to contain the words is left alone."""
    try:
        tokens = sqlglot.tokenize(ddl, read=DIALECT)
    except TokenError as error:
        raise DdlError(str(error).splitlines()[0]) from error
    cuts: list[tuple[int, int]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        after = tokens[index + 1 : index + 3]
        if (
            token.token_type == TokenType.COLLATE
            and after
            and after[0].text.upper() == DEFAULT_COLLATION
        ):
            end = index + 2
        elif token.token_type == TokenType.VAR and token.text.upper() == "MASK":
            end = _past_name(tokens, index + 1)
            if (
                end + 1 < len(tokens)
                and tokens[end].token_type == TokenType.USING
                and tokens[end + 1].text.upper() == "COLUMNS"
            ):
                end = _past_parens(tokens, end + 2)
        elif token.token_type == TokenType.WITH and [t.text.upper() for t in after] == [
            "ROW",
            "FILTER",
        ]:
            end = _past_name(tokens, index + 3)
            if end < len(tokens) and tokens[end].token_type == TokenType.ON:
                end = _past_parens(tokens, end + 1)
        else:
            index += 1
            continue
        cuts.append((token.start, tokens[end - 1].end + 1))
        index = end
    for start, stop in reversed(cuts):
        ddl = ddl[:start] + ddl[stop:]
    return ddl


def _past_name(tokens: list, index: int) -> int:
    """The index just past a dotted name starting at `index`."""
    while index < len(tokens) and tokens[index].token_type in (
        TokenType.VAR,
        TokenType.IDENTIFIER,
    ):
        index += 1
        if index < len(tokens) and tokens[index].token_type == TokenType.DOT:
            index += 1
        else:
            break
    return index


def _past_parens(tokens: list, index: int) -> int:
    """The index just past a parenthesised group starting at `index`."""
    if index >= len(tokens) or tokens[index].token_type != TokenType.L_PAREN:
        return index
    depth = 0
    while index < len(tokens):
        if tokens[index].token_type == TokenType.L_PAREN:
            depth += 1
        elif tokens[index].token_type == TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return index


def _integer(node: exp.Expression | None, fallback: int) -> int:
    if node is None:
        return fallback
    return int(node.this if isinstance(node, exp.Literal) else node.sql(dialect=DIALECT))
