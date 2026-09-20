"""Targets and Unity Catalog objects from a Databricks Asset Bundle.

A project that already has a `databricks.yml` shouldn't have to list its targets
twice. With `bundle: databricks.yml` in `deltaplan.yml`, the bundle's targets
become deltaplan's: their names, the one marked `default: true`, the workspace
each points at, and the bundle's variables as each target resolves them.

A bundle can also declare catalogs, schemas and volumes. Those are read too —
as context, never as deltaplan's work: a spec can name one with the bundle's own
spelling (`${resources.schemas.sales.name}`), and deltaplan leaves the object
itself to the bundle, which owns it.

What a bundle *says* isn't what it *deploys*: the Databricks CLI resolves
variables, runs lookups against the workspace, and applies the mutators that
rename things — a schema `sales` becomes `dev_jane_sales` in development mode,
and a `team_` prefix makes it `teamsales`. deltaplan reimplements none of it. It
asks the CLI (`databricks bundle validate -o json -t <target>`), which answers
with every `${var.…}` filled in, every `lookup:` resolved, and every name as a
deploy would make it (verified against a workspace, 2026-09-20).

Reading the file is the fallback, for when the CLI isn't installed or can't
reach a workspace — it needs one for anything it must look up, and answers
nothing at all without credentials. The bundle is someone else's format, so it
is read leniently: only what deltaplan uses is looked at, nothing is validated.

What that fallback resolves: variable defaults, target overrides,
`BUNDLE_VAR_<name>` from the environment, and references to `${var.<name>}`,
`${bundle.name}` and `${bundle.target}`. What it doesn't — lookups, complex
variables, `${workspace.current_user.short_name}`, and every name a renaming
target deploys under — is recorded with the reason, so a spec that uses one
fails saying why instead of planning against the wrong object. The one lookup
deltaplan resolves by itself is a warehouse named by a `warehouse_id` variable,
once connected.

https://docs.databricks.com/aws/en/dev-tools/bundles/variables
https://docs.databricks.com/aws/en/dev-tools/bundles/settings
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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
    #: Why this target's resources are renamed by the CLI, when they are.
    renames: str | None = None


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

    def reference_names(self) -> tuple[str, ...]:
        """Every name a spec could write for it, readable or not."""
        return tuple(
            f"resources.{self.kind}.{self.key}.{field}"
            for field in RESOURCE_PARTS[self.kind]
        )


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


def resolve_target(
    path: Path,
    target: str,
    *,
    profile: str | None = None,
    executable: str = "databricks",
) -> BundleTarget | None:
    """The target as the Databricks CLI resolves it, or None if it can't say.

    `databricks bundle validate -o json -t <target>` is the configuration a
    deploy would use: `${var.…}` filled in, `lookup:` variables resolved against
    the workspace, and every object under the name the target really deploys it
    with. Verified against a live workspace (2026-09-20): each variable comes
    back with a `value`, a warehouse lookup among them, and a development
    target's schema as `dev_<user>_<name>`.

    None when the CLI isn't installed, or answers with an error — it needs
    credentials for anything it looks up, and refuses to resolve without them.
    The caller then falls back to reading the file, which says *unknown* for
    what only the CLI can settle.
    https://docs.databricks.com/aws/en/dev-tools/cli/bundle-commands
    """
    document = _ask_cli(path, target, profile=profile, executable=executable)
    if document is None:
        return None
    variables: dict[str, str] = {}
    unresolved: dict[str, str] = {}
    for name, spec in _mapping(document.get("variables"), "variables").items():
        body = spec if isinstance(spec, Mapping) else {}
        value = body.get("value", body.get("default"))
        if isinstance(value, str | int | float | bool):
            variables[str(name)] = str(value)
        else:
            unresolved[str(name)] = (
                "the bundle leaves it a complex value, which a name can't be "
                "built from — give it under this target's `vars`"
            )
    workspace = _mapping(document.get("workspace"), "workspace")
    host, workspace_profile = workspace.get("host"), workspace.get("profile")
    return BundleTarget(
        name=target,
        variables=tuple(sorted(variables.items())),
        unresolved=tuple(sorted(unresolved.items())),
        profile=workspace_profile if isinstance(workspace_profile, str) else None,
        host=host if isinstance(host, str) else None,
        # Nothing is left to look up or rename: these are the deployed names.
        resources=_from_mapping(_mapping(document.get("resources"), "resources")),
    )


def _ask_cli(
    path: Path,
    target: str,
    *,
    profile: str | None,
    executable: str,
) -> Mapping[str, object] | None:
    """`databricks bundle validate -o json`, parsed — or None if it didn't answer."""
    found = shutil.which(executable)
    if found is None:
        return None
    command = [found, "bundle", "validate", "-o", "json", "-t", target]
    if profile:
        command += ["-p", profile]
    try:
        result = subprocess.run(  # noqa: S603 - the CLI, found on PATH
            command,
            cwd=path.parent,
            capture_output=True,
            text=True,
            # It is asked on every command, so it may not hang around: without
            # credentials it fails at once, and reading the file is the answer.
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # It prints the unresolved configuration along with the error; taking
        # that would be worse than reading the file ourselves.
        return None
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def _from_mapping(resources: Mapping[str, object]) -> tuple[BundleResource, ...]:
    found: list[BundleResource] = []
    known: dict[str, str] = {}
    for kind in RESOURCE_PARTS:
        for key, body in _mapping(resources.get(kind), kind).items():
            resource = _resource(kind, str(key), _mapping(body, kind), {}, known)
            known.update(resource.references())
            found.append(resource)
    return tuple(found)


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
    top_presets = _mapping(document.get("presets"), "presets")
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
                top_presets,
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
    top_presets: Mapping[str, object],
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

    renames = _renames(body, top_presets)
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
        renames=renames,
        resources=_resources(
            top_resources,
            _mapping(body.get("resources"), f"resources of target {name!r}"),
            variables,
            context,
            renames,
        ),
    )


def _renames(body: Mapping[str, object], top_presets: Mapping[str, object]) -> str | None:
    """Why this target's resources come out under other names, if they do.

    `mode: development` and `presets.name_prefix` both rename what the bundle
    deploys, in ways only the Databricks CLI knows exactly — it made `sales`
    into `dev_jane_sales` under one and `teamsales` under the other.
    """
    presets = {**top_presets, **_mapping(body.get("presets"), "presets")}
    if body.get("mode") == "development":
        return "its mode is development, which renames what the bundle deploys"
    prefix = presets.get("name_prefix")
    if isinstance(prefix, str) and prefix:
        return f"its presets put {prefix!r} in front of what the bundle deploys"
    return None


def _resources(
    top: Mapping[str, object],
    own: Mapping[str, object],
    variables: Mapping[str, str],
    context: Mapping[str, str],
    renames: str | None = None,
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
            if renames is not None:
                # The name here isn't the name it deploys under; only the CLI
                # knows that, so nothing is claimed until it is asked.
                resource = BundleResource(
                    kind, key, unreadable=f"{kind}.{key}: {renames}"
                )
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
