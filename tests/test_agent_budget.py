"""Repair budgets, failure classification, focused selection and dependency reuse."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.agent.diagnosis import CATEGORIES, classify, normalize, signature
from infrastructure.shared.agent.files import FileWorkspace
from infrastructure.shared.agent.loop import (
    AgentBudgetExceeded, AgentExhausted, AgentStalled, check_budget, repair_budget, run_file_agent)
from infrastructure.shared.agent.recovery import implementation_task
from infrastructure.shared.project.dependencies import (
    MARKER, REQUIRED, dependencies_ready, dependency_signature, install_dependencies)
from infrastructure.shared.project.entrypoints import remember_path
from infrastructure.shared.project.initialize import initialize_project
from infrastructure.stages.frontend.triggers.workspaces import (
    implementation_workspace, page_paths, test_workspace, triggers_for_pages)
from infrastructure.utils.files import save_json, save_text

DURATION_FAILURE = "Error: expect(received).toBeVisible() after {seconds} at (3:14) id 9f8e7d6c5b4a"


class DiagnosisTests(unittest.TestCase):
    """One defect must map to one category and one stable signature."""

    def test_each_failure_kind_is_classified_separately(self):
        cases = {
            "api_route": 'E2E_API_ROUTE_ERROR: context.api("API-a") sent GET /api/a -> 404 Unknown API route',
            "api_contract": "E2E_API_CONTRACT_ERROR: request.params.id is not a route parameter of GET /api/a",
            "typescript": "src/home.tsx(3,10): error TS2339: Property 'x' does not exist",
            "assertion": "expect(received).toBeVisible()\n\nExpected: visible",
            "ui_locator": "waiting for locator('button[name=\"Save\"]') to be visible",
            "database": "SQLITE_CONSTRAINT_UNIQUE: UNIQUE constraint failed: accounts.username",
            "browser": "browserType.launch: Executable doesn't exist at chromium-1234",
            "backend": "E2E_BACKEND_ERROR: GET /api/a -> 500 Internal Server Error",
            "protocol": "Return a JSON object with an edits list",
        }
        self.assertEqual(set(cases), set(CATEGORIES) - {"unknown"})
        for expected, text in cases.items():
            with self.subTest(category=expected):
                self.assertEqual(classify(text)["category"], expected)
        self.assertEqual(classify("something else entirely")["category"], "unknown")
        self.assertEqual(set(classify("")), {"category", "signature", "files", "reason"})

    def test_signature_ignores_timings_positions_and_counts(self):
        first = classify(DURATION_FAILURE.format(seconds="12.5s"))
        second = classify(DURATION_FAILURE.format(seconds="41.2s"))
        self.assertEqual(first["signature"], second["signature"])
        self.assertEqual(signature({"error": first["reason"]}),
            signature({"error": second["reason"]}))
        self.assertNotEqual(classify("error TS2339: unknown property").get("signature"),
            classify("expect(received).toBeVisible()").get("signature"))
        self.assertNotIn("12.5", normalize("took 12.5s"))

    def test_named_source_files_are_reported_for_the_next_request(self):
        found = classify("frontend/src/screens/home.tsx imports backend/src/handlers/a.ts; "
            "also frontend/tests/e2e/a.spec.ts:12:3")
        self.assertEqual(found["files"], ["backend/src/handlers/a.ts",
            "frontend/src/screens/home.tsx", "frontend/tests/e2e/a.spec.ts"])


class RepairLoopCase(unittest.TestCase):
    """Shared fixture: one writable workspace and a model that always rewrites it."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.relative = "frontend/src/triggers/example.ts"
        save_text(self.root / self.relative, "export function handle() {}")

    def workspace(self):
        """Return a writable workspace with one supplied source."""
        workspace = FileWorkspace(self.root, {self.relative})
        workspace.state()
        return workspace

    def edit(self, content="export function handle() {}"):
        """Return a model whose every round rewrites the supplied file."""
        model = Mock()
        model.generate_json.return_value = {"edits": [{"path": self.relative, "content": content}]}
        return model


