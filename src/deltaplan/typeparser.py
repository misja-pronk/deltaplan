"""Parse Databricks type strings into the type tree.

The grammar is the one Spark's DDL parser accepts, which is also what
`information_schema.columns.full_data_type` hands back:

    type    := name | name '(' int [, int] ')' | array '<' type '>'
             | map '<' type ',' type '>' | struct '<' [field {',' field}] '>'
    field   := ident ':' type ['not' 'null'] ['comment' string]
    ident   := bare | '`' backtick-escaped '`'

Everything is case-insensitive; identifiers and comments keep their case.
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-datatypes
"""

from __future__ import annotations

from dataclasses import dataclass

from deltaplan.model.types import (
    Array,
    Char,
    DataType,
    Decimal,
    Field,
    Map,
    Primitive,
    Struct,
    Varchar,
)


class TypeParseError(ValueError):
    """A type string that isn't one."""

    def __init__(self, message: str, text: str, position: int) -> None:
        self.text = text
        self.position = position
        super().__init__(f"{message}\n  {text}\n  {' ' * position}^")


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str  # "ident" | "number" | "string" | "punct"
    value: str
    position: int


_PUNCT = frozenset("<>,:()")


def _tokenise(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
        elif char in _PUNCT:
            tokens.append(_Token("punct", char, index))
            index += 1
        elif char == "`":
            start = index
            value, index = _read_quoted(text, index, "`")
            tokens.append(_Token("ident", value, start))
        elif char in "'\"":
            start = index
            value, index = _read_string(text, index, char)
            tokens.append(_Token("string", value, start))
        elif char.isdigit():
            start = index
            while index < len(text) and text[index].isdigit():
                index += 1
            tokens.append(_Token("number", text[start:index], start))
        elif char.isalpha() or char == "_":
            start = index
            while index < len(text) and (text[index].isalnum() or text[index] == "_"):
                index += 1
            tokens.append(_Token("ident", text[start:index], start))
        else:
            raise TypeParseError(f"unexpected character {char!r}", text, index)
    return tokens


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}


def _read_string(text: str, start: int, quote: str) -> tuple[str, int]:
    """Read a string literal. Databricks escapes with a backslash (`'it\\'s'`),
    and that is how it writes comments back; a doubled quote is read as one
    quote too, as specs written that way have always been."""
    index = start + 1
    chunks: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            chunks.append(_ESCAPES.get(following, following))
            index += 2
            continue
        if char == quote:
            if index + 1 < len(text) and text[index + 1] == quote:
                chunks.append(quote)
                index += 2
                continue
            return "".join(chunks), index + 1
        chunks.append(char)
        index += 1
    raise TypeParseError(f"unterminated {quote}", text, start)


