"""Accept SQLite DDL only when it can actually store a legal record.

SQLite accepts a ``CREATE TABLE`` statement that it will later refuse to use. An
expression that reads the clock or a random source is not rejected while the table is
created; it is rejected when the first row is written::

    non-deterministic use of date() in a CHECK constraint

That difference matters for the pipeline. Step 05 used to trust the statement because it
parsed, so a table such as ``CHECK ("passport_expiration_date" > date('now'))`` looked
implemented while every tested INSERT failed in Step 08. Step 08 can read the SQL but must
not edit it, so its repair loop could never succeed and repeated the same failure.

This module closes that gap deterministically and without a model:

* ``non_deterministic`` names the CHECK, generated-column and index expressions that
  SQLite cannot evaluate for a stored row;
* ``smoke_problems`` executes the DDL in an in-memory database and inserts one legal
  record per entity, so a refusal is reported against the table that caused it;
* ``upstream_blocker`` turns a later pass's database evidence into a Step 05 repair
  request instead of a repair attempt in a stage that cannot change the DDL, and
  ``upstream_repair`` keeps that request only while the published SQL still fails a
  legal record, so a sound schema is never rewritten on stale evidence.

Nothing here decides acceptance of a requirement or a test: it only decides whether the
declared structure can hold the records the schema describes.
"""

import json
import re
import sqlite3
from pathlib import Path

from infrastructure.stages.database.order import table_dependencies

#: Directory of the published table implementations, relative to the application output.
TABLES_ROOT = "backend/src/db/tables"
#: The one pass allowed to write that directory.
DATABASE_STAGE = "05_implement_db"

CHECK = re.compile(r"\bCHECK\s*\(", re.I)
GENERATED = re.compile(r"\bGENERATED\s+ALWAYS\s+AS\s*\(", re.I)
CREATE_INDEX = re.compile(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b", re.I)
INDEX_ON = re.compile(r"\bON\b\s+(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|\w+)\s*(?=\()", re.I)
INDEX_WHERE = re.compile(r"\bWHERE\b", re.I)
IN_LIST = re.compile(r"\"?(\w+)\"?\s+IN\s*\(([^)]*)\)", re.I)
EQUALS = re.compile(r"\"?(\w+)\"?\s*=\s*('(?:[^']*)'|\d+(?:\.\d+)?)")
LITERAL = re.compile(r"'([^']*)'")
MINIMUM = re.compile(r"(?:at least|minimum(?: of)?|no fewer than|length\s*>=\s*)\s*(\d+)", re.I)
OPTIONS = re.compile(r"one of:?\s*(.+)", re.I)

#: Expressions SQLite refuses once a row has to be stored. ``CURRENT_TIMESTAMP`` and a
#: ``DEFAULT (date('now'))`` are deliberately not listed: SQLite evaluates a default per
#: row, so it is storable, while a stored row can never satisfy a clock comparison.
NON_DETERMINISTIC = (
    (re.compile(r"\b(?:date|time|datetime|julianday|unixepoch|strftime|timediff)\s*"
        r"\(\s*['\"](?:now|localtime)['\"]", re.I), "depends on the current clock"),
    (re.compile(r"\bCURRENT_(?:DATE|TIME|TIMESTAMP)\b", re.I), "depends on the current clock"),
    (re.compile(r"\b(?:random|randomblob)\s*\(", re.I), "depends on a random value"),
    (re.compile(r"\b(?:last_insert_rowid|changes|total_changes|sqlite_version|sqlite_source_id)\s*\(",
        re.I), "depends on session state"),
)

#: Evidence that a later pass failed because of the database structure, not its own code.
STRUCTURE_MARKERS = ("non-deterministic use of", "in a CHECK constraint", "in a generated column",
    "in an index", "SQLITE_ERROR", "SQLITE_CONSTRAINT", "SQLITE_MISUSE", "no such table",
    "no such column", "FOREIGN KEY constraint failed", "foreign key mismatch", TABLES_ROOT)


class SchemaRejected(ValueError):
    """One table cannot store a legal record; ``feedback`` is the repair request."""

    def __init__(self, table: str, problems: list[str]):
        super().__init__(table + ": " + " ".join(problems))
        self.feedback = {"error": f"SQLite cannot store a legal {table} record",
            "details": {"table": table, "problems": list(problems),
                "rule": "Return complete replacement SQL that SQLite executes and stores a legal record for."}}


class SchemaBlocked(ValueError):
    """A later pass found a database defect that only Step 05 can repair."""

    def __init__(self, message: str, upstream: dict):
        super().__init__(message)
        self.upstream = upstream


def statements(content: str) -> list[str]:
    """Split DDL into statements, ignoring comments and quoted text."""
    found, current, quote = [], [], None
    position, text = 0, str(content)
    while position < len(text):
        char = text[position]
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            position += 1
            continue
        if char in "'\"`":
            quote = char
            current.append(char)
        elif text.startswith("--", position):
            end = text.find("\n", position)
            position = len(text) if end < 0 else end
            continue
        elif text.startswith("/*", position):
            end = text.find("*/", position)
            position = len(text) if end < 0 else end + 2
            continue
        elif char == ";":
            found.append("".join(current))
            current = []
        else:
            current.append(char)
        position += 1
    found.append("".join(current))
    return [statement.strip() for statement in found if statement.strip()]


def expression(text: str, start: int) -> str:
    """Return the parenthesised expression that begins at ``start``."""
    depth = 0
    for position in range(start, len(text)):
        if text[position] == "(":
            depth += 1
        elif text[position] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:position].strip()
    return text[start + 1:].strip()


