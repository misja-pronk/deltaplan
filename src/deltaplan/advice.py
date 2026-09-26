"""What deltaplan knows about the Databricks failures it meets often.

The workspace's own sentence is the truth about what went wrong, and it comes
through untouched — deltaplan never talks over it. But for a handful of failures
it also knows something the message doesn't, and staying quiet about that costs
somebody an afternoon. Each entry below cost one.

The match is on the error class Databricks puts in brackets, because that is the
part that doesn't get reworded. An error with no entry is passed through exactly
as it arrived: no guessing, and nothing that tells you to install something you
have.

Every entry is tested against a message recorded in the wild, so a Databricks
rewording shows up as a failing test rather than as silence.
https://docs.databricks.com/aws/en/error-messages/
"""

from __future__ import annotations

import re

#: The error class in `[QUOTA_EXCEEDED.UC_RESOURCE_QUOTA_EXCEEDED] …`, or the
#: `[DLT ERROR CODE: …]` and `ErrorClass=…` shapes the platform also uses.
_CLASS = re.compile(
    r"\[(?:DLT ERROR CODE:\s*)?([A-Z][A-Z0-9_.]+)\]|ErrorClass=([A-Z][A-Z0-9_.]+)"
)

#: Error class -> what deltaplan knows, in one paragraph. Keyed by prefix, so
#: `QUOTA_EXCEEDED.UC_RESOURCE_QUOTA_EXCEEDED` matches `QUOTA_EXCEEDED`.
ADVICE: dict[str, str] = {
    "QUOTA_EXCEEDED": (
        "Unity Catalog counts tables as they are created and catches up with "
        "deletions later, so this count is often far above what the catalogs "
        "really hold — a dropped table also counts for as long as UNDROP could "
        "bring it back. Reading the quota asks it to recount, which lands within "
        "about half an hour; a schema that is only ever used for tests can turn "
        "the recovery period off with ALTER SCHEMA … SET RETAIN DROPPED TO 0 "
        "HOURS. Some limits can be raised on request. "
        "https://docs.databricks.com/aws/en/data-governance/unity-catalog/resource-quotas"
    ),
    "QUOTA_EXCEEDED_EXCEPTION": (
        "This is the limit on *active pipelines*, not on tables: a materialized "
        "view or a streaming table runs one, and a workspace tier allows a fixed "
        "number at a time. deltaplan doesn't create either — something else in "
        "the workspace is holding them, and they finish or can be stopped. "
        "https://docs.databricks.com/aws/en/lakeflow-declarative-pipelines/"
    ),
    "TABLE_OR_VIEW_NOT_FOUND": (
        "If the table is one this project describes, it has to exist before "
        "whatever names it — a function's body is resolved when the function is "
        "created. deltaplan plans in that order; a table that is *not* in the "
        "project has to be there already."
    ),
    "PERMISSION_DENIED": (
        "The principal deltaplan is connected as needs the privilege on the "
        "object the message names. `deltaplan doctor` says which privileges it "
        "has where."
    ),
    "DELTA_FEATURES_REQUIRE_MANUAL_ENABLEMENT": (
        "The table needs a Delta feature turned on before this change fits. "
        "deltaplan plans that as its own step where it knows which feature it is "
        "(column mapping, type widening, column defaults); for anything else, "
        "enable it by hand and plan again. "
        "https://docs.databricks.com/aws/en/delta/table-features"
    ),
    "UNSUPPORTED_OVERWRITE": (
        "This runtime won't let a statement read the table it overwrites. "
        "deltaplan stages a rewrite in a second table when a conversion is "
        "involved; if you see this on a rewrite that converts nothing, the "
        "one-statement route isn't available here — say so in an issue, and use "
        "`--clone` meanwhile so the data is safe."
    ),
    "SPECIFY_CLUSTER_BY_WITH_PARTITIONED_BY_IS_NOT_ALLOWED": (
        "Delta takes partitioning or liquid clustering, never both. A spec that "
        "sets `cluster_by` on a partitioned table is asking for both; take the "
        "partitioning out (`partitioned_by: []`) in the same spec."
    ),
    "DELTA_ALTER_TABLE_CLUSTER_BY_ON_PARTITIONED_TABLE_NOT_ALLOWED": (
        "A partitioned table can't be given clustering keys in place. deltaplan "
        "rebuilds it instead; this error means the rebuild was skipped, which is "
        "worth an issue."
    ),
    "DELTA_CLUSTERING_TO_PARTITIONED_TABLE_WITH_NON_EMPTY_CLUSTERING_COLUMNS": (
        "A clustered table has to lose its keys before it can be partitioned. "
        "deltaplan plans `CLUSTER BY NONE` first; this error means that step "
        "didn't run."
    ),
    "UC_UNDROP_RESOURCE_PAST_CUSTOM_RETENTION_PERIOD": (
        "The table is beyond its recovery period, so UNDROP can't bring it back. "
        "A restore point from deltaplan is a Delta version, not an UNDROP: "
        "`RESTORE TABLE … TO VERSION AS OF <n>` works while the table exists."
    ),
}

#: Failures that arrive without an error class, matched on their text instead —
#: the warehouse ones, which is what a stopped serverless warehouse looks like.
BY_TEXT: tuple[tuple[str, str], ...] = (
    (
        "could not be processed by the warehouse",
        "The SQL warehouse took the request and couldn't run it, which is what a "
        "stopped or unstartable serverless warehouse looks like. Check it in "
        "SQL Warehouses — if it is STOPPED and won't start, the workspace can't "
        "give it compute right now. `deltaplan doctor` reports its state.",
    ),
    (
        "Cannot create the resource, please try again later",
        "The workspace couldn't give the warehouse compute: serverless capacity, "
        "or an account whose entitlement for it has run out. Nothing in the "
        "project is wrong, and nothing ran.",
    ),
    (
        "default auth: cannot configure default credentials",
        "No credentials were found. Set a profile on the target, pass --profile, "
        "or export DATABRICKS_HOST and a token. "
        "https://docs.databricks.com/aws/en/dev-tools/auth/unified-auth",
    ),
)


def error_class(message: str) -> str | None:
    """The error class Databricks names in a message, if it names one."""
    found = _CLASS.search(message)
    if found is None:
        return None
    return found.group(1) or found.group(2)


def advice(message: str) -> str | None:
    """What deltaplan can add to this failure — or None, which is most of them.

    Matched on the error class first, then on the text for the failures that
    arrive without one. Never rewrites the message it was given.
    """
    found = error_class(message)
    if found is not None:
        for key, said in ADVICE.items():
            # An error class is a path: `BAD_REQUEST.UC_UNDROP_…` is both, and
            # either part may be the one deltaplan knows about.
            if found == key or found.startswith(f"{key}.") or found.endswith(f".{key}"):
                return said
    for text, said in BY_TEXT:
        if text in message:
            return said
    return None


def with_advice(message: str) -> str:
    """The message as it arrived, and underneath it what deltaplan knows.

    The workspace's words come first, whole: whoever reads this needs to be able
    to search for them.
    """
    said = advice(message)
    return f"{message}\n\n{said}" if said else message
