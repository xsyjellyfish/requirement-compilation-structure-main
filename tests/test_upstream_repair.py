"""The SQLite structure a later pass cannot write must be repaired by the pass that owns it.

Three defects keep Step 08 from ever reaching RED: DDL that SQLite creates but refuses to
store, a RED phase that accepts a failure no assertion caused, and a repair loop that
retries a cause outside its writable files. These tests pin the evidence each one uses:
the insert smoke test, the failure classification, and the request that routes the defect
back to Step 05 instead of repeating an edit that cannot take effect.
"""

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from infrastructure.compiler import Compiler
from infrastructure.reporting.status import GREEN, classify as classify_status
from infrastructure.shared.agent.diagnosis import blocker
from infrastructure.shared.agent.files import FileWorkspace
from infrastructure.shared.agent.loop import (
    STALL_LIMIT, AgentBlocked, AgentExhausted, AgentStalled, run_file_agent)
from infrastructure.shared.agent.recovery import implementation_task
from infrastructure.stages.backend.testing import RedRejected, require_red
from infrastructure.stages.database.acceptance import (
    DATABASE_STAGE, SchemaBlocked, record_problems, repair_request, smoke_problems, upstream_blocker,
    upstream_repair)
from infrastructure.utils.files import save_json, save_text

#: The 13 passes the compiler runs, with the function each module must expose.
PASSES = [
    ("00_project_initialization", "project_initialization"),
    ("01_explore_requirement", "explore_requirement"),
    ("02_build_db_schema", "build_db_schema"),
    ("03_infer_db_obligations", "infer_db_obligations"),
    ("04_check_db_obligations", "check_db_obligations"),
    ("05_implement_db", "implement_db"),
    ("06_design_backend_apis", "design_backend_apis"),
    ("07_check_backend_apis", "check_backend_apis"),
    ("08_implement_backend_tdd", "implement_backend_tdd"),
    ("09_design_ui", "design_frontend_ui"),
    ("10_implement_ui", "implement_frontend_ui"),
    ("11_design_frontend_triggers", "design_frontend_triggers"),
    ("12_implement_frontend_tdd", "implement_frontend_triggers"),
]

SCHEMA = {"entities": {"notes": {"fields": {
    "id": {"type": "integer", "primary_key": True, "references": None},
    "status": {"type": "text", "references": None},
    "archived_at": {"type": "date", "references": None}}}}}

#: SQLite creates this table and then refuses every row: the CHECK reads the clock.
CLOCK_DEPENDENT = '''CREATE TABLE "notes" (
    "id" INTEGER PRIMARY KEY,
    "status" TEXT NOT NULL CHECK ("status" IN ('draft', 'published')),
    "archived_at" DATE NOT NULL CHECK ("archived_at" > date('now'))
);'''

#: The same rule expressed deterministically keeps the structure storable.
STORABLE = '''CREATE TABLE "notes" (
    "id" INTEGER PRIMARY KEY,
    "status" TEXT NOT NULL CHECK ("status" IN ('draft', 'published')),
    "archived_at" DATE NOT NULL DEFAULT (date('now'))
);'''

ASSERTION_FAILURE = """not ok 1 - rejects a note without a status
  ---
  failureType: 'testCodeFailure'
  error: |-
    Expected values to be strictly equal:
    + actual - expected
  code: 'ERR_ASSERTION'
  ...
"""

DATABASE_FAILURE = """not ok 2 - stores a published note
  ---
  failureType: 'testCodeFailure'
  error: |
    SqliteError: non-deterministic use of date() in a CHECK constraint
  code: 'SQLITE_ERROR'
  ...
"""

INFRASTRUCTURE_FAILURE = """not ok 1 - suite
  ---
  failureType: 'testCodeFailure'
  error: |
    SyntaxError: Unexpected token 'export'
  code: 'ERR_TEST_FAILURE'
  ...
"""

RUNTIME_FAILURE = """not ok 1 - stores a published note
  ---
  failureType: 'testCodeFailure'
  error: |
    TypeError: Cannot read properties of undefined (reading 'status')
  code: 'ERR_TEST_FAILURE'
  ...
"""


def report(output, returncode=1, tests=2, passed=0, failed=2):
    """Build the test report shape the stage hands to the RED gate."""
    return {"returncode": returncode, "output": output, "tests": tests, "pass": passed, "fail": failed}


