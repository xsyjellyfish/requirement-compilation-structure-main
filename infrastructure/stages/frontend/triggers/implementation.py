"""Step 12 orchestration: scenario test generation -> trigger repair -> regression.

For each leaf requirement, one agent generates key E2E cases and another repairs
the trigger implementation. Complete that requirement before advancing; include
accepted functional smoke scenarios in regression checks."""

import json

from infrastructure.shared.agent.files import editable_sources
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.recovery import (
    implementation_task, final_verification, complete_report)
from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.project.entrypoints import selected_path
from infrastructure.stages.frontend.triggers.e2e_runtime import prepare_e2e
from infrastructure.stages.frontend.triggers.testing import E2EChecks, require_behavioral_red, test_path
from infrastructure.stages.frontend.acceptance.pipeline import DESIGN_STAGE, run_acceptance
from infrastructure.stages.frontend.triggers.runtime import sync_triggers, prepare_trigger_paths
from infrastructure.stages.frontend.triggers.contracts import contract_report, require_contract, route_problems
from infrastructure.stages.frontend.triggers.workspaces import (
    implementation_workspace as implementation_workspace_for,
    test_workspace as test_workspace_for)
from infrastructure.stages.frontend.ui.runtime import prepare_frontend
from infrastructure.stages.frontend.ui.testing import require_green
from infrastructure.stages.backend.testing import BackendChecks, require_green as require_backend_green
from infrastructure.stages.frontend.triggers.design import api_ids, trigger_obligations, relevant_pages
from infrastructure.stages.frontend.triggers.scope import TriggerCheckFailure, model_context
from infrastructure.shared.project.dependencies import install_dependencies
from infrastructure.utils.files import read_source, save_json
from infrastructure.stages.frontend.ui.design import page_context, page_elements


def scenario_context(ui, design, rid):
    """Include direct requirement triggers and required page-entry lifecycle behavior."""
    pages = set(relevant_pages(ui, rid))
    while True:
        elements = {uid for pid in pages for uid in page_elements(ui, pid)}
        tids = set(design["requirements"].get(rid, [])) | {
            tid for tid, trigger in design["triggers"].items()
            if trigger["source"] in elements and trigger["event"] == "mount"}
        targets = {action.get("ui") for tid in tids for action in design["triggers"][tid]["action"] if action["kind"] == "ui"}
        destinations = {pid for pid, page in ui["pages"].items() if page["root"] in targets}
        hrefs = {ui["elements"][uid]["prop"].get("href") for uid in elements}
        destinations.update(pid for pid, page in ui["pages"].items() if page["route"] in hrefs)
        if destinations.issubset(pages):
            return {"page_subtrees": {pid: page_context(ui, pid) for pid in sorted(pages)},
                "triggers": {tid: design["triggers"][tid] for tid in sorted(tids)}}
        pages.update(destinations)


def trigger_audit(output, ui, design, backend, active):
    """Collect every frontend source whose requests must match the accepted API contract.

    A trigger is compared with the APIs its own designed actions declare, which rejects
    replacing the designed operation with an unrelated one. A shared helper or page is
    checked for request shape only: one helper may legitimately serve several
    requirements, so it has no single designed action to compare with.
    """
    designs = {tid: api_ids(design["triggers"][tid]) for tid in sorted(active)
        if tid in design["triggers"]}
    entries, known = [], set()
    for tid, declared in designs.items():
        relative = selected_path(output, "trigger", tid)
        if relative and (output / relative).is_file():
            entries.append((relative, read_source(output / relative), declared))
            known.add(relative)
    for relative in editable_sources(output, "frontend/src"):
        if relative not in known and (output / relative).is_file():
            entries.append((relative, read_source(output / relative), None))
    return contract_report(backend, entries)


def route_report(backend, result):
    """Turn an `Unknown API route` failure into a diagnosis of the underlying route."""
    routed = route_problems(backend, [*(result.get("failures") or []), result.get("output") or ""])
    if not routed:
        return None
    return {"returncode": 1, "problems": routed,
        "output": "\n".join(f"[{item['api'] or 'request'}] {item['reason']} Fix: {item['fix']}"
            for item in routed)}


