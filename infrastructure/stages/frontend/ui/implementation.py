"""Step 10 orchestration: UI existence tests -> component repair -> regression.

Tests are rendered deterministically from the complete step 09 design. A shared
file agent implements one page at a time, with previously accepted page tests
included in its checks. Tests and compiler-owned wiring are supplied as read-only files."""

import json
from pathlib import Path

from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.recovery import implementation_task, final_checks, complete_report
from infrastructure.shared.project.entrypoints import selected_path
from infrastructure.stages.frontend.ui.design import owners, page_context
from infrastructure.stages.frontend.ui.constraints import required_controls_for_page, requirement_controls
from infrastructure.stages.frontend.ui.runtime import prepare_frontend, sync_media, sync_pages
from infrastructure.stages.frontend.ui.testing import FrontendChecks, existence_suite, require_green, presentation_run_status, test_path
from infrastructure.shared.project.dependencies import install_dependencies
from infrastructure.utils.files import save_json, save_text

INSTRUCTIONS = """Implement the current page from its UI/data design, requirement uses and visual_references.
Create the task.new_file target or update implementation_path. Export a named React
component Page accepting UiPageProps, imported from frontend/src/ui-runtime.ts using
a relative import. The compiler owns routing, initial data and display fixtures.

Render layout, style, labels, controls, lists and conditional content from data[var]
and static element props. Props also include params, the decoded parameters of the current
dynamic route (for example params.bookName on /books/:bookName); render them when the page
identifies its subject in the requirement. Use the configured Tailwind utilities with complete class
strings; use installed React/ReactDOM without new dependencies. Reproduce visual
references through real elements, not a screenshot background in place of controls.
Put data-testid=ELEMENT_ID on each actual designed element, including the page root.
Each test ID must be on the interactive DOM node itself, never an invisible proxy.
Preserve actual input/select/button/anchor semantics so Playwright can fill/click
and the trigger host receives submit/click/change events. Use associated labels,
specified input types/options, and role="alert" for alert elements.
The designed element type IS the role an external reviewer locates: a designed link is an
<a href> (or an element with role="link"), a designed button a <button>, a designed
checkbox an <input type="checkbox">, a heading an h1-h6, and a list a <ul>/<ol>.
A styled div or span carrying the test ID and the text is not that control; the display
suite asserts the designed role, so render the native element instead of styling a copy.
Respect prop.visible booleans or variable-equality rules and original visible text.

ACCESSIBLE NAMES: a user, a screen reader and the black-box acceptance journey all find a
control by role and accessible name, so render the designed name exactly.
- Associate every designed label with its control (<label htmlFor>, a wrapping <label>, or
  aria-label) and render prop.text as the button's or link's own text.
- When prop.name_from is set, render exactly that variable's value as the control's accessible
  name and nothing else: put a description, timestamp or other value in sibling elements, or
  set aria-label to the single value. A record control whose name mixes several values is a
  defect to fix, never something a test should match around.
- Show a rejection in the designed alert element and keep the user on the page.

Use defaultValue/defaultChecked for native editable controls. The host captures native
input/change events for user_write bindings: checkbox values become booleans and other
values remain strings. Do not add duplicate input handlers. This presentation task has
no business API calls, guards, session restoration or state transitions; triggers supply
those later. Native form submission is suppressed; structural links can use designed hrefs.
Render fixture data through the ordinary data prop, without inspecting test globals.

INHERITED CONTROL REPETITION: required_controls lists the links, buttons and checkboxes this page
must show because the requirement that owns the page, or a parent of it, declares them. Render
each one with that exact role, accessible name and href, including controls inherited from a parent
requirement that this page's own wording never restates: put inherited controls in a shared
header or footer region of the page and keep them on every page of that parent's subtree. Never
rename, truncate or replace one with text, and never drop one because the current page looks fine
without it.
Use each visual reference's url for its image, for example <img src="/media/IMG-HOMEPAGE.png" alt="..."/>.
The compiler copies those assets into the served public directory, so only that URL is fetchable.

Repair supplied writable frontend dependencies when needed, preserving other pages
and behavior. Use the files map for permissions. The compiler runs TypeScript, current
and accepted page display tests, then a build. Implement the design without modifying
tests or compiler wiring to bypass failures.

Return the smallest complete set of
changed files; avoid rewriting existing correct components during a local repair.
"""

