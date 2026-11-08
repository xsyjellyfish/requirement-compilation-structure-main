"""Declared prerequisites: requirement wording lowered onto the schema, then stored and verified.

Every case is deterministic. The requirement text is the only authority for the seed, the
schema is the only authority for storage, and SQLite observes both: nothing here mocks the
model, the DDL or the credential encoding the runtime verifies with.
"""

import json
import re
import sqlite3
import unittest
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from infrastructure.stages.database.initial_data import (
    SeedRejected, entity_match, field_for, hashed_storage, initial_data, is_credential,
    password_matches, seed_data_source, seed_gaps, seed_prerequisites, seed_problems,
    transform_for)
from infrastructure.utils.files import save_json

CREDENTIAL = "Password123!"
ACCOUNT = ('verified account: {"nickname": "BookStack User", '
    '"email": "bookstack_user@example.com", "password": "' + CREDENTIAL + '"}')
SHELF = ('bootstrap shelf: {"name": "Benchmark Bootstrap Shelf", '
    '"description": "Entry shelf for the New Shelf creation action.", "tags": [], "books": []}')


def field(kind="string", description="", nullable=False, primary_key=False, unique=False,
        references=None, constraints=()):
    """Build one schema field record with the properties step 02 publishes."""
    return {"description": description, "type": kind, "nullable": nullable,
        "primary_key": primary_key, "unique": unique, "references": references,
        "constraints": list(constraints)}


def account_fields(password_hash=None, **extra):
    """Build the account fields a compiled schema produces for a verified-account flow."""
    fields = {
        "id": field("uuid", "Primary key for the account.", primary_key=True, unique=True),
        "username": field(description="Unique login name for the account.", unique=True,
            constraints=["must be non-empty"]),
        "password_hash": password_hash or field(
            description="Hashed representation of the account password; raw passwords are never stored.",
            constraints=["must not contain plaintext passwords"]),
        "verified": field("boolean", "Whether the account has completed verification."),
        "created_at": field("datetime", "Timestamp when the account was created."),
        "email": field(description="Email address used as the login identifier for the account.",
            unique=True, constraints=["must be non-empty"]),
        "nickname": field("string", "Display name shown to the authenticated user.", nullable=True),
    }
    fields.update(extra)
    return fields


def account_schema(fields=None, extra_entities=None):
    """Build a schema whose account entity mirrors the compiled BookStack schema."""
    entities = {"account": {"description": "A registered user account that can be verified "
        "and later authenticated.", "fields": fields if fields is not None else account_fields()}}
    entities.update(extra_entities or {})
    return {"entities": entities, "requirements": {"REQ-2.2": ["account.email", "account.password_hash"]}}


def requirement_tree(*declarations, rid="REQ-2.2"):
    """Build a requirement tree whose step states the supplied seed prerequisites."""
    return {"id": "ROOT", "children": [{"id": rid, "name": "Log in", "description": "Log in",
        "scenarios": [{"id": f"SCN-{rid}", "name": "Login", "steps": [{"id": f"STEP-{rid}",
            "Given": "Seed prerequisites (prepared before this scenario):\n"
                + "\n".join("  - " + item for item in declarations),
            "When": "Click Login and fill Email and Password with those credentials.",
            "Then": "The dashboard opens for the authenticated account."}]}]}]}


ACCOUNT_SQL = '''CREATE TABLE IF NOT EXISTS "account" (
  "id" TEXT NOT NULL PRIMARY KEY,
  "username" TEXT NOT NULL UNIQUE CHECK (length("username") > 0),
  "password_hash" TEXT NOT NULL,
  "verified" INTEGER NOT NULL CHECK ("verified" IN (0, 1)),
  "created_at" TEXT NOT NULL,
  "email" TEXT NOT NULL UNIQUE CHECK (length("email") > 0),
  "nickname" TEXT
);'''


def table_set():
    """Return the published DDL step 05 writes for the account entity."""
    return {"account": ACCOUNT_SQL}


def stored_hash(row):
    """Return the value the seed row writes into the credential column."""
    columns = re.findall(r'"([^"]+)"', row["insert"]["sql"].split("(", 1)[1])
    return dict(zip(columns, row["insert"]["params"]))["password_hash"]



