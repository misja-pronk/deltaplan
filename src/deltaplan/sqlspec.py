"""SQL specs: a Databricks `CREATE` statement as the desired state.

A `.sql` spec is read with sqlglot's Databricks dialect and turned into the same
model a YAML spec becomes, so everything after the loader — differ, planner,
executor — can't tell them apart. The rule is simple: what sqlglot parses into
structure, deltaplan can use; what it can't parse, or parses only as an opaque
command, is refused with the line it's on — and YAML, which supports
everything, is the way to say it. `deltaplan.features` lists which is which.

A spec file holds one object: a `CREATE TABLE`, `CREATE VIEW` or `CREATE
FUNCTION`, optionally followed by `ALTER … SET TAGS` and `GRANT` statements
about that same object. `CREATE`, `CREATE OR REPLACE` and `IF NOT EXISTS` all
mean the same thing here — this is a declaration, never run as written.

View queries and function bodies are kept exactly as written, sliced from the
source by token position: sqlglot re-renders SQL in its own style, and the
catalog stores the text it was given, so a re-rendered body would look changed
on every plan. Column-level expressions (CHECK, generated, DEFAULT) are short,
and taken as sqlglot renders them.

https://sqlglot.com/sqlglot/dialects/databricks.html
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import Token, TokenType

from deltaplan.loader import (
    VARIABLE,
    Loc,
    SpecError,
    spec_properties,
    with_catalog_variable,
)
from deltaplan.model.function import Function, Parameter
from deltaplan.model.schema import Schema
from deltaplan.model.table import (
    Check,
    Constraint,
    ForeignKey,
    Grant,
    PrimaryKey,
    Table,
    is_bookkeeping,
)
from deltaplan.model.types import DataType, Field, Identity, render_type
from deltaplan.model.view import Relation, View
from deltaplan.model.volume import Volume
from deltaplan.sql import (
    FUNCTION_PRIVILEGES,
    SCHEMA_PRIVILEGES,
    TABLE_PRIVILEGES,
    maybe_quote_ident,
    privilege_sql,
    quote_ident,
    quote_literal,
)
from deltaplan.typeparser import TypeParseError, parse_type

DIALECT = "databricks"
USE_YAML = "write this spec in YAML, which supports it"


def load_sql_spec(
    path: Path,
    variables: Mapping[str, str] | None = None,
    unresolved: Mapping[str, str] | None = None,
) -> Relation:
    """Read one `.sql` spec into a table, a view or a function."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SpecError(f"cannot read spec: {error}", Loc(path, 1, 1)) from error
    text = _substitute(path, raw, variables or {}, unresolved or {})
    return _Reader(path, text).read()


def sql_cannot_say(relation: Relation) -> str | None:
    """Why a SQL spec couldn't describe this object — or None when it can.

    `import --format sql` writes YAML for these instead. Kept in step with the
    — rows of `deltaplan.features`.
    """
    if isinstance(relation, Schema):
        # sqlglot reads ALTER SCHEMA … SET TAGS as an opaque command.
        return "schema tags" if relation.tags else None
    if isinstance(relation, Volume):
        # … and CREATE VOLUME and GRANT … ON VOLUME.
        return "volumes"
    if not isinstance(relation, Table):
        return None
    found: list[str] = []
    if any(column.tags for column in relation.columns):
        found.append("column tags")
    if any(column.mask is not None for column in relation.columns):
        found.append("column masks")
    if relation.row_filter is not None:
        found.append("a row filter")
    return ", ".join(found) or None