def _read_quoted(text: str, start: int, quote: str) -> tuple[str, int]:
    """Read a quoted run, where the quote character is escaped by doubling it."""
    index = start + 1
    chunks: list[str] = []
    while index < len(text):
        char = text[index]
        if char == quote:
            if index + 1 < len(text) and text[index + 1] == quote:
                chunks.append(quote)
                index += 2
                continue
            return "".join(chunks), index + 1
        chunks.append(char)
        index += 1
    raise TypeParseError(f"unterminated {quote}", text, start)


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokenise(text)
        self.index = 0

    # -- token helpers -----------------------------------------------------
    def peek(self) -> _Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> _Token:
        token = self.peek()
        if token is None:
            raise TypeParseError("unexpected end of type", self.text, len(self.text))
        self.index += 1
        return token

    def at_punct(self, value: str) -> bool:
        token = self.peek()
        return token is not None and token.kind == "punct" and token.value == value

    def at_keyword(self, word: str) -> bool:
        token = self.peek()
        return token is not None and token.kind == "ident" and token.value.lower() == word

    def expect_punct(self, value: str) -> _Token:
        token = self.next()
        if token.kind != "punct" or token.value != value:
            raise TypeParseError(
                f"expected {value!r}, found {token.value!r}", self.text, token.position
            )
        return token

    def expect_number(self) -> int:
        token = self.next()
        if token.kind != "number":
            raise TypeParseError(
                f"expected a number, found {token.value!r}", self.text, token.position
            )
        return int(token.value)

    # -- grammar -----------------------------------------------------------
    def parse(self) -> DataType:
        parsed = self.parse_type()
        trailing = self.peek()
        if trailing is not None:
            raise TypeParseError(
                f"unexpected trailing {trailing.value!r}", self.text, trailing.position
            )
        return parsed

    def parse_type(self) -> DataType:
        token = self.next()
        if token.kind != "ident":
            raise TypeParseError(
                f"expected a type name, found {token.value!r}", self.text, token.position
            )
        name = token.value.lower()
        match name:
            case "array":
                self.expect_punct("<")
                element = self.parse_type()
                self.expect_punct(">")
                return Array(element)
            case "map":
                self.expect_punct("<")
                key = self.parse_type()
                self.expect_punct(",")
                value = self.parse_type()
                self.expect_punct(">")
                return Map(key, value)
            case "struct":
                return self.parse_struct()
            case "decimal" | "dec" | "numeric":
                precision, scale = self.parse_decimal_args(token.position)
                return Decimal(precision, scale)
            case "char" | "varchar":
                length = self.parse_length(name, token.position)
                return Char(length) if name == "char" else Varchar(length)
            case _:
                if self.at_punct("("):
                    raise TypeParseError(
                        f"type {name!r} takes no parameters", self.text, token.position
                    )
                return Primitive(name)

    def parse_struct(self) -> Struct:
        self.expect_punct("<")
        fields: list[Field] = []
        if self.at_punct(">"):
            self.next()
            return Struct(())
        while True:
            fields.append(self.parse_field())
            if self.at_punct(","):
                self.next()
                continue
            self.expect_punct(">")
            return Struct(tuple(fields))

    def parse_field(self) -> Field:
        token = self.next()
        if token.kind != "ident":
            raise TypeParseError(
                f"expected a field name, found {token.value!r}",
                self.text,
                token.position,
            )
        self.expect_punct(":")
        field_type = self.parse_type()
        nullable = True
        comment: str | None = None
        if self.at_keyword("not"):
            self.next()
            if not self.at_keyword("null"):
                found = self.peek()
                raise TypeParseError(
                    "expected 'null' after 'not'",
                    self.text,
                    found.position if found else len(self.text),
                )
            self.next()
            nullable = False
        if self.at_keyword("comment"):
            self.next()
            literal = self.next()
            if literal.kind != "string":
                raise TypeParseError(
                    f"expected a quoted comment, found {literal.value!r}",
                    self.text,
                    literal.position,
                )
            comment = literal.value
        return Field(token.value, field_type, nullable=nullable, comment=comment)

    def parse_decimal_args(self, position: int) -> tuple[int, int]:
        if not self.at_punct("("):
            # Bare `decimal` is decimal(10, 0) in Databricks.
            return 10, 0
        self.next()
        precision = self.expect_number()
        scale = 0
        if self.at_punct(","):
            self.next()
            scale = self.expect_number()
        self.expect_punct(")")
        if not 1 <= precision <= 38:
            raise TypeParseError("decimal precision must be 1..38", self.text, position)
        if not 0 <= scale <= precision:
            raise TypeParseError(
                "decimal scale must be 0..precision", self.text, position
            )
        return precision, scale

    def parse_length(self, name: str, position: int) -> int:
        if not self.at_punct("("):
            raise TypeParseError(f"{name} needs a length", self.text, position)
        self.next()
        length = self.expect_number()
        self.expect_punct(")")
        if length < 1:
            raise TypeParseError(f"{name} length must be positive", self.text, position)
        return length


def parse_type(text: str) -> DataType:
    """Parse a Databricks type string. Raises `TypeParseError` on anything else."""
    if not text.strip():
        raise TypeParseError("empty type", text, 0)
    return _Parser(text).parse()