class AccountEntityRecognition(unittest.TestCase):
    """A verified account is a local entity, and the schema owns its storage."""

    def test_a_verified_account_is_recognised_as_the_account_entity(self):
        row = initial_data(requirement_tree(ACCOUNT), account_schema())["rows"][0]
        self.assertEqual(row["table"], "account")
        self.assertEqual(row["identity"], {"email": "bookstack_user@example.com"})
        self.assertEqual(sorted(item["declared"] for item in row["mapped"]),
            ["email", "nickname", "password"])
        self.assertTrue(all(item["column"] in account_fields() for item in row["mapped"]))

    def test_the_schema_names_account_storage_for_every_declared_value(self):
        values = {"nickname": "BookStack User", "email": "bookstack_user@example.com",
            "password": CREDENTIAL}
        table, unresolved, closest = entity_match("verified account", values,
            account_schema()["entities"])
        self.assertEqual((table, unresolved, closest), ("account", [], "account"))
        shelf_only = {"shelf": {"description": "A shelf", "fields": {"name": field()}}}
        self.assertIsNone(entity_match("verified account", values, shelf_only)[0])

    def test_an_entity_without_a_credential_column_reports_only_the_unstored_value(self):
        fields = account_fields()
        del fields["password_hash"]
        problems = initial_data(requirement_tree(ACCOUNT), account_schema(fields))["problems"]
        self.assertEqual(len(problems), 1)
        self.assertIn("'password'", problems[0])
        self.assertIn("closest entity 'account'", problems[0])
        self.assertNotIn("'email'", problems[0])
        self.assertNotIn("'nickname'", problems[0])


class SemanticFieldMapping(unittest.TestCase):
    """A requirement keeps its own wording; the schema keeps its own names."""

    def test_equivalent_field_names_are_mapped_and_recorded(self):
        fields = {
            "id": field("uuid", "Primary key.", primary_key=True),
            "email_address": field(description="Login email.", unique=True),
            "display_name": field("string", "Name shown to the user.", nullable=True),
            "hashed_password": field(description="One-way encoding of the credential."),
            "verified": field("boolean", "Whether the account is verified."),
        }
        data = initial_data(requirement_tree(ACCOUNT), account_schema(fields))
        self.assertEqual(data["problems"], [])
        mapped = {item["declared"]: (item["column"], item["how"])
            for item in data["rows"][0]["mapped"]}
        self.assertEqual(mapped["nickname"], ("display_name", "synonym"))
        self.assertEqual(mapped["email"], ("email_address", "synonym"))
        self.assertEqual(mapped["password"], ("hashed_password", "transform"))

    def test_an_unrelated_field_is_never_used_as_an_equivalent(self):
        fields = {"email": field(unique=True), "description": field("string", nullable=True)}
        self.assertIsNone(field_for("nickname", fields))
        self.assertIsNone(field_for("password", fields))

    def test_a_credential_is_only_stored_in_a_hash_column(self):
        fields = account_fields()
        self.assertTrue(is_credential("Password"))
        self.assertTrue(hashed_storage("password_hash", fields["password_hash"]))
        self.assertFalse(hashed_storage("password", field(description="The account password.")))
        self.assertIsNotNone(transform_for("password", {"fields": fields}))
        self.assertIsNone(transform_for("password", {"fields": {
            "password": field(description="The raw account password.")}}))


