"""The fake warehouse's own guard rails.

The fake is only useful while it fails loudly on what a real warehouse would
reject. The first live run failed on a column `information_schema.column_masks`
doesn't have — a query the fake had answered without blinking. It now knows the
real column lists and refuses anything else.
"""

import pytest

from fake_warehouse import FakeSqlError, FakeWarehouse


def test_a_column_information_schema_does_not_have_is_refused() -> None:
    with pytest.raises(FakeSqlError, match="no column 'mask_catalog'"):
        FakeWarehouse().query(
            "SELECT table_name, mask_catalog "
            "FROM `main`.information_schema.column_masks WHERE table_schema = 's'"
        )


def test_a_view_information_schema_does_not_have_is_refused() -> None:
    with pytest.raises(FakeSqlError, match="no such information_schema view"):
        FakeWarehouse().query(
            "SELECT table_name FROM `main`.information_schema.masks "
            "WHERE table_schema = 's'"
        )


def test_literals_aliases_and_joins_are_not_mistaken_for_columns() -> None:
    FakeWarehouse().query(
        "SELECT tc.table_name, cc.check_clause "
        "FROM `main`.information_schema.table_constraints tc "
        "LEFT JOIN `main`.information_schema.check_constraints cc "
        "ON cc.constraint_name = tc.constraint_name "
        "WHERE tc.table_schema = 'not_a_column'"
    )
