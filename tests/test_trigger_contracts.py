"""Generated trigger code must replay the accepted API contract, not a private one.

The BookStack failure this suite describes produced triggers that "worked" only because
a hidden query flag replaced a real operation: ``GET /api/books?detailTitle=`` stood in
for the detail read, ``POST /api/books?deleteTitle=`` for the delete and
``POST /api/books?createPageFor=`` for the child create. The rules checked here run
before a passing E2E suite can accept such code, and the compiled client rejects the
same request at run time when the payload is built dynamically.
"""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from infrastructure.stages.frontend.triggers import contracts
from infrastructure.stages.frontend.triggers.runtime import api_client_source


def schema(fields, required=()):
    """Build the request-object shape step 06 writes for one part."""
    return {"type": "object", "properties": {name: {"type": "string"} for name in fields},
        "required": list(required)}


def api(name, method, path, params=(), query=(), body=(), required_query=(), required_body=()):
    """Build one API IR member with the request contract and DB effects the stages read."""
    return {"description": name, "read": [], "write": ["Book.title"],
        "signature": {"name": name, "method": method, "path": path},
        "request": {"params": schema(params, params), "query": schema(query, required_query),
            "body": schema(body, required_body), "headers": schema(())}}


def book_backend():
    """The contract the shortcut triggers bypassed with private query names."""
    return {"requirements": {"REQ-BOOKS": ["API-list-books", "API-get-book-details"]},
        "apis": {
            "API-list-books": api("listBooks", "GET", "/api/books"),
            "API-get-book-details": api("getBookDetails", "GET", "/api/books/{title}",
                params=("title",)),
            "API-search-books": api("searchBooks", "GET", "/api/books/search",
                query=("query", "limit"), required_query=("query",)),
            "API-create-book": api("createBook", "POST", "/api/books",
                body=("title", "author", "summary"), required_body=("title", "author")),
            "API-update-book": api("updateBook", "PATCH", "/api/books/{title}",
                params=("title",), body=("summary",)),
            "API-delete-book": api("deleteBook", "DELETE", "/api/books/{title}",
                params=("title",)),
            "API-create-page": api("createPage", "POST", "/api/books/{bookTitle}/pages",
                params=("bookTitle",), body=("title", "content"), required_body=("title",)),
        }}


def trigger(*calls):
    """Wrap context.api calls in a complete trigger handler."""
    return ("import type { UiTriggerContext } from \"../ui-runtime\";\n\n"
        "export async function handle(context: UiTriggerContext): Promise<void> {\n"
        + "\n".join(calls) + "\n}\n")


GREEN = {"returncode": 0, "output": "passed", "passed": 1, "failed": 0, "skipped": 0,
    "flaky": 0, "errors": [], "failures": []}

#: A green black-box acceptance layer. These tests drive the trigger contract rules, not the
#: journey derivation, and a derived journey would start a real browser; the derivation itself
#: is covered by AcceptanceGate below.
ACCEPTANCE_GREEN = {"status": "GREEN", "specs": ["frontend/tests/acceptance/books.spec.ts"],
    "tests": {"passed": 3, "failed": 0, "skipped": 0, "flaky": 0},
    "problems": [], "derivation_problems": [], "unverified": [], "repair_turns": 0,
    "audit": {"status": "GREEN", "problems": [], "files": {}, "output": "acceptance passed"}}

# The canonical implementation the requirement asks for.
CANONICAL_TRIGGER = trigger(
    "  const response = await context.api(\"API-get-book-details\",",
    "    { params: { title: String(context.data[\"book.title\"]) } });",
    "  const detail = response.body as { title?: string } | null;",
    "  context.update({ \"book.detail\": detail?.title ?? \"\" });")

# The shortcut: a private query name standing in for the detail read.
HACK_TRIGGER = trigger(
    "  const response = await context.api(\"API-list-books\",",
    "    { query: { detailTitle: String(context.data[\"book.title\"]) } });",
    "  context.update({ \"book.detail\": response.status === 200 ? \"loaded\" : \"\" });")


