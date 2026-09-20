"""Targets and Unity Catalog objects from a Databricks Asset Bundle.

A project that already has a `databricks.yml` shouldn't have to list its targets
twice. With `bundle: databricks.yml` in `deltaplan.yml`, the bundle's targets
become deltaplan's: their names, the one marked `default: true`, the workspace
each points at, and the bundle's variables as each target resolves them.

A bundle can also declare catalogs, schemas and volumes. Those are read too —
as context, never as deltaplan's work: a spec can name one with the bundle's own
spelling (`${resources.schemas.sales.name}`), and deltaplan leaves the object
itself to the bundle, which owns it.

The bundle is someone else's format, so it is read leniently: only what
deltaplan uses is looked at, and nothing else is validated.

What resolves offline: variable defaults, target overrides, `BUNDLE_VAR_<name>`
from the environment, and references to `${var.<name>}`, `${bundle.name}` and
`${bundle.target}`. What doesn't — lookups, complex variables,
`${workspace.current_user.short_name}` — is recorded with the reason, so a spec
that uses one fails saying why instead of "undefined". The one lookup deltaplan
does resolve is a warehouse named by a `warehouse_id` variable, once connected.

https://docs.databricks.com/aws/en/dev-tools/bundles/variables
https://docs.databricks.com/aws/en/dev-tools/bundles/settings
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

#: The file names the Databricks CLI accepts for a bundle, in its order.
BUNDLE_FILES = ("databricks.yml", "databricks.yaml", "bundle.yml", "bundle.yaml")

#: The variable a bundle conventionally keeps its SQL warehouse in — the
#: `default-sql` template's name for it.
WAREHOUSE_VARIABLE = "warehouse_id"

_REFERENCE = re.compile(r"\$\{([^}]+)\}")


class BundleError(Exception):
    """A bundle deltaplan can't read targets from."""


@dataclass(frozen=True, slots=True)
class BundleTarget:
    """One bundle target, resolved as far as it can be without a workspace."""

    name: str
    default: bool = False
    variables: tuple[tuple[str, str], ...] = ()
    #: Variable name -> why it has no value here.
    unresolved: tuple[tuple[str, str], ...] = ()
    profile: str | None = None
    host: str | None = None
    #: The warehouse a `warehouse_id: {lookup: {warehouse: …}}` names.
    warehouse_lookup: str | None = None
    #: The catalogs, schemas and volumes the bundle declares for this target.
    resources: tuple[BundleResource, ...] = ()


#: The Unity Catalog resources deltaplan reads, and the parts each name is
#: built from, widest first.
RESOURCE_PARTS: dict[str, tuple[str, ...]] = {
    "catalogs": ("name",),
    "schemas": ("catalog_name", "name"),
    "volumes": ("catalog_name", "schema_name", "name"),
}


@dataclass(frozen=True, slots=True)
class BundleResource:
    """A catalog, schema or volume a bundle declares — and therefore owns."""

    #: As the bundle spells it, so a reference to it reads the same: `schemas`.
    kind: str
    key: str
    #: Each field as the bundle spells it: `name`, `catalog_name`, `schema_name`.
    values: tuple[tuple[str, str], ...] = ()
    #: Its full name, when every part could be read.
    full_name: str | None = None
    #: Why the full name couldn't be read, when it couldn't.
    unreadable: str | None = None

    @property
    def singular(self) -> str:
        """`schema`, for a sentence about one of them."""
        return self.kind.removesuffix("s")

    def references(self) -> dict[str, str]:
        """What a spec can write: `${resources.schemas.sales.catalog_name}`."""
        return {
            f"resources.{self.kind}.{self.key}.{field}": value
            for field, value in self.values
        }


@dataclass(frozen=True, slots=True)
class Bundle:
    path: Path
    name: str
    targets: tuple[BundleTarget, ...]

    @property
    def default(self) -> str | None:
        """The target marked `default: true`, or the only one."""
        marked = [t.name for t in self.targets if t.default]
        if marked:
            return marked[0]
        return self.targets[0].name if len(self.targets) == 1 else None

    def target(self, name: str) -> BundleTarget | None:
        for target in self.targets:
            if target.name == name:
                return target
        return None