def dump_sql_spec(relation: Relation, *, catalog_variable: str | None = None) -> str:
    """Render an object as a SQL spec, the way `import --format sql` writes it.

    The result loads back into the same model — tested — so it must only use
    what `load_sql_spec` reads. Check `sql_cannot_say` first.
    """
    reason = sql_cannot_say(relation)
    if reason is not None:
        raise ValueError(f"a SQL spec can't say {reason}")
    catalog = relation.name.partition(".")[0]

    def name(full: str) -> str:
        shown = with_catalog_variable(full, catalog, catalog_variable)
        if shown != full:
            first, _, rest = shown.partition(".")
            return ".".join([first, *(maybe_quote_ident(p) for p in rest.split("."))])
        return ".".join(maybe_quote_ident(part) for part in full.split("."))

    if isinstance(relation, Table):
        statements, kind = [_create_table(relation, name)], "TABLE"
    elif isinstance(relation, View):
        statements, kind = [_create_view(relation, name)], "VIEW"
    elif isinstance(relation, Function):
        statements, kind = [_create_function(relation, name)], "FUNCTION"
    elif isinstance(relation, Volume):  # pragma: no cover - refused above
        raise ValueError("a SQL spec can't say volumes")
    else:
        text = f"CREATE SCHEMA {name(relation.name)}"
        if relation.comment is not None:
            text += f"\nCOMMENT {quote_literal(relation.comment)}"
        statements, kind = [text + ";"], "SCHEMA"
    if relation.tags and isinstance(relation, Table | View):
        tags = ", ".join(
            f"{quote_literal(k)} = {quote_literal(v)}" for k, v in relation.tags
        )
        statements.append(f"ALTER {kind} {name(relation.name)} SET TAGS ({tags});")
    for grant in relation.grants:
        statements.append(
            f"GRANT {', '.join(grant.privileges)} ON {kind} {name(relation.name)} "
            f"TO {quote_ident(grant.principal)};"
        )
    return "\n\n".join(statements) + "\n"


def _create_table(table: Table, name: Callable[[str], str]) -> str:
    lines = [f"  {_column(column)}" for column in table.columns]
    for constraint in table.constraints:
        prefix = (
            f"CONSTRAINT {maybe_quote_ident(constraint.name)} " if constraint.name else ""
        )
        if isinstance(constraint, PrimaryKey):
            columns = ", ".join(maybe_quote_ident(c) for c in constraint.columns)
            lines.append(f"  {prefix}PRIMARY KEY ({columns})")
        elif isinstance(constraint, ForeignKey):
            columns = ", ".join(maybe_quote_ident(c) for c in constraint.columns)
            referenced = ", ".join(
                maybe_quote_ident(c) for c in constraint.referenced_columns
            )
            lines.append(
                f"  {prefix}FOREIGN KEY ({columns}) "
                f"REFERENCES {name(constraint.references)} ({referenced})"
            )
        else:
            lines.append(f"  {prefix}CHECK ({constraint.expression})")
    text = f"CREATE TABLE {name(table.name)} (\n" + ",\n".join(lines) + "\n)"
    if table.comment is not None:
        text += f"\nCOMMENT {quote_literal(table.comment)}"
    if table.cluster_auto:
        text += "\nCLUSTER BY AUTO"
    elif table.cluster_by:
        text += (
            f"\nCLUSTER BY ({', '.join(maybe_quote_ident(c) for c in table.cluster_by)})"
        )
    properties = spec_properties(table)
    if properties:
        entries = ",\n".join(
            f"  {quote_literal(k)} = {quote_literal(v)}" for k, v in properties.items()
        )
        text += f"\nTBLPROPERTIES (\n{entries}\n)"
    return text + ";"


def _column(column: Field) -> str:
    text = f"{maybe_quote_ident(column.name)} {render_type(column.type, upper=True)}"
    if not column.nullable:
        text += " NOT NULL"
    if column.generated is not None:
        text += f" GENERATED ALWAYS AS ({column.generated})"
    if column.identity is not None:
        how = "ALWAYS" if column.identity.always else "BY DEFAULT"
        text += (
            f" GENERATED {how} AS IDENTITY (START WITH {column.identity.start} "
            f"INCREMENT BY {column.identity.increment})"
        )
    if column.default is not None:
        text += f" DEFAULT {column.default}"
    if column.comment is not None:
        text += f" COMMENT {quote_literal(column.comment)}"
    return text


def _create_view(view: View, name: Callable[[str], str]) -> str:
    text = f"CREATE VIEW {name(view.name)}"
    if view.comment is not None:
        text += f"\nCOMMENT {quote_literal(view.comment)}"
    properties = {k: v for k, v in view.properties if not is_bookkeeping(k)}
    if properties:
        entries = ",\n".join(
            f"  {quote_literal(k)} = {quote_literal(v)}" for k, v in properties.items()
        )
        text += f"\nTBLPROPERTIES (\n{entries}\n)"
    # Written as the catalog holds it, catalog names and all — rewriting names
    # inside SQL isn't something to do by text search.
    return f"{text}\nAS\n{view.query.strip().rstrip(';')};"


def _create_function(function: Function, name: Callable[[str], str]) -> str:
    parameters = ", ".join(
        f"{maybe_quote_ident(p.name)} {render_type(p.type, upper=True)}"
        for p in function.parameters
    )
    text = f"CREATE FUNCTION {name(function.name)}({parameters})"
    text += f"\nRETURNS {render_type(function.returns, upper=True)}"
    if function.comment is not None:
        text += f"\nCOMMENT {quote_literal(function.comment)}"
    return f"{text}\nRETURN {function.body.strip().rstrip(';')};"


