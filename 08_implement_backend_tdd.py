"""Step 08: TDD-driven enforcement of P, Q, and Dataflow Obligations."""

import json
from contextlib import ExitStack
from pathlib import Path

from infrastructure.reporting.status import is_passed
from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.project.entrypoints import selected_path
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.recovery import implementation_task, final_checks, complete_report
from infrastructure.stages.backend.context import load_context, requirement_context, api_requirement_uses, supplied_sources
from infrastructure.utils.files import read_artifact
from infrastructure.stages.backend.runtime import TEST_BINDINGS_PATH, prepare_runtime, sync_implementations, test_bindings_source, test_path
from infrastructure.stages.backend.contracts import format_traceability_summary, requirement_traceability
from infrastructure.stages.backend.http_tests import (creating_apis, flow_verdict, prepare_http_tests,
    probe_cases, unverified_flows)
from infrastructure.stages.backend.testing import BackendChecks, RedRejected, require_green, require_red
from infrastructure.stages.database.acceptance import (
    DATABASE_STAGE, SchemaBlocked, implemented_tables, upstream_blocker)
from infrastructure.utils.cli import create_parser, resolve_directories
from infrastructure.shared.models.text import TextModel
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.utils.presentation.logging import console_log
from infrastructure.shared.project.dependencies import install_dependencies
from infrastructure.utils.files import save_bytes, save_json, save_text


TEST_INSTRUCTIONS = """Write one canonical test suite for task.api, shared by all_requirement_uses, before
implementing the API. Base expected outcomes on the requirement scenarios, API I/O,
preconditions, postconditions and DB effects. Use db_schema and supplied SQL for fixtures.

Create a .test.ts file using task.new_file, or update test_path. Use node:test with
{test}, node:assert/strict as assert, and async test callbacks. Import the function
named api.signature.name from api_module; the compiler maps it to the API implementation.
Its signature is (request: ApiRequest, connection) => Promise<ApiResponse>.
Requests have params, query, body and headers; responses have status, body and optional
headers. They are plain transport objects, not Express request/response objects.
Compute relative .js imports to api_module and support_module from the chosen test path.

Use createTestDatabase(t) and request(partial) from support_module. Each test receives
a fresh real SQLite database. Seed Given state directly with parameterized SQL, supplying
required columns; do not invoke another API to manufacture fixtures. Bind booleans as 0/1.
Use the supplied test utility definitions for function signatures and fixture data
conventions. Construct fixtures compatible with those conventions and the API contract;
do not guess helper arguments or substitute arbitrary encoded values. No account is preseeded.

Within max_tests, prioritize distinct requirement-defined outcomes:
the primary valid workflow, critical rejection behavior, and
explicitly required persistence or session invariants.

For each test, assert the relevant API response and observable
database effect. Rejected operations must not create unintended
records or sessions.

Combine compatible assertions within one test rather than creating
redundant test cases. Do not invent constraints absent from the
requirement or API contract.

Verify DB outcomes with SQL, not source-code scans or
test-title labels. Treat R/W as possible effects; rejection need not perform them all.
Do not assert random IDs/hashes/timestamps or exact wording absent from the contract.
Cast response.body from unknown as needed. HTTP 501 is not a successful business result.

Use real API functions and SQLite, without mocks, skipped/focused tests or extra libraries.
Cross-API browser workflows belong to frontend E2E tests. The current test is writable;
other tests and infrastructure follow the files map. During test generation, repair
supplied writable sources only to restore existing behavior or compilation, not to
implement the new operation. The compiler compiles generated tests and runs accepted
API regressions, then establishes assertion RED against a 501 stub before implementation.
When repairing tests, preserve requirement-defined cases and assertions. Missing API
behavior is expected RED; fix test/fixture errors without deleting that behavioral coverage.
"""