def find_bundle(directory: Path) -> Path | None:
    """The bundle file in a directory, if there is one."""
    for name in BUNDLE_FILES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def read_bundle(path: Path, environ: Mapping[str, str] | None = None) -> Bundle:
    """Read a bundle's targets. `environ` supplies `BUNDLE_VAR_<name>` overrides."""
    document = _read(path)
    for pattern in _strings(document.get("include"), "include"):
        for included in sorted(path.parent.glob(pattern)):
            _merge(document, _read(included))

    name = _get(document, "bundle", "name")
    declared = _mapping(document.get("variables"), "variables")
    top_workspace = _mapping(document.get("workspace"), "workspace")
    top_resources = _mapping(document.get("resources"), "resources")
    targets = _mapping(document.get("targets"), "targets")
    if not targets:
        raise BundleError(f"{path} has no targets")

    return Bundle(
        path,
        name if isinstance(name, str) else "",
        tuple(
            _target(
                str(target_name),
                _mapping(body, f"target {target_name!r}"),
                declared,
                top_workspace,
                top_resources,
                bundle_name=name if isinstance(name, str) else "",
                environ=environ or {},
            )
            for target_name, body in targets.items()
        ),
    )


# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Value:
    text: str


@dataclass(frozen=True, slots=True)
class _Lookup:
    spec: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _Unresolved:
    reason: str


_Raw = _Value | _Lookup | _Unresolved


def _target(
    name: str,
    body: Mapping[str, object],
    declared: Mapping[str, object],
    top_workspace: Mapping[str, object],
    top_resources: Mapping[str, object],
    *,
    bundle_name: str,
    environ: Mapping[str, str],
) -> BundleTarget:
    raw: dict[str, _Raw] = {}
    for variable, spec in declared.items():
        raw[str(variable)] = _declared(str(variable), spec, name)
    overrides = _mapping(body.get("variables"), f"variables of target {name!r}")
    for variable, value in overrides.items():
        raw[str(variable)] = _override(value)
    for variable in list(raw):
        if (value := environ.get(f"BUNDLE_VAR_{variable}")) is not None:
            raw[variable] = _Value(value)

    context = {"bundle.name": bundle_name, "bundle.target": name}
    variables: dict[str, str] = {}
    unresolved: dict[str, str] = {}
    for variable in raw:
        outcome = _resolve(variable, raw, context, ())
        if isinstance(outcome, _Value):
            variables[variable] = outcome.text
        else:
            unresolved[variable] = outcome.reason

    workspace = _mapping(body.get("workspace"), f"workspace of target {name!r}")
    profile = workspace.get("profile", top_workspace.get("profile"))
    host = workspace.get("host", top_workspace.get("host"))
    if isinstance(host, str):
        resolved_host = _substitute(host, variables, context)
        host = resolved_host if isinstance(resolved_host, str) else None

    lookup = raw.get(WAREHOUSE_VARIABLE)
    warehouse = lookup.spec.get("warehouse") if isinstance(lookup, _Lookup) else None

    return BundleTarget(
        name=name,
        default=body.get("default") is True,
        variables=tuple(sorted(variables.items())),
        unresolved=tuple(sorted(unresolved.items())),
        profile=profile if isinstance(profile, str) else None,
        host=host if isinstance(host, str) else None,
        warehouse_lookup=warehouse if isinstance(warehouse, str) else None,
        resources=_resources(
            top_resources,
            _mapping(body.get("resources"), f"resources of target {name!r}"),
            variables,
            context,
        ),
    )


def _resources(
    top: Mapping[str, object],
    own: Mapping[str, object],
    variables: Mapping[str, str],
    context: Mapping[str, str],
) -> tuple[BundleResource, ...]:
    """The catalogs, schemas and volumes a target ends up with.

    A target's `resources:` adds to the bundle's, field by field, as the
    Databricks CLI merges them. Names are resolved as far as they can be —
    a bundle's own `${resources.catalogs.x.name}` included, which is why
    catalogs come before schemas and schemas before volumes.
    """
    merged: dict[tuple[str, str], dict[str, object]] = {}
    for source in (top, own):
        for kind in RESOURCE_PARTS:
            for key, body in _mapping(source.get(kind), kind).items():
                entry = merged.setdefault((kind, str(key)), {})
                entry.update(_mapping(body, f"{kind}.{key}"))

    found: list[BundleResource] = []
    known = dict(context)
    for kind in RESOURCE_PARTS:
        for (entry_kind, key), body in merged.items():
            if entry_kind != kind:
                continue
            resource = _resource(kind, key, body, variables, known)
            known.update(resource.references())
            found.append(resource)
    return tuple(found)


def _resource(
    kind: str,
    key: str,
    body: Mapping[str, object],
    variables: Mapping[str, str],
    context: Mapping[str, str],
) -> BundleResource:
    values: dict[str, str] = {}
    missing: list[str] = []
    for field in RESOURCE_PARTS[kind]:
        raw = body.get(field)
        if not isinstance(raw, str):
            missing.append(f"{field} is not text" if raw is not None else f"no {field}")
            continue
        resolved = _substitute(raw, variables, context)
        if isinstance(resolved, _Unresolved):
            missing.append(f"{field} {resolved.reason}")
            continue
        values[field] = resolved
    if missing:
        return BundleResource(
            kind, key, tuple(values.items()), unreadable=f"{kind}.{key}: {missing[0]}"
        )
    full = ".".join(values[field] for field in RESOURCE_PARTS[kind])
    return BundleResource(kind, key, tuple(values.items()), full_name=full)


