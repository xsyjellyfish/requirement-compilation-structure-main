"""Derive the prerequisite records the requirements declare and prove they can be stored.

A scenario that starts with "a verified account and a bootstrap shelf exist" states a
precondition about *stored data*, not about source code. The requirement tree is the only
authority for that data, so the compiler reads it here, once, and publishes the result as
an executable contract:

* ``declarations`` are the ``Seed prerequisites (prepared before this scenario):`` items
  found anywhere in the requirement tree, with the requirement and scenario that stated
  them, so a missing record can be traced back to the sentence that demanded it;
* ``rows`` are those declarations lowered onto the schema: entity, identity columns,
  concrete values, and the reason for every column the declaration does not spell out;
* ``mapped`` records why each declared key landed on each column, so the mapping from
  requirement wording to schema spelling is reviewable rather than implicit: an exact
  field, a known equivalent (``nickname`` -> ``display_name``), a plural fold, or a
  transform (``password`` -> ``password_hash``, which is never stored as plaintext);
* ``external`` are the declarations that name state the application does not own, kept
  as an explicit contract instead of a stored record;
* ``seed_problems`` executes the published DDL in SQLite, inserts every row twice and
  reads each row back, so "the seed works" is observed rather than assumed and "the seed
  is idempotent" is proven by the second insert adding no row;
* ``seed_data_source`` renders the rows as the TypeScript module the runtime seeds from;
* ``seed_gaps`` answers the same question without any DDL, so step 02 can add a missing
  entity before step 05 writes tables that cannot store the prerequisite.

The contract fails closed. A declaration whose entity, identity or required column cannot
be derived is a ``SeedRejected`` in step 05, because a seed that is silently dropped is
what made the acceptance layer green for an application whose scenario prerequisites did
not exist. Deployment seeding and test seeding then share one module:
``backend/src/db/seed.ts`` runs ``seedRows`` at startup and after a test fixture truncates
its own tables, so a compiled test observes the same prerequisites as the deployed app.

Salt policy: a seeded credential is fixture data, so its salt is derived from the row
identity instead of a random source. ``backend/src/db/password.ts`` (compiler-owned) reads
the salt from the stored value, so a random-salt application verifies a seeded credential.
"""

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from pathlib import Path

#: Published seed contract, relative to the application output.
SEED_ROOT = "backend/src/db/seed-data.ts"
#: The one pass allowed to write it.
DATABASE_STAGE = "05_implement_db"

HEADER = re.compile(r"^\s*seed\s+prerequisites?\b", re.I)
ITEM = re.compile(r"^\s*[-*\u2022]\s*(?P<kind>[^:{}]{2,80}?)\s*:\s*(?P<body>\{.*\})\s*$")
WORD = re.compile(r"[a-z0-9]+")

#: scrypt parameters shared with ``backend/src/db/password.ts`` and the fixture helper.
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SCRYPT_SCHEME = "scrypt"

#: Declaration keys and the schema field spellings that name the same persisted concept.
#: A prerequisite states a value in the wording of its scenario ("nickname"); the schema
#: names the column that stores it ("display_name"). The table is explicit so every mapping
#: is reviewable instead of re-inferred per run, and it never merges unrelated concepts.
FIELD_EQUIVALENTS = {
    "nickname": ("nickname", "displayname", "name", "fullname", "screenname", "preferredname"),
    "displayname": ("displayname", "nickname", "name", "fullname", "screenname"),
    "email": ("email", "emailaddress", "mail"),
    "mail": ("email", "emailaddress", "mail"),
    "username": ("username", "loginname", "handle", "user"),
    "loginname": ("loginname", "username", "handle"),
    "phonenumber": ("phonenumber", "phone", "mobile", "telephone"),
    "phone": ("phone", "phonenumber", "mobile", "telephone"),
    "avatarurl": ("avatarurl", "avatar", "picture", "imageurl"),
    "title": ("title", "name", "label"),
    "slug": ("slug", "handle", "key"),
    "url": ("url", "href", "link"),
}

