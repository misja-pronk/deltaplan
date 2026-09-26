"""Editing a YAML file instead of rewriting it.

The point of `yamledit` is what it *doesn't* do: a file comes back with the same
comments, the same blank lines, the same quoting and the same `${catalog}` it
went in with, and only what differs from the wanted document is touched. These
hold it to that — most of them by asserting the whole text, because "nothing
else changed" is the assertion.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from deltaplan.yamledit import merge_into

SPEC = """\
# Orders, from the ingest pipeline
table: ${catalog}.sales.orders
comment: Order facts

columns:
  - name: id            # the surrogate key
    type: bigint
    nullable: false
  - {name: placed, type: date}
tags:
  domain: sales
"""

#: What reading SPEC gives, for a target whose catalog is `dev`.
AS_READ: dict[str, Any] = {
    "table": "dev.sales.orders",
    "comment": "Order facts",
    "columns": [
        {"name": "id", "type": "bigint", "nullable": False},
        {"name": "placed", "type": "date"},
    ],
    "tags": {"domain": "sales"},
}


def rendered(text: str) -> str:
    return text.replace("${catalog}", "dev")


def merged(wanted: Mapping[str, Any], source: str = SPEC) -> str:
    once = merge_into(source, wanted, rendered=rendered)
    twice = merge_into(once, wanted, rendered=rendered)
    assert twice == once, "editing a file that already says it changes it again"
    return once


def test_a_file_that_already_says_it_is_not_touched() -> None:
    assert merged(AS_READ) == SPEC


def test_a_variable_survives_the_name_it_stands_for() -> None:
    """The file says `${catalog}`; the document says what that resolves to. They
    are the same thing, and the file keeps its way of saying it."""
    assert "${catalog}" in merged({**AS_READ, "comment": "Orders"})


def test_only_the_value_that_differs_is_written() -> None:
    after = merged({**AS_READ, "comment": "Orders, per region"})
    assert after == SPEC.replace("comment: Order facts", "comment: Orders, per region")
    assert "# the surrogate key" in after, "a comment on another line is untouched"


def test_a_key_is_added_where_the_document_has_it() -> None:
    """Not at the bottom: a new `owner` belongs under the keys it follows."""
    wanted = {
        "table": "dev.sales.orders",
        "comment": "Order facts",
        "owner": "crm@example.com",
        **{key: AS_READ[key] for key in ("columns", "tags")},
    }
    after = merged(wanted)
    assert after == SPEC.replace(
        "comment: Order facts\n",
        "comment: Order facts\nowner: crm@example.com\n",
    )


def test_a_key_the_document_hasnt_got_goes_with_its_line() -> None:
    after = merged({key: value for key, value in AS_READ.items() if key != "comment"})
    assert after == SPEC.replace("comment: Order facts\n", "")


def test_a_list_item_is_matched_by_name_and_edited_in_place() -> None:
    wanted = {
        **AS_READ,
        "columns": [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "placed", "type": "timestamp"},
        ],
    }
    assert merged(wanted) == SPEC.replace(
        "{name: placed, type: date}", "{name: placed, type: timestamp}"
    )


def test_a_new_column_is_written_the_way_the_last_one_is() -> None:
    """The file's own style, flow or block — a spec someone wrote by hand keeps
    reading like one."""
    wanted = {
        **AS_READ,
        "columns": [
            *AS_READ["columns"],
            {"name": "region", "type": "string", "comment": "ISO code"},
        ],
    }
    after = merged(wanted)
    assert "  - {name: region, type: string, comment: ISO code}\n" in after
    assert after.startswith("# Orders, from the ingest pipeline\n")

    block = SPEC.replace(
        "  - {name: placed, type: date}\n", "  - name: placed\n    type: date\n"
    )
    after = merged(wanted, block)
    assert "  - name: region\n    type: string\n    comment: ISO code\n" in after


def test_a_column_the_document_hasnt_got_goes_whole() -> None:
    wanted = {**AS_READ, "columns": [{"name": "placed", "type": "date"}]}
    after = merged(wanted)
    assert "id" not in after.replace("ingest", "")
    assert "# the surrogate key" not in after, "its comment went with it"
    assert "domain: sales" in after


def test_a_key_is_added_inside_a_flow_mapping() -> None:
    wanted = {
        **AS_READ,
        "columns": [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "placed", "type": "date", "comment": "when it was placed"},
        ],
    }
    assert "{name: placed, type: date, comment: when it was placed}" in merged(wanted)


def test_a_key_is_removed_from_a_flow_mapping() -> None:
    source = SPEC.replace(
        "{name: placed, type: date}", "{name: placed, type: date, comment: gone}"
    )
    assert "{name: placed, type: date}" in merged(AS_READ, source)


def test_a_list_of_plain_values_is_replaced_whole() -> None:
    source = SPEC.replace("tags:\n  domain: sales\n", "cluster_by: [placed, id]\n")
    wanted = {key: value for key, value in AS_READ.items() if key != "tags"} | {
        "cluster_by": ["placed"]
    }
    assert "cluster_by: [placed]" in merged(wanted, source)


def test_a_block_list_is_replaced_as_a_block() -> None:
    source = SPEC.replace("tags:\n  domain: sales\n", "cluster_by:\n  - placed\n  - id\n")
    wanted = {key: value for key, value in AS_READ.items() if key != "tags"} | {
        "cluster_by": ["placed"]
    }
    after = merged(wanted, source)
    assert after.endswith("cluster_by:\n  - placed\n")


def test_a_file_without_a_last_newline_gets_one() -> None:
    after = merge_into("table: main.t", {"table": "main.t", "comment": "hi"})
    assert after == "table: main.t\ncomment: hi\n"


def test_an_empty_file_is_written_from_the_document() -> None:
    after = merge_into("", {"table": "main.t", "comment": "hi"})
    assert after == "table: main.t\ncomment: hi\n"


def test_a_spec_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(ValueError, match="mapping"):
        merge_into("- one\n- two\n", {"table": "main.t"})


def test_the_files_own_spelling_of_a_value_is_left_alone() -> None:
    """`'bigint'`, `yes`, `9` — a value that reads the same is the same."""
    source = "table: main.t\ncolumns:\n  - {name: id, type: 'bigint', nullable: no}\n"
    wanted = {
        "table": "main.t",
        "columns": [{"name": "id", "type": "bigint", "nullable": False}],
    }
    assert merge_into(source, wanted) == source
