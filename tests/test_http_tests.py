"""HTTP-level integration suites derived from the API IR: probes, flows and emitters."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import Mock, patch

from infrastructure.stages.backend.http_runtime import http_support_source
from infrastructure.stages.backend.http_tests import (FLOW_PATH, SUPPORT_PATH, body_example, flow_plan,
    flow_suite_source, flow_verdict, normalize_name, parameters, plan_flows, prepare_http_tests, probe_cases,
    probe_suite_source, resolve, sentinel, singular, suite_path, synthesize, unverified_flows, value_names)


def object_schema(properties=None, required=None):
    """Build the inline object schema the API IR uses for one request or response part."""
    return {"type": "object", "properties": properties or {}, "required": required or []}


def api(name, method, path, *, params=(), body=None, read=(), write=(), success=200,
        properties=None, errors=()):
    """Build one complete API IR member, including every part contract validation requires."""
    return {
        "description": "Perform the business operation",
        "read": list(read),
        "write": list(write),
        "signature": {"name": name, "method": method, "path": path},
        "request": {"params": object_schema({key: {"type": "string"} for key in params}),
            "query": object_schema(), "body": body or object_schema(), "headers": object_schema()},
        "response": {
            "success": {"status": success, "body": object_schema(properties or {"ok": {"type": "boolean"}}),
                "headers": object_schema()},
            "errors": [{"status": status, "body": object_schema({"error": {"type": "string"}}),
                "headers": object_schema()} for status in errors],
        },
        "precondition": [],
        "postcondition": [{"id": f"Q-{name}", "description": "Perform the operation", "status": success}],
    }


BOOK_PROPERTIES = {"id": {"type": "string"}, "title": {"type": "string"}}
PAGE_PROPERTIES = {"id": {"type": "string"}, "title": {"type": "string"}}


def bookstack_design():
    """Build the BookStack-style design the acceptance scope names."""
    apis = {
        "API-list-books": api("listBooks", "GET", "/api/books", read=("Book.id",),
            properties={"books": {"type": "array", "items": {"type": "object"}}}),
        "API-create-book": api("createBook", "POST", "/api/books", success=201,
            body=object_schema({"title": {"type": "string"}, "author": {"type": "string"}}, ["title"]),
            write=("Book.id", "Book.title"), properties=BOOK_PROPERTIES, errors=(400, 409)),
        "API-read-book": api("readBook", "GET", "/api/books/{title}", params=("title",),
            read=("Book.title",), properties=BOOK_PROPERTIES, errors=(404,)),
        "API-update-book-metadata": api("updateBookMetadata", "PATCH", "/api/books/:title/metadata",
            params=("title",), write=("Book.metadata",),
            body=object_schema({"metadata": object_schema({"author": {"type": "string"}}, ["author"])}, ["metadata"]),
            properties=BOOK_PROPERTIES, errors=(404, 422)),
        "API-delete-book": api("deleteBook", "DELETE", "/api/books/{title}", params=("title",),
            write=("Book.id",), properties={"deleted": {"type": "boolean"}}, errors=(404,)),
        "API-create-page": api("createPage", "POST", "/api/books/{bookTitle}/pages", params=("bookTitle",),
            success=201, body=object_schema({"title": {"type": "string"}}, ["title"]),
            write=("Page.id",), properties=PAGE_PROPERTIES, errors=(400, 404)),
        "API-list-pages": api("listPages", "GET", "/api/books/{bookTitle}/pages", params=("bookTitle",),
            read=("Page.id",), properties={"pages": {"type": "array", "items": {"type": "object"}}},
            errors=(404,)),
        "API-read-page": api("readPage", "GET", "/api/books/{bookTitle}/pages/{pageTitle}",
            params=("bookTitle", "pageTitle"), read=("Page.id",), properties=PAGE_PROPERTIES, errors=(404,)),
    }
    requirements = {"ROOT": [], "REQ-BOOKS": ["API-list-books", "API-create-book", "API-read-book",
        "API-update-book-metadata", "API-delete-book"],
        "REQ-PAGES": ["API-create-page", "API-list-pages", "API-read-page"]}
    return {"apis": apis, "requirements": requirements}


def kinds(flow):
    """Return the ordered step kinds of one derived flow."""
    return [step["kind"] for step in flow["steps"]]


def sign_in_api():
    """Build a sign-in contract whose precondition needs an account that already exists."""
    member = api("loginAccount", "POST", "/api/login",
        body=object_schema({"username": {"type": "string"}, "password": {"type": "string"}},
            ["username", "password"]), write=("AuthSession.id",),
        properties={"username": {"type": "string"}}, errors=(401,))
    member["precondition"] = [{"id": "P-1", "description": "The account already exists",
        "error_status": 401}]
    return member


def registration_design():
    """Build a registration plus the sign-in that must reuse the account its flow creates."""
    register = api("registerAccount", "POST", "/api/register", success=201,
        body=object_schema({"username": {"type": "string"}, "name": {"type": "string"},
            "password": {"type": "string"}}, ["username", "name", "password"]),
        write=("Account.id", "Account.username", "Account.name"),
        properties={"username": {"type": "string"}}, errors=(400, 409))
    return {"apis": {"API-register": register, "API-login": sign_in_api()},
        "requirements": {"REQ-1": ["API-register"], "REQ-2": ["API-login"]}}


class HarnessTests(unittest.TestCase):
    def test_harness_boots_the_real_app_on_an_isolated_database(self):
        source = http_support_source()
        self.assertIn('import { createApp } from "../../src/app.js";', source)
        self.assertIn('createDatabaseClient(":memory:")', source)
        self.assertIn("createApp(connection).listen(0", source)
        self.assertIn("context.after(async () => { await server.close(); });", source)
        self.assertNotIn("data/app.db", source)

    def test_harness_observes_database_side_effects(self):
        source = http_support_source()
        for fragment in ("FROM sqlite_master", "snapshot(): Snapshot", "changedTables(", "addedRows(",
                "rejectionWithoutWrite", "unknown api route"):
            self.assertIn(fragment, source)

    def test_generated_suites_never_call_handlers_directly(self):
        design = bookstack_design()
        for aid in design["apis"]:
            source = probe_suite_source(design, aid)
            self.assertNotIn("support/apis", source)
            self.assertNotIn("await handle", source)
            self.assertIn("contractTests(cases);", source)

    def test_the_flow_harness_never_greens_an_unexercised_step(self):
        source = http_support_source()
        # A documented rejection on a required step stops the journey and keeps it UNVERIFIED.
        self.assertIn("unverified.push(", source)
        self.assertIn('" was not verified: " + unverified.join("; ")', source)
        self.assertIn("assert.ok(verified > 0", source)
        # A create must return the value it was given, not merely persist some row.
        self.assertIn("response.text.toLowerCase().includes(step.echo.toLowerCase())", source)

    def test_the_flow_harness_submits_borrowed_values_in_the_request_body(self):
        source = http_support_source()
        # A placeholder is resolved in the path AND in the body; only the path is URL-encoded.
        self.assertIn("function renderBody(", source)
        self.assertIn("fill(value, false)", source)
        self.assertIn("fill(path, true)", source)
        self.assertIn("{ body }", source)
        # What a step submitted is published for later steps, even when it is never persisted.
        self.assertIn("const submitted = (body ?? {}) as Record<string, unknown>;", source)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.design = bookstack_design()

    def cases(self, aid):
        """Return the probes emitted for one API."""
        return probe_cases(self.design, aid)

    def test_every_api_gets_a_real_route_probe(self):
        self.assertEqual(self.cases("API-read-book")[0]["path"], "/api/books/arc-http-probe-1")
        self.assertEqual(self.cases("API-read-page")[0]["path"],
            "/api/books/arc-http-probe-1/pages/arc-http-probe-2")
        self.assertEqual(self.cases("API-read-book")[0]["expect"], "registered")

    def test_braced_and_colon_parameters_are_requested_as_values(self):
        design = {"apis": {"A": api("read", "GET", "/api/books/{title}"),
            "B": api("read2", "GET", "/api/books/:title")}, "requirements": {"REQ": ["A", "B"]}}
        for aid in design["apis"]:
            with self.subTest(aid=aid):
                self.assertEqual(probe_cases(design, aid)[0]["path"], "/api/books/arc-http-probe-1")
                self.assertIn("GET /api/books/:title", probe_cases(design, aid)[0]["title"])

    def test_method_isolation_probe_rejects_undeclared_methods(self):
        method_probe = next(case for case in self.cases("API-read-book") if case["kind"] == "method")
        self.assertEqual(method_probe["expect"], "unknownRoute")
        self.assertNotEqual(method_probe["method"], "GET")
        self.assertEqual(method_probe["path"], "/api/books/arc-http-probe-1")

    def test_method_isolation_never_duplicates_a_declared_method(self):
        design = {"apis": {"A": api("read", "GET", "/api/books"), "B": api("create", "POST", "/api/books")},
            "requirements": {"REQ": ["A", "B"]}}
        probes = [case for case in probe_cases(design, "A") if case["kind"] == "method"]
        self.assertTrue(probes)
        self.assertNotIn(probes[0]["method"], {"GET", "POST"})

    def test_missing_identifier_probe_requires_a_documented_rejection(self):
        cases = {case["kind"]: case for case in self.cases("API-read-book")}
        self.assertEqual(cases["unknown-param"]["expect"], "documentedError")
        self.assertEqual(cases["unknown-param"]["statuses"], [404])
        self.assertEqual(cases["unknown-param"]["success"], 200)

    def test_missing_identifier_probe_is_omitted_without_a_documented_error(self):
        design = {"apis": {"A": api("read", "GET", "/api/books/{title}")}, "requirements": {"REQ": ["A"]}}
        self.assertNotIn("unknown-param", [case["kind"] for case in probe_cases(design, "A")])

    def test_rejection_probe_only_covers_writes_with_required_fields(self):
        cases = {case["kind"]: case for case in self.cases("API-create-book")}
        self.assertEqual(cases["rejection"]["expect"], "rejectionWithoutWrite")
        self.assertEqual(cases["rejection"]["body"], {})
        self.assertEqual(cases["rejection"]["statuses"], [400, 409])
        self.assertNotIn("rejection", [case["kind"] for case in self.cases("API-list-books")])

    def test_rejection_probe_is_omitted_without_required_fields(self):
        design = {"apis": {"A": api("create", "POST", "/api/books", write=("Book.id",),
            body=object_schema({"title": {"type": "string"}}), errors=(400,))}, "requirements": {"REQ": ["A"]}}
        self.assertNotIn("rejection", [case["kind"] for case in probe_cases(design, "A")])


class NamingTests(unittest.TestCase):
    def test_value_names_address_the_created_column(self):
        self.assertEqual(value_names("bookTitle", "books"), ["title", "booktitle"])
        self.assertEqual(value_names("pageTitle", "pages"), ["title", "pagetitle"])
        self.assertEqual(value_names("title", "books"), ["title"])
        self.assertEqual(value_names("id", "books"), ["id"])

    def test_plural_and_camel_case_names_normalize(self):
        self.assertEqual(singular("books"), "book")
        self.assertEqual(singular("pages"), "page")
        self.assertEqual(singular("libraries"), "library")
        self.assertEqual(normalize_name("book_title"), "booktitle")
        self.assertEqual(normalize_name("passportExpirationDate"), "passportexpirationdate")

    def test_resolve_uses_the_scope_of_the_parent_collection(self):
        scopes = {"/api/books": {"title": "/api/books::title"}}
        self.assertEqual(resolve("/api/books/:title", scopes), ("/api/books/{{/api/books::title}}", True))
        self.assertEqual(resolve("/api/books/:bookTitle/pages", scopes),
            ("/api/books/{{/api/books::title}}/pages", True))
        self.assertEqual(resolve("/api/shelves/:name", scopes), ("/api/shelves/:name", False))

    def test_parameters_are_listed_in_path_order(self):
        self.assertEqual(parameters("/api/books/:bookTitle/pages/:pageTitle"), ["bookTitle", "pageTitle"])
        self.assertEqual(parameters("/api/books"), [])


class SynthesisTests(unittest.TestCase):
    def test_scalars_and_enums_become_admitted_values(self):
        self.assertEqual(synthesize({"type": "string"}, "seed"), ("arc-http-probe-seed", True))
        self.assertEqual(synthesize({"type": "integer"}, "seed"), (4242, True))
        self.assertEqual(synthesize({"type": "boolean"}, "seed"), (True, True))
        self.assertEqual(synthesize({"type": ["string", "null"]}, "seed"), ("arc-http-probe-seed", True))
        self.assertEqual(synthesize({"type": "string", "enum": ["Male", "Female"]}, "seed"), ("Male", True))

    def test_required_members_fill_the_request_body(self):
        schema = object_schema({"title": {"type": "string"}, "author": {"type": "string"}}, ["title"])
        self.assertEqual(body_example({"request": {"body": schema}}, "create"), {"title": "arc-http-probe-create-title"})

    def test_nested_objects_and_arrays_are_derived(self):
        nested = object_schema({"metadata": object_schema({"author": {"type": "string"}}, ["author"])}, ["metadata"])
        self.assertEqual(body_example({"request": {"body": nested}}, "update"),
            {"metadata": {"author": "arc-http-probe-update-metadata-author"}})
        listed = object_schema({"tags": {"type": "array", "items": {"type": "string"}}}, ["tags"])
        self.assertEqual(body_example({"request": {"body": listed}}, "create"),
            {"tags": ["arc-http-probe-create-tags"]})

    def test_a_body_that_cannot_be_derived_is_reported(self):
        opaque = object_schema({"payload": {"type": "object"}}, ["payload"])
        self.assertIsNone(body_example({"request": {"body": opaque}}, "create"))

    def test_the_marker_survives_nesting(self):
        self.assertEqual(sentinel({"metadata": {"author": "arc-http-probe-update-author"}}),
            "arc-http-probe-update-author")
        self.assertIsNone(sentinel({"metadata": {"author": "Anna"}}))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.design = bookstack_design()
        self.flows = flow_plan(self.design)

    def flow(self, resource):
        """Return the derived flow for one collection route."""
        return next(flow for flow in self.flows if flow["resource"] == resource)

    def test_the_book_collection_has_a_complete_crud_chain(self):
        flow = self.flow("/api/books")
        item = "/api/books/{{/api/books::title}}"
        self.assertEqual(kinds(flow), ["create", "list", "read", "update", "delete", "read-empty"])
        self.assertEqual([step["path"] for step in flow["steps"]][2:],
            [item, item + "/metadata", item, item])

    def test_the_chain_asserts_statuses_bodies_and_side_effects(self):
        create, listing, read, update, delete, read_empty = self.flow("/api/books")["steps"]
        self.assertEqual((create["success"], create["statuses"]), (201, [400, 409]))
        self.assertEqual(create["body"], {"title": "arc-http-probe-create-API-create-book-title"})
        self.assertTrue(listing["contains"].startswith("arc-http-probe"))
        self.assertTrue(read["checkBody"])
        self.assertEqual(update["body"],
            {"metadata": {"author": "arc-http-probe-update-API-update-book-metadata-metadata-author"}})
        self.assertEqual(delete["contains"], create["contains"])
        self.assertEqual(read_empty["path"], read["path"])
        self.assertEqual(read_empty["success"], read["success"])

    def test_nested_resources_recreate_their_parent_first(self):
        flow = self.flow("/api/books/:bookTitle/pages")
        self.assertEqual(kinds(flow), ["create", "create", "list", "read"])
        parent, page, listing, read = flow["steps"]
        self.assertEqual(parent["path"], "/api/books")
        self.assertEqual(page["path"], "/api/books/{{/api/books::title}}/pages")
        self.assertEqual(listing["path"], page["path"])
        self.assertEqual(read["path"], "/api/books/{{/api/books::title}}/pages/{{/api/books/{{/api/books::title}}/pages::title}}")

    def test_flows_are_deterministic(self):
        self.assertEqual(json.dumps(flow_plan(bookstack_design())), json.dumps(self.flows))

    def test_a_collection_without_a_declared_delete_ends_after_its_read(self):
        design = {"apis": {"A": api("create", "POST", "/api/items",
            body=object_schema({"title": {"type": "string"}}, ["title"]), write=("Item.id",))},
            "requirements": {"REQ": ["A"]}}
        self.assertEqual(kinds(flow_plan(design)[0]), ["create"])

    def test_an_unaddressable_create_produces_no_flow(self):
        design = {"apis": {"A": api("createPage", "POST", "/api/books/{bookTitle}/pages",
            params=("bookTitle",), body=object_schema({"title": {"type": "string"}}, ["title"]),
            write=("Page.id",))}, "requirements": {"REQ": ["A"]}}
        self.assertEqual(flow_plan(design), [])


class FlowCoverageTests(unittest.TestCase):
    """An unexercised create is reported as a reason, never silently dropped from the suite."""

    def test_every_create_a_flow_exercises_is_not_reported_as_unexercised(self):
        flows, notes = plan_flows(bookstack_design())
        self.assertEqual(notes, [])
        exercised = {step["api"] for flow in flows for step in flow["steps"] if step["kind"] == "create"}
        self.assertEqual(exercised, {"API-create-book", "API-create-page"})

    def test_a_create_without_a_derivable_payload_is_reported(self):
        design = {"apis": {"A": api("create", "POST", "/api/items", write=("Item.id",),
            body=object_schema({"payload": object_schema()}, ["payload"]))}, "requirements": {"REQ": ["A"]}}
        flows, notes = plan_flows(design)
        self.assertEqual(flows, [])
        self.assertEqual([(note["api"], note["kind"]) for note in notes], [("A", "payload")])
        self.assertIn("payload", notes[0]["reason"])

    def test_a_create_whose_route_no_flow_can_address_is_reported(self):
        design = {"apis": {"A": api("createPage", "POST", "/api/books/{bookTitle}/pages",
            params=("bookTitle",), body=object_schema({"title": {"type": "string"}}, ["title"]),
            write=("Page.id",))}, "requirements": {"REQ": ["A"]}}
        flows, notes = plan_flows(design)
        self.assertEqual(flows, [])
        self.assertEqual([(note["api"], note["kind"]) for note in notes], [("A", "route")])
        self.assertIn("no earlier create", notes[0]["reason"])

    def test_a_sign_in_with_no_creating_flow_is_reported_instead_of_passing(self):
        design = {"apis": {"API-login": sign_in_api()}, "requirements": {"REQ": ["API-login"]}}
        flows, notes = plan_flows(design)
        self.assertEqual(flows, [])
        self.assertEqual([(note["api"], note["kind"]) for note in notes], [("API-login", "given")])
        self.assertIn("already exists", notes[0]["reason"])

    def test_a_sign_in_flow_borrows_the_values_the_registration_flow_submits(self):
        flows, notes = plan_flows(registration_design())
        self.assertEqual(notes, [])
        self.assertEqual([flow["resource"] for flow in flows], ["/api/register", "/api/login"])
        register, sign_in = flows
        # The flow creates the account it signs in to instead of depending on pre-existing data.
        self.assertEqual([(step["kind"], step["api"]) for step in sign_in["steps"]],
            [("create", "API-register"), ("create", "API-login")])
        self.assertEqual(sign_in["steps"][1]["body"],
            {"username": "{{/api/register::username}}", "password": "{{/api/register::password}}"})
        # A borrowed value is a placeholder, so only the path values can prove the echo.
        self.assertIsNone(sign_in["steps"][1]["echo"])
        self.assertEqual(register["steps"][0]["echo"], register["steps"][0]["body"]["username"])
        self.assertTrue(sign_in["steps"][1]["required"])

    def test_a_create_step_asserts_the_value_the_success_body_echoes(self):
        flow = next(flow for flow in flow_plan(bookstack_design()) if flow["resource"] == "/api/books")
        create, _, _, update, _, read_empty = flow["steps"]
        self.assertEqual(create["echo"], create["body"]["title"])
        self.assertIsNone(update["echo"])
        self.assertTrue(create["required"])
        self.assertFalse(read_empty["required"])

    def test_the_summary_publishes_the_unexercised_creates(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        design = {"apis": {"A": api("create", "POST", "/api/items", write=("Item.id",),
            body=object_schema({"payload": object_schema()}, ["payload"]))}, "requirements": {"REQ": ["A"]}}
        summary = prepare_http_tests(Path(temporary.name), design)
        self.assertEqual(summary["flows"]["notExercised"][0]["api"], "A")
        self.assertEqual(summary["flows"]["apis"], [])


class PrepareTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.design = bookstack_design()

    def test_prepare_publishes_the_harness_and_one_suite_per_api(self):
        summary = prepare_http_tests(self.root, self.design)
        self.assertEqual(summary["support"], SUPPORT_PATH)
        self.assertTrue((self.root / SUPPORT_PATH).is_file())
        self.assertTrue((self.root / FLOW_PATH).is_file())
        self.assertEqual(set(summary["files"]), set(self.design["apis"]))
        for aid, relative in summary["files"].items():
            self.assertTrue((self.root / relative).is_file())
            self.assertTrue(relative.endswith(f"{aid.replace('/', '-')}.http.test.ts"))
            self.assertIn("contractTests(cases);", (self.root / relative).read_text(encoding="utf-8"))

    def test_suite_paths_use_the_owning_requirement_and_stay_reproducible(self):
        summary = prepare_http_tests(self.root, self.design)
        self.assertEqual(summary["files"]["API-read-book"], "backend/tests/http/REQ-BOOKS/API-read-book.http.test.ts")
        self.assertEqual(summary["files"]["API-read-page"], "backend/tests/http/REQ-PAGES/API-read-page.http.test.ts")
        self.assertEqual(suite_path(self.design, "API-read-page"), summary["files"]["API-read-page"])
        first = (self.root / FLOW_PATH).read_text(encoding="utf-8")
        prepare_http_tests(self.root, self.design)
        self.assertEqual(first, (self.root / FLOW_PATH).read_text(encoding="utf-8"))

    def test_flow_suite_records_the_derived_resource_and_step_counts(self):
        summary = prepare_http_tests(self.root, self.design)
        self.assertEqual(summary["flows"]["resources"], ["/api/books", "/api/books/:bookTitle/pages"])
        self.assertEqual(summary["flows"]["steps"], 10)
        self.assertIn("crudFlowTests(flows);", flow_suite_source(flow_plan(self.design)))

    def test_unusual_identifiers_produce_safe_paths(self):
        design = {"apis": {"API/odd id": api("odd", "GET", "/api/odd")}, "requirements": {"REQ odd": ["API/odd id"]}}
        relative = suite_path(design, "API/odd id")
        self.assertNotIn("/odd id", relative)
        self.assertTrue(relative.startswith("backend/tests/http/id-"))


class FlowVerdictTests(unittest.TestCase):
    """A flow run reports PASS, FAIL or UNVERIFIED; a documented rejection is never a pass."""

    def entry(self, output="", returncode=0, tests=2, fail=0):
        """Build the final-check entry step 08 records for the flow suite."""
        return {"status": "GREEN" if not returncode else "FAILED",
            "result": {"returncode": returncode, "output": output, "tests": tests,
                "pass": tests - fail, "fail": fail}}

    def tap(self, name, error):
        """Render one TAP failure block the way the Node tap reporter does."""
        return "\n".join([f"not ok 1 - {name}", "  ---", "    code: 'ERR_ASSERTION'",
            f"  error: '{error}'", "  ..."])

    def test_observed_success_journeys_are_a_pass(self):
        self.assertEqual(flow_verdict(self.entry(output="ok 1 - CRUD flow /api/books")), "PASS")

    def test_a_documented_rejection_leaves_the_journey_unverified(self):
        # The reported defect: a 400/401 answer was read as success. It proves nothing, so
        # the journey is UNVERIFIED and the run still fails the gate.
        report = self.entry(self.tap("CRUD flow /api/accounts",
            "API-register answered 401 with a documented rejection, so CRUD flow /api/accounts "
            "was not verified"), returncode=1, fail=1)
        self.assertEqual(flow_verdict(report), "UNVERIFIED")
        self.assertEqual(unverified_flows(report), ["CRUD flow /api/accounts"])

    def test_a_broken_route_is_a_failure_not_an_unverified_journey(self):
        report = self.entry(self.tap("CRUD flow /api/books",
            "API-create-book never reached an Express route"), returncode=1, fail=1)
        self.assertEqual(flow_verdict(report), "FAIL")
        self.assertEqual(unverified_flows(report), [])

    def test_a_suite_that_ran_nothing_is_unverified(self):
        self.assertEqual(flow_verdict(self.entry(output="", tests=0)), "UNVERIFIED")


class StageReportTests(unittest.TestCase):
    """Step 08 must gate acceptance on real HTTP and record the results in .arc."""

    def setUp(self):
        from importlib import import_module
        from infrastructure.shared.project.entrypoints import remember_path
        from infrastructure.utils.files import save_text
        try:
            from test_backend_contracts import prepare_artifacts
        except ModuleNotFoundError:
            from tests.test_backend_contracts import prepare_artifacts
        self.stage = import_module("08_implement_backend_tdd")
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.arc, self.schema, self.design = prepare_artifacts(self.root)
        # An existing implementation for every API selects the compiler's no-agent fast path.
        for aid in self.design["apis"]:
            relative = f"backend/src/api/{aid.lower()}.ts"
            save_text(self.root / relative, "export async function handle() { return { status: 200, body: {} }; }\n")
            remember_path(self.root, "api", aid, relative)

    def checks(self, http_ok=True):
        """Build a mocked compiler whose HTTP suites pass or fail on demand.

        The RED phase replaces the test bindings with a 501 stub, so handler suites are
        reported as failing exactly while that stub is installed.
        """
        checks = Mock()
        checks.typecheck.return_value = {"returncode": 0, "output": "compiled"}

        def tests(files):
            """Answer handler suites green and HTTP suites according to the scenario."""
            bindings = self.root / "backend/tests/support/apis.ts"
            if bindings.is_file() and "status: 501" in bindings.read_text(encoding="utf-8"):
                return {"returncode": 1, "output": "AssertionError [ERR_ASSERTION]: expected 200 to equal 501",
                    "tests": 2, "pass": 0, "fail": 2}
            if any(str(name).endswith(".http.test.ts") or "/http/flows/" in str(name).replace("\\", "/")
                    for name in files):
                return {"returncode": 0 if http_ok else 1, "output": "GET /api/notes never reached an Express route",
                    "tests": 2, "pass": 2 if http_ok else 1, "fail": 0 if http_ok else 1}
            return {"returncode": 0, "output": "handler suites pass", "tests": 2, "pass": 2, "fail": 0}

        checks.tests.side_effect = tests
        checks.build.return_value = {"returncode": 0, "output": "built"}
        return checks

    def run_stage(self, checks):
        """Run step 08 with every model and dependency interaction mocked out."""
        def agent(*arguments, **keywords):
            """Accept the implementation only if the compiler gate accepts it."""
            return {"turns": 1, "checks": arguments[4]()}

        with patch.object(self.stage, "TextModel"), patch.object(self.stage, "DemoConsole", return_value=Mock()), \
                patch.object(self.stage, "install_dependencies"), \
                patch.object(self.stage, "BackendChecks", return_value=checks), \
                patch.object(self.stage, "run_file_agent", side_effect=agent):
            return self.stage.implement_backend_tdd(self.arc, max_turns=1)

    def test_a_round_that_only_wrote_its_own_suite_does_not_rerun_accepted_suites(self):
        """A new suite cannot break an accepted one at runtime; compiling still covers it."""
        checks = self.checks()
        report = self.run_stage(checks)
        accepted = report["tests"]["REQ-A"]["API-SAVE"]
        self.assertEqual(report["agents"]["tests:API-COUNT"]["accepted_rechecks"], 0)
        # Every accepted suite is still compiled with the new one; only the run is skipped.
        self.assertTrue(any(accepted in files for files in
            (call.args[0] for call in checks.typecheck.call_args_list)))

    def test_a_round_that_also_changed_a_shared_file_reruns_accepted_suites(self):
        """A wider writable set must not let a shared edit reach an accepted suite unchecked."""
        stage = self.stage
        workspace_for = stage.SourceFiles.workspace

        def workspace(instance, *arguments, **keywords):
            """Return the real workspace with one shared edit recorded for this round."""
            value = workspace_for(instance, *arguments, **keywords)
            value.round_changed = {"backend/tests/support/apis.ts"}
            return value

        checks = self.checks()
        with patch.object(stage.SourceFiles, "workspace", workspace):
            report = self.run_stage(checks)
        self.assertEqual(report["agents"]["tests:API-COUNT"]["accepted_rechecks"], 1)

    def test_accepted_apis_report_their_http_suites_and_flows(self):
        report = self.run_stage(self.checks())
        self.assertEqual(report["status"], "GREEN")
        self.assertEqual(set(report["http"]["apis"]), set(self.design["apis"]))
        entry = report["http"]["apis"]["API-SAVE"]
        self.assertEqual(entry["file"], "backend/tests/http/REQ-A/API-SAVE.http.test.ts")
        self.assertEqual(entry["probes"], len(probe_cases(self.design, "API-SAVE")))
        self.assertEqual((entry["tests"], entry["pass"], entry["fail"]), (2, 2, 0))
        self.assertEqual(report["http"]["flows"]["file"], FLOW_PATH)
        self.assertEqual(report["http"]["flows"]["status"], "GREEN")
        self.assertEqual(report["http"]["flows"]["verdict"], "PASS")
        self.assertEqual(report["http"]["flows"]["unverified"], [])
        self.assertEqual(report["http"]["summary"], {"tests": 6, "pass": 6, "fail": 0})
        self.assertTrue((self.root / report["http"]["support"]).is_file())
        self.assertTrue((self.root / FLOW_PATH).is_file())

    def test_an_api_whose_http_route_fails_is_not_accepted(self):
        report = self.run_stage(self.checks(http_ok=False))
        self.assertEqual(report["status"], "COMPLETED_WITH_FAILURES")
        self.assertEqual(report["http"]["apis"], {})
        self.assertEqual(report["http"]["flows"]["verdict"], "FAIL")
        self.assertEqual(report["http"]["flows"]["unverified"], [])
        self.assertEqual(report["requirements"]["REQ-A"]["status"], "FAILED")
        self.assertIn("Tests are not GREEN", report["failures"][0]["error"])
        self.assertEqual(json.loads((self.arc / "backend_tdd.json").read_text())["status"], "COMPLETED_WITH_FAILURES")


    def test_the_report_publishes_which_creating_apis_a_flow_exercised(self):
        report = self.run_stage(self.checks())
        coverage = report["http"]["coverage"]
        self.assertEqual(coverage["returncode"], 0)
        self.assertEqual(coverage["unverified"], [])
        self.assertIn("HTTP FLOW COVERAGE 1/1", coverage["output"])
        self.assertIn("API-SAVE", report["http"]["flows"]["apis"])

    def test_a_create_no_flow_can_execute_is_reported_as_unverified(self):
        from infrastructure.shared.project.entrypoints import remember_path
        from infrastructure.utils.files import read_artifact, save_json, save_text
        design = read_artifact(self.arc, "backend_apis.json")
        blind = json.loads(json.dumps(design["apis"]["API-SAVE"]))
        blind["signature"] = {"name": "tagNote", "method": "POST", "path": "/api/notes/{noteId}/tags"}
        design["apis"]["API-BLIND"] = blind
        design["requirements"]["REQ-C"].append("API-BLIND")
        save_json(self.arc / "backend_apis.json", design)
        relative = "backend/src/api/api-blind.ts"
        save_text(self.root / relative, "export async function handle() { return { status: 200, body: {} }; }\n")
        remember_path(self.root, "api", "API-BLIND", relative)
        report = self.run_stage(self.checks())
        coverage = report["http"]["coverage"]
        self.assertEqual(coverage["returncode"], 1)
        self.assertEqual([item["api"] for item in coverage["unverified"]], ["API-BLIND"])
        self.assertIn("UNVERIFIED API-BLIND (route)", coverage["output"])


if __name__ == "__main__":
    unittest.main()