def problems(source, declared=None, backend=None):
    """Collect the contract problems of one trigger source."""
    return contracts.contract_problems(backend or book_backend(),
        [("frontend/src/triggers/t.ts", source, declared)])


def fields(found):
    """Return the reported field names so a test asserts on rules, not on prose."""
    return {item["field"] for item in found}


class ContractRules(unittest.TestCase):
    """A request is accepted only when the accepted API model declares every field."""

    def test_a_canonical_detail_read_with_path_parameters_is_accepted(self):
        """Case A: the designed detail API with its route parameter is legal."""
        source = trigger(
            "  const response = await context.api(\"API-get-book-details\",",
            "    { params: { title: String(context.params.bookTitle) } });",
            "  if (response.status === 200) context.update({ \"book.detail\": response.body });")
        self.assertEqual(problems(source, {"API-get-book-details"}), [])
        report = contracts.contract_report(book_backend(),
            [("frontend/src/triggers/TR-OPEN.ts", source, {"API-get-book-details"})])
        self.assertEqual(report["returncode"], 0)
        self.assertEqual(report["calls"], 1)

    def test_a_hidden_query_parameter_cannot_stand_in_for_a_detail_read(self):
        """Case B: GET /api/books?detailTitle= is rejected, not silently accepted."""
        source = trigger(
            "  const response = await context.api(\"API-list-books\",",
            "    { query: { detailTitle: String(context.data[\"book.title\"]) } });")
        found = problems(source, {"API-get-book-details"})
        self.assertEqual(fields(found), {"api", "query.detailTitle"})
        message = str(contracts.TriggerContractError(found))
        self.assertIn("detailTitle", message)
        self.assertIn("GET /api/books", message)
        self.assertIn("API-get-book-details", message)
        self.assertIn("declares no query field", message)

    def test_a_delete_cannot_be_expressed_as_a_query_flag_on_the_list_api(self):
        """POST /api/books?deleteTitle= is not the designed delete operation."""
        source = trigger(
            "  await context.api(\"API-list-books\",",
            "    { query: { deleteTitle: String(context.data[\"book.title\"]) } });")
        self.assertEqual(fields(problems(source, {"API-delete-book"})),
            {"api", "query.deleteTitle"})

    def test_creating_a_child_through_its_parent_collection_is_rejected(self):
        """POST /api/books?createPageFor= is neither the page API nor a declared field."""
        source = trigger(
            "  await context.api(\"API-create-book\",",
            "    { query: { createPageFor: String(context.data[\"book.title\"]) } });")
        found = fields(problems(source, {"API-create-page"}))
        self.assertTrue({"api", "query.createPageFor", "body.title", "body.author"} <= found, found)

    def test_post_body_fields_must_be_declared_and_required_ones_are_checked(self):
        """A body is compared with the declared properties and required list."""
        undeclared = trigger(
            "  await context.api(\"API-create-book\",",
            "    { body: { title: \"T\", author: \"A\", pages: 3 } });")
        self.assertEqual(fields(problems(undeclared, {"API-create-book"})), {"body.pages"})
        partial = trigger("  await context.api(\"API-create-book\", { body: { title: \"T\" } });")
        self.assertEqual(fields(problems(partial, {"API-create-book"})), {"body.author"})

    def test_declared_optional_body_fields_are_accepted(self):
        """Optional fields of the same API stay legal; only undeclared names fail."""
        source = trigger("  await context.api(\"API-create-book\",",
            "    { body: { title: \"T\", author: \"A\", summary: \"S\" } });")
        self.assertEqual(problems(source, {"API-create-book"}), [])

    def test_a_read_cannot_carry_a_request_body(self):
        """The compiled client sends no body for GET, so a body field is a defect."""
        source = trigger("  await context.api(\"API-get-book-details\",",
            "    { params: { title: \"T\" }, body: { title: \"T\" } });")
        found = problems(source, {"API-get-book-details"})
        self.assertEqual(fields(found), {"body"})
        self.assertIn("cannot carry request.body", found[0]["reason"])

    def test_route_parameters_are_required_and_limited_to_the_api_path(self):
        """A missing or invented route parameter cannot produce a routable URL."""
        missing = trigger("  await context.api(\"API-delete-book\");")
        self.assertEqual(fields(problems(missing, {"API-delete-book"})), {"params.title"})
        invented = trigger("  await context.api(\"API-delete-book\",",
            "    { params: { title: \"T\", id: 7 } });")
        self.assertEqual(fields(problems(invented, {"API-delete-book"})), {"params.id"})
        legal = trigger("  await context.api(\"API-delete-book\", { params: { title: \"T\" } });")
        self.assertEqual(problems(legal, {"API-delete-book"}), [])

    def test_declared_optional_query_fields_are_accepted_and_required_ones_are_checked(self):
        """Optional query names are legal; omitting a required one is not."""
        legal = trigger("  await context.api(\"API-search-books\",",
            "    { query: { query: \"react\", limit: 5 } });")
        self.assertEqual(problems(legal, {"API-search-books"}), [])
        partial = trigger("  await context.api(\"API-search-books\", { query: { limit: 5 } });")
        self.assertEqual(fields(problems(partial, {"API-search-books"})), {"query.query"})

    def test_an_unrelated_api_is_rejected_even_with_an_empty_request(self):
        """Replacing the designed operation with another API is not reuse."""
        source = trigger("  await context.api(\"API-list-books\", {});")
        self.assertEqual(fields(problems(source, {"API-get-book-details"})), {"api"})

    def test_reusing_an_api_that_another_requirement_also_uses_is_accepted(self):
        """Legitimate reuse must not be blocked by the operation-substitution rule."""
        source = trigger("  await context.api(\"API-list-books\");")
        report = contracts.contract_report(book_backend(), [
            ("frontend/src/triggers/TR-A.ts", source, {"API-list-books", "API-get-book-details"}),
            ("frontend/src/triggers/TR-B.ts", source, {"API-list-books"})])
        self.assertEqual(report["returncode"], 0)
        self.assertEqual(report["calls"], 2)

    def test_top_level_request_keys_and_raw_network_calls_are_rejected(self):
        """A private top-level key and a hand-written fetch both bypass the contract."""
        source = trigger("  await context.api(\"API-list-books\", { detailTitle: \"T\" });",
            "  await fetch(\"/api/books?detailTitle=\" + encodeURIComponent(\"T\"));")
        self.assertEqual(fields(problems(source, {"API-list-books"})), {"detailTitle", "network"})

    def test_headers_are_limited_to_the_declared_contract(self):
        """The host owns transport headers; a private header cannot carry a parameter."""
        private = trigger("  await context.api(\"API-create-book\",",
            "    { headers: { \"x-detail-title\": \"T\" }, body: { title: \"T\", author: \"A\" } });")
        self.assertEqual(fields(problems(private, {"API-create-book"})), {"headers.x-detail-title"})
        host = trigger("  await context.api(\"API-create-book\",",
            "    { headers: { \"content-type\": \"application/json\" },",
            "      body: { title: \"T\", author: \"A\" } });")
        self.assertEqual(problems(host, {"API-create-book"}), [])

    def test_a_computed_api_id_or_an_unknown_api_id_is_rejected(self):
        """An indirection would make the request unauditable, so the ID must be literal."""
        computed = trigger("  await context.api(`API-${context.data[\"op\"]}`, {});")
        self.assertEqual(fields(problems(computed, {"API-list-books"})), {"api_id"})
        invented = trigger("  await context.api(\"API-book-detail-by-title\",",
            "    { query: { detailTitle: \"T\" } });")
        found = problems(invented, None)
        self.assertEqual(fields(found), {"api_id"})
        self.assertIn("not part of the accepted backend contract", found[0]["reason"])

    def test_a_request_built_elsewhere_is_left_to_the_run_time_check(self):
        """A dynamic payload cannot be read statically; the client still rejects it."""
        source = trigger("  const request = { body: { title: \"T\", author: \"A\" } };",
            "  await context.api(\"API-create-book\", request);")
        self.assertEqual(problems(source, {"API-create-book"}), [])

    def test_the_contract_digest_lists_the_exact_request_fields(self):
        """The implementation context names every allowed field instead of guessing."""
        digest = contracts.contract_digest(book_backend()["apis"])
        self.assertEqual(digest["API-get-book-details"]["params"], ["title"])
        self.assertEqual(digest["API-list-books"]["query"], [])
        self.assertEqual(digest["API-create-book"]["required_body"], ["author", "title"])
        self.assertEqual(digest["API-search-books"]["required_query"], ["query"])
        self.assertIn("no hidden parameters", digest["API-search-books"]["note"])