def test_run_status(result):
    """Route existing frontend runtime failures to code repair, not to test rewriting or valid RED.

    A request the canonical client rejected (contract or unknown route) is an application
    defect, so it must reach the repair loop instead of counting as expected RED.
    """
    if result["returncode"] == 0:
        require_green(result)
        return "GREEN"
    headlines = [error.split("\n", 1)[0] for error in result.get("failures", [])]
    runtime_defect = ("E2E_RUNTIME_ERROR:", "E2E_API_CONTRACT_ERROR:", "E2E_API_ROUTE_ERROR:")
    if not result.get("errors") and not result.get("skipped") and not result.get("flaky") and result.get("failed") and any(prefix in error for prefix in runtime_defect for error in headlines) and not any("E2E_BACKEND_ERROR:" in error or "E2E_NETWORK_ERROR:" in error or "browserType.launch" in error for error in headlines):
        return "REPAIR_REQUIRED"
    require_behavioral_red(result)
    return "RED_CONFIRMED"


def acceptance_check(report) -> dict:
    """Expose the black-box acceptance layer as a final check that must actually pass.

    A layer that could not derive or run its journeys (``BLOCKED_UPSTREAM``,
    ``UNVERIFIED``) or whose journeys failed (``FAILED``) has verified nothing, so it may
    never be recorded as a pass. The evidence is carried in the result as well as the
    output, so the report can name the design or application defect behind the failure.
    """
    entry = report or {}
    status, audit = entry.get("status"), entry.get("audit") or {}
    problems = [*(entry.get("derivation_problems") or []), *(entry.get("problems") or []),
        *(entry.get("unverified") or [])]
    test = entry.get("tests") or {}
    output = ("Black-box acceptance: " + str(status) + "\n"
        + str(entry.get("error") or audit.get("output") or "")
        + ("" if not problems else "\n" + "\n".join(problems)))
    return {"returncode": 0 if status in {"GREEN", "NOT_APPLICABLE"} else 1,
        "status": status, "passed": test.get("passed", 0),
        "failed": test.get("failed", 0), "problems": problems, "output": output}


def require_acceptance(report) -> None:
    """Stop the stage when the layer could not verify the business journeys at all.

    ``BLOCKED_UPSTREAM`` means the journeys cannot be derived from the published IR, and
    ``UNVERIFIED`` means no journey could be derived from it. Neither is an application
    defect, so the repair loop never ran and no later pass may paper over it: only the
    pass that owns the IR can remove the cause. Continuing would compile, test and deliver
    an application whose whole business flow no test ever drove, which is exactly the false
    pass this layer exists to prevent. A journey that ran and failed stays a recoverable
    failure of the stage, so only these two statuses raise here.
    """
    entry = report or {}
    status = entry.get("status")
    if status not in {"BLOCKED_UPSTREAM", "UNVERIFIED"}:
        return
    error = TriggerCheckFailure("Black-box acceptance " + str(status)
        + ": the business journeys cannot be verified", {
            key: entry.get(key) for key in
            ("status", "problems", "derivation_problems", "unverified", "error")})
    error.upstream = {"stage": DESIGN_STAGE, "category": "design_contract",
        "reason": "The UI IR cannot express the acceptance journey",
        "evidence": entry.get("error"),
        "problems": entry.get("derivation_problems") or entry.get("problems") or []}
    raise error


def reconcile_requirements(report, tree, output, result) -> list[str]:
    """Replace an earlier per-requirement GREEN with the verdict of the final full run.

    The suites are implemented one requirement at a time, so a suite that passed on its own
    may still fail once every neighbour is in place (a shared helper, a trigger another
    requirement rewrote, an assembled page). The last full run is the only run whose verdict
    describes the application that is delivered, so its per-spec failures and its missing
    specs downgrade those requirements instead of leaving them accepted.
    """
    files, failed = result.get("files") or {}, result.get("files_failed") or {}
    changed = []
    for rid in tree.leaves:
        entry = report["requirements"].get(rid) or {}
        if entry.get("status") != "GREEN" or not files:
            continue
        relative = test_path(rid, output)
        if failed.get(relative):
            entry.update({"status": "FAILED", "final_failures": failed[relative],
                "error": f"The final full frontend run failed {failed[relative]} case(s) of {relative}"})
        elif relative not in files:
            entry.update({"status": "UNVERIFIED",
                "error": f"The final full frontend run never executed {relative}"})
        else:
            continue
        changed.append(rid)
    return changed