def blocked_gate():
    """Stand in for an acceptance check that SQLite refuses to run at all."""
    raise ValueError("SqliteError: non-deterministic use of date() in a CHECK constraint")


def blocked_agent(root):
    """Return a model, workspace and console whose first acceptance check is blocked."""
    relative = "backend/src/note.ts"
    save_text(root / relative, "export function handle() {}")
    workspace = FileWorkspace(root, {relative})
    model = Mock()
    model.generate_json.return_value = {"edits": [{"path": relative,
        "content": "export function handle() { return 1; }"}]}
    return model, workspace, Mock()


class DatabaseSmokeTests(unittest.TestCase):
    """A table is accepted only when a legal record can actually be stored in it."""

    def test_a_clock_dependent_check_is_reported_against_its_table(self):
        problems = smoke_problems({"notes": CLOCK_DEPENDENT}, SCHEMA)
        self.assertEqual(set(problems), {"notes"})
        self.assertIn("non-deterministic", problems["notes"][0])
        # The same defect is reported before publication, so Step 05 never writes the SQL.
        self.assertTrue(record_problems("notes", CLOCK_DEPENDENT, {}, SCHEMA))

    def test_a_deterministic_check_and_a_clock_default_remain_storable(self):
        self.assertEqual(smoke_problems({"notes": STORABLE}, SCHEMA), {})
        self.assertEqual(record_problems("notes", STORABLE, {}, SCHEMA), [])

    def test_an_unexecutable_statement_names_the_creator(self):
        problems = smoke_problems({"notes": 'CREATE TABLE "notes" ("id" INTEGER, "other" TEXT)'},
            SCHEMA)
        self.assertIn("no column named", " ".join(problems["notes"]))


class RedAcceptanceTests(unittest.TestCase):
    """RED is proven by failing assertions, never by a suite that could not run."""

    def test_a_database_refusal_rejects_red_next_to_an_assertion(self):
        """The assertion proves the contract, but no round can turn this report GREEN."""
        with self.assertRaises(RedRejected) as raised:
            require_red(report(ASSERTION_FAILURE + DATABASE_FAILURE))
        self.assertEqual(raised.exception.category, "database")
        self.assertEqual(raised.exception.failures[0]["code"], "SQLITE_ERROR")

    def test_a_shortened_report_is_judged_by_its_evidence(self):
        # Step 08 mocks and older reporters summarize instead of emitting TAP blocks.
        require_red(report("AssertionError [ERR_ASSERTION]: expected 200 to equal 501"))

    def test_a_database_refusal_is_not_red(self):
        with self.assertRaises(RedRejected) as raised:
            require_red(report(DATABASE_FAILURE, tests=1, failed=1))
        self.assertEqual(raised.exception.category, "database")
        self.assertEqual([block["code"] for block in raised.exception.failures], ["SQLITE_ERROR"])

    def test_an_unrunnable_suite_is_not_red(self):
        with self.assertRaises(RedRejected) as raised:
            require_red(report(INFRASTRUCTURE_FAILURE, tests=1, failed=1))
        self.assertEqual(raised.exception.category, "infrastructure")

    def test_a_thrown_error_is_not_red(self):
        with self.assertRaises(RedRejected) as raised:
            require_red(report(RUNTIME_FAILURE, tests=1, failed=1))
        self.assertEqual(raised.exception.category, "runtime")

    def test_a_passing_suite_is_not_red(self):
        with self.assertRaises(RedRejected) as raised:
            require_red(report("ok 1 - rejects\nok 2 - stores", returncode=0, passed=2, failed=0))
        self.assertEqual(raised.exception.category, "vacuous")


