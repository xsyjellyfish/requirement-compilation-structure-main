"""Black-box acceptance orchestration: prepare, derive, audit, run and repair.

The layer is independent of step 12's per-requirement suites: it has its own Playwright
project, its own fixture, its own runner, its own report (``.arc/frontend_acceptance.json``)
and its own repair loop. Step 12 runs it after the requirements are implemented so a
complete application is checked once as a whole; step 13 runs only this layer.

The repair loop may only change application sources. The journey and the harness are
compiler-owned: a failing journey is evidence that an ordinary visitor cannot complete the
business flow, so the fix belongs to the UI/API, never to the test.
"""

import json

from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.stages.frontend.acceptance.audit import acceptance_audit, require_audit
from infrastructure.stages.frontend.acceptance.checks import AcceptanceChecks
from infrastructure.stages.frontend.acceptance.journey import journey_plan, write_journeys
from infrastructure.stages.frontend.acceptance.runtime import SUPPORT, prepare_acceptance
from infrastructure.stages.frontend.triggers.scope import TriggerCheckFailure
from infrastructure.stages.frontend.triggers.workspaces import (
    ROUTE_REGISTRATION, RUNTIME, STYLE, handler_paths, page_paths, triggers_for_pages)
from infrastructure.stages.frontend.ui.testing import require_green
from infrastructure.utils.files import read_source, save_json
from infrastructure.utils.presentation.console import DemoConsole

REPORT = "frontend_acceptance.json"

#: The pass that publishes the UI IR the journeys are derived from. A derivation defect is
#: not an application defect: only this pass may add what the contract needs (an identity
#: control with ``prop.name_from``, a create form field bound to the identity property), so
#: a blocked layer names it instead of asking the application repair to invent it.
DESIGN_STAGE = "09_design_ui"

#: The journey fields the repair model and the report see. The rendered case bodies are left
#: out on purpose: the spec files are supplied unchanged, so the model reads the real test.
JOURNEY_FIELDS = ("kind", "resource", "collection", "item", "apis", "pages", "routes", "controls",
    "fills", "steps", "spec", "database", "coverage", "problems")


def brief(entry: dict) -> dict:
    """Expose one derived journey without the case bodies the spec file already carries."""
    return {key: entry[key] for key in JOURNEY_FIELDS if key in entry}


INSTRUCTIONS = """Make the generated application pass its black-box acceptance journey.

The journey and its fixture are compiler-owned: frontend/tests/acceptance/** is read-only and
must never be edited, skipped or weakened. A failure means an ordinary visitor cannot
complete the business flow the requirement describes, so repair the application.

What each failing step means, and what to repair:
- the record list must expose the record's own name as the EXACT accessible name of a control.
  If a control concatenates the name with a description, timestamp or other text, add a
  dedicated identity control whose accessible name is exactly the record name (render only
  that value inside it, or set aria-label to it). Never widen or relax the test instead.
- clicking that control must open the record's detail page and the URL must match the
  designed route with the record name encoded in its path.
- after a reload the page must load the record from the API again and show it; a client-only
  cache may never stand in for the stored record.
- the designed save control must submit the edited value through the API, and the value must
  be readable back after a fresh load.
- the designed delete control must delete through the API, and the record must disappear from
  the list and from the database.
- an invalid submission must stay on the form, show the designed alert element and write
  nothing; it must never navigate as if it had succeeded.

The account lifecycle journey drives the same way, and its failures mean:
- the home page must show the signed-in account's own identity and the designed entry links
  (register, sign in) while nobody is signed in, and must not show the identity then.
- the registration form must create the account through the API with the values the user typed;
  the page it redirects to must display the new account, and the session must live on the server,
  so a reload still shows the account. A client-side flag or browser storage may never stand in
  for the stored session.
- the designed sign-out control must end the session on the server: the account identity must be
  gone after a reload, and an independent browser context must never see it.
- signing in again with the same identity and secret must create a new session, and a wrong secret
  must be rejected on the form with the designed alert and without a session.
- a duplicate registration must be rejected: the visitor stays on the form, the alert appears,
  no second account row is written and no session is created.

Keep every accessible name, label, placeholder, role and input type the design declares:
getByLabel and getByRole with exact names are how a reviewer drives the page. Preserve native
input, button and anchor semantics and keep real labels associated with their controls.
Do not add mocks, test hooks, fixture detection, browser storage or optimistic client-side
persistence. Return only the changed application sources.
"""


def acceptance_context(plan: dict, output, files) -> dict:
    """Supply the derived journeys, their expectations and the current rendered specs."""
    return {"acceptance_journeys": [brief(entry) for entry in plan["journeys"]],
        "problems": plan["problems"], "notes": plan["notes"],
        "unverified": plan.get("unverified") or [],
        "specs": {relative: read_source(output / relative) for relative in files if (output / relative).is_file()}}


def acceptance_report(plan: dict, files) -> dict:
    """Build the independent report of the acceptance layer."""
    return {"status": "PREPARING", "layer": "black-box acceptance", "specs": list(files),
        "problems": plan["problems"], "derivation_problems": plan.get("derivation_problems") or [],
        "notes": plan["notes"],
        "unverified": plan.get("unverified") or [],
        "journeys": [brief(entry) for entry in plan["journeys"]]}