def expressions(content: str) -> list[tuple[str, str]]:
    """Name every expression SQLite evaluates while a row is written, with its context."""
    found = []
    for statement in statements(content):
        for match in CHECK.finditer(statement):
            found.append(("CHECK constraint", expression(statement, match.end() - 1)))
        for match in GENERATED.finditer(statement):
            found.append(("generated column", expression(statement, match.end() - 1)))
        if CREATE_INDEX.match(statement):
            on = INDEX_ON.search(statement)
            if on and statement[on.end():on.end() + 1] == "(":
                found.append(("index expression", expression(statement, on.end())))
            for match in INDEX_WHERE.finditer(statement):
                rest = statement[match.end():]
                offset = len(rest) - len(rest.lstrip())
                found.append(("index predicate",
                    expression(statement, match.end() + offset) if rest.lstrip().startswith("(")
                    else rest.strip()))
    return found


def non_deterministic(content: str) -> list[str]:
    """Reject DDL whose CHECK, generated column or index cannot store a stable row."""
    problems = []
    for context, body in expressions(content):
        for pattern, reason in NON_DETERMINISTIC:
            if pattern.search(body):
                problems.append(
                    f"The {context} {body!r} {reason}, which SQLite cannot evaluate while storing a "
                    f"row. SQLite creates the table but refuses every row that reaches the unstable "
                    f"value, so no legal record can be stored. Remove the time- or "
                    f"randomness-dependent expression and implement that rule in the API instead of "
                    f"in the DDL.")
    return problems


def check_values(content: str) -> dict:
    """Derive sample column values from the DDL's own IN and equality CHECK constraints."""
    values = {}
    for _context, body in expressions(content):
        for column, options in IN_LIST.findall(body):
            literals = LITERAL.findall(options)
            if literals:
                values.setdefault(column, literals[0])
        for column, literal in EQUALS.findall(body):
            if literal.startswith("'"):
                values.setdefault(column, literal[1:-1])
            else:
                values.setdefault(column, float(literal) if "." in literal else int(literal))
    return values


