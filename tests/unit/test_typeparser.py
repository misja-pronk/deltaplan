"""Databricks type strings parse, round-trip, and fail loudly.

Grammar reference:
https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-datatypes
"""

import pytest

from deltaplan.model.types import (
    Array,
    Char,
    Decimal,
    Field,
    Map,
    Primitive,
    Struct,
    Varchar,
    render_type,
    type_kind,
    walk,
)
from deltaplan.typeparser import TypeParseError, parse_type

ROUND_TRIPS = [
    "string",
    "bigint",
    "timestamp_ntz",
    "decimal(18,2)",
    "char(3)",
    "varchar(255)",
    "array<int>",
    "array<array<string>>",
    "map<string,int>",
    "map<string,array<struct<a:int>>>",
    "struct<street:string,zip:string>",
    "struct<a:int not null>",
    "struct<a:int comment 'hi'>",
    "struct<a:int not null comment 'hi'>",
    "struct<`first name`:string>",
    "struct<>",
    "array<struct<sku:string,quantity:int>>",
]


@pytest.mark.parametrize("text", ROUND_TRIPS)
def test_round_trips(text: str) -> None:
    assert render_type(parse_type(text)) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("STRING", "string"),
        ("Array<Int>", "array<int>"),
        ("STRUCT<A:INT>", "struct<A:int>"),  # names keep their case, keywords don't
        ("integer", "int"),
        ("long", "bigint"),
        ("short", "smallint"),
        ("byte", "tinyint"),
        ("real", "float"),
        ("dec(5,2)", "decimal(5,2)"),
        ("numeric(5,2)", "decimal(5,2)"),
        ("bool", "boolean"),
        ("decimal", "decimal(10,0)"),  # Databricks' default precision/scale
        ("decimal(5)", "decimal(5,0)"),
        ("struct< a : int , b : string >", "struct<a:int,b:string>"),
        ("struct<a:int\n  ,b:string>", "struct<a:int,b:string>"),
    ],
)
def test_normalises(text: str, expected: str) -> None:
    assert render_type(parse_type(text)) == expected


def test_backticks_are_escaped_by_doubling() -> None:
    parsed = parse_type("struct<`odd``name`:string>")
    assert parsed == Struct((Field("odd`name", Primitive("string")),))
    assert render_type(parsed) == "struct<`odd``name`:string>"


def test_comment_quotes_are_escaped_by_doubling() -> None:
    parsed = parse_type("struct<a:int comment 'it''s fine'>")
    assert parsed == Struct((Field("a", Primitive("int"), comment="it's fine"),))
    assert render_type(parsed) == "struct<a:int comment 'it''s fine'>"


def test_double_quoted_comments_are_accepted_and_normalised() -> None:
    assert (
        render_type(parse_type('struct<a:int comment "hi">'))
        == "struct<a:int comment 'hi'>"
    )


def test_nested_struct_tree() -> None:
    parsed = parse_type("struct<address:struct<street:string,zip:string>>")
    assert parsed == Struct(
        (
            Field(
                "address",
                Struct(
                    (
                        Field("street", Primitive("string")),
                        Field("zip", Primitive("string")),
                    )
                ),
            ),
        )
    )


def test_uppercase_rendering_leaves_names_and_comments_alone() -> None:
    parsed = parse_type("struct<zip:string comment 'Postal code'>")
    assert render_type(parsed, upper=True) == "STRUCT<zip:STRING COMMENT 'Postal code'>"


def test_unknown_primitives_parse_but_are_flagged() -> None:
    # Databricks keeps adding types; refusing to model a live table over one would
    # be worse than passing it through. `validate` warns instead.
    parsed = parse_type("geography")
    assert parsed == Primitive("geography")
    assert isinstance(parsed, Primitive) and not parsed.known
    assert Primitive("variant").known


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("string", "string"),
        ("decimal(5,2)", "decimal"),
        ("array<int>", "array"),
        ("map<string,int>", "map"),
        ("struct<a:int>", "struct"),
        ("char(2)", "char"),
        ("varchar(2)", "varchar"),
    ],
)
def test_type_kind(text: str, kind: str) -> None:
    assert type_kind(parse_type(text)) == kind


def test_walk_uses_databricks_nested_paths() -> None:
    # `a.b` in a struct, `a.element.b` in an array, `m.key` / `m.value` in a map.
    # https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table-manage-column
    parsed = parse_type(
        "struct<address:array<struct<zip:string>>,by_code:map<string,struct<n:int>>>"
    )
    assert [path for path, _ in walk(parsed)] == [
        "address",
        "address.element.zip",
        "by_code",
        "by_code.value.n",
    ]


def test_array_and_map_values() -> None:
    assert parse_type("array<int>") == Array(Primitive("int"))
    assert parse_type("map<string,int>") == Map(Primitive("string"), Primitive("int"))
    assert parse_type("char(3)") == Char(3)
    assert parse_type("varchar(3)") == Varchar(3)
    assert parse_type("decimal(9,3)") == Decimal(9, 3)


def test_renamed_from_does_not_affect_equality() -> None:
    # The hint says how the live table got here; it isn't part of the desired state.
    plain = Field("customer_ref", Primitive("string"))
    hinted = Field("customer_ref", Primitive("string"), renamed_from="cust_id")
    assert plain == hinted
    assert hash(plain) == hash(hinted)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty type"),
        ("   ", "empty type"),
        ("array<int", "unexpected end of type"),
        ("array<int>>", "unexpected trailing"),
        ("struct<a int>", "expected ':'"),
        ("struct<a:int not>", "expected 'null' after 'not'"),
        ("struct<a:int comment hi>", "expected a quoted comment"),
        ("string(3)", "takes no parameters"),
        ("decimal(39,2)", "precision must be 1..38"),
        ("decimal(5,9)", "scale must be 0..precision"),
        ("varchar", "varchar needs a length"),
        ("struct<`unclosed:string>", "unterminated `"),
        ("int$", "unexpected character"),
    ],
)
def test_parse_errors(text: str, message: str) -> None:
    with pytest.raises(TypeParseError, match=message.replace("`", "`")):
        parse_type(text)


def test_error_points_at_the_offending_character() -> None:
    with pytest.raises(TypeParseError) as raised:
        parse_type("struct<a:int, b:>")
    assert raised.value.position == 16
    assert "^" in str(raised.value)