class BlockerTests(unittest.TestCase):
    """A cause outside the editable files names the pass that owns it."""

    def test_database_evidence_names_step_five(self):
        cause = blocker("SqliteError: non-deterministic use of date() in a CHECK constraint")
        self.assertEqual((cause["category"], cause["stage"]), ("database_structure", DATABASE_STAGE))
        self.assertIn("CHECK constraint", cause["evidence"])

    def test_a_read_only_file_names_no_pass(self):
        cause = blocker("SQLITE_READONLY: attempt to write a readonly database")
        self.assertEqual(cause["category"], "write_permission")
        self.assertIsNone(cause["stage"])

    def test_a_supplied_file_repair_is_not_a_blocker(self):
        self.assertIsNone(blocker("src/home.tsx(3,10): error TS2339: Property 'x' does not exist"))

    def test_a_blocked_loop_stops_on_its_first_round(self):
        with TemporaryDirectory() as temporary:
            model, workspace, console = blocked_agent(Path(temporary))
            with self.assertRaises(AgentBlocked) as raised:
                run_file_agent(model, "Implement", {}, workspace, blocked_gate, 5, console, "blocked")
            self.assertTrue(raised.exception.blocked)
            self.assertEqual(raised.exception.upstream["stage"], DATABASE_STAGE)
            self.assertEqual(raised.exception.turns, 1)
            self.assertEqual(model.generate_json.call_count, 1)

    def test_a_blocked_task_is_recorded_and_propagates(self):
        """Isolating a blocked task would report a failure that no retry can remove."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            model, workspace, console = blocked_agent(root)
            entry = {"status": "GENERATING_TESTS"}
            with self.assertRaises(AgentBlocked):
                with implementation_task(root, entry, console, "08 / API-A"):
                    run_file_agent(model, "Implement", {}, workspace, blocked_gate, 5, console,
                        "blocked")
            self.assertEqual(entry["status"], "BLOCKED_UPSTREAM")
            self.assertTrue(entry["blocked"])
            self.assertEqual(classify_status(entry["status"]), "failed")
            self.assertEqual(entry["category"], "database_structure")
            self.assertEqual(entry["upstream_stage"], DATABASE_STAGE)
            failure = json.loads((root / entry["archive"] / "failure.json").read_text(encoding="utf-8"))
            self.assertTrue(failure["blocked"])
            self.assertEqual(failure["status"], "BLOCKED_UPSTREAM")


class RepairRoutingTests(unittest.TestCase):
    """The repair request names the table and survives only while the SQL is defective."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.arc = self.root / ".arc"
        self.output = self.root / "output"
        save_json(self.arc / "db_schema.json", SCHEMA)

    def publish(self, sql):
        save_text(self.output / "backend/src/db/tables/notes.sql", sql)

    def test_a_table_that_cannot_store_a_record_is_requested_for_repair(self):
        self.publish(CLOCK_DEPENDENT)
        repair = upstream_repair(self.arc, self.output,
            "SqliteError: non-deterministic use of date() in a CHECK constraint")
        self.assertEqual(list(repair), ["notes"])
        self.assertEqual(repair["notes"]["details"]["table"], "notes")
        self.assertIn("non-deterministic", " ".join(repair["notes"]["details"]["problems"]))

    def test_sound_published_sql_is_never_rewritten_on_stale_evidence(self):
        # The evidence names the database, but this DDL stores a legal record: the cause is
        # elsewhere (a read-only file, a wrong database path) and a rewrite cannot remove it.
        self.publish(STORABLE)
        self.assertEqual(upstream_repair(self.arc, self.output,
            "SqliteError: non-deterministic use of date() in a CHECK constraint"), {})

    def test_evidence_about_an_unpublished_table_still_names_it(self):
        cause = upstream_blocker("SQLITE_ERROR: no such table: notes", SCHEMA, {})
        self.assertEqual((cause["stage"], cause["tables"]), (DATABASE_STAGE, ["notes"]))

    def test_a_foreign_key_that_cannot_resolve_names_its_child(self):
        schema = {"entities": {
            "notes": {"fields": {"id": {"type": "integer", "primary_key": True,
                "references": None}}},
            "comments": {"fields": {"id": {"type": "integer", "primary_key": True,
                "references": None},
                "note_id": {"type": "integer", "references": "notes.id"}}}}}
        tables = {"notes": 'CREATE TABLE "notes" ("id" INTEGER PRIMARY KEY)',
            "comments": 'CREATE TABLE "comments" ("id" INTEGER PRIMARY KEY, '
                '"note_id" INTEGER NOT NULL REFERENCES "missing" ("id"))'}
        problems = smoke_problems(tables, schema, ["notes", "comments"])
        self.assertEqual(set(problems), {"comments"})
        repair = repair_request({"tables": ["comments"], "problems": problems,
            "reason": "foreign key", "evidence": "foreign key mismatch"})
        self.assertEqual(repair["comments"]["details"]["problems"], problems["comments"])


