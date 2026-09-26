"""What deltaplan adds to the Databricks failures it meets often.

Each message below was recorded in the wild — most of them in one week of real
use. They are here verbatim so that a Databricks rewording shows up as a failing
test rather than as silence, and so nobody has to guess what these look like.

Two rules hold for every entry: the workspace's own sentence comes first and
whole, and an error deltaplan has nothing to add to is passed through untouched.
"""

from __future__ import annotations

import pytest

from deltaplan.advice import ADVICE, advice, error_class, with_advice

QUOTA = (
    "[RequestId=567896ae-de7c-4366-9417-b2c5c32eb347 "
    "ErrorClass=QUOTA_EXCEEDED.UC_RESOURCE_QUOTA_EXCEEDED] Cannot create 1 "
    "Table(s) in Metastore 29148bb9-1baf-4a9c-b395-5da015738282 "
    "(estimated count: 523, limit: 500)."
)
NOT_FOUND = (
    "[TABLE_OR_VIEW_NOT_FOUND] The table or view "
    "`hu_hdp_001_outlook_development`.`bronze__mpronk1`.`mailbox_access` cannot "
    "be found. Verify the spelling and correctness of the schema and catalog. "
    "SQLSTATE: 42P01; line 5 pos 9"
)
UNDROP = (
    "[RequestId=e8c41b4d ErrorClass=BAD_REQUEST.UC_UNDROP_RESOURCE_PAST_CUSTOM_"
    "RETENTION_PERIOD] Cannot undrop 'Table' because the 'Table' with id "
    "'64cbca15' is beyond its custom restoration period."
)
NTZ = (
    "[DELTA_FEATURES_REQUIRE_MANUAL_ENABLEMENT] Your table schema requires "
    "manually enablement of the following table feature(s): timestampNtz."
)
PIPELINE = (
    "[DLT ERROR CODE: QUOTA_EXCEEDED_EXCEPTION] Cannot start update 'ac8f4c77' "
    "because the limit for active pipelines of type 'DBSQL' has been reached. "
    "Limit: 1; used: 2."
)
WAREHOUSE = "The request could not be processed by the warehouse."
NO_COMPUTE = "Cannot create the resource, please try again later."
NO_AUTH = (
    "default auth: cannot configure default credentials, please check "
    "https://docs.databricks.com/en/dev-tools/auth.html"
)
CLUSTERED = "SPECIFY_CLUSTER_BY_WITH_PARTITIONED_BY_IS_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (QUOTA, "QUOTA_EXCEEDED.UC_RESOURCE_QUOTA_EXCEEDED"),
        (NOT_FOUND, "TABLE_OR_VIEW_NOT_FOUND"),
        (UNDROP, "BAD_REQUEST.UC_UNDROP_RESOURCE_PAST_CUSTOM_RETENTION_PERIOD"),
        (NTZ, "DELTA_FEATURES_REQUIRE_MANUAL_ENABLEMENT"),
        (PIPELINE, "QUOTA_EXCEEDED_EXCEPTION"),
        (WAREHOUSE, None),
    ],
)
def test_the_error_class_is_read_out_of_the_message(
    message: str, expected: str | None
) -> None:
    assert error_class(message) == expected


@pytest.mark.parametrize(
    ("message", "says"),
    [
        (QUOTA, "catches up with deletions later"),
        (PIPELINE, "limit on *active pipelines*"),
        (NOT_FOUND, "has to exist before whatever names it"),
        (UNDROP, "RESTORE TABLE"),
        (NTZ, "Delta feature turned on"),
        (WAREHOUSE, "stopped or unstartable serverless warehouse"),
        (NO_COMPUTE, "serverless capacity"),
        (NO_AUTH, "Set a profile on the target"),
    ],
)
def test_what_deltaplan_adds(message: str, says: str) -> None:
    said = advice(message)
    assert said is not None, f"no advice for {message[:40]}…"
    assert says in said


def test_the_workspaces_own_words_come_first_and_whole() -> None:
    together = with_advice(QUOTA)
    assert together.startswith(QUOTA), "searchable, unaltered, at the top"
    assert advice(QUOTA) in together


def test_an_error_nobody_has_advice_for_is_passed_through() -> None:
    odd = "[SOME_NEW_THING] Databricks changed something. SQLSTATE: 42000"
    assert advice(odd) is None
    assert with_advice(odd) == odd


def test_a_subclass_of_a_known_class_is_still_known() -> None:
    """`QUOTA_EXCEEDED.UC_RESOURCE_QUOTA_EXCEEDED` is a `QUOTA_EXCEEDED`."""
    assert advice(QUOTA) == ADVICE["QUOTA_EXCEEDED"]


def test_nothing_tells_anyone_to_install_what_they_have() -> None:
    """The lesson of the bundle CLI message: never advise an install blind."""
    for said in ADVICE.values():
        assert "install " not in said.lower(), said[:60]


def test_every_entry_says_where_to_read_more_or_what_to_do() -> None:
    for name, said in ADVICE.items():
        actionable = any(
            hint in said
            # A way forward is a link, a command, or a named thing to change.
            for hint in ("http", "deltaplan ", "ALTER ", "RESTORE ", "issue", "`")
        )
        assert actionable, f"{name} states a fact but no way forward"


def test_a_failed_statement_carries_it_through_the_error() -> None:
    """The place it matters: a statement that a warehouse refused."""
    from deltaplan.introspect import IntrospectionError, WarehouseRunner

    class Refusing:
        """A workspace client whose statement execution won't take the request."""

        class statement_execution:  # noqa: N801 - mirrors the SDK's attribute
            @staticmethod
            def execute_statement(**_kwargs: object) -> object:
                raise RuntimeError(WAREHOUSE)

    runner = WarehouseRunner(Refusing(), "w1")  # type: ignore[arg-type]
    with pytest.raises(IntrospectionError) as raised:
        runner.query("SELECT 1")
    said = str(raised.value)
    assert said.startswith(WAREHOUSE)
    assert "serverless warehouse" in said