def run_acceptance(arc_dir, tree, schema, backend, ui, design, checks=None, console=None,
        model=None, max_turns=3):
    """Derive, audit and run the black-box journeys, repairing the application when needed."""
    output = arc_dir.parent.resolve()
    prepare_acceptance(output)
    plan = journey_plan(tree, schema, backend, ui, design)
    files = write_journeys(output, plan)
    report = acceptance_report(plan, files)
    audit = acceptance_audit(output, plan)
    report["audit"] = {key: audit[key] for key in ("status", "problems", "files", "output")}
    console = console or DemoConsole(False)
    console.show("Black-box acceptance | Derived journeys",
        json.dumps(report["journeys"], ensure_ascii=False, indent=2))
    derived = plan.get("derivation_problems") or []
    if derived:
        # The derivation itself is unsound: no field of a create form carries the record property
        # the contract requires, so every journey rendered from this IR would type an unrelated
        # control and then blame the application for the empty value. Repairing the application
        # against that evidence rewrites working triggers, so the layer stops before it runs or
        # repairs anything. Deriving a weaker journey is not an option, and a green suite that
        # skipped a whole collection is exactly the false pass this layer exists to prevent.
        report.update({"status": "BLOCKED_UPSTREAM", "error": "\n".join(derived)})
        save_json(arc_dir / REPORT, report)
        console.show("Black-box acceptance | BLOCKED",
            "The journeys cannot be derived soundly:\n" + "\n".join(derived))
        return report
    if not files and plan["problems"]:
        # Nothing could be derived, and the reason is a defect of the application's design or
        # implementation. Reporting NOT_APPLICABLE here would accept an application that no test
        # ever drove, so the layer states the gap instead.
        report.update({"status": "UNVERIFIED", "error": "\n".join(plan["problems"])})
        save_json(arc_dir / REPORT, report)
        console.show("Black-box acceptance | UNVERIFIED",
            "No journey could be derived:\n" + "\n".join(plan["problems"]))
        return report
    if not files:
        report["status"] = "NOT_APPLICABLE"
        save_json(arc_dir / REPORT, report)
        console.show("Black-box acceptance | NOT_APPLICABLE",
            audit["output"] + "\n" + "\n".join(plan["notes"]))
        return report
    checks = checks or AcceptanceChecks(output)

    def acceptance_checks():
        """Audit the suite, compile it, then run it in a real browser."""
        current = acceptance_audit(output, plan)
        compiled = checks.typecheck(files)
        result = {"audit": current, "typecheck": compiled, "tests": None}
        if compiled["returncode"] or current["problems"]:
            return result
        result["tests"] = checks.tests(files)
        return result

    def acceptance_gate():
        """Accept the layer only when the suite is honest, compiles and passes in a browser."""
        result = acceptance_checks()
        try:
            require_audit(result["audit"])
            if result["typecheck"]["returncode"]:
                raise ValueError(result["typecheck"]["output"])
            require_green(result["tests"])
        except ValueError as error:
            raise TriggerCheckFailure(
                "Repair the application so an ordinary visitor can complete the journey",
                result) from error
        return result

    try:
        agent = {"turns": 0, "checks": acceptance_gate()}
    except ValueError as error:
        feedback = getattr(error, "feedback", {"error": str(error)})
        if model is None:
            report.update({"status": "FAILED", "error": str(error), "feedback": feedback})
            save_json(arc_dir / REPORT, report)
            console.show("Black-box acceptance | FAILED", str(error))
            return report
        selection = SourceFiles(output, ui)
        backend_selection = SourceFiles(output, root="backend/src", tests_root="backend/tests")
        pages = {pid for entry in plan["journeys"] for pid in (entry.get("pages") or {}).values() if pid}
        apis = {aid for entry in plan["journeys"] for aid in (entry.get("apis") or {}).values() if aid}
        # A journey repairs the pages it drives, the triggers on them and the APIs it calls;
        # anything else is supplied only after a failing step names it.
        workspace = selection.workspace(
            triggers=triggers_for_pages(ui, design, pages),
            paths={*page_paths(selection, pages or set(ui["pages"])), STYLE},
            references=[*files, SUPPORT, RUNTIME, ROUTE_REGISTRATION,
                *sorted(handler_paths(output, apis))])
        backend_selection.include(workspace, handler_paths(output, apis))

        def repair_feedback(value):
            """Expose the sources named by the failing journey."""
            return selection.feedback(workspace, backend_selection.feedback(workspace, value))

        try:
            agent = run_file_agent(model, INSTRUCTIONS, acceptance_context(plan, output, files),
                workspace, acceptance_gate, max_turns, console, "Acceptance / Repair",
                feedback=feedback, feedback_filter=repair_feedback)
        except ValueError as error:
            report.update({"status": "FAILED", "error": str(error),
                "feedback": getattr(error, "feedback", feedback)})
            save_json(arc_dir / REPORT, report)
            console.show("Black-box acceptance | FAILED", str(error))
            return report
    result = agent["checks"]["tests"] or {}
    report.update({"status": "GREEN", "repair_turns": agent["turns"],
        "tests": {key: result.get(key) for key in ("passed", "failed", "skipped", "flaky")},
        "files": result.get("files") or {}, "audit": {key: agent["checks"]["audit"][key]
            for key in ("status", "problems", "files", "output")}})
    save_json(arc_dir / REPORT, report)
    console.show("Black-box acceptance | GREEN", report["audit"]["output"] + "\n"
        + "Passed journeys: " + ", ".join(entry["spec"] for entry in plan["journeys"]))
    return report
