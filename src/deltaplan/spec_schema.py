"""JSON Schemas for YAML specs and `deltaplan.yml`, for editors.

An editor with a YAML language server (VS Code's Red Hat YAML extension, most
JetBrains IDEs) uses these for completion, hover docs and inline errors. They
are built from the loader's own key sets, and a test holds every object's keys
to the loader's, so the editor can't accept what `validate` refuses — or refuse
what it accepts.

The loader stays the authority: it checks more than a schema can say (names
with three parts, a primary key's columns being NOT NULL, …). The schema is
there to catch the typo while you type.

Published with the docs; `deltaplan schema` prints them for the installed
version. Regenerate the published copies with
`python -m deltaplan.spec_schema docs/schema`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from deltaplan import loader
from deltaplan.sql import FUNCTION_PRIVILEGES, TABLE_PRIVILEGES

BASE_URL = "https://misja-pronk.github.io/deltaplan/schema"
SPEC_SCHEMA_URL = f"{BASE_URL}/spec.json"
PROJECT_SCHEMA_URL = f"{BASE_URL}/project.json"
#: The first line `import` writes, so an editor finds the schema by itself.
MODELINE = f"# yaml-language-server: $schema={SPEC_SCHEMA_URL}"

Schema = dict[str, Any]


def _object(
    keys: Iterable[str],
    properties: dict[str, Schema],
    *,
    required: Iterable[str] = (),
    description: str | None = None,
) -> Schema:
    """A closed object, as strict as the loader: unknown keys are errors."""
    keys = set(keys)
    missing = keys - set(properties)
    extra = set(properties) - keys
    if missing or extra:  # pragma: no cover - a programming error, caught by tests
        raise ValueError(f"schema/loader mismatch: missing {missing}, extra {extra}")
    schema: Schema = {
        "type": "object",
        "properties": {key: properties[key] for key in sorted(keys)},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = sorted(required)
    if description:
        schema["description"] = description
    return schema


def _text(description: str) -> Schema:
    return {"type": "string", "description": description}


def _ignoring_case(words: Iterable[str]) -> str:
    """A pattern matching any of `words` in any case, `_` for a space."""
    alternatives = [
        "[ _]+".join(
            "".join(f"[{c.upper()}{c.lower()}]" if c.isalpha() else c for c in word)
            for word in words_in.split()
        )
        for words_in in sorted(words)
    ]
    return f"^(?:{'|'.join(alternatives)})$"


def _privileges(allowed: Iterable[str]) -> Schema:
    """Completion offers the canonical spelling; any case is accepted, as the
    loader accepts it."""
    allowed = sorted(allowed)
    return {
        "type": "array",
        "items": {
            "anyOf": [
                {"enum": allowed},
                {"type": "string", "pattern": _ignoring_case(allowed)},
            ]
        },
        "minItems": 1,
    }


def _grants(allowed: Iterable[str]) -> Schema:
    return {
        "type": "array",
        "description": "Privileges per principal. Principals not named are left alone.",
        "items": _object(
            loader.GRANT_KEYS,
            {
                "principal": _text("A user, group or service principal."),
                "privileges": _privileges(allowed),
            },
            required=loader.GRANT_KEYS,
        ),
    }


STRING_MAP: Schema = {"type": "object", "additionalProperties": {"type": "string"}}
NAMES: Schema = {"type": "array", "items": {"type": "string"}}


def spec_schema() -> Schema:
    """The schema for a YAML table, view or function spec."""
    defs: dict[str, Schema] = {}
    defs["type"] = {
        "description": (
            "A Databricks type string (`decimal(18,2)`, "
            "`struct<street:string,zip:string>`) or the nested YAML form."
        ),
        "oneOf": [
            {"type": "string"},
            _object(
                loader.TYPE_KEYS,
                {
                    "struct": {"type": "array", "items": {"$ref": "#/definitions/field"}},
                    "array": {
                        "oneOf": [
                            {"$ref": "#/definitions/type"},
                            _object(
                                loader.ARRAY_KEYS,
                                {
                                    "element": {"$ref": "#/definitions/type"},
                                    "contains_null": {"type": "boolean"},
                                },
                                required={"element"},
                            ),
                        ]
                    },
                    "map": _object(
                        loader.MAP_KEYS,
                        {
                            "key": {"$ref": "#/definitions/type"},
                            "value": {"$ref": "#/definitions/type"},
                        },
                        required=loader.MAP_KEYS,
                    ),
                },
            )
            | {"minProperties": 1, "maxProperties": 1},
        ],
    }
    # The loader reads it in any case, with a space or an underscore.
    identity_kind = {
        "anyOf": [
            {"enum": ["always", "by_default"]},
            {"type": "string", "pattern": _ignoring_case(["always", "by default"])},
        ]
    }
    defs["field"] = _object(
        loader.FIELD_KEYS,
        {
            "name": _text("The column or field name."),
            "type": {"$ref": "#/definitions/type"},
            "nullable": {"type": "boolean", "description": "false for NOT NULL."},
            "comment": {"type": "string"},
            "renamed_from": _text("The old name, while a rename is still to happen."),
            "using": _text(
                "How to fill it from the rest of the row: for a rewrite's conversion, "
                "or a new column's backfill."
            ),
            "tags": STRING_MAP,
            "mask": {
                "description": "A column mask: a function name, or with using_columns.",
                "oneOf": [
                    {"type": "string"},
                    _object(
                        loader.MASK_KEYS,
                        {"function": {"type": "string"}, "using_columns": NAMES},
                        required={"function"},
                    ),
                ],
            },
            "identity": {
                "description": "`always`, `by_default`, or with start and increment.",
                "oneOf": [
                    identity_kind,
                    _object(
                        loader.IDENTITY_KEYS,
                        {
                            "generated": identity_kind,
                            "start": {"type": "integer"},
                            "increment": {"type": "integer", "not": {"const": 0}},
                        },
                    ),
                ],
            },
            "generated": _text("GENERATED ALWAYS AS (…): the expression."),
            "default": _text(
                "DEFAULT …: the expression, e.g. `'new'` or `current_date()`."
            ),
        },
        required={"name", "type"},
    )
    constraint = {
        "oneOf": [
            _object(
                {"primary_key"},
                {
                    "primary_key": {
                        "oneOf": [
                            NAMES,
                            _object(
                                loader.PRIMARY_KEY_KEYS,
                                {"columns": NAMES, "name": {"type": "string"}},
                                required={"columns"},
                            ),
                        ]
                    }
                },
                required={"primary_key"},
            ),
            _object(
                {"check"},
                {
                    "check": _object(
                        loader.CHECK_KEYS,
                        {"name": {"type": "string"}, "expression": {"type": "string"}},
                        required=loader.CHECK_KEYS,
                    )
                },
                required={"check"},
            ),
            _object(
                {"foreign_key"},
                {
                    "foreign_key": _object(
                        loader.FOREIGN_KEY_KEYS,
                        {
                            "columns": NAMES,
                            "references": _text("The referenced table, in full."),
                            "referenced_columns": NAMES,
                            "name": {"type": "string"},
                        },
                        required={"columns", "references", "referenced_columns"},
                    )
                },
                required={"foreign_key"},
            ),
        ]
    }
    table = _object(
        loader.TABLE_KEYS,
        {
            "table": _text("catalog.schema.table — `${catalog}` and friends allowed."),
            "renamed_from": _text("The table's old name, while a rename is to happen."),
            "comment": {"type": "string"},
            "cluster_by": {
                "description": "Liquid clustering keys, or `auto`.",
                "oneOf": [NAMES, {"type": "string", "pattern": "^[Aa][Uu][Tt][Oo]$"}],
            },
            "tags": STRING_MAP,
            "properties": STRING_MAP,
            "columns": {
                "type": "array",
                "items": {"$ref": "#/definitions/field"},
                "minItems": 1,
            },
            "constraints": {"type": "array", "items": constraint},
            "grants": _grants(TABLE_PRIVILEGES),
            "row_filter": _object(
                loader.ROW_FILTER_KEYS,
                {"function": {"type": "string"}, "columns": NAMES},
                required=loader.ROW_FILTER_KEYS,
            ),
            "hooks": _object(
                loader.HOOK_KEYS,
                {
                    "before": _text("SQL run before the table's changes."),
                    "after": _text("SQL run after the table's changes."),
                },
            ),
        },
        required={"table", "columns"},
        description="A Delta table.",
    )
    view = _object(
        loader.VIEW_KEYS,
        {
            "view": _text("catalog.schema.view"),
            "query": _text("The view's query, kept as written."),
            "comment": {"type": "string"},
            "tags": STRING_MAP,
            "properties": STRING_MAP,
            "grants": _grants(TABLE_PRIVILEGES),
        },
        required={"view", "query"},
        description="A view.",
    )
    function = _object(
        loader.FUNCTION_KEYS,
        {
            "function": _text("catalog.schema.function"),
            "parameters": {
                "type": "array",
                "items": _object(
                    loader.PARAMETER_KEYS,
                    {"name": {"type": "string"}, "type": {"$ref": "#/definitions/type"}},
                    required=loader.PARAMETER_KEYS,
                ),
            },
            "returns": {"$ref": "#/definitions/type"},
            "body": _text("The expression after RETURN, kept as written."),
            "comment": {"type": "string"},
            "grants": _grants(FUNCTION_PRIVILEGES),
        },
        required={"function", "returns", "body"},
        description="A SQL function.",
    )
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": SPEC_SCHEMA_URL,
        "title": "deltaplan spec",
        "description": "A table, view or SQL function. https://misja-pronk.github.io/deltaplan/spec/",
        "oneOf": [table, view, function],
        "definitions": defs,
    }


def project_schema() -> Schema:
    """The schema for `deltaplan.yml`."""
    mode = {"enum": ["additive", "strict"]}
    target = _object(
        loader.TARGET_KEYS,
        {
            "vars": STRING_MAP | {"description": "What `${name}` becomes in specs."},
            "warehouse_id": {"type": "string"},
            "mode": mode | {"description": "additive never drops; strict drops orphans."},
            "profile": _text("A ~/.databrickscfg profile."),
            "default": {"type": "boolean", "description": "Used when -t is left out."},
        },
    )
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": PROJECT_SCHEMA_URL,
        "title": "deltaplan project",
        "description": "deltaplan.yml. https://misja-pronk.github.io/deltaplan/spec/#the-project-file",
        **_object(
            loader.CONFIG_KEYS,
            {
                "version": {"type": "integer"},
                "specs": NAMES | {"description": "Spec files or directories."},
                "targets": {"type": "object", "additionalProperties": target},
                "history_schema": _text("Where apply keeps its run history."),
                "schemas": {
                    "type": "object",
                    "description": "Per-schema mode, keyed catalog.schema.",
                    "additionalProperties": mode,
                },
                "bundle": _text("A databricks.yml whose targets to use."),
            },
        ),
    }


def write(directory: Path) -> None:
    """Write both schemas where the docs publish them."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, schema in (
        ("spec.json", spec_schema()),
        ("project.json", project_schema()),
    ):
        (directory / name).write_text(
            json.dumps(schema, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":  # pragma: no cover - a maintenance script
    write(Path(sys.argv[1]))