class CredentialStorage(unittest.TestCase):
    """The declared password is stored as the hash the runtime verifies, never as plaintext."""

    def test_the_declared_password_is_stored_as_a_scrypt_hash(self):
        row = initial_data(requirement_tree(ACCOUNT), account_schema())["rows"][0]
        self.assertTrue(stored_hash(row).startswith("scrypt$"))
        self.assertNotIn(CREDENTIAL, json.dumps(row["insert"]["params"]))

    def test_the_seeded_account_verifies_the_declared_password(self):
        schema = account_schema()
        data = initial_data(requirement_tree(ACCOUNT), schema)
        self.assertEqual(seed_problems(schema, table_set(), data), [])
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(ACCOUNT_SQL)
            row = data["rows"][0]
            connection.execute(row["insert"]["sql"], row["insert"]["params"])
            stored = connection.execute('SELECT "password_hash" FROM "account" WHERE "email" = ?',
                ("bookstack_user@example.com",)).fetchone()[0]
        finally:
            connection.close()
        self.assertTrue(password_matches(CREDENTIAL, stored))
        self.assertFalse(password_matches("password123!", stored))
        self.assertFalse(password_matches(CREDENTIAL, "plain-" + CREDENTIAL))

    def test_a_plaintext_password_column_is_refused(self):
        fields = account_fields()
        fields["password"] = field(description="The account password.")
        data = initial_data(requirement_tree(ACCOUNT), account_schema(fields))
        self.assertEqual(data["rows"], [])
        self.assertTrue(any("plaintext credential" in problem for problem in data["problems"]))

    def test_a_plaintext_credential_column_is_named_in_the_diagnosis(self):
        fields = {"email": field(description="Login email.", unique=True),
            "password": field(description="The account password.")}
        problems = initial_data(requirement_tree(ACCOUNT), account_schema(fields))["problems"]
        self.assertEqual(len(problems), 1)
        self.assertIn("'nickname'", problems[0])
        self.assertIn("account.password stores a credential verbatim", problems[0])

    def test_the_published_seed_module_never_contains_the_plaintext_password(self):
        source = seed_data_source(initial_data(requirement_tree(ACCOUNT), account_schema()))
        self.assertNotIn(CREDENTIAL, source)
        self.assertIn("scrypt$", source)
        self.assertIn('"mapped"', source)
        body = source.split("export const seedRows: readonly SeedRow[] = ", 1)[1].rstrip().rstrip(";")
        self.assertEqual(json.loads(body)[0]["table"], "account")

    def test_the_published_interface_declares_every_key_a_row_carries(self):
        """A row key the interface omits is a TypeScript excess-property error in step 08."""
        source = seed_data_source(initial_data(requirement_tree(ACCOUNT), account_schema()))
        interface = source.split("export interface SeedRow {", 1)[1].split("\n}", 1)[0]
        declared = set(re.findall(r"readonly (\w+):", interface))
        body = source.split("export const seedRows: readonly SeedRow[] = ", 1)[1].rstrip().rstrip(";")
        self.assertEqual(set(json.loads(body)[0]) - declared, set())
        self.assertIn("columns", declared)


class SharedAndIdempotentSeeding(unittest.TestCase):
    """Several requirements share one stored account, and a restart adds no row."""

    def test_two_requirements_sharing_one_account_store_one_row(self):
        tree = {"id": "ROOT", "children": [
            {"id": rid, "name": "Log in", "description": "Log in", "scenarios": [{"id": f"SCN-{rid}",
                "steps": [{"id": f"STEP-{rid}", "Given": "Seed prerequisites (prepared before "
                    f"this scenario):\n  - {ACCOUNT}", "When": "Log in.", "Then": "The home opens."}]}]}
            for rid in ("REQ-2.2", "REQ-3.1")]}
        data = initial_data(tree, account_schema())
        self.assertEqual(data["problems"], [])
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["sources"], ["REQ-2.2/SCN-REQ-2.2/STEP-REQ-2.2",
            "REQ-3.1/SCN-REQ-3.1/STEP-REQ-3.1"])
        self.assertEqual(seed_problems(account_schema(), table_set(), data), [])

    def test_initialization_is_idempotent_when_the_seed_runs_twice(self):
        schema = account_schema()
        data = initial_data(requirement_tree(ACCOUNT), schema)
        self.assertEqual(seed_problems(schema, table_set(), data), [])
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(ACCOUNT_SQL)
            row = data["rows"][0]
            for _ in range(3):
                connection.execute(row["insert"]["sql"], row["insert"]["params"])
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM "account"').fetchone()[0], 1)
        finally:
            connection.close()

    def test_a_shelf_and_an_account_seed_together(self):
        shelf_fields = {
            "id": field("uuid", "Primary key.", primary_key=True, unique=True),
            "name": field(description="Shelf name.", unique=True),
            "description": field("string", "Shelf description."),
            "tags": field("json", "Shelf tags."),
            "books": field("json", "Books on the shelf."),
            "is_bootstrap": field("boolean", "Whether this is the bootstrap shelf."),
        }
        schema = account_schema(extra_entities={"shelf": {
            "description": "A shelf of books.", "fields": shelf_fields}})
        data = initial_data(requirement_tree(ACCOUNT, SHELF), schema)
        self.assertEqual(data["problems"], [])
        self.assertEqual([row["table"] for row in data["rows"]], ["account", "shelf"])