class RouteDiagnosis(unittest.TestCase):
    """An `Unknown API route` answer must expose the route, never trigger an API switch."""

    def failure(self, aid="API-list-books", method="GET", path="/api/books/details"):
        return (f'E2E_API_ROUTE_ERROR: context.api("{aid}") sent {method} {path} and the backend '
            f"answered 404 Unknown API route")

    def test_a_reported_route_failure_names_the_api_and_the_registered_route(self):
        found = contracts.route_problems(book_backend(), [self.failure()])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["api"], "API-list-books")
        self.assertEqual(found[0]["field"], "route")
        self.assertIn("GET /api/books/details", found[0]["reason"])
        self.assertIn("GET /api/books", found[0]["reason"])
        self.assertIn("API-get-book-details", found[0]["reason"])
        self.assertIn("register.ts", found[0]["fix"])
        self.assertIn("instead of calling a different business API", found[0]["fix"])

    def test_a_route_failure_without_a_reported_request_still_points_at_the_registration(self):
        found = contracts.route_problems(book_backend(), ['expect(received).toBe(expected)\n{"error":"Unknown API route"}'])
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0]["api"])
        self.assertIn("register.ts", found[0]["fix"])
        self.assertIn("do not replace the call with a different API", found[0]["fix"])

    def test_an_unrelated_failure_is_not_a_route_problem(self):
        self.assertEqual(contracts.route_problems(book_backend(), ["expect(received).toBe(expected)"]), [])
        self.assertEqual(contracts.route_problems(book_backend(), []), [])