#: Declaration keys that state a credential. A credential is never stored verbatim.
CREDENTIAL_KEYS = frozenset({"password", "passwd", "pwd", "passphrase", "secret", "credential"})

#: Stems that name a one-way encoding of a credential. Matched as substrings so the schema
#: wording a model chooses ("Hashed representation", "b-crypt digest") is recognised.
HASH_STEMS = ("hash", "digest", "scrypt", "bcrypt", "argon", "pbkdf2")

#: Words that mark a prerequisite the application does not own, so no table has to store it.
EXTERNAL_WORDS = frozenset({"external", "thirdparty", "remote", "provider", "sso", "oidc",
    "saml", "ldap", "upstream"})


class SeedRejected(ValueError):
    """A declared prerequisite cannot be stored; ``problems`` names every reason."""

    def __init__(self, message: str, problems: list[str]):
        super().__init__(message + (" " + " ".join(problems) if problems else ""))
        self.problems = list(problems)


def words(text: str) -> set[str]:
    """Return the comparable words of a phrase, with simple plurals folded."""
    found = set()
    for word in WORD.findall(str(text).lower()):
        found.add(word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word)
    return found


def key(text: str) -> str:
    """Return a comparison key that ignores spacing, underscores and case."""
    return "".join(WORD.findall(str(text).lower()))


def quote(identifier: str) -> str:
    """Quote a SQLite identifier."""
    return '"' + str(identifier).replace('"', '""') + '"'


def nodes(tree) -> list[dict]:
    """Return every requirement node of the tree, parents before children."""
    roots = [tree] if isinstance(tree, dict) else [node for node in (tree or []) if isinstance(node, dict)]
    found, stack = [], list(reversed(roots))
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        found.append(node)
        stack.extend(reversed([child for child in node.get("children") or [] if isinstance(child, dict)]))
    return found


def _items(lines: list[str]) -> list[str]:
    """Join the item lines of one seed block, keeping a continuation line with its item."""
    items, pending = [], None
    for line in lines:
        if ITEM.match(line):
            if pending:
                items.append(pending)
            pending = line.strip()
        elif pending and line.strip() and line.startswith((" ", "\t")):
            pending += " " + line.strip()
        elif pending:
            items.append(pending)
            pending = None
    if pending:
        items.append(pending)
    return items


def _block(text: str) -> list[str]:
    """Return the lines that follow a seed-prerequisite header."""
    lines = str(text or "").splitlines()
    for position, line in enumerate(lines):
        if HEADER.search(line):
            return lines[position + 1:]
    return []


def declarations(tree) -> list[dict]:
    """Read every ``Seed prerequisites`` item of the requirement tree, with its source."""
    found = []
    for node in nodes(tree):
        for scenario in node.get("scenarios") or []:
            if not isinstance(scenario, dict):
                continue
            for step in scenario.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                for label in ("Given", "When", "Then", "And"):
                    body = _block(step.get(label) or "")
                    if not body:
                        continue
                    for item in _items(body):
                        match = ITEM.match(item)
                        if not match:
                            continue
                        found.append({"kind": match.group("kind").strip(),
                            "values": match.group("body").strip(),
                            "source": f"{node.get('id')}/{scenario.get('id')}/{step.get('id')}"})
    return found


def json_values(text: str) -> dict | None:
    """Parse one declaration body, tolerating a YAML-wrapped line."""
    candidate = str(text).strip()
    for end in range(len(candidate), 1, -1):
        if candidate[end - 1] != "}":
            continue
        try:
            value = json.loads(candidate[:end])
        except ValueError:
            continue
        if isinstance(value, dict) and value:
            return value
    return None


def is_credential(name: str) -> bool:
    """Return whether a declaration key or column name states a credential."""
    wanted = key(name)
    return bool(wanted) and (wanted in CREDENTIAL_KEYS or wanted.rstrip("s") in CREDENTIAL_KEYS)


def hashed_storage(declared: str, field: dict) -> bool:
    """Return whether a field stores a credential as a one-way encoding, not plaintext."""
    if (field.get("type") or "string") != "string":
        return False
    if any(stem in key(declared) for stem in HASH_STEMS):
        return True
    wording = " ".join([str(field.get("description") or "")]
        + [str(item) for item in field.get("constraints") or []]).lower()
    return any(stem in wording for stem in HASH_STEMS)