TEST_INSTRUCTIONS = """Write a small Playwright functional smoke suite for the CURRENT requirement.
The goal is to verify that real UI input reaches the backend API and its result is
reflected in the application. Backend contract details were already tested in step 08;
UI presentation was already tested in step 10.

Normally write one primary successful workflow. Use the second available case for
the most discriminative *explicitly required* failure or anonymous state (rather
than another happy-path variation). If one test can cover a required reload/session
transition, include it in that workflow. Stay within max_tests;
do not fill the quota with field combinations or exhaustive branches. A successful
workflow may include navigation and reload when those are part of this requirement.
Destination pages and their mount triggers are supporting context, not instructions
to test other requirements' registration, login or logout workflows in this suite.

Create the .spec.ts target using task.new_file or update test_path. Import test and
expect from frontend/tests/e2e/support/context.ts with a relative .js import. Tests use
async ({page,database}) callbacks. Use the supplied test utility definitions and actual
backend source to understand fixture signatures and stored-data conventions. The fixture
resets a real isolated SQLite database before each case. Seed only necessary Given rows
with parameterized SQL and valid values; bind booleans as 0/1. Do not use another API to
manufacture Given state.

Drive actions through the browser, without mocks or injected UI state. Locate controls the way
an external reviewer does: page.getByRole("button", { name: "Save", exact: true }),
page.getByLabel("Email"), or page.getByPlaceholder(...). Use the supplied data-testid only as a
fallback for a node that has no accessible name, such as a container or a row. A control whose
accessible name is missing, or mixes several values, is an implementation defect to repair,
never a reason to select by test ID. Assert the visible business result with retrying Playwright
assertions, including error visibility and lack of false navigation on required
rejection paths. If the requirement says state survives reload, actually reload
and assert the restored visible state. Check a small persisted business outcome
only when necessary. Do not
assert every DB field, full-table snapshots, exact cookie/hash encodings, generated IDs,
timestamps, decorative text, styling or intermediate loading states. If deletion versus
soft invalidation is not specified, assert the observable session behavior instead of
choosing one database strategy. Do not invent wording or storage policies.

Tests still need real assertions and must not be skipped or weakened to pass. During
test generation, fix test syntax, fixtures or invalid selectors. Missing application
behavior is valid RED; retain that case for implementation. Runtime failures in supplied
application sources are implementation feedback, not a reason to rewrite the expected
business result. Compilation and a real browser run check the suite before code repair.
"""