CLIENT_HARNESS = '''
import { readFileSync } from "node:fs";

globalThis.window = { location: { origin: "http://localhost:3000" } };
const calls = [];
let answer = { status: 200, body: { ok: true } };
globalThis.fetch = async (url, init) => {
  calls.push({ url: String(url), method: init.method, body: init.body === undefined ? null : init.body });
  return { status: answer.status, json: async () => answer.body, headers: new Headers({ "content-type": "application/json" }) };
};
const client = await import(new URL("./ui-api.ts", import.meta.url).href);
const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const results = [];
for (const item of cases) {
  const start = calls.length;
  answer = item.response ?? { status: 200, body: { ok: true } };
  let entry;
  try {
    entry = { ok: true, value: await client.callApi(item.call, item.request ?? {}) };
  } catch (error) {
    entry = { ok: false, error: String(error && error.message ? error.message : error) };
  }
  entry.requests = calls.slice(start);
  results.push(entry);
}
console.log(JSON.stringify({ results, violations: globalThis.window.__ARC_API_VIOLATIONS__ ?? [] }));
'''


class EmittedClient(unittest.TestCase):
    """Execute the emitted TypeScript client so a rejected request is observed, not assumed."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def run_client(self, cases):
        (self.directory / "ui-api.ts").write_text(api_client_source(book_backend()), encoding="utf-8")
        (self.directory / "harness.mjs").write_text(CLIENT_HARNESS, encoding="utf-8")
        (self.directory / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to execute the emitted API client")
        arguments = [str(self.directory / "harness.mjs"), str(self.directory / "cases.json")]
        result = subprocess.run([node, "--experimental-strip-types", *arguments], cwd=self.directory,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        if result.returncode and "bad option" in result.stderr:
            result = subprocess.run([node, *arguments], cwd=self.directory,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_emitted_client_rejects_the_hidden_detail_query(self):
        """The same shortcut cannot reach the backend even when it is built dynamically."""
        output = self.run_client([{"call": "API-list-books",
            "request": {"query": {"detailTitle": "My Book"}}}])
        result = output["results"][0]
        self.assertFalse(result["ok"])
        self.assertIn("E2E_API_CONTRACT_ERROR", result["error"])
        self.assertIn("detailTitle", result["error"])
        self.assertIn("GET /api/books", result["error"])
        self.assertEqual(result["requests"], [])
        self.assertEqual(len(output["violations"]), 1)
        self.assertIn("detailTitle", output["violations"][0])

    def test_the_emitted_client_sends_the_canonical_detail_read(self):
        """Case A on the wire: path parameter, no query string, no violation."""
        output = self.run_client([{"call": "API-get-book-details",
            "request": {"params": {"title": "My Book"}}}])
        result = output["results"][0]
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["requests"]), 1)
        url = urlparse(result["requests"][0]["url"])
        self.assertEqual(url.path, "/api/books/My%20Book")
        self.assertEqual(url.query, "")
        self.assertEqual(result["requests"][0]["method"], "GET")
        self.assertEqual(output["violations"], [])

    def test_the_emitted_client_rejects_an_undeclared_or_missing_body_field(self):
        """A write request is compared with the declared body before it is sent."""
        output = self.run_client([
            {"call": "API-create-book", "request": {"body": {"title": "T", "author": "A", "pages": 3}}},
            {"call": "API-create-book", "request": {"body": {"title": "T"}}}])
        undeclared, missing = output["results"]
        self.assertFalse(undeclared["ok"])
        self.assertIn("request.body.pages", undeclared["error"])
        self.assertEqual(undeclared["requests"], [])
        self.assertFalse(missing["ok"])
        self.assertIn("body field author is required", missing["error"])
        self.assertEqual(missing["requests"], [])

    def test_the_emitted_client_rejects_a_body_on_a_read_and_a_missing_parameter(self):
        """A read carries no body, and its route parameter is mandatory."""
        output = self.run_client([
            {"call": "API-get-book-details", "request": {"params": {"title": "T"}, "body": {"title": "T"}}},
            {"call": "API-delete-book", "request": {}}])
        with_body, no_parameter = output["results"]
        self.assertFalse(with_body["ok"])
        self.assertIn("cannot carry request.body", with_body["error"])
        self.assertFalse(no_parameter["ok"])
        self.assertIn("route parameter title", no_parameter["error"])
        self.assertEqual(with_body["requests"] + no_parameter["requests"], [])

    def test_the_emitted_client_accepts_declared_optional_query_fields(self):
        """Legal optional parameters keep working, so validation is not a blanket ban."""
        output = self.run_client([{"call": "API-search-books",
            "request": {"query": {"query": "react", "limit": 5}}}])
        result = output["results"][0]
        self.assertTrue(result["ok"], result)
        self.assertEqual(parse_qs(urlparse(result["requests"][0]["url"]).query),
            {"query": ["react"], "limit": ["5"]})
        self.assertEqual(output["violations"], [])

    def test_the_emitted_client_reports_an_unknown_route_with_the_requested_path(self):
        """A 404 fallback is recorded with the exact request so the route is diagnosable."""
        output = self.run_client([{"call": "API-get-book-details",
            "request": {"params": {"title": "Missing"}},
            "response": {"status": 404, "body": {"error": "Unknown API route"}}}])
        result = output["results"][0]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"]["status"], 404)
        self.assertEqual(len(output["violations"]), 1)
        self.assertIn("E2E_API_ROUTE_ERROR", output["violations"][0])
        self.assertIn("GET /api/books/Missing", output["violations"][0])
        found = contracts.route_problems(book_backend(), output["violations"])
        self.assertEqual(found[0]["api"], "API-get-book-details")
        self.assertIn("GET /api/books/{title}", found[0]["reason"])


def node(kind, parent, data=(), mode=(), **props):
    return {"type": kind, "parent": parent, "data": list(data), "mode": list(mode), "prop": props}


def binding(dtype, initial, source):
    return {"dtype": dtype, "constraints": [], "initial": initial, "source": source}


def stage_fixture():
    """One requirement whose single trigger opens a book through the detail API."""
    raw = {"id": "ROOT", "name": "Books", "children": [{"id": "REQ-BOOKS", "name": "REQ-BOOKS",
        "description": "Open one book by its title from the library page.",
        "scenarios": [{"id": "SCN-BOOKS", "steps": [{"id": "STEP-BOOKS",
            "Given": "A book exists", "When": "Click the book in the library",
            "Then": "Its detail is shown"}]}]}]}
    ui = {"pages": {"library": {"name": "Library", "route": "/", "root": "UI-LIBRARY",
            "images": [], "fixtures": {"default": {}}}},
        "elements": {"UI-LIBRARY": node("page", None),
            "UI-TITLE": node("input", "UI-LIBRARY", ["book.title"], ["user_write"], label="Title",
                input_type="text"),
            "UI-OPEN": node("button", "UI-LIBRARY", text="Open"),
            "UI-DETAIL": node("text", "UI-LIBRARY", ["book.detail"])},
        "bindings": {"book.title": binding("string", "", "Native title input"),
            "book.detail": binding("string", "", "Detail API response")},
        "requirements": {"REQ-BOOKS": ["UI-TITLE", "UI-OPEN"]}}
    design = {"triggers": {"TR-OPEN": {"source": "UI-OPEN", "event": "click", "guard": None,
            "read": ["book.title"], "write": ["book.detail"],
            "action": [{"kind": "api", "api": "API-get-book-details",
                    "description": "GET the book named by book.title"},
                {"kind": "ui", "ui": "UI-LIBRARY",
                    "description": "Show the returned detail"}]}},
        "requirements": {"REQ-BOOKS": ["TR-OPEN"]}, "steps": {"STEP-BOOKS": ["TR-OPEN"]}}
    return raw, ui, design, book_backend()


class StageAcceptance(unittest.TestCase):
    """Step 12 must reject a passing implementation that contradicts the contract."""

    def setUp(self):
        from infrastructure.shared.project.initialize import initialize_project
        from infrastructure.stages.requirements.tree import RequirementTree
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.arc = self.output / ".arc"
        self.raw, self.ui, self.design, self.backend = stage_fixture()
        self.tree = RequirementTree(self.raw)
        initialize_project(self.output)

    def run_stage(self, generate, tests):
        """Run the real orchestration with fake browser reports and model edits."""
        from infrastructure.stages.frontend.triggers import implementation
        checks, api_checks = Mock(), Mock()
        checks.typecheck.return_value = {"returncode": 0, "output": "compiled"}
        checks.build.return_value = {"returncode": 0, "output": "built"}
        checks.tests.side_effect = tests
        api_checks.typecheck.return_value = {"returncode": 0, "output": "compiled backend"}
        api_checks.tests.return_value = {"returncode": 0, "tests": 1, "pass": 1, "fail": 0,
            "output": "API regression passed"}
        with patch.object(implementation, "install_dependencies"), \
                patch.object(implementation, "run_acceptance",
                    return_value=deepcopy(ACCEPTANCE_GREEN)), \
                patch.object(implementation, "E2EChecks", return_value=checks), \
                patch.object(implementation, "BackendChecks", return_value=api_checks):
            report = implementation.run_trigger_tdd(self.arc, self.tree, {"entities": {}},
                self.backend, self.ui, self.design, {},
                {rid: {"id": rid} for rid in self.tree.leaves},
                Mock(generate_json=generate), Mock(), {"requirements": {}}, max_turns=2)
        return report, checks

    def generate(self, calls, bodies):
        """Return a model that writes bodies in order and freezes one E2E spec."""
        def generate(instructions, payload):
            task = payload["task"]
            if "new_file" not in task:
                calls.append(deepcopy(payload))
                body = bodies[min(len(calls), len(bodies)) - 1]
                return {"edits": [{"path": task["implementation_paths"]["TR-OPEN"],
                    "content": body}]}
            creation = task["new_file"]
            return {"edits": [{"path": f"{creation['directory']}/REQ-BOOKS.spec.ts",
                "content": "// frozen scenario"}]}
        return generate

    def test_a_passing_suite_cannot_accept_a_contract_violation(self):
        """The hidden-query shortcut is rejected before acceptance and repaired."""
        from infrastructure.shared.project.entrypoints import selected_path
        calls = []
        report, _ = self.run_stage(self.generate(calls, [HACK_TRIGGER, CANONICAL_TRIGGER]),
            lambda files: dict(GREEN))
        self.assertEqual(len(calls), 2)
        feedback = json.dumps(calls[1]["feedback"], ensure_ascii=False)
        self.assertIn("detailTitle", feedback)
        self.assertIn("API-get-book-details", feedback)
        self.assertEqual(calls[1]["feedback"][0]["checks"]["contract"]["returncode"], 1)
        entry = report["requirements"]["REQ-BOOKS"]
        self.assertEqual(entry["implementation_turns"], 2)
        self.assertEqual(entry["status"], "GREEN")
        self.assertEqual(entry["api_contract"], "GREEN")
        relative = selected_path(self.output, "trigger", "TR-OPEN")
        self.assertIn("API-get-book-details", (self.output / relative).read_text(encoding="utf-8"))

    def test_an_unknown_route_failure_reaches_the_repair_loop_with_its_route(self):
        """A 404 fallback is diagnosed as a routing defect, never as a reason to switch APIs."""
        calls = []
        routed = {**GREEN, "returncode": 1, "passed": 0, "failed": 1, "failures": [
            'E2E_API_ROUTE_ERROR: context.api("API-get-book-details") sent GET /api/books/Missing '
            "and the backend answered 404 Unknown API route"]}
        results = [routed, routed]

        def tests(files):
            return results.pop(0) if results else dict(GREEN)
        report, _ = self.run_stage(self.generate(calls, [CANONICAL_TRIGGER, CANONICAL_TRIGGER]), tests)
        self.assertEqual(len(calls), 2)
        feedback = json.dumps(calls[1]["feedback"], ensure_ascii=False)
        self.assertIn("Unknown API route", feedback)
        self.assertIn("register.ts", feedback)
        self.assertIn("instead of calling a different business API", feedback)
        self.assertEqual(calls[1]["feedback"][0]["checks"]["routes"]["returncode"], 1)
        self.assertEqual(report["requirements"]["REQ-BOOKS"]["implementation_turns"], 2)
        self.assertEqual(report["requirements"]["REQ-BOOKS"]["status"], "GREEN")
        self.assertEqual(report["verification"]["api_contract"]["status"], "GREEN")
        self.assertEqual(report["verification"]["acceptance"]["status"], "GREEN")


class AcceptanceGate(unittest.TestCase):
    """A journey that cannot be derived from the accepted IR is never a green layer.

    The staged fixture publishes a create API no designed control submits. An earlier
    BookStack run accepted such an application: the layer recorded the gap as a problem and
    the stage still reported GREEN, so the whole business flow no test drove was delivered.
    """

    def setUp(self):
        from infrastructure.shared.project.initialize import initialize_project
        from infrastructure.stages.requirements.tree import RequirementTree
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.arc = self.output / ".arc"
        self.raw, self.ui, self.design, self.backend = stage_fixture()
        self.tree = RequirementTree(self.raw)
        initialize_project(self.output)

    def acceptance(self):
        """Derive the journeys exactly as step 12 does, without starting a browser."""
        from infrastructure.stages.frontend.acceptance.pipeline import run_acceptance
        return run_acceptance(self.arc, self.tree, {}, self.backend, self.ui, self.design)

    def test_a_create_api_no_control_submits_stays_unverified(self):
        report = self.acceptance()
        self.assertEqual(report["status"], "UNVERIFIED")
        self.assertEqual(report["journeys"], [])
        self.assertIn("no designed control submits the create API API-create-book",
            " ".join(report["problems"]))
        self.assertEqual(report["specs"], [])

    def test_an_unverifiable_layer_fails_its_final_check_and_blocks_the_stage(self):
        from infrastructure.stages.frontend.triggers import implementation
        report = self.acceptance()
        check = implementation.acceptance_check(report)
        self.assertEqual(check["returncode"], 1)
        self.assertEqual(check["status"], "UNVERIFIED")
        self.assertIn("API-create-book", check["output"])
        self.assertTrue(check["problems"])
        with self.assertRaises(implementation.TriggerCheckFailure) as caught:
            implementation.require_acceptance(report)
        self.assertEqual(caught.exception.upstream["stage"], "09_design_ui")
        self.assertIn("API-create-book",
            " ".join(caught.exception.upstream["problems"]))

    def test_a_green_layer_passes_its_final_check_and_does_not_block(self):
        from infrastructure.stages.frontend.triggers import implementation
        report = deepcopy(ACCEPTANCE_GREEN)
        check = implementation.acceptance_check(report)
        self.assertEqual(check["returncode"], 0)
        self.assertEqual(check["status"], "GREEN")
        self.assertEqual(check["problems"], [])
        self.assertIsNone(implementation.require_acceptance(report))

    def test_a_failed_journey_is_repairable_and_does_not_stop_the_stage(self):
        """Only a layer that verified nothing raises; a journey that ran and failed does not."""
        from infrastructure.stages.frontend.triggers import implementation
        report = {**deepcopy(ACCEPTANCE_GREEN), "status": "FAILED",
            "tests": {"passed": 2, "failed": 1}, "error": "the create button submitted nothing"}
        self.assertEqual(implementation.acceptance_check(report)["returncode"], 1)
        self.assertIsNone(implementation.require_acceptance(report))


class FinalRunReconciliation(unittest.TestCase):
    """The last full run decides: an earlier per-requirement GREEN cannot outlive it.

    Suites are implemented one requirement at a time, so a suite can pass alone and fail once
    its neighbours exist. Leaving the earlier GREEN in place is the false-green this class
    prevents: the delivered application would carry a requirement the final run proved broken.
    """

    def setUp(self):
        from infrastructure.stages.requirements.tree import RequirementTree
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.tree = RequirementTree({"id": "ROOT", "name": "App", "description": "An application.",
            "scenarios": [], "children": [
                {"id": "REQ-A", "name": "First", "description": "First.", "scenarios": []},
                {"id": "REQ-B", "name": "Second", "description": "Second.", "scenarios": []},
                {"id": "REQ-C", "name": "Third", "description": "Third.", "scenarios": []}]})

    def report(self, *statuses):
        return {"requirements": {rid: {"status": status}
            for rid, status in zip(self.tree.leaves, statuses)}}

    def test_a_failed_spec_downgrades_and_a_missing_spec_becomes_unverified(self):
        from infrastructure.stages.frontend.triggers import implementation
        from infrastructure.stages.frontend.triggers.testing import test_path
        first, third = test_path("REQ-A", self.output), test_path("REQ-C", self.output)
        report = self.report("GREEN", "GREEN", "GREEN")
        result = {"files": {first: {}, third: {}}, "files_failed": {first: 2}}
        changed = implementation.reconcile_requirements(report, self.tree, self.output, result)
        self.assertEqual(changed, ["REQ-A", "REQ-B"])
        self.assertEqual(report["requirements"]["REQ-A"]["status"], "FAILED")
        self.assertIn("failed 2 case(s)", report["requirements"]["REQ-A"]["error"])
        self.assertEqual(report["requirements"]["REQ-B"]["status"], "UNVERIFIED")
        self.assertEqual(report["requirements"]["REQ-C"]["status"], "GREEN")

    def test_a_report_without_specs_is_left_for_the_run_that_produced_it(self):
        from infrastructure.stages.frontend.triggers import implementation
        report = self.report("GREEN", "UNVERIFIED", "FAILED")
        self.assertEqual(implementation.reconcile_requirements(report, self.tree, self.output, {}),
            [])
        self.assertEqual([report["requirements"][rid]["status"] for rid in self.tree.leaves],
            ["GREEN", "UNVERIFIED", "FAILED"])