def implement_ui(arc_dir: Path, design: dict, model, console, report: dict, max_turns: int = 10,
        images: dict | None = None, requirement_contexts: dict | None = None, tree=None,
        input_dir: Path | None = None) -> dict:
    """Implement the complete saved UI/data design through component-existence TDD.

    Prepare the project and browser, generate and freeze presence tests, then
    repair model-selected page components until checks pass. Update the caller's
    report; runtime wiring remains compiler-owned."""
    output = arc_dir.parent.resolve()
    prepare_frontend(output)
    sync_media(output, images, input_dir)
    sync_pages(output, design)
    controls = requirement_controls(tree) if tree is not None else []

    console.show("UI TDD preparation | Dependencies", "Install workspace dependencies and Playwright Chromium. No backend is started.")
    install_dependencies(output)
    checks = FrontendChecks(output)
    checks.install_browser()
    accepted = []
    selection = SourceFiles(output, design)
    report["status"] = "IMPLEMENTING"
    save_json(arc_dir / "ui_checks.json", report)
    for number, pid in enumerate(design["pages"], 1):
        uses = owners(design, pid)
        relative = test_path(pid)
        required = required_controls_for_page(design, tree, pid, controls) if tree is not None else []
        suite = existence_suite(design, pid, required)
        save_text(output / relative, suite)
        report["pages"][pid] = {"status": "TESTS_READY", "requirements": uses, "test_path": relative,
            "required_controls": [control["id"] for control in required]}
        save_json(arc_dir / "ui_checks.json", report)
        label = f"UI {number}/{len(design['pages'])} {pid}"
        console.show(label + " | Existence tests before implementation", suite)
        files = [test_path(key) for key in [*accepted, pid]]

        def implementation_checks():
            """Refresh page registration and check selected page tests plus the frontend TypeScript sources."""
            sync_pages(output, design)
            compiled = checks.typecheck(files)
            if compiled["returncode"]:
                return {"typecheck": compiled, "tests": None}
            tested = checks.tests(files)
            return {"typecheck": compiled, "tests": tested}

        def implementation_gate():
            """Accept the current page only after compilation and presence tests pass."""
            result = implementation_checks()
            for kind in ("typecheck",):
                if result[kind] and result[kind]["returncode"]:
                    raise ValueError(f"{kind} failed: {result[kind]['output']}")
            require_green(result["tests"])
            return result

        with implementation_task(output, report["pages"][pid], console, "10 / " + pid):
            # Compile tests separately before deciding whether browser failure is RED.
            current = implementation_checks()
            if current["tests"] is not None:
                report["pages"][pid]["status"] = presentation_run_status(current["tests"])
                console.show(label + " | " + report["pages"][pid]["status"], current["tests"]["output"])
            save_json(arc_dir / "ui_checks.json", report)
            if current["tests"] and current["tests"]["returncode"] == 0:
                agent = {"turns": 0, "checks": current}
            else:
                context = {"page": page_context(design, pid),
                    "requirements": [{key: value for key, value in (requirement_contexts or {}).get(rid, {"id": rid}).items() if key != "images"} for rid in uses],
                    "required_controls": [{key: control.get(key) for key in ("id", "role", "name", "href", "element", "inherited", "required_by") if control.get(key) is not None} for control in required],
                    "visual_references": {iid: (images or {})[iid] for iid in design["pages"][pid]["images"]},
                    "implementation_path": selected_path(output, "page", pid)}

                workspace = selection.workspace(target=("page", pid),
                    references=["frontend/src/ui-runtime.ts", relative, "frontend/package.json", "frontend/tsconfig.json"])
                report["pages"][pid]["status"] = "IMPLEMENTING"
                agent = run_file_agent(model, INSTRUCTIONS, context, workspace, implementation_gate,
                    max_turns, console, label + " / Implementation", feedback=current,
                    feedback_filter=lambda value: selection.feedback(workspace, value))
            accepted.append(pid)
            report["pages"][pid].update({"status": "GREEN", "turns": agent["turns"],
                "implementation_path": selected_path(output, "page", pid),
                "checks": {key: value for key, value in agent["checks"]["tests"].items() if key != "output"}})
            save_json(arc_dir / "ui_checks.json", report)
            console.show(label + " | GREEN", agent["checks"]["tests"]["output"])
        save_json(arc_dir / "ui_checks.json", report)
    console.show("UI final verification", "All page tasks were attempted. Run display regression and build, retaining any failures.")
    files = [test_path(pid) for pid in design["pages"]]
    verification = final_checks(console,
        typecheck=(lambda: checks.typecheck(files), None),
        tests=(lambda: checks.tests(files), require_green),
        build=(checks.build, None))
    final = verification["tests"].get("result") or {}
    report["summary"] = {"pages": len(accepted), "tests": final.get("passed", 0), "failed": final.get("failed", 0)}
    complete_report(report, report["pages"].values(), verification)
    save_json(arc_dir / "ui_checks.json", report)
    return report