IMPLEMENT_INSTRUCTIONS = """Implement task.api for all_requirement_uses against its generated tests.
Create the task.new_file target or update implementation_path. Export:
export async function handle(request: ApiRequest, connection: Database.Database): Promise<ApiResponse>.
The export is always handle; api.signature.name is the compiler-provided business alias
used by test bindings. Import ApiRequest and ApiResponse from backend/src/api/types.ts
using a relative .js ESM import. Return status/body/optional headers, not Express objects.

Implement request validation, API preconditions, success/rejection responses and DB
effects according to the contract and scenarios. Use the supplied connection, parameterized
better-sqlite3 SQL, and transactions for related writes. Respect schema-generated integer
keys and use node:crypto for UUIDs or hashing when the schema/contract requires them.
Use the supplied test utilities and frozen tests to understand existing fixture data
conventions. Preserve compatibility when reading those records. Production storage must
satisfy the contract and keep plaintext passwords out of persistent data.
Preserve required comparison rules and shared credential/header conventions.

Do not start a server, open another database, change schema or seed production data.
Use installed dependencies and Node standard modules. Implement the operation directly;
no DB-access primitive layer is generated. Routing and test aliases are compiler-owned.
Preserve unrelated behavior when repairing other supplied writable backend sources.
Tests, SQL, designs and compiler infrastructure are references according to files.
The compiler runs TypeScript and current/accepted API tests. Repair implementation
failures without weakening assertions or detecting tests to fake business behavior.

The compiler also replays this API over real HTTP: it boots the generated Express
app on an ephemeral port with an isolated in-memory database, calls the registered
route, and checks the HTTP status, the JSON body and the database side effects. A
handler that only works when called directly, or that crashes or writes on invalid
input, fails that gate. Do not edit or add files under backend/tests.

Before editing, identify the first concrete failing assertion
or type error and its root cause.

Preserve existing passing behavior. Make only the minimum
necessary source changes and do not regenerate working modules.

Keep validation, database writes, response fields and
session behavior consistent with the API contract.

On rejection, avoid partial writes; on success, persist all fields
necessary for subsequent login/refresh/navigation scenarios.
"""


def flow_coverage(contracts: dict, http: dict) -> dict:
    """Report the record-creating APIs whose success journey no derived flow exercises.

    A create no flow executes is observed only through its rejections, so accepting it
    would green-wash the API: the journey was never proven. The planner's own reason is
    carried into the report, so the defect is actionable instead of a silent skip.
    """
    exercised = set(http["flows"]["apis"])
    notes = {note["api"]: note for note in http["flows"]["notExercised"]}
    creators = creating_apis(contracts)
    unverified = [{"api": aid, **notes.get(aid, {"kind": "flow", "reason": "no CRUD flow executes it"})}
        for aid in creators if aid not in exercised]
    lines = [f"HTTP FLOW COVERAGE {len(creators) - len(unverified)}/{len(creators)}"
        " record-creating API(s) exercised by a recorded success flow"]
    lines.extend(f"  UNVERIFIED {item['api']} ({item['kind']}): {item['reason']}" for item in unverified)
    return {"returncode": 1 if unverified else 0, "unverified": unverified, "output": "\n".join(lines)}


