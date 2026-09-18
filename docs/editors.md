# Editor support

YAML specs and `deltaplan.yml` have JSON Schemas, so an editor with a YAML language
server — VS Code with the [Red Hat YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml),
JetBrains IDEs, Neovim with `yaml-language-server` — completes keys, shows what each
one means, and underlines a typo as you type it.

| File | Schema |
|---|---|
| A table, view or function spec | <https://misja-pronk.github.io/deltaplan/schema/spec.json> |
| `deltaplan.yml` | <https://misja-pronk.github.io/deltaplan/schema/project.json> |

## Per file

Put this on the first line — `deltaplan import` writes it for you:

```yaml
# yaml-language-server: $schema=https://misja-pronk.github.io/deltaplan/schema/spec.json
table: ${catalog}.sales.orders
```

## For a whole project, in VS Code

In `.vscode/settings.json`, with the paths your specs live in:

```json
{
  "yaml.schemas": {
    "https://misja-pronk.github.io/deltaplan/schema/spec.json": ["tables/**/*.yml"],
    "https://misja-pronk.github.io/deltaplan/schema/project.json": ["deltaplan.yml"]
  }
}
```

## Offline, or pinned to your version

The published schemas follow the latest release. `deltaplan schema` prints the one for
the version you have installed:

```sh
deltaplan schema spec > .deltaplan/spec.json
deltaplan schema project > .deltaplan/project.json
```

and point `yaml.schemas` (or the `$schema=` line) at those files instead.

## What the schema can't check

The schema catches unknown keys, wrong types and misspelt privileges. `deltaplan
validate` checks everything else — that a name has three parts, that a primary key's
columns are `NOT NULL`, that a clustering key is a column — and stays the authority:
both are built from the same list of keys, and the tests hold them to it. SQL specs
don't have a schema; sqlglot is their validator.