def field_match(name: str, fields: dict) -> tuple[str, str] | None:
    """Return the declared field a prerequisite key names, and how it was matched.

    Matching order is exact spelling, then the explicit equivalent table, then a plural
    fold, so a schema keeps its own names while a requirement keeps its own wording.
    """
    wanted = key(name)
    if not wanted:
        return None
    for declared in fields:
        if key(declared) == wanted:
            return declared, "field"
    for candidate in FIELD_EQUIVALENTS.get(wanted, ()):
        for declared in sorted(fields):
            if key(declared) == candidate:
                return declared, "synonym"
    for declared in sorted(fields):
        if key(declared).rstrip("s") == wanted.rstrip("s"):
            return declared, "plural"
    return None


def field_for(name: str, fields: dict) -> str | None:
    """Return the declared field a prerequisite key names, ignoring case and plurals."""
    match = field_match(name, fields)
    return match[0] if match else None


def transform_for(column: str, entity: dict) -> tuple[str, str, str] | None:
    """Map a declared credential onto the column that stores its hash.

    ``{"password": "..."}`` states a credential, while the schema stores
    ``password_hash`` or ``hashed_password``. The declaration is a contract about the
    value, so the compiler applies the encoding the runtime verifies with, names the
    transform, and never writes the declared plaintext.
    """
    if not is_credential(column):
        return None
    fields = entity.get("fields") or {}
    for declared in sorted(fields):
        if not hashed_storage(declared, fields[declared] or {}):
            continue
        return declared, SCRYPT_SCHEME, (f"{SCRYPT_SCHEME} encoding of the declared {column!r}; "
            f"the declared plaintext is never stored")
    return None


def entity_match(kind: str, values: dict, entities: dict) -> tuple[str | None, list[str], str | None]:
    """Choose the entity a declaration describes and name what no entity stores.

    Returns the entity, the declared keys no entity of the schema can store, and the
    closest entity (the one storing the most declared keys) so a rejection can say which
    value has no home instead of blaming every value of the declaration.
    """
    stores = {}
    for column in values:
        stores[column] = [name for name in sorted(entities)
            if field_match(column, (entities[name] or {}).get("fields") or {}) is not None
            or transform_for(column, entities[name] or {}) is not None]
    unresolved = [column for column in values if not stores[column]]
    closest, best = None, None
    for name in sorted(entities):
        covered = sum(1 for column in values if name in stores[column])
        if covered and (closest is None or covered > closest[0]):
            closest = (covered, name)
        if covered != len(values):
            continue
        score = (3 * len(words(kind) & words(name))) + covered
        if best is None or score > best[0]:
            best = (score, name)
    return (best[1] if best else None), unresolved, (closest[1] if closest else None)


def entity_for(kind: str, values: dict, entities: dict) -> str | None:
    """Choose the entity a declaration describes, or ``None`` when no entity fits."""
    return entity_match(kind, values, entities)[0]