class FailClosedStorage(unittest.TestCase):
    """A schema that cannot store the prerequisite still blocks the pass."""

    def test_a_schema_without_account_storage_fails_closed(self):
        schema = {"entities": {"shelf": {"description": "A shelf of books", "fields": {
            "name": field(description="Shelf name.", unique=True)}}}, "requirements": {}}
        data = initial_data(requirement_tree(ACCOUNT), schema)
        self.assertEqual(data["rows"], [])
        self.assertEqual(len(data["problems"]), 1)
        self.assertIn("verified account", data["problems"][0])
        self.assertIn("'password'", data["problems"][0])
        self.assertEqual(seed_gaps(requirement_tree(ACCOUNT), schema), data["problems"])

    def test_step_two_reports_gaps_only_for_prerequisites_without_storage(self):
        schema = account_schema()
        self.assertEqual(seed_gaps(requirement_tree(ACCOUNT), schema), [])
        gaps = seed_gaps(requirement_tree(ACCOUNT, SHELF), schema)
        self.assertEqual(len(gaps), 1)
        self.assertIn("bootstrap shelf", gaps[0])
        self.assertIn("'description'", gaps[0])
        fields = account_fields()
        del fields["password_hash"]
        self.assertEqual(len(seed_gaps(requirement_tree(ACCOUNT), account_schema(fields))), 1)
        self.assertEqual(seed_gaps(None, schema), [])

    def test_an_external_identity_prerequisite_is_published_not_stored(self):
        external = ('external identity provider account: {"email": "bookstack_user@example.com", '
            '"password": "' + CREDENTIAL + '"}')
        data = initial_data(requirement_tree(external), account_schema())
        self.assertEqual(data["problems"], [])
        self.assertEqual(data["rows"], [])
        self.assertEqual(len(data["external"]), 1)
        self.assertEqual(data["external"][0]["values"]["email"], "bookstack_user@example.com")
        self.assertIn("contract", data["external"][0])
        self.assertEqual(seed_gaps(requirement_tree(external), account_schema()), [])
        self.assertEqual(seed_prerequisites(requirement_tree(external)), [])


class SchemaDerivationRepair(unittest.TestCase):
    """Step 02 owns entity discovery, so a prerequisite without storage is repaired there."""

    def setUp(self):
        self.stage = import_module("02_build_db_schema")
        self.model = Mock()
        self.console = Mock()

    def prepare(self, schema):
        """Publish the artifacts step 02 reads, and return the .arc directory."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        arc = Path(temporary.name) / ".arc"
        arc.mkdir()
        save_json(arc / "requirements.json", requirement_tree(ACCOUNT))
        save_json(arc / "db_schema.json", schema)
        return arc

    def shelf_only_schema(self):
        return {"entities": {"shelf": {"description": "A shelf of books", "fields": {
            "name": field(description="Shelf name.", unique=True)}}},
            "requirements": {"REQ-2.2": []}}

    def test_a_storable_schema_needs_no_model_call(self):
        schema = account_schema()
        arc = self.prepare(schema)
        self.assertIs(self.stage.repair_seed_storability(arc, schema, self.model, self.console, 3),
            schema)
        self.assertEqual(self.model.generate_json.call_count, 0)
        self.assertFalse((arc / "seed_precheck.json").exists())

    def test_step_two_repairs_a_schema_that_cannot_store_a_prerequisite(self):
        schema = self.shelf_only_schema()
        arc = self.prepare(schema)
        self.model.generate_json.return_value = {
            "entities": {"account": {"description": "A registered user account.", 
                "fields": account_fields()}},
            "related_fields": ["account.email", "account.password_hash"]}
        repaired = self.stage.repair_seed_storability(arc, schema, self.model, self.console, 3)
        self.assertIn("account", repaired["entities"])
        self.assertIn("account.password_hash", repaired["requirements"]["REQ-2.2"])
        self.assertEqual(seed_gaps(requirement_tree(ACCOUNT), repaired), [])
        record = json.loads((arc / "seed_precheck.json").read_text(encoding="utf-8"))
        self.assertTrue(record["ok"])
        self.assertEqual(len(record["attempts"]), 1)
        published = json.loads((arc / "db_schema.json").read_text(encoding="utf-8"))
        self.assertIn("password_hash", published["entities"]["account"]["fields"])

    def test_step_two_fails_closed_when_no_repair_can_store_it(self):
        schema = self.shelf_only_schema()
        arc = self.prepare(schema)
        self.model.generate_json.return_value = {"entities": {}, "related_fields": []}
        with self.assertRaises(SeedRejected) as raised:
            self.stage.repair_seed_storability(arc, schema, self.model, self.console, 3)
        self.assertTrue(any("'password'" in problem for problem in raised.exception.problems))
        self.assertEqual(self.model.generate_json.call_count, 2)
        record = json.loads((arc / "seed_precheck.json").read_text(encoding="utf-8"))
        self.assertFalse(record["ok"])
        self.assertEqual(len(record["gaps"]), 1)



if __name__ == "__main__":
    unittest.main()

