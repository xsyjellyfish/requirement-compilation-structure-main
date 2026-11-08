"""Prove that the final report never converts a failed check into a success.

Every scenario drives the real Compiler and CLI through their public seams only: the passes
are stubbed, while the per-pass status records, the honest test accounting, the failure
index, the digest and the exit code are the production implementations.
"""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
from infrastructure.compiler import Compiler
from infrastructure.reporting import accounting, final, status as run


def compile_with(root: Path, results: dict) -> Compiler:
    """Run the real pipeline with stubbed passes; a BaseException value is raised instead."""
    try:
        from test_compiler import PASSES
    except ModuleNotFoundError:
        from tests.test_compiler import PASSES

    passes = dict(PASSES)

    def load(module):
        def execute(*arguments, **options):
            outcome = results.get(module, {"status": run.GREEN})
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return SimpleNamespace(**{passes[module]: execute})

    compiler = Compiler(root / "input", root / "output")
    with patch("infrastructure.compiler.import_module", side_effect=load), \
            redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        try:
            compiler.compile()
        except BaseException:  # the death of a stubbed pass is part of the scenario
            pass
    return compiler


def make_deployable(compiler: Compiler) -> None:
    """Write the deployment evidence the ARC-Bench Runner relies on."""
    (compiler.output_dir / "deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (compiler.output_dir / "package.json").write_text("{}\n", encoding="utf-8")


def finish(compiler: Compiler, policy: str = run.STRICT, accepted: bool = False) -> tuple[int, dict]:
    """Finalise exactly as the CLI does, then read the report the CLI wrote."""
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        code = main.finish(compiler, policy, accepted)
    return code, json.loads((compiler.arc_dir / final.REPORT).read_text(encoding="utf-8"))


def digest(compiler: Compiler) -> str:
    """Read the human-readable digest of the last run."""
    return (compiler.arc_dir / final.DIGEST).read_text(encoding="utf-8")


def kinds(report: dict) -> list[tuple]:
    """Name every indexed failure as (kind, id, stage, status)."""
    return [(entry["kind"], entry["id"], entry["stage"], entry["status"])
        for entry in report["failures"]]


def backend_result(**overrides) -> dict:
    """A backend TDD report shaped like the one pass 08 writes."""
    result = {"status": run.GREEN,
        "requirements": {"REQ-1": {"status": run.GREEN}},
        "verification": {"typecheck": {"status": run.GREEN, "result": {"returncode": 0}},
            "tests": {"status": run.GREEN, "result": {"tests": 15, "pass": 15, "fail": 0}}}}
    result.update(overrides)
    return result


def frontend_result(**overrides) -> dict:
    """A frontend TDD report shaped like the one pass 12 writes."""
    result = {"status": run.GREEN, "requirements": {"REQ-1": {"status": run.GREEN}},
        "verification": {"e2e": {"status": run.GREEN, "result": {"passed": 4, "failed": 0}}}}
    result.update(overrides)
    return result


class StatusVocabulary(unittest.TestCase):
    def test_a_missing_or_in_progress_status_is_never_a_pass(self):
        for status in (None, "", 0, [], "UNKNOWN", "RUNNING", "PENDING", "PREPARING",
                "VALIDATING", "GENERATING_TESTS", "CHECKING_RED"):
            self.assertEqual(run.classify(status), "unknown", status)
            self.assertFalse(run.is_passed(status), status)

    def test_explicit_accepting_statuses_are_passes(self):
        for status in (run.GREEN, run.NOT_APPLICABLE, "NO_APIS", "NO_DIRECT_APIS", "NO_UI",
                "NO_TRIGGERS", "NO_DIRECT_TRIGGERS", "SKIPPED"):
            self.assertEqual(run.classify(status), "passed", status)

    def test_reported_and_in_progress_failures_are_failures(self):
        for status in (run.FAILED, run.COMPLETED_WITH_FAILURES, "REJECT", "REPAIR_REQUIRED",
                "RED_CONFIRMED", "IMPLEMENTING", "IMPORTING"):
            self.assertEqual(run.classify(status), "failed", status)

    def test_run_status_keeps_partial_unknown_and_interrupted_apart_from_green(self):
        self.assertEqual(run.run_status([]), run.NOT_STARTED)
        self.assertEqual(run.run_status([run.GREEN, "NO_UI", run.NOT_APPLICABLE]), run.GREEN)
        self.assertEqual(run.run_status([run.GREEN, run.COMPLETED_WITH_FAILURES]),
            run.COMPLETED_WITH_FAILURES)
        self.assertEqual(run.run_status([run.GREEN, None]), run.COMPLETED_WITH_FAILURES)
        self.assertEqual(run.run_status([run.GREEN, "UNKNOWN"]), run.COMPLETED_WITH_FAILURES)
        self.assertEqual(run.run_status([run.GREEN, run.INTERRUPTED]), run.INTERRUPTED)

    def test_exit_policy_never_hides_a_failure_under_strict(self):
        self.assertEqual(run.exit_code(run.GREEN, run.STRICT, False), run.EXIT_GREEN)
        self.assertEqual(run.exit_code(run.GREEN, run.DEPLOY, False), run.EXIT_GREEN)
        self.assertEqual(run.exit_code(run.COMPLETED_WITH_FAILURES, run.STRICT, True),
            run.EXIT_PARTIAL)
        self.assertEqual(run.exit_code(run.COMPLETED_WITH_FAILURES, run.DEPLOY, True),
            run.EXIT_PARTIAL)
        self.assertEqual(run.exit_code(run.COMPLETED_WITH_FAILURES, run.DEPLOY, False),
            run.EXIT_PARTIAL)
        self.assertEqual(run.exit_code(run.FAILED, run.DEPLOY, True), run.EXIT_FATAL)
        self.assertEqual(run.exit_code(run.INTERRUPTED, run.DEPLOY, True), run.EXIT_INTERRUPTED)
        self.assertIn("still reporting the failures",
            run.exit_reason(run.COMPLETED_WITH_FAILURES, run.STRICT, True))

    def test_a_partial_run_exits_zero_only_after_an_explicit_opt_in(self):
        """COMPLETED_WITH_FAILURES is a gate: nothing but the accept flag opens it."""
        partial = run.COMPLETED_WITH_FAILURES
        self.assertEqual(run.exit_code(partial, run.DEPLOY, deployable=True, accepted=True),
            run.EXIT_GREEN)
        # Neither policy nor deployability is enough on its own, in any combination.
        for policy in run.POLICIES:
            for deployable in (True, False):
                self.assertEqual(run.exit_code(partial, policy, deployable, False),
                    run.EXIT_PARTIAL, (policy, deployable))
        # Strict never accepts, even when the operator asked to accept.
        self.assertEqual(run.exit_code(partial, run.STRICT, True, True), run.EXIT_PARTIAL)

    def test_the_reason_says_which_knob_is_missing(self):
        partial = run.COMPLETED_WITH_FAILURES
        implicit = run.exit_reason(partial, run.DEPLOY, True, False)
        self.assertIn(run.ACCEPT_FLAG, implicit)
        self.assertIn("never accepted implicitly", implicit)
        accepted = run.exit_reason(partial, run.DEPLOY, True, True)
        self.assertIn(run.ACCEPT_FLAG, accepted)
        self.assertIn("deployable application kept", accepted)


class HonestAccounting(unittest.TestCase):
    def test_a_failed_check_keeps_failures_and_loses_its_pass_count(self):
        checks = final.check_records({"tests": {"status": run.FAILED,
            "error": "Command failed: npx playwright test",
            "result": {"passed": 6, "failed": 2, "skipped": 1, "flaky": 0}}})
        check = checks["tests"]
        self.assertEqual(check["status"], run.FAILED)
        self.assertEqual(check["tests"], {"total": 9, "passed": 6, "failed": 2, "skipped": 1,
            "flaky": 0, "unverified": 0})
        self.assertEqual(check["accepted"], {"total": 9, "passed": 0, "failed": 2, "skipped": 1,
            "flaky": 0, "unverified": 6})
        self.assertEqual(check["reason"], "Command failed: npx playwright test")

    def test_recorded_failure_lines_beat_the_generic_error_and_lose_color_codes(self):
        checks = final.check_records({"ui": {"status": run.FAILED,
            "error": '{"returncode": 1, "failed": 3}',
            "result": {"passed": 0, "failed": 1,
                "failures": ["\u001b[31mexpect(locator).toHaveValue()\u001b[0m failed"]}}})
        self.assertEqual(checks["ui"]["reason"], "expect(locator).toHaveValue() failed")

    def test_a_green_check_keeps_its_passing_tests(self):
        check = final.check_records({"tests": {"status": run.GREEN,
            "result": {"tests": 15, "pass": 15, "fail": 0}}})["tests"]
        self.assertEqual(check["accepted"]["passed"], 15)
        self.assertEqual(check["accepted"]["unverified"], 0)

    def test_malformed_counts_are_ignored_instead_of_inflating_the_total(self):
        counts = accounting.test_counts({"tests": "15", "pass": True, "fail": -2, "todo": 3})
        self.assertEqual(counts, {"total": 3, "passed": 0, "failed": 0, "skipped": 3,
            "flaky": 0, "unverified": 0})
        self.assertEqual(accounting.accepted_counts({"status": run.GREEN, "result": None}),
            {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "flaky": 0, "unverified": 0})

    def test_a_long_reason_is_bounded_and_says_where_the_full_text_is(self):
        clipped = accounting.clip("x" * (accounting.REASON_LIMIT + 10))
        self.assertLess(len(clipped), accounting.REASON_LIMIT + 200)
        self.assertIn("the full text is in the stage report", clipped)

    def test_a_check_without_a_status_is_never_credited(self):
        check = final.check_records({"tests": {"result": {"passed": 4, "failed": 0}}})["tests"]
        self.assertEqual(check["status"], run.UNKNOWN)
        self.assertEqual(check["accepted"]["passed"], 0)
        self.assertEqual(check["accepted"]["unverified"], 4)

    def test_a_requirement_is_never_green_without_evidence(self):
        self.assertEqual(final.requirement_status({}), run.UNKNOWN)
        self.assertEqual(final.requirement_status({"a": run.GREEN}), run.GREEN)
        self.assertEqual(final.requirement_status({"a": "NO_DIRECT_APIS"}), run.NOT_APPLICABLE)
        self.assertEqual(final.requirement_status({"a": run.GREEN, "b": "PENDING"}), run.UNKNOWN)
        self.assertEqual(final.requirement_status({"a": run.GREEN, "b": run.FAILED}), run.FAILED)


class ReportScenarios(unittest.TestCase):
    def test_a_fully_verified_application_is_green_and_exits_zero(self):
        with TemporaryDirectory() as temporary:
            compiler = compile_with(Path(temporary), {
                "01_explore_requirement": {"id": "ROOT", "children": [{"id": "REQ-1"}]},
                "08_implement_backend_tdd": backend_result(
                    requirements={"REQ-1": {"status": run.GREEN}, "ROOT": {"status": "NO_DIRECT_APIS"}}),
                "12_implement_frontend_tdd": frontend_result(
                    requirements={"REQ-1": {"status": run.GREEN},
                        "ROOT": {"status": "NO_DIRECT_TRIGGERS"}})})
            make_deployable(compiler)
            code, report = finish(compiler)
            self.assertEqual(report["status"], run.GREEN)
            self.assertEqual(code, run.EXIT_GREEN)
            self.assertEqual(report["failures"], [])
            self.assertEqual(report["requirements"]["REQ-1"]["status"], run.GREEN)
            self.assertEqual(report["requirements"]["ROOT"]["status"], run.NOT_APPLICABLE)
            self.assertEqual(report["summary"]["tests"]["passed"], 19)
            self.assertEqual(report["summary"]["tests"]["unverified"], 0)
            self.assertTrue(report["deployable"])
            self.assertEqual(report["exit"]["code"], run.EXIT_GREEN)

    def test_a_failed_browser_test_is_never_counted_as_success(self):
        with TemporaryDirectory() as temporary:
            compiler = compile_with(Path(temporary), {
                "01_explore_requirement": {"id": "REQ-1"},
                "08_implement_backend_tdd": backend_result(),
                "10_implement_ui": {"status": run.COMPLETED_WITH_FAILURES,
                    "pages": {"home": {"status": run.GREEN},
                        "editor": {"status": run.FAILED, "error": "getByTestId('save') stayed hidden"}},
                    "verification": {"typecheck": {"status": run.GREEN, "result": {"returncode": 0}},
                        "tests": {"status": run.FAILED, "error": "Command failed: npx playwright test",
                            "result": {"passed": 6, "failed": 2, "skipped": 1, "flaky": 0,
                                "failures": ["\u001b[31mexpect(locator).toBeVisible()\u001b[0m failed"]}}}}})
            make_deployable(compiler)
            code, report = finish(compiler)
            self.assertEqual(report["status"], run.COMPLETED_WITH_FAILURES)
            self.assertEqual(code, run.EXIT_PARTIAL)
            self.assertEqual(report["summary"]["checks"], {"total": 4, "green": 3, "failed": ["tests"]})
            self.assertEqual(report["summary"]["tests"], {"total": 24, "passed": 15, "failed": 2,
                "skipped": 1, "flaky": 0, "unverified": 6})
            self.assertIn(("page", "editor", "10_implement_ui", run.FAILED), kinds(report))
            self.assertIn(("check", "tests", "10_implement_ui", run.FAILED), kinds(report))
            failed_check = report["stages"]["10_implement_ui"]["checks"]["tests"]
            self.assertEqual(failed_check["accepted"]["passed"], 0)
            self.assertEqual(failed_check["reason"], "expect(locator).toBeVisible() failed")
            text = digest(compiler)
            self.assertIn("editor", text)
            self.assertIn("expect(locator).toBeVisible() failed", text)
            self.assertNotIn("\u001b[", text)
            # The deploy policy keeps the partial application deployable but never renames it,
            # and it does not deliver it either: only the explicit opt-in exits 0.
            self.assertEqual(finish(compiler, run.DEPLOY)[0], run.EXIT_PARTIAL)
            self.assertEqual(finish(compiler, run.DEPLOY)[1]["status"], run.COMPLETED_WITH_FAILURES)
            self.assertEqual(finish(compiler, run.DEPLOY)[1]["delivery"]["status"], "BLOCKED")
            accepted_code, accepted_report = finish(compiler, run.DEPLOY, accepted=True)
            self.assertEqual(accepted_code, run.EXIT_GREEN)
            self.assertEqual(accepted_report["status"], run.COMPLETED_WITH_FAILURES)
            self.assertEqual(accepted_report["delivery"], {"status": "ACCEPTED",
                "failed_checks": ["tests"], "accepted": True, "application": True})
            self.assertEqual(accepted_report["exit"]["accepted_failures"], True)
            self.assertIn(run.ACCEPT_FLAG, accepted_report["exit"]["reason"])

    def test_a_fatal_error_stops_the_pipeline_and_exits_one(self):
        with TemporaryDirectory() as temporary:
            compiler = compile_with(Path(temporary), {"05_implement_db": OSError("disk full")})
            make_deployable(compiler)
            code, report = finish(compiler)
            self.assertEqual(report["status"], run.FAILED)
            self.assertEqual(code, run.EXIT_FATAL)
            stage = report["stages"]["05_implement_db"]
            self.assertTrue(stage["fatal"])
            self.assertFalse(stage["recoverable"])
            self.assertEqual(stage["error_type"], "OSError")
            self.assertIn("disk full", stage["error"])
            self.assertEqual(list(report["stages"])[-1], "05_implement_db")
            self.assertEqual([(entry["kind"], entry["id"]) for entry in report["failures"]],
                [("fatal", "05_implement_db")])
            lines = (compiler.output_dir / final.LOG).read_text(encoding="utf-8").splitlines()
            self.assertEqual(stage["log"]["file"], final.LOG)
            self.assertGreaterEqual(len(lines), stage["log"]["end_line"])
            chunk = "\n".join(lines[stage["log"]["start_line"] - 1:stage["log"]["end_line"]])
            self.assertIn("Stage 05", chunk)
            self.assertNotIn("Stage 06", chunk)
            self.assertEqual(digest(compiler).splitlines()[0], "# Compilation report: FAILED")
            # No policy turns a crashed run into a deployable one.
            self.assertEqual(finish(compiler, run.DEPLOY)[0], run.EXIT_FATAL)

    def test_an_interrupted_run_is_never_reported_as_a_failure_or_a_success(self):
        with TemporaryDirectory() as temporary:
            compiler = compile_with(Path(temporary), {"10_implement_ui": KeyboardInterrupt()})
            make_deployable(compiler)
            code, report = finish(compiler)
            self.assertEqual(report["status"], run.INTERRUPTED)
            self.assertEqual(code, run.EXIT_INTERRUPTED)
            stage = report["stages"]["10_implement_ui"]
            self.assertEqual(stage["status"], run.INTERRUPTED)
            self.assertTrue(stage["fatal"])
            self.assertEqual(stage["error_type"], "KeyboardInterrupt")
            self.assertIn(("fatal", "10_implement_ui"), [(entry["kind"], entry["id"])
                for entry in report["failures"]])
            self.assertIn("interrupted", report["exit"]["reason"])
            self.assertEqual(digest(compiler).splitlines()[0], "# Compilation report: INTERRUPTED")
            self.assertEqual(finish(compiler, run.DEPLOY)[0], run.EXIT_INTERRUPTED)

    def test_a_requirement_no_pass_reported_stays_unknown_and_blocks_green(self):
        with TemporaryDirectory() as temporary:
            compiler = compile_with(Path(temporary), {
                "01_explore_requirement": {"id": "ROOT", "children": [
                    {"id": "REQ-1"}, {"id": "REQ-2"}, {"id": "REQ-3"}]},
                "08_implement_backend_tdd": backend_result(requirements={
                    "REQ-1": {"status": "NO_DIRECT_APIS"}, "REQ-2": {"status": "NO_DIRECT_APIS"},
                    "ROOT": {"status": "NO_DIRECT_APIS"}}),
                "12_implement_frontend_tdd": frontend_result(status=run.COMPLETED_WITH_FAILURES,
                    requirements={"REQ-1": {"status": run.GREEN},
                        "REQ-2": {"status": run.FAILED, "error": "trigger 'submit' never fired"}})})
            make_deployable(compiler)
            code, report = finish(compiler)
            requirements = report["requirements"]
            self.assertEqual(requirements["REQ-1"]["status"], run.GREEN)
            self.assertEqual(requirements["REQ-2"]["status"], run.FAILED)
            self.assertEqual(requirements["REQ-3"]["status"], run.UNKNOWN)
            self.assertEqual(requirements["ROOT"]["status"], run.NOT_APPLICABLE)
            self.assertEqual(report["summary"]["requirements"], {"total": 4, "green": 1,
                "not_applicable": 1, "failed": 1, "unknown": 1})
            self.assertEqual(report["status"], run.COMPLETED_WITH_FAILURES)
            self.assertEqual(code, run.EXIT_PARTIAL)
            rejected = digest(compiler).split("## Requirements that are not accepted")[1]
            self.assertIn("REQ-2: FAILED", rejected)
            self.assertIn("REQ-3: UNKNOWN", rejected)
            self.assertNotIn("ROOT:", rejected)

    def test_a_failing_api_is_named_with_its_reason_and_report(self):
        with TemporaryDirectory() as temporary:
            compiler = compile_with(Path(temporary), {
                "08_implement_backend_tdd": backend_result(status=run.COMPLETED_WITH_FAILURES,
                    requirements={"REQ-1": {"status": run.FAILED}},
                    apis={"API-create-item": {"status": run.FAILED,
                        "error": "POST /api/items returned 500"}},
                    http={"apis": {"API-create-item": {"status": run.FAILED,
                        "tests": 2, "pass": 1, "fail": 1}}})})
            make_deployable(compiler)
            code, report = finish(compiler)
            self.assertEqual(code, run.EXIT_PARTIAL)
            self.assertIn(("api", "API-create-item", "08_implement_backend_tdd", run.FAILED),
                kinds(report))
            api = next(entry for entry in report["failures"] if entry["id"] == "API-create-item")
            self.assertEqual(api["reason"], "POST /api/items returned 500")
            self.assertEqual(api["report"], "backend_tdd.json")
            self.assertEqual(api["log"]["file"], final.LOG)
            stage_reason = report["stages"]["08_implement_backend_tdd"]["reason"]
            self.assertIn("API-create-item", stage_reason)
            self.assertIn("REQ-1", stage_reason)
            text = digest(compiler)
            self.assertIn("API-create-item", text)
            self.assertIn("POST /api/items returned 500", text)