def _declared(variable: str, spec: object, target: str) -> _Raw:
    """A variable as the top-level `variables:` declares it."""
    if not isinstance(spec, Mapping):
        # Not the documented shape, but unambiguous: a bare default.
        return _scalar(spec) or _Unresolved("is not a value deltaplan can use")
    if "lookup" in spec:
        return _Lookup(_mapping(spec["lookup"], f"lookup of {variable!r}"))
    if spec.get("type") == "complex":
        return _Unresolved("is a complex variable; specs can only use plain values")
    if "default" in spec:
        return _scalar(spec["default"]) or _Unresolved(
            "has a default deltaplan can't use as text"
        )
    return _Unresolved(f"has no default, and target {target!r} doesn't set it")


def _override(value: object) -> _Raw:
    """A target's `variables:` entry: a value, `{default: …}` or `{lookup: …}`."""
    if isinstance(value, Mapping):
        if "lookup" in value:
            return _Lookup(_mapping(value["lookup"], "lookup"))
        if "default" in value:
            value = value["default"]
    return _scalar(value) or _Unresolved("is set to something that isn't plain text")


def _scalar(value: object) -> _Value | None:
    if isinstance(value, bool):
        return _Value("true" if value else "false")
    if isinstance(value, str | int | float):
        return _Value(str(value))
    return None


def _resolve(
    variable: str,
    raw: Mapping[str, _Raw],
    context: Mapping[str, str],
    seen: tuple[str, ...],
) -> _Value | _Unresolved:
    """A variable's final text, following `${var.…}` references."""
    if variable in seen:
        return _Unresolved(f"refers to itself through {' → '.join((*seen, variable))}")
    entry = raw.get(variable)
    if entry is None:
        return _Unresolved("isn't declared in the bundle")
    if isinstance(entry, _Unresolved):
        return entry
    if isinstance(entry, _Lookup):
        kind = next(iter(entry.spec), "something")
        return _Unresolved(
            f"is a lookup of a {kind} by name, which only the workspace can answer"
        )
    values: dict[str, str] = {}
    for reference in _REFERENCE.findall(entry.text):
        if reference.startswith("var."):
            inner = reference.removeprefix("var.")
            outcome = _resolve(inner, raw, context, (*seen, variable))
            if isinstance(outcome, _Unresolved):
                return _Unresolved(f"uses ${{var.{inner}}}, which {outcome.reason}")
            values[inner] = outcome.text
    result = _substitute(entry.text, values, context)
    return _Value(result) if isinstance(result, str) else result


def _substitute(
    text: str, variables: Mapping[str, str], context: Mapping[str, str]
) -> str | _Unresolved:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        reference = match.group(1)
        if reference.startswith("var.") and reference[4:] in variables:
            return variables[reference[4:]]
        if reference in context:
            return context[reference]
        missing.append(reference)
        return match.group(0)

    result = _REFERENCE.sub(replace, text)
    if missing:
        return _Unresolved(
            f"uses ${{{missing[0]}}}, which deltaplan can't know without the workspace"
        )
    return result


# -- YAML plumbing ------------------------------------------------------------


def _read(path: Path) -> dict[str, object]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise BundleError(f"cannot read {path}: {error}") from error
    except yaml.YAMLError as error:
        raise BundleError(f"{path} is not valid YAML: {error}") from error
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise BundleError(f"{path} is not a mapping")
    return document


def _merge(into: dict[str, object], other: Mapping[str, object]) -> None:
    """Fold an included file's variables and targets into the bundle's.

    A target named in several files gets its keys from all of them, as the
    Databricks CLI merges them, and so does a kind of resource — which is how
    bundles usually keep them, one file per resource. The other top-level keys
    aren't deltaplan's business.
    """
    for key in ("variables", "targets", "resources"):
        incoming = _mapping(other.get(key), key)
        if not incoming:
            continue
        current = dict(_mapping(into.get(key), key))
        for name, body in incoming.items():
            existing = current.get(name)
            if (
                key in {"targets", "resources"}
                and isinstance(existing, Mapping)
                and isinstance(body, Mapping)
            ):
                current[name] = {**existing, **body}
            else:
                current[name] = body
        into[key] = current


def _mapping(value: object, what: str) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BundleError(f"{what} should be a mapping")
    return value


def _strings(value: object, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BundleError(f"{what} should be a list of paths")
    return list(value)


def _get(document: Mapping[str, object], *keys: str) -> object:
    current: object = document
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current