# ---------------------------------------------------------------------------


def _substitute(
    path: Path, text: str, variables: Mapping[str, str], unresolved: Mapping[str, str]
) -> str:
    """`${name}` / `${var.name}`, as in YAML — with the error at the variable."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return variables[name]
        start = match.start()
        line = text.count("\n", 0, start) + 1
        column = start - (text.rfind("\n", 0, start) + 1) + 1
        if name in unresolved:
            message = (
                f"variable ${{{name}}} comes from the bundle but {unresolved[name]}; "
                "set it under this target's vars in deltaplan.yml"
            )
        else:
            known = ", ".join(sorted(variables)) or "none defined for this target"
            message = f"undefined variable ${{{name}}} (known: {known})"
        raise SpecError(message, Loc(path, line, column))

    return VARIABLE.sub(replace, text)


@contextmanager
def _quiet() -> Iterator[None]:
    """sqlglot logs a warning when it falls back to an opaque Command; deltaplan
    turns that into an error of its own, so the warning is noise."""
    logger = logging.getLogger("sqlglot")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(level)


class _Reader:
    def __init__(self, path: Path, text: str) -> None:
        self.path = path
        self.text = text

    # -- statements --------------------------------------------------------
    def read(self) -> Relation:
        statements = self._parse()
        if not statements:
            raise SpecError("the SQL spec is empty", Loc(self.path, 1, 1))
        (create, tokens), *rest = statements
        if not isinstance(create, exp.Create):
            raise self._error(
                "a SQL spec starts with CREATE TABLE, CREATE VIEW or CREATE FUNCTION",
                tokens[0],
            )
        kind = str(create.args.get("kind") or "").upper()
        if kind == "TABLE":
            relation: Relation = self._table(create, tokens)
        elif kind == "VIEW":
            relation = self._view(create, tokens)
        elif kind == "FUNCTION":
            relation = self._function(create, tokens)
        elif kind == "SCHEMA":
            relation = self._schema(create, tokens)
        else:
            raise self._error(
                f"CREATE {kind} isn't something deltaplan manages", tokens[0]
            )
        for statement, statement_tokens in rest:
            relation = self._follow_up(relation, statement, statement_tokens)
        return relation

    def _parse(self) -> list[tuple[exp.Expr, list[Token]]]:
        try:
            tokens = sqlglot.tokenize(self.text, read=DIALECT)
        except TokenError as error:
            raise SpecError(
                f"sqlglot can't read this SQL: {error}", Loc(self.path, 1, 1)
            ) from error
        groups: list[list[Token]] = [[]]
        for token in tokens:
            if token.token_type == TokenType.SEMICOLON:
                groups.append([])
            else:
                groups[-1].append(token)
        groups = [group for group in groups if group]
        try:
            with _quiet():
                parsed: list[exp.Expr] = [
                    e for e in sqlglot.parse(self.text, read=DIALECT) if e is not None
                ]
        except ParseError as error:
            first = error.errors[0] if error.errors else {}
            raise SpecError(
                f"sqlglot can't parse this: {first.get('description', error)} — "
                "if it's valid Databricks SQL, sqlglot doesn't support it yet: "
                f"{USE_YAML}",
                Loc(self.path, int(first.get("line") or 1), int(first.get("col") or 1)),
            ) from error
        if len(parsed) != len(groups):  # pragma: no cover - a tokenizer/parser mismatch
            raise SpecError(
                "couldn't match statements to their text", Loc(self.path, 1, 1)
            )
        for statement, group in zip(parsed, groups, strict=True):
            if isinstance(statement, exp.Command):
                words = " ".join(t.text.upper() for t in group[:4])
                raise self._error(
                    f"sqlglot doesn't understand `{words} …` (it only passes it through "
                    f"as text), so a SQL spec can't use it: {USE_YAML}",
                    group[0],
                )
        return list(zip(parsed, groups, strict=True))

    def _follow_up(
        self, relation: Relation, statement: exp.Expr, tokens: list[Token]
    ) -> Relation:
        """`ALTER … SET TAGS` and `GRANT` about the object the spec creates."""
        if isinstance(statement, exp.Alter):
            if _name(statement.this) != relation.name.lower():
                raise self._error(
                    "an ALTER in a spec must be about the object it creates", tokens[0]
                )
            if isinstance(relation, Function):
                raise self._error(f"functions don't take tags: {USE_YAML}", tokens[0])
            tags = dict(relation.tags)
            for action in statement.args.get("actions") or []:
                pairs = (
                    action.args.get("tag") if isinstance(action, exp.AlterSet) else None
                )
                if not pairs:
                    raise self._error(
                        "only ALTER … SET TAGS may follow the CREATE; for anything "
                        f"else, {USE_YAML}",
                        tokens[0],
                    )
                # One tag comes back as Paren(EQ), several as Tuple(EQ, EQ, …).
                for node in pairs:
                    for pair in node.find_all(exp.EQ):
                        tags[_literal(pair.this)] = _literal(pair.expression)
            return _with(relation, tags=tuple(sorted(tags.items())))
        if isinstance(statement, exp.Grant):
            securable = statement.args.get("securable")
            if securable is None or _name(securable) != relation.name.lower():
                raise self._error(
                    "a GRANT in a spec must be on the object it creates", tokens[0]
                )
            allowed = (
                FUNCTION_PRIVILEGES
                if isinstance(relation, Function)
                else SCHEMA_PRIVILEGES
                if isinstance(relation, Schema)
                else TABLE_PRIVILEGES
            )
            privileges: list[str] = []
            for privilege in statement.args.get("privileges") or []:
                raw = (
                    privilege.this.name if privilege.this is not None else privilege.name
                )
                try:
                    privileges.append(privilege_sql(raw, allowed))
                except ValueError as error:
                    raise self._error(str(error), tokens[0]) from error
            held = {grant.principal: set(grant.privileges) for grant in relation.grants}
            for principal in statement.args.get("principals") or []:
                held.setdefault(principal.this.name, set()).update(privileges)
            return _with(
                relation,
                grants=tuple(Grant(p, tuple(sorted(v))) for p, v in held.items()),
            )
        raise self._error(
            "after the CREATE, a SQL spec may only have ALTER … SET TAGS and GRANT "
            f"statements about the same object; for anything else, {USE_YAML}",
            tokens[0],
        )

    # -- tables ------------------------------------------------------------
    def _table(self, create: exp.Create, tokens: list[Token]) -> Table:
        schema = create.this
        if create.args.get("expression") is not None or not isinstance(
            schema, exp.Schema
        ):
            raise self._error(
                "a table spec lists its columns; CREATE TABLE … AS SELECT isn't a spec",
                tokens[0],
            )
        name = _name(schema.this)
        columns: list[Field] = []
        constraints: list[Constraint] = []
        for entry in schema.expressions:
            if isinstance(entry, exp.ColumnDef):
                column, inline = self._column(entry, tokens)
                columns.append(column)
                constraints.extend(inline)
            else:
                constraints.append(self._constraint(entry, tokens))

        comment: str | None = None
        cluster_by: tuple[str, ...] = ()
        cluster_auto = False
        properties: dict[str, str] = {}
        for prop in self._properties(create):
            if isinstance(prop, exp.FileFormatProperty):
                if prop.name.upper() != "DELTA":
                    raise self._error(
                        f"deltaplan manages Delta tables, not USING {prop.name}",
                        tokens[0],
                        "USING",
                    )
            elif isinstance(prop, exp.SchemaCommentProperty):
                comment = _literal(prop.this)
            elif isinstance(prop, exp.ClusterProperty):
                if prop.this is not None and prop.name.upper() == "AUTO":
                    cluster_auto = True
                elif prop.this is not None and prop.name.upper() == "NONE":
                    cluster_by = ()
                else:
                    cluster_by = tuple(column.name for column in prop.expressions)
            elif isinstance(prop, exp.PartitionedByProperty):
                # Not a SQL limitation: YAML can't say it either.
                raise self._error(
                    "deltaplan doesn't model partitioning, in any spec format — it "
                    "reports partitions on a live table but never manages them",
                    tokens[0],
                    "PARTITIONED",
                )
            elif type(prop) is exp.Property:
                properties[_literal(prop.this)] = _literal(prop.args["value"])
            else:
                raise self._unsupported(prop, tokens)
        return Table(
            name=name,
            columns=tuple(columns),
            comment=comment,
            cluster_by=cluster_by,
            cluster_auto=cluster_auto,
            properties=tuple(sorted(properties.items())),
            constraints=tuple(constraints),
        )

    def _column(
        self, definition: exp.ColumnDef, tokens: list[Token]
    ) -> tuple[Field, list[Constraint]]:
        name = definition.name
        kind = definition.args.get("kind")
        if kind is None:
            raise self._error(f"column {name!r} has no type", tokens[0], name)
        data_type = self._type(kind, name, tokens)
        nullable, comment = True, None
        identity: Identity | None = None
        generated: str | None = None
        default: str | None = None
        inline: list[Constraint] = []
        for constraint in definition.args.get("constraints") or []:
            spec = constraint.args.get("kind")
            if isinstance(spec, exp.NotNullColumnConstraint):
                nullable = bool(spec.args.get("allow_null"))
            elif isinstance(spec, exp.CommentColumnConstraint):
                comment = _literal(spec.this)
            elif isinstance(spec, exp.PrimaryKeyColumnConstraint):
                inline.append(PrimaryKey((name,)))
            elif isinstance(spec, exp.GeneratedAsIdentityColumnConstraint):
                identity = Identity(
                    always=bool(spec.this),
                    start=_integer(spec.args.get("start"), 1),
                    increment=_integer(spec.args.get("increment"), 1),
                )
            elif isinstance(spec, exp.ComputedColumnConstraint):
                generated = spec.this.sql(dialect=DIALECT)
            elif isinstance(spec, exp.DefaultColumnConstraint):
                default = spec.this.sql(dialect=DIALECT)
            else:
                raise self._unsupported(constraint, tokens, f"on column {name!r}")
        field = Field(
            name,
            data_type,
            nullable=nullable,
            comment=comment,
            identity=identity,
            generated=generated,
            default=default,
        )
        return field, inline

    def _constraint(self, entry: exp.Expression, tokens: list[Token]) -> Constraint:
        name: str | None = None
        body = entry
        if isinstance(entry, exp.Constraint):
            name = entry.name
            if len(entry.expressions) != 1:
                raise self._unsupported(entry, tokens)
            body = entry.expressions[0]
        if isinstance(body, exp.PrimaryKey):
            return PrimaryKey(tuple(e.name for e in body.expressions), name)
        if isinstance(body, exp.ForeignKey):
            reference = body.args.get("reference")
            target = reference.this if reference is not None else None
            if not isinstance(target, exp.Schema):
                raise self._error(
                    "a FOREIGN KEY names the columns it references", tokens[0], "FOREIGN"
                )
            return ForeignKey(
                tuple(e.name for e in body.expressions),
                _name(target.this),
                tuple(e.name for e in target.expressions),
                name,
            )
        if isinstance(body, exp.CheckColumnConstraint):
            if name is None:
                raise self._error(
                    "a CHECK needs a name: CONSTRAINT <name> CHECK (…)",
                    tokens[0],
                    "CHECK",
                )
            return Check(name, body.this.sql(dialect=DIALECT))
        raise self._unsupported(entry, tokens)

    # -- views and functions -----------------------------------------------
    def _view(self, create: exp.Create, tokens: list[Token]) -> View:
        if isinstance(create.this, exp.Schema):
            raise self._error(
                f"a view's column list isn't supported in a spec: {USE_YAML}", tokens[0]
            )
        comment: str | None = None
        properties: dict[str, str] = {}
        for prop in self._properties(create):
            if isinstance(prop, exp.SchemaCommentProperty):
                comment = _literal(prop.this)
            elif type(prop) is exp.Property:
                properties[_literal(prop.this)] = _literal(prop.args["value"])
            else:
                raise self._unsupported(prop, tokens)
        return View(
            _name(create.this),
            self._text_after(tokens, TokenType.ALIAS, "AS", "the view's query after AS"),
            comment,
            tuple(sorted(properties.items())),
        )

    def _function(self, create: exp.Create, tokens: list[Token]) -> Function:
        udf = create.this
        if not isinstance(udf, exp.UserDefinedFunction):
            raise self._error(
                "couldn't read the function's name and parameters", tokens[0]
            )
        if not isinstance(create.args.get("expression"), exp.Return):
            raise self._error(
                f"only SQL functions with a RETURN body are supported: {USE_YAML}",
                tokens[0],
            )
        parameters: list[Parameter] = []
        for definition in udf.expressions:
            if not isinstance(definition, exp.ColumnDef) or definition.args.get(
                "constraints"
            ):
                raise self._unsupported(definition, tokens, "in the parameter list")
            kind = definition.args.get("kind")
            if kind is None:
                raise self._error(f"parameter {definition.name!r} has no type", tokens[0])
            parameters.append(
                Parameter(definition.name, self._type(kind, definition.name, tokens))
            )
        returns: DataType | None = None
        comment: str | None = None
        for prop in self._properties(create):
            if isinstance(prop, exp.ReturnsProperty) and not prop.args.get("is_table"):
                returns = self._type(prop.this, "RETURNS", tokens)
            elif isinstance(prop, exp.SchemaCommentProperty):
                comment = _literal(prop.this)
            elif isinstance(prop, exp.LanguageProperty) and prop.name.upper() == "SQL":
                continue
            else:
                raise self._unsupported(prop, tokens)
        if returns is None:
            raise self._error("a function spec says what it RETURNS", tokens[0])
        # sqlglot tokenizes RETURN as a plain word, so it's matched by its text.
        body = self._text_after(
            tokens, TokenType.VAR, "RETURN", "the function's body after RETURN"
        )
        return Function(_name(udf.this), tuple(parameters), returns, body, comment)

    def _schema(self, create: exp.Create, tokens: list[Token]) -> Schema:
        comment: str | None = None
        for prop in self._properties(create):
            if isinstance(prop, exp.SchemaCommentProperty):
                comment = _literal(prop.this)
            else:
                raise self._unsupported(prop, tokens)
        return Schema(_name(create.this), comment)

    # -- helpers -----------------------------------------------------------
    def _properties(self, create: exp.Create) -> list[exp.Expression]:
        properties = create.args.get("properties")
        return list(properties.expressions) if properties is not None else []

    def _type(self, kind: exp.Expression, where: str, tokens: list[Token]) -> DataType:
        rendered = kind.sql(dialect=DIALECT)
        try:
            return parse_type(rendered)
        except TypeParseError as error:
            raise self._error(
                f"type of {where!r}: {str(error).splitlines()[0]}", tokens[0], where
            ) from error

    def _text_after(
        self, tokens: list[Token], marker: TokenType, word: str, what: str
    ) -> str:
        """The statement's source text after the first top-level `word` token of
        type `marker` — both, so a string literal reading 'as' can't match."""
        depth = 0
        for index, token in enumerate(tokens):
            if token.token_type in (TokenType.L_PAREN, TokenType.L_BRACKET):
                depth += 1
            elif token.token_type in (TokenType.R_PAREN, TokenType.R_BRACKET):
                depth -= 1
            elif (
                depth == 0
                and token.token_type == marker
                and token.text.upper() == word
                and index + 1 < len(tokens)
            ):
                start, end = tokens[index + 1].start, tokens[-1].end + 1
                return self.text[start:end].strip()
        raise self._error(f"couldn't find {what}", tokens[0])

    def _unsupported(
        self, node: exp.Expression, tokens: list[Token], where: str = ""
    ) -> SpecError:
        clause = node.sql(dialect=DIALECT)
        first = clause.split()[0] if clause.split() else ""
        where = f" {where}" if where else ""
        return self._error(
            f"`{clause}`{where} isn't supported in a SQL spec: {USE_YAML}",
            tokens[0],
            first,
        )

    def _error(self, message: str, at: Token, word: str | None = None) -> SpecError:
        """An error at a token — or, better, at the first occurrence of `word`
        in the statement starting there."""
        line, column = at.line, at.col - len(at.text) + 1
        if word:
            offset = self.text.upper().find(word.upper(), at.start)
            if offset >= 0:
                line = self.text.count("\n", 0, offset) + 1
                column = offset - (self.text.rfind("\n", 0, offset) + 1) + 1
        return SpecError(message, Loc(self.path, line, max(column, 1)))


def _name(node: exp.Expression) -> str:
    """`catalog.schema.name`, unquoted, from a sqlglot table reference."""
    parts = [node.args.get("catalog"), node.args.get("db"), node.this]
    return ".".join(part.name for part in parts if part is not None)


def _literal(node: exp.Expression | None) -> str:
    if node is None:
        return ""
    if isinstance(node, exp.Literal):
        return str(node.this)
    return node.name or node.sql(dialect=DIALECT)


def _integer(node: exp.Expression | None, fallback: int) -> int:
    if node is None:
        return fallback
    return int(node.this if isinstance(node, exp.Literal) else node.sql(dialect=DIALECT))


def _with(relation: Relation, **changes: object) -> Relation:
    from dataclasses import replace

    return replace(relation, **changes)