def implement_backend_tdd(arc_dir: Path, pause: bool = False, max_turns: int = 10, max_tests: int = 5) -> dict:
    if max_turns < 1:
        raise ValueError("--max-turns must be positive")
    if max_tests < 1:
        raise ValueError("--max-tests must be positive")
    tree, schema, obligations = load_context(arc_dir)
    contracts = read_artifact(arc_dir, "backend_apis.json")
    output = arc_dir.parent.resolve()
    if not (output / "backend/src/db/initialize.ts").is_file():
        raise ValueError("Run 00_project_initialization.py and 05_implement_db.py first")
    for name in schema["entities"]:
        if not (output / f"backend/src/db/tables/{name}.sql").is_file():
            raise ValueError(f"Missing implemented SQL for {name}; run step 05")
    prepare_runtime(output, contracts["apis"])
    console = DemoConsole(pause)
    console.show("TDD preparation | Install dependencies", f"Workspace: {output}\nCommand: npm install --include=dev --no-audit --no-fund")
    install_dependencies(output)
    if not contracts["apis"]:
        report = {"status": "NO_APIS", "requirements": {rid: {"status": "NO_DIRECT_APIS", "apis": []}
                for rid in tree.postorder},
            "tests": {}, "red": {}, "green": {}, "agents": {}, "summary": {"tests": 0, "pass": 0, "fail": 0}}
        save_json(arc_dir / "backend_tdd.json", report)
        print("No Backend API Primitives require implementation; no tests or model calls were run.")
        return report
    checks = BackendChecks(output)
    http = prepare_http_tests(output, contracts)
    coverage = flow_coverage(contracts, http)
    console.show("HTTP integration | Compile real Express routes",
        f"Support: {http['support']}\nProbe suites: {len(http['files'])} ({http['probes']} probes)"
        + f"\nCRUD flows: {http['flows']['file']} ({len(http['flows']['resources'])} resources, {http['flows']['steps']} steps)"
        + "\nEach probe boots createApp(connection) on an ephemeral port over an isolated in-memory database and issues a real HTTP request."
        + "\nA flow passes only when every documented success status was actually observed; a documented rejection marks the journey UNVERIFIED."
        + "\n" + format_traceability_summary(contracts)
        + ("\n" + coverage["output"] if coverage["unverified"] else ""))
    selection = SourceFiles(output, root="backend/src", tests_root="backend/tests")
    model = TextModel() if contracts["apis"] else None
    owners = {aid: [rid for rid in tree.postorder if aid in contracts["requirements"][rid]] for aid in contracts["apis"]}
    targets = {aid: selected_path(output, "backend_test", aid) or test_path(ids[0], aid) for aid, ids in owners.items()}
    report = {"status": "RUNNING", "requirements": {}, "tests": {}, "red": {}, "green": {}, "agents": {},
        "traceability": requirement_traceability(contracts),
        "http": {"support": http["support"], "flows": {**http["flows"], "status": "PENDING",
                "verdict": "PENDING", "unverified": []},
            "coverage": coverage, "apis": {}}}
    # Publish a current status before work; an old GREEN is never reused as proof.
    save_json(arc_dir / "backend_tdd.json", report)
    try:
        files_by_api = {aid: [relative] for aid, relative in targets.items()}
        accepted, attempted = [], set()
        for requirement_number, rid in enumerate(tree.postorder, 1):
            ids = contracts["requirements"][rid]
            pending = [aid for aid in ids if aid not in attempted]
            requirement_label = f"Requirement {requirement_number}/{len(tree.requirements)} {rid}"
            console.show(f"{requirement_label} | Begin requirement TDD", json.dumps(requirement_context(tree, rid), ensure_ascii=False, indent=2))
            report["requirements"][rid] = {"status": "RUNNING", "apis": ids}
            save_json(arc_dir / "backend_tdd.json", report)
            if not ids:
                report["requirements"][rid]["status"] = "NO_DIRECT_APIS"
                console.show(f"{requirement_label} | No direct APIs", "Child APIs are not inherited. Proceed to the next requirement.")
                save_json(arc_dir / "backend_tdd.json", report)
                continue
            reused = [aid for aid in ids if aid in accepted]
            if reused:
                console.show(f"{requirement_label} | Reuse accepted APIs", ", ".join(reused) + " already passed their canonical contract tests.")
            for aid in pending:
                attempted.add(aid)
                entry = report.setdefault("apis", {}).setdefault(aid, {"status": "GENERATING_TESTS", "api": aid})
                with implementation_task(output, entry, console, "08 / " + aid):
                    api_owners = owners[aid]
                    api = contracts["apis"][aid]
                    relative = targets[aid]
                    label = f"{rid} / {aid} (uses: {', '.join(api_owners)})"
                    console.show(f"{label} | Test Generation: P / Q / R / W", json.dumps(api, ensure_ascii=False, indent=2))

                    accepted_runs = []

                    def test_gate(aid=aid):
                        chosen = selected_path(output, "backend_test", aid) or relative
                        targets[aid] = chosen
                        files_by_api[aid] = [chosen]
                        generation_files = [targets[key] for key in [*accepted, aid]]
                        compiled = checks.typecheck(generation_files)
                        if compiled["returncode"]:
                            raise ValueError(f"Test TypeScript compilation failed: {compiled['output']}")
                        # Compiling every accepted suite still proves the new file cannot break
                        # them at the type level. Re-running them costs one Node process per
                        # round and is only meaningful when this round also changed a file they
                        # share, so a round that edited the target suite alone skips it. The
                        # RED confirmation, the implementation gate and the final regression
                        # still run every accepted suite.
                        if accepted and (workspace.round_changed - {chosen}):
                            accepted_runs.append(checks.tests([targets[key] for key in accepted]))
                            require_green(accepted_runs[-1])
                        return compiled

                    context = {
                        "api_id": aid, "api": api,
                        "all_requirement_uses": api_requirement_uses(tree, contracts["requirements"], aid),
                        "db_schema": schema["entities"], "test_path": relative if (output / relative).is_file() else None,
                        "max_tests": max_tests,
                        "api_module": TEST_BINDINGS_PATH, "support_module": "backend/tests/support/context.ts",
                    }
                    workspace = selection.workspace(target=("backend_test", aid), existing=relative,
                        references=[TEST_BINDINGS_PATH, *supplied_sources(output, api, schema)])
                    agent = run_file_agent(model, TEST_INSTRUCTIONS, context, workspace,
                        test_gate, max_turns, console, label + " / Test Generation",
                        feedback_filter=lambda value: selection.feedback(workspace, value))
                    for owner in api_owners:
                        report["tests"].setdefault(owner, {})[aid] = targets[aid]
                    report["agents"][f"tests:{aid}"] = {"turns": agent["turns"],
                        "seconds": agent.get("seconds"), "accepted_rechecks": len(accepted_runs)}
                    save_json(arc_dir / "backend_tdd.json", report)
                    entry["status"] = "CHECKING_RED"
                    binding_source = (output / TEST_BINDINGS_PATH).read_bytes()
                    try:
                        save_text(output / TEST_BINDINGS_PATH,
                            test_bindings_source(output, contracts["apis"], stubbed=[aid]))
                        result = checks.tests(files_by_api[aid])
                        try:
                            require_red(result)
                        except RedRejected as error:
                            # SQLite refusing the published structure is not a test defect:
                            # this pass may read the SQL but never rewrite it, so the request
                            # goes back to the pass that owns backend/src/db.
                            if error.category != "database":
                                raise
                            upstream = upstream_blocker(result["output"], schema,
                                implemented_tables(output, schema)) or {
                                "stage": DATABASE_STAGE, "tables": [], "problems": {},
                                "reason": error.category, "evidence": result["output"][:1500]}
                            entry["status"] = "BLOCKED_UPSTREAM"
                            save_json(arc_dir / "backend_tdd.json", report)
                            raise SchemaBlocked(f"{aid}: RED cannot be proven because SQLite "
                                f"refuses the published table structure ({upstream['reason']})",
                                upstream) from error
                        report["red"][aid] = {key: result[key] for key in ("tests", "pass", "fail")}
                        console.show(f"{aid} | RED: contract assertions fail against the test stub", result["output"])
                    finally:
                        save_bytes(output / TEST_BINDINGS_PATH, binding_source)
                    entry["status"] = "IMPLEMENTING"
                    api = contracts["apis"][aid]
                    console.show(f"{aid} | Implementation context", (
                        json.dumps(api, ensure_ascii=False, indent=2)
                        + "\nFrozen tests: " + ", ".join(files_by_api[aid])
                        + "\nAPI may directly operate the database; no further Primitive decomposition."
                    ))
                    regression_files = [relative for key in [*accepted, aid] for relative in files_by_api[key]]
                    http_files = [http["files"][aid]]

                    def implementation_checks():
                        sync_implementations(output, contracts["apis"])
                        compiled = checks.typecheck([*regression_files, *http_files])
                        if compiled["returncode"]:
                            return {"typecheck": compiled, "tests": None, "http": None}
                        return {"typecheck": compiled, "tests": checks.tests(regression_files),
                            "http": checks.tests(http_files)}

                    def implementation_gate():
                        result = implementation_checks()
                        if result["typecheck"]["returncode"]:
                            raise ValueError(result["typecheck"]["output"])
                        require_green(result["tests"])
                        require_green(result["http"])
                        return result

                    api_entry = selected_path(output, "api", aid)
                    current = implementation_checks() if api_entry and (output / api_entry).is_file() else None
                    if current and current["tests"] and current["http"] and not current["tests"]["returncode"] \
                            and not current["http"]["returncode"] and current["tests"]["tests"] and current["http"]["tests"]:
                        agent = {"turns": 0, "checks": current}
                    else:
                        context = {
                            "api_id": aid, "api": api, "db_schema": schema["entities"],
                            "all_requirement_uses": api_requirement_uses(tree, contracts["requirements"], aid),
                            "implementation_path": selected_path(output, "api", aid),
                        }
                        workspace = selection.workspace(target=("api", aid),
                            references=[*files_by_api[aid], *supplied_sources(output, api, schema)])
                        agent = run_file_agent(model, IMPLEMENT_INSTRUCTIONS, context, workspace,
                            implementation_gate, max_turns, console, aid + " / Implementation", feedback=current,
                            feedback_filter=lambda value: selection.feedback(workspace, value))
                    accepted.append(aid)
                    result = agent["checks"]["tests"]
                    report["green"][aid] = {key: result[key] for key in ("tests", "pass", "fail")}
                    report["http"]["apis"][aid] = {"file": http["files"][aid], "probes": len(probe_cases(contracts, aid)),
                        **{key: agent["checks"]["http"][key] for key in ("tests", "pass", "fail")}}
                    report["agents"][f"implementation:{aid}"] = {"turns": agent["turns"],
                        "seconds": agent.get("seconds")}
                    save_json(arc_dir / "backend_tdd.json", report)
                    console.show(f"{aid} | GREEN: handler tests and real HTTP routes pass",
                        result["output"] + "\n" + agent["checks"]["http"]["output"])
                    entry["status"] = "GREEN"
                save_json(arc_dir / "backend_tdd.json", report)
            report["requirements"][rid]["status"] = "GREEN" if all(aid in accepted for aid in ids) else "FAILED"
            save_json(arc_dir / "backend_tdd.json", report)
            console.show(f"{requirement_label} | " + report["requirements"][rid]["status"], "Proceed to the next requirement; failed APIs remain recorded.")
        all_files = [targets[aid] for aid in accepted] + list(http["files"].values())
        console.show("Final regression | All requirements complete",
            "Compile the full backend and run the handler suites plus every real HTTP probe suite.")
        verification = final_checks(console,
            typecheck=(checks.typecheck, None),
            tests=(lambda: checks.tests(all_files), require_green if all_files else None),
            http_flows=(lambda: checks.tests([http["flows"]["file"]]), require_green if http["flows"]["resources"] else None),
            http_coverage=(lambda: coverage, None),
            build=(checks.build, None))
        final = verification["tests"].get("result") or {}
        flows = verification["http_flows"]
        report["http"]["flows"].update({**flows, "file": http["flows"]["file"],
            **{key: (flows.get("result") or {}).get(key, 0) for key in ("tests", "pass", "fail")},
            # A design that creates no record has no journey to replay, so the empty suite
            # is vacuous; otherwise the run is classified instead of being read as green.
            "verdict": flow_verdict(flows) if http["flows"]["resources"] else "PASS",
            "unverified": unverified_flows(flows)})
        report["http"]["summary"] = {key: sum(entry.get(key, 0) for entry in report["http"]["apis"].values()) + report["http"]["flows"].get(key, 0)
            for key in ("tests", "pass", "fail")}
        report["summary"] = {key: final.get(key, 0) for key in ("tests", "pass", "fail")}
        complete_report(report, report.get("apis", {}).values(), verification)
        save_json(arc_dir / "backend_tdd.json", report)
        print(f"TDD {report['status']}: {report['summary']}"
            f"\nHTTP flows: {report['http']['flows']['verdict']}"
            + ("" if not report["http"]["flows"]["unverified"] else
               " (unverified: " + ", ".join(report["http"]["flows"]["unverified"]) + ")")
            + f"\nReport: {arc_dir / 'backend_tdd.json'}", flush=True)
        return report
    except BaseException as error:
        report["status"] = "FAILED"
        report["error"] = str(error)
        for requirement in report["requirements"].values():
            if requirement["status"] in {"RUNNING", "RED_CONFIRMED"}:
                requirement["status"] = "FAILED"
        save_json(arc_dir / "backend_tdd.json", report)
        raise


def main() -> None:
    parser = create_parser("Step 08: TDD-driven API Implementation (P / Q / Dataflow Obligations)")
    parser.add_argument("--max-turns", type=int, default=10, help="Maximum edit rounds per test/implementation agent")
    parser.add_argument("--max-tests", type=int, default=5, help="Maximum key contract tests per API (default: 5)")
    args = parser.parse_args()
    with ExitStack() as stack:
        try:
            _, output = resolve_directories(args)
            stack.enter_context(console_log(output, debug_model_io=args.debug_model_io))
            result = implement_backend_tdd(output / ".arc", args.pause, args.max_turns, args.max_tests)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Execution failed: {error}\n")

    if not is_passed(result["status"]):
        parser.exit(1, "All implementation tasks were attempted; a final backend check did not"
            " pass. See the .arc report.\n")


if __name__ == "__main__":
    main()