class RepairBudgetTests(RepairLoopCase):
    """A repair loop must stop retrying an error that does not change."""

    def test_a_repeated_identical_failure_stops_the_loop(self):
        model = self.edit()
        gate = Mock(side_effect=ValueError("src/home.tsx(1,1): error TS2322: not assignable"))
        with self.assertRaises(AgentStalled) as raised:
            run_file_agent(model, "Repair", {}, self.workspace(), gate, 10, Mock(), "budget")
        error = raised.exception
        self.assertTrue(error.stalled)
        self.assertEqual((error.turns, error.repeats), (3, 3))
        self.assertEqual(error.diagnosis["category"], "typescript")
        self.assertEqual(model.generate_json.call_count, 3)
        self.assertEqual(gate.call_count, 3)
        self.assertIn("typescript", str(error))

    def test_a_changing_failure_may_use_the_whole_budget(self):
        model = self.edit()
        gate = Mock(side_effect=[ValueError("error TS2322"),
            RuntimeError("waiting for locator('#save')"), ValueError("SQLITE_CONSTRAINT_UNIQUE"),
            ValueError("expect(received).toBeVisible()")])
        with self.assertRaises(AgentExhausted) as raised:
            run_file_agent(model, "Repair", {}, self.workspace(), gate, 4, Mock(), "budget")
        self.assertFalse(getattr(raised.exception, "stalled", False))
        self.assertEqual(raised.exception.turns, 4)
        self.assertEqual(model.generate_json.call_count, 4)

    def test_malformed_responses_are_not_a_stall(self):
        """A response that never reached a check is retried, not counted as no progress."""
        model = Mock()
        model.generate_json.side_effect = [{"edit": []}, {"edit": []},
            {"edits": [{"path": self.relative, "content": "fixed"}]}]
        gate = Mock(return_value={"green": True})
        result = run_file_agent(model, "Repair", {}, self.workspace(), gate, 4, Mock(), "budget")
        self.assertEqual(result["turns"], 3)
        self.assertEqual(gate.call_count, 1)

    def test_a_disabled_stall_limit_keeps_the_round_budget(self):
        gate = Mock(side_effect=ValueError("error TS2322"))
        with self.assertRaises(AgentExhausted) as raised:
            run_file_agent(self.edit(), "Repair", {}, self.workspace(), gate, 5, Mock(), "budget",
                stall_limit=None)
        self.assertEqual(raised.exception.turns, 5)

    def test_a_stalled_task_records_its_category(self):
        entry = {"status": "IMPLEMENTING"}
        gate = Mock(side_effect=ValueError("waiting for locator('#save') to be visible"))
        with implementation_task(self.root, entry, Mock(), "budget / FAILED"):
            run_file_agent(self.edit(), "Repair", {}, self.workspace(), gate, 6, Mock(), "budget")
        self.assertEqual(entry["status"], "FAILED")
        self.assertTrue(entry["stalled"])
        self.assertEqual(entry["category"], "ui_locator")
        self.assertEqual(entry["turns"], 3)
        failure = json.loads((self.root / entry["archive"] / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["category"], "ui_locator")
        self.assertTrue(failure["stalled"])

    def test_a_rejected_stall_limit_is_refused(self):
        with self.assertRaises(ValueError):
            run_file_agent(self.edit(), "Repair", {}, self.workspace(), Mock(), 2, Mock(), "budget",
                stall_limit=0)


class RepairCeilingTests(RepairLoopCase):
    """A divergent task must stop on its own budget, not on the run's last round."""

    def test_a_task_that_spent_its_token_budget_stops_before_the_next_call(self):
        model = self.edit()
        gate = Mock(side_effect=[ValueError("error TS2322"), ValueError("SQLITE_CONSTRAINT_UNIQUE")])
        with patch("infrastructure.shared.agent.loop.model_tokens_used",
                side_effect=[10_000, 100_000, 260_000]):
            with self.assertRaises(AgentBudgetExceeded) as raised:
                run_file_agent(model, "Repair", {}, self.workspace(), gate, 6, Mock(), "budget",
                    budget=repair_budget(0, 250_000))
        error = raised.exception
        self.assertTrue(error.budget)
        self.assertFalse(getattr(error, "stalled", False))
        self.assertEqual(error.turns, 2)
        self.assertEqual(model.generate_json.call_count, 1)
        self.assertEqual(error.spent, {"tokens": 250_000})
        self.assertEqual(gate.call_count, 1)
        self.assertIn("task budget exhausted", str(error))

    def test_a_task_that_spent_its_time_budget_stops_before_the_first_call(self):
        model = self.edit()
        with patch("infrastructure.shared.agent.loop.perf_counter", side_effect=[0.0, 5.0]):
            with self.assertRaises(AgentBudgetExceeded) as raised:
                run_file_agent(model, "Repair", {}, self.workspace(), Mock(), 6, Mock(), "budget",
                    budget={"seconds": 1.0, "tokens": 0})
        self.assertEqual((raised.exception.turns, model.generate_json.call_count), (1, 0))
        self.assertEqual(raised.exception.spent, {"seconds": 5.0})

    def test_a_spent_budget_is_archived_with_the_failure(self):
        entry = {"status": "IMPLEMENTING"}
        with patch("infrastructure.shared.agent.loop.perf_counter", side_effect=[0.0, 5.0]):
            with implementation_task(self.root, entry, Mock(), "budget / FAILED"):
                run_file_agent(self.edit(), "Repair", {}, self.workspace(), Mock(), 4, Mock(), "budget",
                    budget={"seconds": 1.0, "tokens": 0})
        self.assertEqual(entry["status"], "FAILED")
        self.assertFalse(entry["stalled"])
        self.assertEqual(entry["budget"], {"seconds": 5.0})
        failure = json.loads((self.root / entry["archive"] / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["budget"], entry["budget"])

    def test_a_repair_budget_defaults_to_the_module_ceilings(self):
        with patch.dict(os.environ, {"ARC_REPAIR_BUDGET_SECONDS": "", "ARC_REPAIR_BUDGET_TOKENS": ""}):
            self.assertEqual(repair_budget(), {"seconds": 1200.0, "tokens": 600_000})

    def test_a_repair_budget_is_read_from_the_environment_and_can_be_disabled(self):
        with patch.dict(os.environ, {"ARC_REPAIR_BUDGET_SECONDS": "30", "ARC_REPAIR_BUDGET_TOKENS": "5000"}):
            self.assertEqual(repair_budget(), {"seconds": 30.0, "tokens": 5000})
        with patch.dict(os.environ, {"ARC_REPAIR_BUDGET_SECONDS": "0", "ARC_REPAIR_BUDGET_TOKENS": "0"}):
            self.assertIsNone(repair_budget())
        with patch.dict(os.environ, {"ARC_REPAIR_BUDGET_SECONDS": "soon"}):
            with self.assertRaises(ValueError):
                repair_budget()

    def test_one_explicit_ceiling_keeps_the_other_environment_value(self):
        with patch.dict(os.environ, {"ARC_REPAIR_BUDGET_SECONDS": "30", "ARC_REPAIR_BUDGET_TOKENS": "5000"}):
            self.assertEqual(repair_budget(tokens=0), {"seconds": 30.0, "tokens": 0})

    def test_a_check_stops_only_on_the_ceiling_that_was_reached(self):
        started = perf_counter()
        check_budget(None, "Repair", 1, started, [])
        check_budget({"seconds": 0, "tokens": 0}, "Repair", 1, started, [])
        with self.assertRaises(AgentBudgetExceeded) as raised:
            check_budget({"seconds": 1.0, "tokens": 10}, "Repair", 3, started - 5, [], tokens_used=4)
        self.assertEqual(raised.exception.spent, {"seconds": 5.0})
        with self.assertRaises(AgentBudgetExceeded) as raised:
            check_budget({"seconds": 600.0, "tokens": 10}, "Repair", 3, started, [], tokens_used=10)
        self.assertEqual(raised.exception.spent, {"tokens": 10})
        self.assertEqual(raised.exception.turns, 3)


class FocusedSelectionTests(unittest.TestCase):
    """Only the requirement's own surface is sent; a failure can still pull in more."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        initialize_project(self.output)
        self.ui = {"pages": {"home": {"route": "/", "root": "UI-HOME"},
                "other": {"route": "/other", "root": "UI-OTHER"}},
            "elements": {"UI-HOME": {"parent": None}, "UI-SAVE": {"parent": "UI-HOME"},
                "UI-OTHER": {"parent": None}, "UI-BACK": {"parent": "UI-OTHER"}},
            "bindings": {}, "requirements": {}}
        self.design = {"triggers": {
            "TR-SAVE": {"source": "UI-SAVE", "event": "click", "action": [{"kind": "api", "api": "API-SAVE"}]},
            "TR-OTHER": {"source": "UI-OTHER", "event": "mount", "action": [{"kind": "api", "api": "API-OTHER"}]}}}
        self.backend = {"apis": {"API-SAVE": {"read": [], "write": []},
            "API-OTHER": {"read": [], "write": []}}}
        for aid, path in (("API-SAVE", "backend/src/handlers/save.ts"),
                ("API-OTHER", "backend/src/handlers/other.ts")):
            save_text(self.output / path, "export function handle() {}")
            remember_path(self.output, "api", aid, path)
        for pid, path in (("home", "frontend/src/screens/home.tsx"),
                ("other", "frontend/src/screens/other.tsx")):
            save_text(self.output / path, "export function Page() { return null; }\n")
            remember_path(self.output, "page", pid, path)
        self.relative = "frontend/src/triggers/save.ts"
        save_text(self.output / self.relative, "export function handle() {}")
        remember_path(self.output, "trigger", "TR-SAVE", self.relative)
        save_text(self.output / "frontend/src/shared/format.ts", "export const format = 1;")
        save_text(self.output / "frontend/src/unrelated/notes.ts", "export const notes = 1;")
        self.context = {"page_subtrees": {"home": {}},
            "triggers": {"TR-SAVE": self.design["triggers"]["TR-SAVE"]}}

    def build(self):
        """Return the requirement workspace exactly as step 12 builds it."""
        selection = SourceFiles(self.output, self.ui)
        backend_selection = SourceFiles(self.output, root="backend/src", tests_root="backend/tests")
        workspace = implementation_workspace(selection, backend_selection, self.output, self.context,
            {"TR-SAVE"}, "frontend/tests/e2e/home.spec.ts")
        return selection, workspace

    def test_only_the_requirement_surface_is_supplied(self):
        _selection, workspace = self.build()
        files = workspace.state()
        self.assertTrue(files[self.relative]["writable"])
        self.assertTrue(files["frontend/src/screens/home.tsx"]["writable"])
        self.assertTrue(files["backend/src/handlers/save.ts"]["writable"])
        self.assertFalse(files["frontend/tests/e2e/home.spec.ts"]["writable"])
        self.assertFalse(files["frontend/src/ui-runtime.ts"]["writable"])
        for absent in ("frontend/src/screens/other.tsx", "backend/src/handlers/other.ts",
                "frontend/src/unrelated/notes.ts", "backend/src/server.ts"):
            self.assertNotIn(absent, files)

    def test_a_failure_that_names_a_source_supplies_it_next_round(self):
        selection, workspace = self.build()
        workspace.state()
        selection.feedback(workspace, {"error": "TypeError in frontend/src/shared/format.ts"})
        files = workspace.state()
        self.assertTrue(files["frontend/src/shared/format.ts"]["writable"])
        self.assertNotIn("frontend/src/unrelated/notes.ts", files)

    def test_test_generation_reads_pages_without_writing_them(self):
        selection = SourceFiles(self.output, self.ui)
        files = test_workspace(selection, self.output, self.context, "REQ-A",
            "frontend/tests/e2e/home.spec.ts").state()
        self.assertFalse(files["frontend/src/screens/home.tsx"]["writable"])
        self.assertFalse(files["backend/src/handlers/save.ts"]["writable"])

    def test_page_paths_fall_back_when_the_index_is_empty(self):
        selection = SourceFiles(self.output, self.ui)
        self.assertEqual(page_paths(selection, ["unknown"]), selection.editable - {"frontend/src/style.css"})
        self.assertIn("frontend/src/screens/home.tsx", page_paths(selection, ["home"]))

    def test_triggers_are_selected_by_the_page_that_renders_them(self):
        self.assertEqual(triggers_for_pages(self.ui, self.design, {"home"}), {"TR-SAVE"})
        self.assertEqual(triggers_for_pages(self.ui, self.design, {"home", "other"}),
            {"TR-SAVE", "TR-OTHER"})
        self.assertEqual(triggers_for_pages(self.ui, self.design, set()), set())


class DependencyReuseTests(unittest.TestCase):
    """A matching installation is reused; a changed dependency set is installed again."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        save_json(self.output / "package.json", {"name": "generated", "private": True,
            "scripts": {"dev": "vite"}, "dependencies": {"express": "4.19.2"},
            "devDependencies": {"typescript": "5.4.5", "@playwright/test": "1.44.0", "vite": "5.2.0"}})
        save_text(self.output / "package-lock.json", '{"lockfileVersion": 3}')
        for relative in REQUIRED:
            save_text(self.output / "node_modules" / relative, "tool")
        self.marker = self.output / MARKER
        save_json(self.marker, {"signature": dependency_signature(self.output)})

    def installer(self):
        """Patch npm so a reuse is observable and an unexpected install fails loudly."""
        return patch("infrastructure.shared.project.dependencies.start_process",
            side_effect=AssertionError("npm install must not run"))

    def test_a_matching_installation_is_reused(self):
        self.assertTrue(dependencies_ready(self.output))
        with self.installer():
            install_dependencies(self.output)

    def test_a_changed_dependency_set_is_installed_again(self):
        save_json(self.output / "package.json", {"name": "generated",
            "dependencies": {"express": "4.19.2"}, "devDependencies": {"typescript": "5.5.0"}})
        self.assertFalse(dependencies_ready(self.output))
        with self.installer(), self.assertRaises(AssertionError):
            install_dependencies(self.output)

    def test_a_missing_tool_invalidates_the_installation(self):
        (self.output / "node_modules/typescript/bin/tsc").unlink()
        self.assertFalse(dependencies_ready(self.output))

    def test_a_changed_script_keeps_the_installation(self):
        """Step 12 adds npm scripts; that must not trigger a second install."""
        save_json(self.output / "package.json", {"name": "generated",
            "scripts": {"dev": "vite", "test:e2e": "node frontend/scripts/test-e2e.mjs"},
            "dependencies": {"express": "4.19.2"},
            "devDependencies": {"typescript": "5.4.5", "@playwright/test": "1.44.0", "vite": "5.2.0"}})
        self.assertEqual(dependency_signature(self.output),
            json.loads(self.marker.read_text(encoding="utf-8"))["signature"])
        with self.installer():
            install_dependencies(self.output)


if __name__ == "__main__":
    unittest.main()