def declared_options(field: dict) -> list[str]:
    """Read an enumeration the schema describes in words, such as "Must be one of: A, B"."""
    for constraint in field.get("constraints") or []:
        match = OPTIONS.search(str(constraint))
        if match:
            options = [option.strip() for option in match.group(1).replace(" and ", ", ").split(",")]
            return [option for option in options if option]
    return []


def sample_text(field: dict, name: str, table: str) -> str:
    """Return a stable string that honors a declared minimum length."""
    value = f"{table}-{name}".lower().replace("_", "-")
    match = MINIMUM.search(" ".join(str(item) for item in field.get("constraints") or []))
    return value.ljust(int(match.group(1)), "x") if match else value


def sample_value(name: str, field: dict, table: str, hints: dict, inserted: dict, row: dict):
    """Choose one stored value for a field: a CHECK hint, a referenced key, or its type."""
    if name in hints:
        return hints[name]
    references = field.get("references")
    if references:
        parent, _, column = str(references).partition(".")
        column = column or "id"
        parent_row = row if parent == table else (inserted.get(parent) or {})
        if isinstance(parent_row, dict) and column in parent_row:
            return parent_row[column]
    kind = field.get("type")
    if field.get("primary_key"):
        return 1 if kind in {"integer", "number"} else f"{table.lower()}-key"
    if kind in {"integer", "boolean"}:
        return 1
    if kind == "number":
        return 1.5
    if kind == "date":
        return "2000-01-01"
    if kind == "datetime":
        return "2000-01-01T00:00:00.000Z"
    if kind == "uuid":
        return f"00000000-0000-4000-8000-{abs(hash((table, name))) % 10 ** 12:012d}"
    if kind == "json":
        return "{}"
    options = declared_options(field)
    return options[0] if options else sample_text(field, name, table)


def sample_row(table: str, entity: dict, hints: dict, inserted: dict) -> dict:
    """Build one legal record: primary keys first, so references can point at them."""
    fields = entity.get("fields") or {}
    row = {}
    for name, field in fields.items():
        if field.get("primary_key"):
            row[name] = sample_value(name, field, table, hints, inserted, row)
    for name, field in fields.items():
        if name not in row:
            row[name] = sample_value(name, field, table, hints, inserted, row)
    return row


def describe(statement: str, error: sqlite3.Error, row: dict | None = None) -> str:
    """Explain what SQLite refused, keeping the message that names the defect."""
    message = f"SQLite rejected the {statement}: {error}."
    detail = str(error).lower()
    if "non-deterministic" in detail:
        message += (" The expression depends on the clock or on random data, so no stored record "
            "can satisfy it; keep the DDL deterministic and implement that rule in the API.")
    if "no such table" in detail or "no such column" in detail:
        message += " Use the declared entity and field names exactly and create every referenced table."
    if "foreign key mismatch" in detail:
        message += " Declare the FOREIGN KEY against a primary or UNIQUE key of the referenced table."
    if row is not None:
        message += f" The legal sample record was {row}."
    return message


def smoke_problems(tables: dict[str, str], schema: dict, order=None) -> dict[str, list[str]]:
    """Execute every supplied CREATE TABLE and insert one legal record per entity.

    A partial set (a table whose parents are not implemented yet) runs without foreign-key
    enforcement, because a missing parent is not a defect of this table. The complete set
    runs with foreign keys enabled and is checked with ``PRAGMA foreign_key_check``, which
    names the child table whose reference does not resolve.
    """
    entities = schema.get("entities") or {}
    names = [name for name in (order or list(entities)) if name in tables]
    problems = {}
    if not names:
        return problems
    dependencies = table_dependencies(schema)
    complete = all(target in names for name in names for target in dependencies.get(name, []))
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = " + ("ON" if complete else "OFF"))
        created = []
        for name in names:
            try:
                connection.execute(tables[name])
                created.append(name)
            except sqlite3.Error as error:
                problems.setdefault(name, []).append(describe("CREATE TABLE", error))
        connection.execute("BEGIN")
        if complete:
            connection.execute("PRAGMA defer_foreign_keys = ON")
        inserted = {}
        for name in created:
            row = sample_row(name, entities.get(name) or {"fields": {}}, check_values(tables[name]), inserted)
            inserted[name] = row
            columns = ", ".join(f'"{column}"' for column in row)
            marks = ", ".join("?" for _ in row)
            try:
                connection.execute(f'INSERT INTO "{name}" ({columns}) VALUES ({marks})', list(row.values()))
            except sqlite3.Error as error:
                inserted[name] = None
                problems.setdefault(name, []).append(describe("INSERT", error, row))
        if complete:
            for child, _rowid, parent, _key in connection.execute("PRAGMA foreign_key_check").fetchall():
                if inserted.get(parent) is not None:
                    problems.setdefault(child, []).append(
                        f"The FOREIGN KEY of {child} does not resolve to a stored {parent} key.")
        connection.rollback()
    finally:
        connection.close()
    return problems