IMPLEMENT_INSTRUCTIONS = """Implement the requirement's frontend triggers together against its E2E tests.
The task supplies the current requirement's triggers, supporting page-entry behavior,
actual frontend/backend source files. Application sources are writable according to
files; runtime wiring, schema and frozen tests are references. You may update multiple
pages, API handlers and helpers in the same edits list. Complete the functional path:
UI event -> frontend values -> API params/query/body -> real backend response -> shared
frontend state -> visible result and navigation. Use the actual supplied source and API
contract to resolve field names, types, status branches and stored-data conventions.

Before editing, trace
for each requirement scenario: real event -> latest bound inputs -> actual API
request -> documented status/body -> visible state -> destination/reload state.
Fix any missing link; prioritize functional correctness over visual embellishment.
For expected error responses, update the designed error binding and keep the
user on the correct page; never navigate as if the request succeeded.

Implement this requirement's behavior; supporting page context does not require building
unrelated workflows early. Keep responses short by editing only files that need changes.
implementation_paths maps each trigger to its supplied writable file; null file content
means that file must be created. Return all required implementations in one edits list.
Export handle(context: UiTriggerContext): void | Promise<void> from
one source file per trigger, importing UiTriggerContext from frontend/src/ui-runtime.ts.
Ordinary helper functions may be shared through supplied writable sources.

The compiler captures native events, synchronizes UI input, registers handlers, owns
central frontend data and provides routing/API access. UiTriggerContext contains:
- data: current frontend variable values, exposed as a readonly record.
- input: optional value (string), checked (boolean) and key (string) for the native event.
- trigger: {id, source, event}.
- params: decoded parameters of the current dynamic route, for example
  {bookName: "My Book"} while /books/My%20Book is open. Use them for API params/query.
- route: the current normalized pathname.
- update(patch): merge frontend variable values into shared state.
- api(apiId, {params, query, body, headers, credentials}): perform the canonical HTTP call
  and return {status, body:unknown, headers}.
- navigate(pageId, params?): navigate using a PAGE ID from page_subtrees, not its root
  ELEMENT ID. Resolve a navigation action's target root to its owning page through
  page_subtrees. Pass the destination's route parameters as the second argument, for
  example navigate("book", {bookName: context.params.bookName}); a missing parameter is
  an error rather than a literal placeholder URL. navigate(pageId) keeps working for
  static routes.

CANONICAL API CONTRACT (checked before acceptance):
- backend_apis is the accepted contract and api_request_contracts lists the exact
  params, query, body and header names each API accepts. Pass the API ID as a string
  literal so each request can be verified against that contract.
- A request may use only those names. A hidden parameter is not an extension: a detail
  read invented as GET /api/books?detailTitle=, a delete invented as
  POST /api/books?deleteTitle= or a child created by posting a marker field to its
  parent collection contradict the contract and are rejected without being sent.
- Call only the APIs this trigger's designed actions name. Reusing an API that other
  requirements also use is expected and allowed; substituting a different operation is
  not. Never redefine an operation by shaping a request around an existing API.
- Fill the path parameters the API path declares and nothing else; read each value from
  the current route or from page data instead of leaving it empty or literal.
- Keep documented error handling, validation and retries. Never mask an unexpected
  status, and never fabricate a successful response or navigation.
- A 404 "Unknown API route" is a routing defect: fix the request or the generated route
  registration in backend/src/api/register.ts. Do not switch to another business API to
  make the workflow appear to succeed.
- Business requests go through context.api; raw fetch/XHR/axios bypasses the canonical
  method, path and contract and is rejected.

Implement the guard, ordered action conditions, request construction, response handling,
value conversion, UI updates and navigation in ordinary code. read and write describe frontend
data dependencies; obligations describes DB effects derived from API calls, not another
field mapping to implement literally. Native checkbox values are booleans and other input
values are strings; convert numbers explicitly when the API requires them.
Use context.api for business requests. For cookie-based contracts, the browser handles
cookies automatically; do not manually set Cookie or read HttpOnly credentials. Preserve
specified comparisons and failure-without-writes. Handle expected failures visibly and
surface unexpected errors rather than fabricating successful responses.

Before implementation, verify that each required interaction
has a complete event-to-result chain.

Read the latest input values from their actual bindings.
Construct API requests using the documented contract.
Handle success and rejection responses separately.

Never navigate to a success destination after a failed API call.
For session-dependent behavior, update visible authentication
state consistently with the server response.

When repairing a failed E2E test, identify the earliest failing
user-visible behavior and fix only its root cause.
Preserve previously passing workflows.

Pages remain presentational; avoid duplicate handlers or test detection. Repair any
supplied writable frontend source needed for actual behavior, preserving existing UI IDs,
styles and accepted workflows. Files permissions define editable sources; tests,
SQL, designs and compiler wiring remain references. Related API handlers and helpers
are supplied writable for integration fixes; backend tests remain read-only. Compilation,
functional smoke regressions and the final build accept the requirement implementation. Use their feedback to repair code
without weakening tests or hardcoding fixture values.
Fix the earliest failing
observable behavior with minimal edits; preserve previously passing workflows and
return only changed source files.
"""