class PipelineRepairTests(unittest.TestCase):
    """A blocked pass is repaired by Step 05 and retried once instead of staying FAILED."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def run_pipeline(self, blocked_payload):
        calls = []
        attempts = {"08": 0}

        def load(module):
            function = dict(PASSES)[module]
            def execute(*arguments, **options):
                calls.append((module, options))
                if module == "08_implement_backend_tdd":
                    attempts["08"] += 1
                    if attempts["08"] == 1:
                        raise SchemaBlocked("notes: RED cannot be proven", blocked_payload)
                return {"pass": module}
            return SimpleNamespace(**{function: execute})

        compiler = Compiler(self.directory / "input", self.directory / "output")
        with patch("infrastructure.compiler.import_module", side_effect=load), \
                redirect_stdout(StringIO()):
            compiler.compile()
        return compiler, calls

    def test_a_blocked_pass_is_repaired_by_step_five_and_retried(self):
        payload = {"stage": DATABASE_STAGE, "tables": ["notes"],
            "problems": {"notes": ["SQLite refuses every row that reaches date('now')"]},
            "reason": "non-deterministic use of date()", "evidence": "SqliteError: CHECK"}
        compiler, calls = self.run_pipeline(payload)
        order = [module for module, _ in calls]
        backend = [number for number, module in enumerate(order)
            if module == "08_implement_backend_tdd"]
        self.assertEqual(len(backend), 2)
        self.assertEqual(order[backend[0] - 1], "07_check_backend_apis")
        self.assertEqual(order[backend[0] + 1:backend[0] + 3],
            [DATABASE_STAGE, "08_implement_backend_tdd"])
        repair = next(options["repair"] for module, options in calls
            if module == DATABASE_STAGE and "repair" in options)
        self.assertEqual(list(repair), ["notes"])
        # Only the retry decides the status: the blocked attempt is not the final record.
        self.assertEqual(compiler.report["stages"]["08_implement_backend_tdd"]["status"], GREEN)
        self.assertEqual(compiler.report["status"], GREEN)
        saved = json.loads((compiler.arc_dir / "compilation.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], GREEN)

    def test_a_blocked_pass_without_a_repairable_table_stays_fatal(self):
        payload = {"stage": DATABASE_STAGE, "tables": [], "problems": {},
            "reason": "write_permission", "evidence": "attempt to write a readonly database"}
        with self.assertRaises(SchemaBlocked):
            self.run_pipeline(payload)
        saved = json.loads((self.directory / "output/.arc/compilation.json").read_text(
            encoding="utf-8"))
        self.assertEqual(saved["status"], "FAILED")
        self.assertEqual(saved["stages"]["08_implement_backend_tdd"]["status"], "FAILED")


class StallTests(unittest.TestCase):
    """A failure that repeats with one signature stops instead of spending the round budget."""

    def agent(self, root):
        relative = "backend/src/note.ts"
        save_text(root / relative, "export function handle() {}")
        model = Mock()
        model.generate_json.return_value = {"edits": [{"path": relative, "content": "changed"}]}
        return model, FileWorkspace(root, {relative})

    def test_an_identical_failure_stops_at_the_stall_limit(self):
        with TemporaryDirectory() as temporary:
            model, workspace = self.agent(Path(temporary))
            gate = Mock(side_effect=ValueError(
                "backend/src/note.ts(3,10): error TS2339: Property 'title' does not exist"))
            with self.assertRaises(AgentStalled) as raised:
                run_file_agent(model, "Implement", {}, workspace, gate, 10, Mock(), "stalled")
            self.assertEqual((raised.exception.turns, raised.exception.repeats),
                (STALL_LIMIT, STALL_LIMIT))
            self.assertEqual(gate.call_count, STALL_LIMIT)

    def test_progress_between_rounds_uses_the_whole_round_budget(self):
        with TemporaryDirectory() as temporary:
            model, workspace = self.agent(Path(temporary))
            gate = Mock(side_effect=[ValueError(f"error TS2339: missing property {name}")
                for name in ("title", "body", "status", "owner")])
            with self.assertRaises(AgentExhausted) as raised:
                run_file_agent(model, "Implement", {}, workspace, gate, 4, Mock(), "progress")
            # A different defect each round is progress, so the budget is spent, not stalled.
            self.assertFalse(getattr(raised.exception, "stalled", False))
            self.assertEqual(raised.exception.turns, 4)
            self.assertEqual(gate.call_count, 4)