def scalar(field: dict, value):
    """Render one declared value as the scalar SQLite stores in that column."""
    kind = field.get("type")
    if kind == "json":
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if kind == "boolean":
        if isinstance(value, bool):
            return 1 if value else 0
        return 1 if str(value).strip().lower() in {"true", "1", "yes", "on"} else 0
    if kind == "integer":
        return int(value)
    if kind == "number":
        return float(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def derived_value(table: str, name: str, field: dict, identity_text: str, kind: str):
    """Return the value and reason for a required column the declaration does not state."""
    declared = field.get("type")
    if field.get("primary_key"):
        if declared in {"integer", "number"}:
            return None
        value = str(uuid.uuid5(uuid.NAMESPACE_URL, f"arc-seed:{table}:{identity_text}"))
        return value, "primary key generated from the prerequisite identity"
    if field.get("references"):
        return None
    if declared == "boolean":
        matches = words(name) & words(kind)
        return (1 if matches else 0), ("set from the prerequisite wording" if matches
            else "default for a required flag the prerequisite does not state")
    if declared == "json":
        wording = " ".join([str(field.get("description") or "")]
            + [str(item) for item in field.get("constraints") or []]).lower()
        return ("[]" if re.search(r"\barray|\blist|\btags\b", wording) else "{}"), "empty collection"
    if declared == "date":
        return "2000-01-01", "filler date for a required column"
    if declared == "datetime":
        return "2000-01-01 00:00:00", "filler timestamp for a required column"
    if declared in {"integer", "number"}:
        return None
    return f"{table}-{name}".lower().replace("_", "-"), "filler text for a required column"


def password_hash(text: str, salt: str) -> str:
    """Encode a declared credential the way ``backend/src/db/password.ts`` verifies it."""
    digest = hashlib.scrypt(str(text).encode("utf-8"), salt=bytes.fromhex(salt),
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN).hex()
    return f"{SCRYPT_SCHEME}${salt}${digest}"


def password_matches(text: str, stored: str) -> bool:
    """Verify a credential against a stored seed hash, mirroring ``verifyPassword`` in password.ts."""
    scheme, _, rest = str(stored or "").partition("$")
    salt, _, digest = rest.partition("$")
    if scheme != SCRYPT_SCHEME or not salt or not digest:
        return False
    try:
        expected = password_hash(text, salt).split("$", 2)[2]
    except ValueError:
        return False
    return hmac.compare_digest(expected, digest)


def statements(table: str, columns: dict, identity: dict) -> tuple[dict, dict]:
    """Build the idempotent insert and the read-back verification for one record."""
    names = sorted(columns)
    insert = {"sql": f"INSERT OR IGNORE INTO {quote(table)} ("
        + ", ".join(quote(name) for name in names) + ") VALUES ("
        + ", ".join("?" for _ in names) + ")", "params": [columns[name] for name in names]}
    keys = sorted(identity)
    verify = {"sql": f"SELECT {', '.join(quote(name) for name in keys)} FROM {quote(table)} WHERE "
        + " AND ".join(quote(name) + " = ?" for name in keys), "params": [identity[name] for name in keys]}
    return insert, verify


def identity_for(table: str, fields: dict, supplied: dict, unknown: list[str]) -> tuple[dict, tuple | None]:
    """Choose the columns that identify the record, so seeding twice stores it once."""
    natural = sorted(name for name in supplied if fields[name].get("unique")
        and not fields[name].get("primary_key") and (fields[name].get("type") or "string") == "string")
    if natural:
        return {natural[0]: scalar(fields[natural[0]], supplied[natural[0]])}, None
    keys = sorted(name for name in supplied if fields[name].get("primary_key"))
    if keys:
        return {keys[0]: scalar(fields[keys[0]], supplied[keys[0]])}, None
    generated = sorted(name for name, field in fields.items() if field.get("primary_key"))
    if generated:
        text = json.dumps({name: scalar(fields[name], supplied[name]) for name in sorted(supplied)},
            sort_keys=True, default=str)
        value = str(uuid.uuid5(uuid.NAMESPACE_URL, f"arc-seed:{table}:{text}"))
        return {generated[0]: value}, (generated[0], value,
            "primary key generated from the prerequisite identity")
    unknown.append(f"The prerequisite of {table} supplies no unique or primary key, so a repeated "
        f"initialization could not tell the stored records apart.")
    return {}, None


def lower(declaration: dict, table: str, entity: dict) -> dict:
    """Lower one declaration onto one entity, naming every value it had to derive."""
    fields = entity.get("fields") or {}
    domain = declaration["sources"][0]
    values = json_values(declaration["values"])
    if values is None:
        raise SeedRejected(declaration["kind"], [f"The prerequisite {domain} of {declaration['kind']} "
            f"does not state a JSON object of values, so no stored record can be derived."])
    supplied, transforms, problems, reasons, mapped = {}, {}, [], [], []
    for column, value in values.items():
        match = field_match(column, fields)
        if match is not None and not is_credential(column):
            supplied[match[0]] = value
            mapped.append({"declared": column, "column": match[0], "how": match[1],
                "reason": ("declared value stored verbatim" if match[1] == "field"
                    else f"declared value stored in {match[0]!r} by a known equivalent")})
            continue
        if match is not None:
            # A column named like the credential itself is only usable when it stores a
            # hash; otherwise the seed would write the declared plaintext password.
            if not hashed_storage(match[0], fields[match[0]] or {}):
                problems.append(f"The prerequisite {domain} declares a password and {table}.{match[0]} "
                    f"stores it verbatim, so the seed would persist a plaintext credential. "
                    f"Store a hash instead.")
                continue
            transforms[match[0]] = (SCRYPT_SCHEME, value, column)
            continue
        mapped_credential = transform_for(column, entity)
        if mapped_credential is None:
            problems.append(f"The prerequisite {domain} declares {column!r}, which no field of {table} "
                f"stores, so the declared value has nowhere to go.")
            continue
        transforms[mapped_credential[0]] = (mapped_credential[1], value, column)
    if problems:
        raise SeedRejected(declaration["kind"], problems)
    identity, generated = identity_for(table, fields, supplied, problems)
    if problems:
        raise SeedRejected(declaration["kind"], problems)
    identity_text = json.dumps(identity, sort_keys=True, default=str)
    columns = {name: scalar(fields[name], value) for name, value in supplied.items()}
    if generated:
        columns[generated[0]] = generated[1]
        reasons.append({"column": generated[0], "reason": generated[2]})
    for name, field in sorted(fields.items()):
        if name in columns or name in transforms or field.get("nullable", True):
            continue
        result = derived_value(table, name, field, identity_text, declaration["kind"])
        if result is None:
            problems.append(f"The prerequisite {domain} does not supply {name!r}, which {table} requires, "
                f"so the record cannot be stored without inventing it.")
            continue
        columns[name], reason = result
        reasons.append({"column": name, "reason": reason})
    if problems:
        raise SeedRejected(declaration["kind"], problems)
    for name, (scheme, value, declared) in sorted(transforms.items()):
        salt = hashlib.sha256((table + ":" + identity_text + ":" + name).encode("utf-8")).hexdigest()[:32]
        columns[name] = password_hash(value, salt)
        reasons.append({"column": name, "reason": f"{scheme} encoding of the declared value"})
        mapped.append({"declared": declared, "column": name, "how": "transform",
            "reason": f"{scheme} hash of the declared credential; the declared plaintext is never stored"})
    insert, verify = statements(table, columns, identity)
    return {"kind": declaration["kind"], "table": table, "identity": identity, "columns": columns,
        "insert": insert, "verify": verify, "derived": reasons, "mapped": mapped,
        "sources": list(declaration["sources"])}


def _merge(rows: list[dict], row: dict, problems: list[str]) -> None:
    """Fold a repeated declaration of the same record into the record it already describes."""
    marker = (row["table"], json.dumps(row["identity"], sort_keys=True, default=str))
    for existing in rows:
        if (existing["table"], json.dumps(existing["identity"], sort_keys=True, default=str)) != marker:
            continue
        if existing["columns"] != row["columns"]:
            problems.append(f"The prerequisite {row['kind']} is declared with different values by "
                f"{', '.join(existing['sources'])} and {', '.join(row['sources'])}, so no single stored "
                f"record can satisfy both sentences.")
            return
        existing["sources"] = sorted(set(existing["sources"]) | set(row["sources"]))
        return
    rows.append(row)


def is_external(declaration: dict) -> bool:
    """Return whether a prerequisite names state the application does not own.

    An external identity ("an account exists on the identity provider") is a contract
    with something outside the database, so no entity has to store it. The declaration is
    published as an explicit external prerequisite instead of being dropped or forced
    into a table.
    """
    return bool(words(declaration.get("kind") or "") & EXTERNAL_WORDS)


def coverage_problem(source: str, kind: str, values: dict, unresolved: list[str],
        closest: str | None, entities: dict | None = None) -> str:
    """Describe exactly which declared values no entity of the schema can store."""
    keys = ", ".join(repr(name) for name in unresolved) if unresolved else ", ".join(sorted(values))
    hint = (f" The closest entity {closest!r} stores the other declared values."
        if closest and unresolved else "")
    if closest:
        for name, field in sorted(((entities or {}).get(closest) or {}).get("fields", {}).items()):
            if is_credential(name) and not hashed_storage(name, field or {}):
                hint += (f" Note: {closest}.{name} stores a credential verbatim, so a declared "
                    f"password needs a hash column instead.")
                break
    return (f"The prerequisite {source} of {kind} declares {keys}, which no entity of the schema "
        f"stores, so the declared value has nowhere to go.{hint} Add a persisted field for it in "
        f"02_build_db_schema or declare the prerequisite as external.")


def initial_data(tree, schema: dict) -> dict:
    """Derive the prerequisite records of one requirement tree for one schema."""
    entities = schema.get("entities") or {}
    data = {"declarations": [], "rows": [], "external": [], "problems": [], "notes": []}
    if tree is None:
        data["notes"].append("The requirement tree is unavailable, so no prerequisite record can be "
            "derived and the published seed contract stays empty.")
        return data
    if not entities:
        data["notes"].append("The schema declares no entity, so no prerequisite record can be stored.")
        return data
    for declaration in declarations(tree):
        values = json_values(declaration["values"])
        source = declaration["source"]
        data["declarations"].append({"kind": declaration["kind"], "source": source,
            "values": values if values is not None else declaration["values"]})
        if is_external(declaration):
            data["external"].append({"kind": declaration["kind"], "source": source,
                "values": values if values is not None else declaration["values"],
                "contract": ("The application must authenticate these values against the provider "
                    "the prerequisite names; the compiler stores no local record for them.")})
            data["notes"].append(f"The prerequisite {declaration['kind']} of {source} names external "
                f"state, so it is published as an external contract instead of a stored record.")
            continue
        if values is None:
            data["problems"].append(f"The prerequisite {source} of {declaration['kind']} does not state "
                f"a JSON object of values, so no stored record can be derived.")
            continue
        table, unresolved, closest = entity_match(declaration["kind"], values, entities)
        if table is None:
            data["problems"].append(coverage_problem(source, declaration["kind"], values,
                unresolved, closest, entities))
            continue
        try:
            row = lower({**declaration, "sources": [source]}, table, entities[table] or {})
        except SeedRejected as rejected:
            data["problems"].extend(rejected.problems)
            continue
        _merge(data["rows"], row, data["problems"])
    data["rows"].sort(key=lambda row: (row["table"], json.dumps(row["identity"], sort_keys=True, default=str)))
    for row in data["rows"]:
        data["notes"].append(f"The prerequisite {row['kind']} is stored in {row['table']} as "
            f"{json.dumps(row['identity'], sort_keys=True)} for {', '.join(row['sources'])}.")
    return data


def seed_prerequisites(tree) -> list[dict]:
    """Return the prerequisites a schema has to be able to store, with their declared values."""
    found = []
    for declaration in declarations(tree or {}):
        if is_external(declaration):
            continue
        values = json_values(declaration["values"])
        if values is None:
            continue
        found.append({"kind": declaration["kind"], "source": declaration["source"], "values": values})
    return found


def seed_gaps(tree, schema: dict) -> list[str]:
    """Return the prerequisites a schema cannot store yet, without lowering any value.

    Entity discovery belongs to step 02, so a prerequisite whose declared values fit no
    entity is repaired there, before any DDL exists, instead of surfacing in step 05 after
    the tables were written.
    """
    entities = (schema or {}).get("entities") or {}
    if tree is None or not entities:
        return []
    problems = []
    for declaration in declarations(tree):
        if is_external(declaration):
            continue
        values = json_values(declaration["values"])
        if values is None:
            problems.append(f"The prerequisite {declaration['source']} of {declaration['kind']} does not "
                f"state a JSON object of values, so no stored record can be derived.")
            continue
        table, unresolved, closest = entity_match(declaration["kind"], values, entities)
        if table is None:
            problems.append(coverage_problem(declaration["source"], declaration["kind"], values,
                unresolved, closest, entities))
    return problems


def load_tree(arc_dir) -> dict | None:
    """Read the compiled requirement tree, or ``None`` when the tree was not published."""
    path = Path(arc_dir) / "requirements.json"
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else None


def _execute(connection, statement: dict, label: str, problems: list[str]) -> None:
    """Run one seed or verification statement, reporting what SQLite refused."""
    try:
        connection.execute(statement["sql"], statement["params"])
    except sqlite3.Error as error:
        problems.append(f"SQLite rejected the {label} ({error}): {statement['sql']}")


def seed_problems(schema: dict, tables: dict[str, str], data: dict, order=None) -> list[str]:
    """Prove in SQLite that every declared prerequisite is stored, once, in the published DDL.

    The published CREATE TABLE statements are executed in dependency order, then every
    seeded record is written twice: the second write must add no row, which is what
    "initialization is idempotent" means for a real deployment that restarts. Each record
    is then read back through its own identity query, so a silently ignored insert is a
    failure instead of a green acceptance run over data that does not exist.
    """
    rows = data.get("rows") or []
    if not rows:
        return []
    entities = schema.get("entities") or {}
    names = [name for name in (order or list(entities)) if name in tables]
    missing = sorted({row["table"] for row in rows} - set(names))
    if missing:
        return [f"The published database has no table for a declared prerequisite: {', '.join(missing)}. "
            f"Implement that entity before seeding it."]
    problems = []
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for name in names:
            try:
                connection.execute(tables[name])
            except sqlite3.Error as error:
                problems.append(f"SQLite rejected the declared table {name} ({error}).")
        connection.execute("BEGIN")
        connection.execute("PRAGMA defer_foreign_keys = ON")
        for row in rows:
            _execute(connection, row["insert"], f"seed insert of {row['kind']}", problems)
        for row in rows:
            _execute(connection, row["insert"], f"repeated seed insert of {row['kind']}", problems)
        for row in rows:
            before = len(problems)
            _execute(connection, row["verify"], f"seed verification of {row['kind']}", problems)
            if len(problems) != before:
                continue
            if not connection.execute(row["verify"]["sql"], row["verify"]["params"]).fetchall():
                problems.append(f"After initialization no {row['table']} record matches "
                    f"{json.dumps(row['identity'], sort_keys=True)}, so the prerequisite "
                    f"{row['kind']} ({', '.join(row['sources'])}) is still missing.")
        for table in sorted({row["table"] for row in rows}):
            expected = len([row for row in rows if row["table"] == table])
            actual = connection.execute(f"SELECT COUNT(*) FROM {quote(table)}").fetchone()[0]
            if actual != expected:
                problems.append(f"Initializing twice stored {actual} rows in {table} where {expected} "
                    f"prerequisite records are declared, so the seed is not idempotent.")
        connection.rollback()
    finally:
        connection.close()
    return problems


SEED_HEADER = '''// Compiler-owned prerequisite records; step 05 regenerates this file from the
// "Seed prerequisites" statements of the requirement tree. Nothing here is model output.
export interface SeedStatement {
  readonly sql: string;
  readonly params: ReadonlyArray<string | number | null>;
}

export interface SeedMapping {
  readonly declared: string;
  readonly column: string;
  readonly how: "field" | "synonym" | "plural" | "transform";
  readonly reason: string;
}

export interface SeedRow {
  readonly kind: string;
  readonly table: string;
  readonly identity: Readonly<Record<string, string | number | null>>;
  readonly columns: Readonly<Record<string, string | number | null>>;
  readonly insert: SeedStatement;
  readonly verify: SeedStatement;
  readonly derived: ReadonlyArray<{ readonly column: string; readonly reason: string }>;
  readonly mapped: ReadonlyArray<SeedMapping>;
  readonly sources: ReadonlyArray<string>;
}

'''


def seed_data_source(data: dict) -> str:
    """Render the derived records as the TypeScript module the runtime seeds from."""
    rows = data.get("rows") or []
    return SEED_HEADER + "export const seedRows: readonly SeedRow[] = " + json.dumps(
        rows, indent=2, ensure_ascii=False) + ";\n"