def record_problems(name: str, content: str, tables: dict[str, str], schema: dict, order=None) -> list[str]:
    """Return every reason SQLite cannot store a legal record for one table."""
    return non_deterministic(content) + smoke_problems({**tables, name: content}, schema, order).get(name, [])


def published_tables(output, names) -> dict[str, str]:
    """Read the SQL that a previous Step 05 run already published for these tables."""
    root = Path(output)
    tables = {}
    for name in names:
        path = root / TABLES_ROOT / f"{name}.sql"
        if path.is_file():
            tables[name] = path.read_text(encoding="utf-8-sig", errors="replace")
    return tables


def implemented_tables(output, schema: dict, order=None) -> dict[str, str]:
    """Read the published SQL of every declared entity, in implementation order."""
    return published_tables(output, order or list(schema.get("entities") or {}))


def upstream_blocker(text: str, schema: dict, tables: dict[str, str], order=None) -> dict | None:
    """Return a Step 05 repair request when a failure names a defect in the published SQL.

    The evidence names the cause, but the published SQL decides whether Step 05 can act on
    it: while SQLite still stores a legal record for every table, the structure is not the
    defect and rewriting it would spend model calls without changing the outcome. When no
    SQL was published at all there is nothing to verify, so the evidence stands on its own.
    """
    evidence = str(text)
    markers = [marker for marker in STRUCTURE_MARKERS if marker.lower() in evidence.lower()]
    failures = smoke_problems(tables, schema, order)
    if failures:
        names = sorted(failures)
    elif markers and not tables:
        names = sorted(schema.get("entities") or {})
    else:
        return None
    return {"stage": DATABASE_STAGE, "tables": names,
        "reason": "; ".join(markers) or "SQLite refuses a legal record",
        "problems": failures, "evidence": evidence[:1500]}


def repair_request(upstream: dict) -> dict[str, dict]:
    """Turn a blocker into the per-table correction map ``implement_db`` accepts."""
    problems = upstream.get("problems") or {}
    evidence = upstream.get("evidence") or ""
    return {name: {"error": "A later pass could not store a legal record in this table.",
            "details": {"table": name, "problems": problems.get(name) or [upstream.get("reason", "")],
                "evidence": evidence}}
        for name in upstream.get("tables") or []}


def upstream_repair(arc_dir, output, evidence: str) -> dict[str, dict]:
    """Build the Step 05 repair map for a blocked pass, empty when the published DDL is sound.

    A blocked pass reports the text that named the cause; the evidence is only useful if the
    published SQL still refuses a legal record. When it does not, the cause is not the DDL
    (a read-only database file, for example) and Step 05 must not rewrite the whole schema.
    """
    source = Path(arc_dir) / "db_schema.json"
    if not source.is_file():
        return {}
    schema = json.loads(source.read_text(encoding="utf-8-sig"))
    blocker = upstream_blocker(evidence, schema, implemented_tables(output, schema))
    return repair_request(blocker) if blocker else {}