def run_trigger_tdd(arc_dir, tree, schema, backend, ui, design, images, contexts,
        model, console, report, max_turns=10, max_tests=2):
    """Implement design one requirement at a time through real-app E2E TDD.

    Prepare dependencies/runtime, generate and freeze key scenario tests, then
    activate and repair the current trigger implementations before advancing. Regress
    accepted smoke scenarios, build the app, and update the caller's report."""
    output = arc_dir.parent.resolve()
    prepare_frontend(output)
    prepare_e2e(output, backend)
    sync_triggers(output, ui, design, backend, [])
    console.show("E2E preparation | Dependencies", "Install workspace dependencies and Chromium. Each browser run owns a new real test database; production data is not used.")
    install_dependencies(output)
    checks = E2EChecks(output, max_tests)
    checks.install_browser()
    accepted, implemented = [], set()
    selection = SourceFiles(output, ui)
    backend_selection = SourceFiles(output, root="backend/src", tests_root="backend/tests")
    report["status"] = "IMPLEMENTING"
    save_json(arc_dir / "frontend_e2e.json", report)
    for number, rid in enumerate(tree.leaves, 1):
        tids = set(design["requirements"][rid])
        steps = tree.own_steps[rid]
        label = f"E2E requirement {number}/{len(tree.leaves)} {rid}"
        context = scenario_context(ui, design, rid)
        pages = context["page_subtrees"]
        if not pages and not tids:
            report["requirements"][rid] = {"status": "NO_TRIGGERS", "triggers": []}
            save_json(arc_dir / "frontend_e2e.json", report)
            continue
        context_tids = set(context["triggers"])
        targets = tids | (context_tids - implemented)
        active = implemented | context_tids
        relative = test_path(rid, output)
        called = {aid for trigger in context["triggers"].values() for aid in api_ids(trigger)}
        context["obligations"] = {tid: trigger_obligations(trigger, backend) for tid, trigger in context["triggers"].items()}
        context.update({"requirement": contexts[rid], "target_trigger_ids": sorted(targets),
            "backend_apis": {aid: backend["apis"][aid] for aid in sorted(called)}})
        console.show(label + " | Scenario context", json.dumps(context, ensure_ascii=False, indent=2))
        report["requirements"][rid] = {"status": "GENERATING_TESTS", "triggers": sorted(tids),
            "target_triggers": sorted(targets), "test_path": relative}
        save_json(arc_dir / "frontend_e2e.json", report)
        with implementation_task(output, report["requirements"][rid], console, "12 / " + rid):
            regression_files = [test_path(key, output) for key in [*accepted, rid]]
            sync_triggers(output, ui, design, backend, active)

            def generation_checks():
                """Typecheck selected tests; test_gate also runs the real application."""
                nonlocal relative, regression_files
                relative = test_path(rid, output)
                regression_files = [test_path(key, output) for key in [*accepted, rid]]
                return checks.typecheck(regression_files, full_source=False)

            def test_gate():
                """Accept generated tests only when their structure and compilation pass; allow behavioral RED before implementation."""
                compiled = generation_checks()
                if compiled["returncode"]:
                    raise ValueError(compiled["output"])
                behavior = checks.tests([relative])
                try:
                    status = test_run_status(behavior)
                except ValueError as error:
                    raise TriggerCheckFailure("Repair invalid E2E tests or affected application sources", behavior) from error
                return {"typecheck": compiled, "behavior": behavior, "status": status}

            # The E2E spec drives real UI input through the canonical API and reads visible
            # state; it never opens the database, so the entity schema is not task context.
            # The API contract, page subtrees and obligation summary already carry every
            # field the suite must name.
            test_context = {**model_context(context), "test_path": relative if (output / relative).is_file() else None,
                "max_tests": max_tests}
            test_workspace = test_workspace_for(selection, output, context, rid, relative)
            agent = run_file_agent(model, TEST_INSTRUCTIONS, test_context,
                test_workspace, test_gate, max_turns, console, label + " / Test Generation",
                feedback_filter=lambda value: selection.feedback(test_workspace, value))
            report["requirements"][rid]["test_turns"] = agent["turns"]
            report["requirements"][rid]["test_path"] = relative

            workspace = None

            def implementation_checks():
                """Audit requests first; only contract-clean sources are compiled and run."""
                sync_triggers(output, ui, design, backend, active)
                contract = trigger_audit(output, ui, design, backend, active)
                result = {"contract": contract, "typecheck": None, "e2e": None}
                # A request that contradicts the accepted contract already fails acceptance,
                # so compiling and starting a browser would spend time on a discarded result.
                if contract["problems"]:
                    return result
                compiled = checks.typecheck(regression_files)
                result["typecheck"] = compiled
                if compiled["returncode"]:
                    return result
                if workspace and any(path.startswith("backend/src/") for path in workspace.changed_paths):
                    api_checks = BackendChecks(output)
                    result["backend_typecheck"] = api_checks.typecheck()
                    if result["backend_typecheck"]["returncode"]:
                        return result
                    suites = [path for aid in backend["apis"]
                        if (path := selected_path(output, "backend_test", aid))]
                    if suites:
                        result["backend_tests"] = api_checks.tests(suites)
                        if result["backend_tests"]["returncode"]:
                            return result
                result["e2e"] = checks.tests(regression_files)
                result["routes"] = route_report(backend, result["e2e"])
                return result

            def implementation_gate():
                """Require actual implementations, contract-clean requests, compilation and smoke results."""
                absent = [tid for tid in sorted(targets)
                    if not (path := selected_path(output, "trigger", tid)) or not (output / path).is_file()]
                if absent:
                    raise ValueError("Create all supplied trigger files before acceptance: " + ", ".join(absent))
                result = implementation_checks()
                try:
                    require_contract(result["contract"])
                    for kind in ("typecheck", "backend_typecheck"):
                        if result.get(kind) and result[kind]["returncode"]:
                            raise ValueError(result[kind]["output"])
                    if "backend_tests" in result:
                        require_backend_green(result["backend_tests"])
                    if result.get("routes"):
                        raise ValueError(result["routes"]["output"])
                    require_green(result["e2e"])
                except ValueError as error:
                    raise TriggerCheckFailure(
                        "Repair the implementation, its API requests or its integration", result) from error
                return result

            generated = agent["checks"]
            missing = [tid for tid in sorted(targets)
                if not (path := selected_path(output, "trigger", tid)) or not (output / path).is_file()]
            application_changed = any(path.startswith("frontend/src/") for path in test_workspace.changed_paths)
            current = {"typecheck": generated["typecheck"], "e2e": generated["behavior"]}
            report["requirements"][rid]["status"] = generated["status"]
            save_json(arc_dir / "frontend_e2e.json", report)
            # A passing suite can still hide a request the API contract does not declare,
            # so the written requests are audited before this requirement is accepted.
            audit = trigger_audit(output, ui, design, backend, active)
            report["requirements"][rid]["api_contract"] = "REPAIR" if audit["problems"] else "GREEN"
            # Reuse passing smoke results when test generation did not change application sources.
            if not missing and not application_changed and generated["status"] == "GREEN" \
                    and not audit["problems"]:
                agent = {"turns": 0, "checks": current}
            else:
                needs_implementation = (bool(missing) or generated["status"] != "GREEN"
                    or bool(audit["problems"]))
                feedback = current if not audit["problems"] else {**current, "contract": audit}
                if not needs_implementation:
                    try:
                        agent = {"turns": 0, "checks": implementation_gate()}
                    except ValueError as error:
                        needs_implementation = True
                        feedback = getattr(error, "feedback", {"error": str(error)})
                if needs_implementation:
                    report["requirements"][rid]["status"] = "IMPLEMENTING"
                    task = {**model_context(context),
                        "implementation_paths": prepare_trigger_paths(output, context_tids)}
                    workspace = implementation_workspace_for(selection, backend_selection, output,
                        context, context_tids, relative)

                    def repair_feedback(value):
                        return selection.feedback(workspace, backend_selection.feedback(workspace, value))
                    agent = run_file_agent(model, IMPLEMENT_INSTRUCTIONS, task, workspace,
                        implementation_gate, max_turns, console, label + " / Trigger Implementation",
                        feedback=feedback,
                        feedback_filter=repair_feedback)
            accepted.append(rid)
            reused = tids & implemented
            implemented.update(targets)
            result = agent["checks"]["e2e"]
            report["requirements"][rid].update({"status": "GREEN", "implementation_turns": agent["turns"],
                "steps": list(steps), "passed": result["passed"], "reused_triggers": sorted(reused)})
            save_json(arc_dir / "frontend_e2e.json", report)
            console.show(label + " | GREEN", result["output"])
        save_json(arc_dir / "frontend_e2e.json", report)
    sync_triggers(output, ui, design, backend, implemented)
    console.show("Black-box acceptance", "Derive the business journeys from the IR and run them in an independent browser project.")
    # The acceptance layer is independent of the requirement suites above: it derives one
    # complete visitor journey per CRUD resource, runs it in its own Playwright project with
    # its own fixture and database, and repairs the application (never the journey) when a
    # step fails. Its status becomes part of this stage's acceptance below.
    acceptance = run_acceptance(arc_dir, tree, schema, backend, ui, design,
        console=console, model=model, max_turns=2)
    report["acceptance"] = {key: acceptance.get(key) for key in
        ("status", "specs", "audit", "tests", "files", "problems", "derivation_problems",
            "unverified", "repair_turns", "error")}
    save_json(arc_dir / "frontend_e2e.json", report)
    # A layer that could not derive or run a journey has verified nothing, and the cause is
    # the published IR, not the application. Stop here: the requirements below were only
    # proven against their own scenarios, so carrying on would deliver an application whose
    # business flows no test ever drove.
    require_acceptance(acceptance)
    console.show("Final frontend verification", "All requirements were attempted. Run available functional suites, UI regression and both builds.")
    files = [test_path(rid, output) for rid in tree.leaves
        if report["requirements"].get(rid, {}).get("status") != "NO_TRIGGERS"
        and (output / test_path(rid, output)).is_file()]
    from infrastructure.stages.frontend.ui.testing import FrontendChecks, test_path as ui_test_path
    ui_checks = FrontendChecks(output)
    ui_files = [ui_test_path(pid) for pid in ui["pages"] if (output / ui_test_path(pid)).is_file()]
    api_checks = BackendChecks(output)
    api_files = [path for aid in backend["apis"]
        if (path := selected_path(output, "backend_test", aid)) and (output / path).is_file()]
    suite = {
        "api_contract": (lambda: trigger_audit(output, ui, design, backend, set(design["triggers"])),
            require_contract),
        "typecheck": (lambda: checks.typecheck(files), None),
        "backend_typecheck": (api_checks.typecheck, None),
        "backend_tests": (lambda: api_checks.tests(api_files),
            require_backend_green if api_files else None),
        "acceptance": (lambda: acceptance_check(acceptance), None),
        "build": (checks.build, None)}
    if files:
        suite["e2e"] = (lambda: checks.tests(files), require_green)
    if ui_files:
        suite["ui"] = (lambda: ui_checks.tests(ui_files), require_green)
    # Every check runs even after one fails, so the report keeps the full picture; the
    # per-check statuses then decide whether the stage may report a delivery at all.
    verification = final_verification(console, **suite)
    final = verification.get("e2e", {}).get("result") or {}
    ui_final = verification.get("ui", {}).get("result") or {}
    downgraded = reconcile_requirements(report, tree, output, final)
    if downgraded:
        console.show("Final frontend verification | requirements downgraded",
            "The final full run contradicts their earlier GREEN: " + ", ".join(downgraded))
    report["summary"] = {"requirements": len(accepted), "triggers": len(implemented),
        "e2e_tests": final.get("passed", 0), "ui_tests": ui_final.get("passed", 0),
        "acceptance": acceptance.get("status"),
        "acceptance_tests": (acceptance.get("tests") or {}).get("passed", 0),
        "downgraded_requirements": downgraded,
        "failed": final.get("failed", 0) + ui_final.get("failed", 0)}
    complete_report(report, report["requirements"].values(), verification)
    save_json(arc_dir / "frontend_e2e.json", report)
    return report
