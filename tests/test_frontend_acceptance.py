"""Black-box acceptance layer: journey derivation, audit rules, design contract and repair loop.

The fixtures below describe an abstract CRUD resource, never a concrete product: the same
generator must derive the same journey for books, tickets or notes.
"""

import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.project.initialize import initialize_project
from infrastructure.stages.frontend.acceptance import pipeline as pipeline_module
from infrastructure.stages.frontend.acceptance.audit import acceptance_audit, audit_problems, require_audit
from infrastructure.stages.frontend.acceptance.journey import (chrome_elements, identity_field,
    journey_plan, write_journeys)
from infrastructure.stages.frontend.acceptance.pipeline import REPORT, run_acceptance
from infrastructure.stages.frontend.acceptance.runtime import (CONFIG, RUNNER, SUPPORT, SCRIPT,
    VITE_CONFIG, prepare_acceptance)
from infrastructure.stages.frontend.ui.design import accessibility_problems
from infrastructure.stages.frontend.ui.testing import existence_suite
from infrastructure.utils.files import read_source, save_text


def node(kind, parent, data=(), mode=(), **props):
    return {"type": kind, "parent": parent, "data": list(data), "mode": list(mode), "prop": props}


def binding(initial=""):
    return {"dtype": "string", "constraints": [], "initial": initial, "source": "test"}


def api(name, method, path, body=None, write=(), read=()):
    return {"signature": {"name": name, "method": method, "path": path},
        "request": {"params": {"type": "object"}, "query": {"type": "object"},
            "body": body or {"type": "object"}, "headers": {"type": "object"}},
        "read": list(read), "write": list(write)}


def fixture(resource="books", *, parameter="title", label="Title", identity_data=None,
        identity_name_from=True, with_update=True, with_delete=True, with_list=True):
    """Build one abstract CRUD resource: list, create, detail, update and delete."""
    prefix = "UI-" + resource
    identity = list(identity_data or [resource + "." + parameter])
    identity_props = {"name_from": resource + "." + parameter} if identity_name_from else {}
    elements = {
        "UI-home": node("page", None),
        "UI-home-open": node("link", "UI-home", text="Open the list", href="/" + resource),
        prefix: node("page", None),
        prefix + "-list": node("list", prefix),
        prefix + "-new": node("link", prefix, text="Create " + label.lower(), href="/" + resource + "/new"),
        prefix + "-record": node("link", prefix + "-list", identity, ["user_read"], **identity_props),
        prefix + "-new-page": node("page", None),
        prefix + "-form": node("form", prefix + "-new-page"),
        prefix + "-field": node("input", prefix + "-form", ["form." + parameter], ["user_write"],
            label=label, input_type="text"),
        prefix + "-submit": node("button", prefix + "-form", text="Save " + label.lower()),
        prefix + "-error": node("alert", prefix + "-new-page", ["form.error"],
            visible={"var": "form.errorVisible", "equals": True}),
        prefix + "-detail": node("page", None),
        prefix + "-detail-name": node("heading", prefix + "-detail", [resource + "." + parameter], ["user_read"]),
        prefix + "-detail-note": node("input", prefix + "-detail", [resource + ".note"], ["user_write"],
            label="Note"),
    }
    if with_update:
        elements[prefix + "-save"] = node("button", prefix + "-detail", text="Save changes")
    if with_delete:
        elements[prefix + "-delete"] = node("button", prefix + "-detail", text="Delete record")
    pages = {
        "home": {"name": "Home", "route": "/", "root": "UI-home", "images": [], "fixtures": {"default": {}}},
        "list": {"name": "List", "route": "/" + resource, "root": prefix, "images": [],
            "fixtures": {"default": {}}},
        "create": {"name": "Create", "route": "/" + resource + "/new", "root": prefix + "-new-page",
            "images": [], "fixtures": {"default": {}}},
        "detail": {"name": "Detail", "route": "/" + resource + "/:" + parameter, "root": prefix + "-detail",
            "images": [], "fixtures": {"default": {}}},
    }
    bindings = {name: binding() for name in (resource + "." + parameter, resource + ".note",
        "form." + parameter, "form.error")}
    ui = {"pages": pages, "elements": elements, "bindings": bindings,
        "requirements": {"ROOT": [], "REQ-1": [prefix, prefix + "-record"]}, "steps": {"STEP-1": [prefix]}}
    triggers = {
        "TR-list": {"source": prefix, "event": "mount", "guard": None, "read": [], "write": [],
            "action": [{"kind": "api", "api": "API-list", "description": "List the records"}]},
        "TR-create": {"source": prefix + "-form", "event": "submit", "guard": None,
            "read": ["form." + parameter], "write": [],
            "action": [{"kind": "api", "api": "API-create", "description": "Create the record"},
                {"kind": "ui", "ui": prefix}]},
        "TR-open": {"source": prefix + "-record", "event": "click", "guard": None,
            "read": [resource + "." + parameter], "write": [],
            "action": [{"kind": "ui", "ui": prefix + "-detail"}]},
    }
    ids = ["TR-list", "TR-create", "TR-open"]
    if with_update:
        triggers["TR-save"] = {"source": prefix + "-save", "event": "click", "guard": None,
            "read": [resource + ".note"], "write": [],
            "action": [{"kind": "api", "api": "API-update", "description": "Update the record"}]}
        ids.append("TR-save")
    if with_delete:
        triggers["TR-delete"] = {"source": prefix + "-delete", "event": "click", "guard": None,
            "read": [], "write": [],
            "action": [{"kind": "api", "api": "API-delete", "description": "Delete the record"},
                {"kind": "ui", "ui": prefix}]}
        ids.append("TR-delete")
    design = {"triggers": triggers, "requirements": {"REQ-1": ids}, "steps": {}}
    apis = {
        "API-create": api("createRecord", "POST", "/api/" + resource, {"type": "object",
            "required": [parameter], "properties": {parameter: {"type": "string"}}},
            write=["Record.id", "Record." + parameter]),
        "API-detail": api("readRecord", "GET", "/api/" + resource + "/{" + parameter + "}",
            read=["Record." + parameter, "Record.note"]),
        "API-update": api("updateRecord", "PATCH", "/api/" + resource + "/{" + parameter + "}",
            {"type": "object", "properties": {"note": {"type": "string"}}}, write=["Record.note"]),
        "API-delete": api("deleteRecord", "DELETE", "/api/" + resource + "/{" + parameter + "}",
            write=["Record.id"]),
    }
    if with_list:
        apis["API-list"] = api("listRecords", "GET", "/api/" + resource,
            read=["Record.id", "Record." + parameter])
    backend = {"requirements": {"REQ-1": sorted(apis)}, "apis": apis}
    schema = {"entities": {"record": {"fields": {"id": {}, parameter: {}, "note": {}}}}}
    return ui, design, backend, schema


class JourneyDerivation(unittest.TestCase):
    def setUp(self):
        self.ui, self.design, self.backend, self.schema = fixture()
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def render(self, ui=None, design=None, backend=None, schema=None):
        plan = journey_plan(None, schema or self.schema, backend or self.backend, ui or self.ui,
            design or self.design)
        files = write_journeys(self.output, plan)
        return plan, files, (read_source(self.output / files[0]) if files else "")

    def test_derives_the_ten_step_crud_journey_without_problems(self):
        plan, files, source = self.render()
        self.assertEqual(plan["problems"], [])
        self.assertEqual(files, ["frontend/tests/acceptance/books.journey.spec.ts"])
        self.assertEqual(plan["journeys"][0]["steps"], ["open", "list", "create", "find", "open-detail",
            "reload", "independent", "update", "delete", "gone"])
        for fragment in ("await openRoute(page, HOME);", "await action(page, \"Open the list\").click();",
                "await openRoute(page, LIST);", "await control(page, \"Title\").fill(RECORD);",
                "await action(page, \"Save title\").click();", "await record(page, RECORD).first().click();",
                "await page.reload();", "await control(page, \"Note\").fill(EDITED);",
                "await action(page, \"Save changes\").click();", "await action(page, \"Delete record\").click();"):
            self.assertIn(fragment, source)

    def test_journey_verifies_urls_persistence_database_side_effects_and_absence(self):
        _, _, source = self.render()
        self.assertGreaterEqual(source.count("toHaveURL"), 4)
        self.assertIn('await expect(control(page, "Note")).toHaveValue(EDITED);', source)
        self.assertIn("expect(database.stores(RECORD)).toBeGreaterThan(0);", source)
        self.assertIn("expect(database.stores(RECORD)).toBe(0);", source)
        self.assertIn("await expect(record(page, RECORD)).toHaveCount(0);", source)
        self.assertIn("await page.reload();", source)

    def test_journey_never_seeds_state_nor_mocks_the_backend(self):
        _, _, source = self.render()
        for forbidden in ("INSERT", "UPDATE ", "DELETE FROM", "__ARC_UI_FIXTURE__", "page.route(",
                "fetch(", "addInitScript", "localStorage", "test.skip", "waitForTimeout"):
            self.assertNotIn(forbidden, source)

    def test_journey_uses_semantic_locators_for_every_control_that_has_one(self):
        _, _, source = self.render()
        self.assertIn("await control(page, \"Title\")", source)
        self.assertIn("await action(page, \"Save title\")", source)
        self.assertIn("record(page, RECORD)", source)
        self.assertNotIn("getByTestId", source)

    def test_rejected_submission_asserts_error_no_write_and_no_navigation(self):
        _, _, source = self.render()
        self.assertIn('await control(page, "Title").fill("");', source)
        self.assertIn("await expect(page).toHaveURL(CREATE);", source)
        self.assertIn("await expect(errorMessage(page).first()).toBeVisible();", source)
        self.assertIn("const stored = database.total();", source)
        self.assertIn("expect(database.total()).toBe(stored);", source)
        self.assertIn("expectIndependentlyMissing(visitor, LIST, RECORD, test.info());", source)

    def test_generator_is_not_hardcoded_to_one_domain(self):
        ui, design, backend, schema = fixture("tickets", parameter="code", label="Code")
        plan, files, source = self.render(ui, design, backend, schema)
        self.assertEqual(files, ["frontend/tests/acceptance/tickets.journey.spec.ts"])
        self.assertIn('await control(page, "Code").fill(RECORD);', source)
        self.assertNotIn("book", source.lower())
        self.assertNotIn("title", source.lower())


class JourneyDiagnostics(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def plan(self, ui, design, backend, schema):
        plan = journey_plan(None, schema, backend, ui, design)
        write_journeys(self.output, plan)
        return plan

    def test_concatenated_record_name_is_a_design_problem_not_a_weaker_test(self):
        ui, design, backend, schema = fixture(identity_data=["books.title", "books.description",
            "books.created_at"], identity_name_from=False)
        plan = self.plan(ui, design, backend, schema)
        reasons = " ".join(problem for problem in plan["problems"])
        self.assertIn("dedicated identity control", reasons)
        steps = plan["journeys"][0]["steps"]
        self.assertNotIn("find", steps)
        self.assertNotIn("delete", steps)
        source = read_source(self.output / plan["journeys"][0]["spec"])
        self.assertNotIn("record(page, RECORD)", source)

    def test_template_placeholder_blocks_derivation_instead_of_asserting_it(self):
        """A name-bearing prop that still holds ``{...}`` is a design defect, not an app defect."""
        ui, design, backend, schema = fixture()
        ui["elements"]["UI-books-new"]["prop"]["text"] = "{books.title}"
        plan = self.plan(ui, design, backend, schema)
        self.assertEqual(plan["journeys"], [])
        self.assertTrue(any("template placeholder" in problem
            for problem in plan["derivation_problems"]), plan["derivation_problems"])

    def test_missing_delete_control_is_reported_without_faking_the_step(self):
        ui, design, backend, schema = fixture(with_delete=False)
        plan = self.plan(ui, design, backend, schema)
        self.assertTrue(any("API-delete" in problem for problem in plan["problems"]))
        steps = plan["journeys"][0]["steps"]
        self.assertIn("update", steps)
        self.assertNotIn("delete", steps)
        self.assertNotIn("gone", steps)

    def test_missing_detail_page_is_reported_instead_of_guessing_a_url(self):
        ui, design, backend, schema = fixture()
        ui["pages"]["detail"]["route"] = "/elsewhere"
        ui["elements"]["UI-books-detail-name"]["prop"]["href"] = "/elsewhere/:title"
        plan = self.plan(ui, design, backend, schema)
        self.assertTrue(any("detail page is missing" in problem or "item route" in problem
            for problem in plan["problems"]))
        self.assertNotIn("reload", plan["journeys"][0]["steps"])


    def test_non_crud_application_derives_no_journey_and_says_why(self):
        ui, design, backend, schema = fixture()
        backend = {"requirements": {"REQ-1": ["API-login"]}, "apis": {"API-login": api("login",
            "POST", "/api/login", {"type": "object", "required": ["username"],
                "properties": {"username": {"type": "string"}}})}}
        plan = self.plan(ui, design, backend, schema)
        self.assertEqual(plan["journeys"], [])
        self.assertTrue(any("no create plus read pair" in note for note in plan["notes"]))

class IdentityScope(unittest.TestCase):
    """The record name is typed into the create form, never into a page-wide search box.

    This is the RC-2 regression: the old fallback chain picked the first writable input that had
    an accessible name, so a header search box became the record identity and the save trigger
    read the search variable instead of the form field.
    """

    def setUp(self):
        self.ui, self.design, self.backend, self.schema = fixture()

    def test_page_wide_search_box_never_carries_the_record_name(self):
        search = node("input", "UI-books", ["books.title"], ["user_write"], label="Search",
            input_type="text")
        self.ui["elements"] = {"UI-books-search": search, **self.ui["elements"]}
        field, target = identity_field(self.ui, self.design, self.backend, "API-create",
            "TR-create", "create")
        self.assertEqual(target, "title")
        self.assertEqual(field["element"], "UI-books-field")

    def test_control_mounted_on_several_pages_is_chrome_and_excluded(self):
        ui = {"pages": {"list": {"root": "UI-books"}, "detail": {"root": "UI-books-detail"}},
            "elements": {"UI-books": node("page", None),
                "UI-books-list": node("list", "UI-books"),
                "UI-books-detail": node("page", "UI-books-list"),
                "UI-books-note": node("heading", "UI-books-detail", text="Note"),
                "UI-books-name": node("heading", "UI-books", text="Books")}}
        self.assertEqual(chrome_elements(ui), {"UI-books-detail", "UI-books-note"})

    def test_a_design_may_declare_a_repeated_region_as_content(self):
        ui = {"pages": {"list": {"root": "UI-books"}, "detail": {"root": "UI-books-detail"}},
            "elements": {"UI-books": node("page", None),
                "UI-books-list": node("list", "UI-books"),
                "UI-books-detail": node("page", "UI-books-list", area="content"),
                "UI-books-note": node("heading", "UI-books-detail", text="Note")}}
        self.assertEqual(chrome_elements(ui), {"UI-books-note"})


def account_fixture(*, register=True, login=True, logout=True, session=True, display=True,
        links=True, error=True, login_error=True):
    """Build one abstract account lifecycle: register, session, sign out, sign in again.

    The names are the ones a requirement would use for any product that manages accounts; no
    product word appears, so a generator tuned to one application would fail these tests.
    """
    elements = {
        "UI-home": node("page", None),
        "UI-home-links": node("container", "UI-home"),
        "UI-home-username": node("text", "UI-home", ["session.username"], ["user_read"]),
        "UI-home-logout": node("button", "UI-home", text="Logout"),
        "UI-register": node("page", None),
        "UI-register-form": node("form", "UI-register"),
        "UI-register-username": node("input", "UI-register-form", ["form.username"], ["user_write"],
            label="Username", input_type="text"),
        "UI-register-password": node("input", "UI-register-form", ["form.password"], ["user_write"],
            label="Password", input_type="password"),
        "UI-register-submit": node("button", "UI-register-form", text="Create account"),
        "UI-register-error": node("alert", "UI-register", ["form.error"]),
        "UI-login": node("page", None),
        "UI-login-form": node("form", "UI-login"),
        "UI-login-username": node("input", "UI-login-form", ["form.username"], ["user_write"],
            label="Username", input_type="text"),
        "UI-login-password": node("input", "UI-login-form", ["form.password"], ["user_write"],
            label="Password", input_type="password"),
        "UI-login-submit": node("button", "UI-login-form", text="Sign in"),
        "UI-login-error": node("alert", "UI-login", ["form.error"]),
    }
    if links:
        elements["UI-home-register"] = node("link", "UI-home-links", text="Register", href="/register")
        elements["UI-home-login"] = node("link", "UI-home-links", text="Sign in", href="/login")
    pages = {
        "home": {"name": "Home", "route": "/", "root": "UI-home", "images": [],
            "fixtures": {"default": {}}},
        "registration": {"name": "Register", "route": "/register", "root": "UI-register",
            "images": [], "fixtures": {"default": {}}},
        "login": {"name": "Sign in", "route": "/login", "root": "UI-login", "images": [],
            "fixtures": {"default": {}}},
    }
    if not display:
        del elements["UI-home-username"]
    if not logout:
        del elements["UI-home-logout"]
    if not error:
        del elements["UI-register-error"]
    if not login_error:
        del elements["UI-login-error"]
    bindings = {name: binding() for name in ("session.username", "session.state", "form.username",
        "form.password", "form.error")}
    ui = {"pages": pages, "elements": elements, "bindings": bindings,
        "requirements": {"ROOT": [], "REQ-AUTH": ["UI-home", "UI-register", "UI-login"]},
        "steps": {"STEP-1": ["UI-home"]}}
    triggers = {
        "TR-session": {"source": "UI-home", "event": "mount", "guard": None, "read": [], "write": [],
            "action": [{"kind": "api", "api": "API-session", "description": "Resolve the session"}]},
        "TR-logout": {"source": "UI-home-logout", "event": "click", "guard": None, "read": [],
            "write": [], "action": [{"kind": "api", "api": "API-logout",
                "description": "End the session"}]},
    }
    if register:
        triggers["TR-register"] = {"source": "UI-register-submit", "event": "click", "guard": None,
            "read": ["form.username", "form.password"], "write": ["session.state"],
            "action": [{"kind": "api", "api": "API-register", "description": "Create the account"},
                {"kind": "ui", "ui": "UI-home"}]}
    if login:
        triggers["TR-login"] = {"source": "UI-login-submit", "event": "click", "guard": None,
            "read": ["form.username", "form.password"], "write": ["session.state"],
            "action": [{"kind": "api", "api": "API-login", "description": "Sign in"},
                {"kind": "ui", "ui": "UI-home"}]}
    design = {"triggers": triggers, "requirements": {"REQ-AUTH": sorted(triggers)}, "steps": {}}
    apis = {}
    if register:
        apis["API-register"] = api("registerAccount", "POST", "/api/register", {"type": "object",
            "required": ["username", "password"], "properties": {"username": {"type": "string"},
                "password": {"type": "string"}}}, write=["account.id", "account.username",
                "auth_session.id"])
    if login:
        apis["API-login"] = api("login", "POST", "/api/login", {"type": "object",
            "required": ["username", "password"], "properties": {"username": {"type": "string"},
                "password": {"type": "string"}}}, write=["auth_session.id"])
    if logout:
        apis["API-logout"] = api("logout", "POST", "/api/logout", write=["auth_session.id"])
    if session:
        apis["API-session"] = api("currentSession", "GET", "/api/session", read=["account.username"])
    backend = {"requirements": {"REQ-AUTH": sorted(apis)}, "apis": apis}
    schema = {"entities": {"account": {"fields": {"id": {}, "username": {}, "password_hash": {}}},
        "auth_session": {"fields": {"id": {}, "account_id": {}}}}}
    return ui, design, backend, schema

class AccountLifecycle(unittest.TestCase):
    """The second family of journeys: the account lifecycle, derived without a model."""

    def setUp(self):
        self.ui, self.design, self.backend, self.schema = account_fixture()
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def plan(self, ui=None, design=None, backend=None, schema=None, tree=None):
        plan = journey_plan(tree, schema or self.schema, backend or self.backend, ui or self.ui,
            design or self.design)
        write_journeys(self.output, plan)
        return plan

    def source(self, plan):
        account = next(entry for entry in plan["journeys"] if entry.get("kind") == "account")
        return read_source(self.output / account["spec"]), account

    def test_derives_the_lifecycle_cases_from_the_accepted_ir(self):
        plan = self.plan()
        source, account = self.source(plan)
        self.assertEqual(plan["problems"], [])
        self.assertEqual(account["spec"],
            "frontend/tests/acceptance/account-lifecycle.journey.spec.ts")
        self.assertEqual(account["steps"], ["entry", "register", "identity", "restore", "isolated"])
        self.assertEqual([case["id"] for case in account["cases"]],
            ["register-restore", "sign-out", "sign-in-again", "duplicate", "wrong-secret"])
        self.assertEqual(account["database"], {"session": "auth_session", "account": "account"})

    def test_lifecycle_spec_drives_the_designed_controls_and_proves_the_server_state(self):
        plan = self.plan()
        source, _ = self.source(plan)
        for fragment in ('await action(page, "Register").click();',
                'await control(page, "Username").fill(identity);',
                'await action(page, "Create account").click();',
                'await expect(page.getByText(USER)).toHaveCount(0);',
                "expect(database.count(SESSION)).toBe(1);", "expect(database.count(SESSION)).toBe(0);",
                "await page.reload();", 'await action(page, "Logout").click();',
                'await action(page, "Sign in").click();',
                'await expect(errorMessage(page).first()).toBeVisible();',
                "await visitor.page.goto(HOME);"):
            self.assertIn(fragment, source)

    def test_lifecycle_spec_never_fabricates_a_session_nor_seeds_state(self):
        plan = self.plan()
        source, _ = self.source(plan)
        for forbidden in ("localStorage", "sessionStorage", "addCookies", "addInitScript",
                "page.route(", "fetch(", "INSERT", "DELETE FROM", "test.skip", "waitForTimeout"):
            self.assertNotIn(forbidden, source)

    def test_rendered_lifecycle_passes_every_audit_rule(self):
        plan = self.plan()
        result = acceptance_audit(self.output, plan)
        self.assertEqual(result["status"], "GREEN", result["output"])
        self.assertEqual(result["files"],
            ["frontend/tests/acceptance/account-lifecycle.journey.spec.ts"])

    def test_missing_registration_page_is_a_problem_and_never_a_silent_pass(self):
        ui, design, backend, schema = account_fixture()
        route = ui["pages"].pop("registration")["route"]
        ui["pages"]["elsewhere"] = {"name": "Elsewhere", "route": "/elsewhere",
            "root": "UI-register", "images": [], "fixtures": {"default": {}}}
        plan = self.plan(ui, design, backend, schema)
        self.assertEqual(plan["journeys"], [])
        self.assertTrue(any(route in problem for problem in plan["problems"]), plan["problems"])

    def test_capabilities_the_design_cannot_prove_are_reported_not_skipped(self):
        ui, design, backend, schema = account_fixture(error=False, login_error=False, links=False)
        plan = self.plan(ui, design, backend, schema)
        account = next(entry for entry in plan["journeys"] if entry.get("kind") == "account")
        self.assertEqual([case["id"] for case in account["cases"]],
            ["register-restore", "sign-out", "sign-in-again"])
        reported = " ".join(item["capability"] for item in plan["unverified"])
        self.assertIn("a rejected registration", reported)
        self.assertIn("a rejected sign-in", reported)
        self.assertIn("reaching the registration form", reported)
        self.assertEqual(account["routes"]["register"], "/register")

    def test_without_a_sign_in_route_the_lifecycle_is_reported_unverified(self):
        ui, design, backend, schema = account_fixture(login=False, error=False)
        plan = self.plan(ui, design, backend, schema)
        account = next(entry for entry in plan["journeys"] if entry.get("kind") == "account")
        self.assertEqual([case["id"] for case in account["cases"]], ["register-restore", "sign-out"])
        self.assertTrue(any("sign-in route" in item["reason"] for item in plan["unverified"]))

    def test_crud_and_lifecycle_journeys_coexist(self):
        ui, design, backend, schema = fixture()
        account_ui, account_design, account_backend, account_schema = account_fixture()
        ui["pages"].update({"registration": account_ui["pages"]["registration"],
            "login": account_ui["pages"]["login"]})
        ui["elements"].update(account_ui["elements"])
        design["triggers"].update(account_design["triggers"])
        backend["apis"].update(account_backend["apis"])
        schema["entities"].update(account_schema["entities"])
        plan = self.plan(ui, design, backend, schema)
        self.assertEqual([entry.get("kind") for entry in plan["journeys"]], [None, "account"])
        self.assertEqual(plan["problems"], [])
        self.assertEqual(len(write_journeys(self.output, plan)), 2)

class ScenarioCoverage(unittest.TestCase):
    """Every requirement scenario ends with one traceable record, covered or explained."""

    class Tree:
        """The smallest requirement tree the coverage classifier reads."""

        def __init__(self, scenarios):
            self.postorder = list(scenarios)
            self.own_steps = {rid: [sid for sid, *_ in items] for rid, items in scenarios.items()}
            self.steps = {sid: {"scenario": {"id": "SCN-" + sid, "name": values[0]},
                "Given": values[1], "When": values[2], "Then": values[3]}
                for rid, items in scenarios.items() for sid, *values in items}

    def setUp(self):
        self.ui, self.design, self.backend, self.schema = account_fixture()
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def plan(self, scenarios):
        plan = journey_plan(self.Tree(scenarios), self.schema, self.backend, self.ui, self.design)
        write_journeys(self.output, plan)
        return plan

    def test_each_lifecycle_scenario_gets_one_traceable_record(self):
        plan = self.plan({"REQ-AUTH": [("STEP-1", "Register an account", "a visitor on the form",
            "the visitor submits the form", "the home page displays the username")]})
        account = plan["journeys"][0]
        covered = account["coverage"]["covered"]
        self.assertEqual([item["scenario"] for item in covered], ["SCN-STEP-1"])
        self.assertEqual(covered[0]["status"], "COVERED")
        self.assertEqual(covered[0]["cases"][0], "register-restore")
        self.assertEqual(account["coverage"]["uncovered"], [])

    def test_a_scenario_that_needs_a_fabricated_credential_is_not_covered(self):
        plan = self.plan({"REQ-AUTH": [("STEP-1", "Reject an unrecognized session",
            "a session cookie that does not match an existing account",
            "the visitor opens the home page", "no username is displayed")]})
        account = plan["journeys"][0]
        uncovered = account["coverage"]["uncovered"]
        self.assertEqual([item["status"] for item in uncovered], ["NOT_COVERED"])
        self.assertIn("may not fabricate", uncovered[0]["reason"])
        self.assertTrue(any("is not covered" in note for note in plan["notes"]))



BAD_SPEC = '''import { test, expect } from "./support/context.js";
test("a rehearsal that proves nothing", async ({ page, database }) => {
  await page.route("**/api/**", route => route.fulfill({ status: 200, body: "{\\"ok\\":true}" }));
  await page.addInitScript(() => { (window as never as { __ARC_UI_FIXTURE__: unknown }).__ARC_UI_FIXTURE__ = {}; });
  database.prepare("INSERT INTO user_account (id) VALUES (?)").run("1");
  localStorage.setItem("session", "1");
  await fetch("/api/books");
  await page.waitForTimeout(500);
  await expect(page.getByTestId("UI-books")).toBeVisible();
});
'''


class AuditRules(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def test_rejects_every_shortcut_that_makes_a_green_suite_meaningless(self):
        rules = {problem["rule"] for problem in audit_problems("spec.ts", BAD_SPEC)}
        for expected in ("fixture", "mock", "injection", "sql-write", "storage", "network", "sleep"):
            self.assertIn(expected, rules)

    def test_rejects_a_suite_that_only_asserts_visibility(self):
        source = 'test("x", async ({ page }) => { await expect(page.getByTestId("a")).toBeVisible(); });'
        rules = {problem["rule"] for problem in audit_problems("spec.ts", source)}
        self.assertEqual(rules, {"navigation", "input", "click", "database"})

    def test_rejects_a_display_fixture_and_a_weakened_case(self):
        source = ('test("x", async ({ page }) => { await page.addInitScript(() => {});\n'
            '  (window as never as { __ARC_UI_FIXTURE__: unknown }).__ARC_UI_FIXTURE__ = {};\n'
            '  test.skip(true, "later"); });')
        rules = {problem["rule"] for problem in audit_problems("spec.ts", source)}
        self.assertIn("fixture", rules)
        self.assertIn("injection", rules)
        self.assertIn("weakened", rules)

    def test_rendered_journey_passes_every_rule(self):
        ui, design, backend, schema = fixture()
        plan = journey_plan(None, schema, backend, ui, design)
        write_journeys(self.output, plan)
        result = acceptance_audit(self.output, plan)
        self.assertEqual(result["status"], "GREEN")
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["files"], ["frontend/tests/acceptance/books.journey.spec.ts"])

    def test_missing_capability_of_a_declared_step_is_reported(self):
        ui, design, backend, schema = fixture()
        plan = journey_plan(None, schema, backend, ui, design)
        files = write_journeys(self.output, plan)
        source = read_source(self.output / files[0]).replace(
            'await expect(control(page, "Note")).toHaveValue(EDITED);', "")
        save_text(self.output / files[0], source)
        rules = {problem["rule"] for problem in acceptance_audit(self.output, plan)["problems"]}
        self.assertIn("step:update", rules)

    def test_design_problems_reach_the_audit_and_block_acceptance(self):
        ui, design, backend, schema = fixture(identity_data=["books.title", "books.description"],
            identity_name_from=False)
        plan = journey_plan(None, schema, backend, ui, design)
        write_journeys(self.output, plan)
        result = acceptance_audit(self.output, plan)
        rules = {problem["rule"] for problem in result["problems"]}
        self.assertIn("design", rules)
        with self.assertRaises(ValueError):
            require_audit(result)

    def test_empty_suite_is_not_applicable_rather_than_green(self):
        result = acceptance_audit(self.output, {"journeys": [], "problems": [], "notes": []})
        self.assertEqual(result["status"], "NOT_APPLICABLE")


class DesignContract(unittest.TestCase):
    def test_clean_design_declares_no_accessibility_problem(self):
        ui, _, _, _ = fixture()
        self.assertEqual(accessibility_problems(ui), [])

    def test_flags_a_record_control_without_an_accessible_name(self):
        ui, _, _, _ = fixture()
        ui["elements"]["UI-books-record"]["prop"] = {}
        rules = {problem["rule"] for problem in accessibility_problems(ui)}
        self.assertIn("access_name", rules)

    def test_flags_an_unknown_binding_and_a_mixed_name_source(self):
        ui, _, _, _ = fixture()
        ui["elements"]["UI-books-record"]["prop"] = {"name_from": "books.unknown"}
        self.assertIn("name_from", {problem["rule"] for problem in accessibility_problems(ui)})
        ui["elements"]["UI-books-record"]["prop"] = {"name_from": "books.title", "text": "Open"}
        problem = next(problem for problem in accessibility_problems(ui) if problem["rule"] == "name_from")
        self.assertIn("concatenation", problem["reason"])

    def test_flags_duplicate_labels_inside_one_form(self):
        ui, _, _, _ = fixture()
        ui["elements"]["UI-books-author"] = node("input", "UI-books-form", ["form.author"],
            ["user_write"], label="Title")
        rules = {problem["rule"] for problem in accessibility_problems(ui)}
        self.assertIn("label", rules)

    def test_flags_a_name_that_still_holds_a_template_placeholder(self):
        ui, _, _, _ = fixture()
        ui["elements"]["UI-books-record"]["prop"] = {"text": "{books.title}"}
        problem = next(problem for problem in accessibility_problems(ui)
            if problem["rule"] == "placeholder")
        self.assertIn("prop.text", problem["reason"])
        self.assertIn("{books.title}", problem["reason"])

    def test_existence_suite_asserts_the_exact_accessible_name(self):
        ui, _, _, _ = fixture()
        ui["pages"]["list"]["fixtures"]["named"] = {"books.title": "Referenced record"}
        suite = existence_suite(ui, "list")
        self.assertIn('toHaveAccessibleName("Referenced record")', suite)
        self.assertIn('toHaveAccessibleName("Create title")', suite)
        detail = existence_suite(ui, "detail")
        self.assertIn('toHaveAccessibleName("Delete record")', detail)
        self.assertIn('toHaveAccessibleName("Save changes")', detail)


class AcceptanceRuntime(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        initialize_project(self.output)

    def test_publishes_an_independent_playwright_project(self):
        prepare_acceptance(self.output)
        for relative in (CONFIG, SUPPORT, RUNNER, VITE_CONFIG):
            self.assertTrue((self.output / relative).is_file(), relative)
        config = read_source(self.output / CONFIG)
        self.assertIn('testDir: "./tests/acceptance"', config)
        self.assertIn('outputDir: "./test-results/acceptance"', config)
        self.assertIn("storageState: undefined", config)
        self.assertIn("workers: 1", config)
        package = json.loads((self.output / "package.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(package["scripts"][SCRIPT], "node frontend/scripts/test-acceptance.mjs")
        self.assertIn("@playwright/test", package["devDependencies"])

    def test_fixture_forbids_mocks_sql_seeding_and_display_fixtures(self):
        prepare_acceptance(self.output)
        source = read_source(self.output / SUPPORT)
        for marker in ("E2E_MOCK_FORBIDDEN", "E2E_SEED_FORBIDDEN", "E2E_FIXTURE_FORBIDDEN",
                "browser.newContext", "isRead", "stores(", "__ARC_API_VIOLATIONS__", "total()"):
            self.assertIn(marker, source)
        self.assertIn('for (const name of ["route", "routeFromHAR", "unroute", "unrouteAll"])', source)
        # The database is read-only: every read is checked before it reaches better-sqlite3.
        self.assertIn("if (!isRead(sql)) throw new Error(READ_ONLY", source)

    def test_runner_owns_a_temporary_database_the_guard_accepts(self):
        prepare_acceptance(self.output)
        runner = read_source(self.output / RUNNER)
        self.assertIn('"arc-acceptance-"', runner)
        self.assertIn("ARC_ACCEPTANCE_DB_FILE", runner)
        self.assertIn("ARC_ACCEPTANCE_FRONTEND_PORT", runner)


GREEN = {"returncode": 0, "output": "passed", "passed": 2, "failed": 0, "skipped": 0, "flaky": 0,
    "errors": [], "failures": [],
    "files": {"frontend/tests/acceptance/books.journey.spec.ts": 2}}
RED = {"returncode": 1, "output": "failed", "passed": 1, "failed": 1, "skipped": 0, "flaky": 0,
    "errors": [], "failures": ["expect(record(page, RECORD).first(), IDENTITY_HINT).toBeVisible()"],
    "files": {}}


class FakeChecks:
    """Stand in for the browser; the orchestration and the report are what these tests verify."""

    def __init__(self, results):
        self.results, self.calls = list(results), []

    def typecheck(self, files):
        return {"returncode": 0, "output": "compiled"}

    def tests(self, files):
        self.calls.append(list(files))
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


class AcceptancePipeline(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.arc = self.output / ".arc"
        self.arc.mkdir()
        initialize_project(self.output)
        self.ui, self.design, self.backend, self.schema = fixture()
        save_text(self.output / "frontend/src/books.tsx", "export function Page() { return null; }\n")

    def saved(self):
        return json.loads((self.arc / REPORT).read_text(encoding="utf-8"))

    def test_not_applicable_application_never_starts_a_browser(self):
        backend = {"requirements": {"REQ-1": ["API-login"]}, "apis": {"API-login": api("login",
            "POST", "/api/login", {"type": "object", "required": ["username"],
                "properties": {"username": {"type": "string"}}})}}
        with patch.object(pipeline_module, "AcceptanceChecks",
                side_effect=AssertionError("no browser work is expected")):
            report = run_acceptance(self.arc, None, self.schema, backend, self.ui, self.design,
                console=Mock())
        self.assertEqual(report["status"], "NOT_APPLICABLE")
        self.assertTrue(any("no create plus read pair" in note for note in report["notes"]))
        self.assertEqual(self.saved()["status"], "NOT_APPLICABLE")
        for relative in (CONFIG, SUPPORT, RUNNER, VITE_CONFIG):
            self.assertTrue((self.output / relative).is_file(), relative)

    def test_underivable_lifecycle_is_unverified_rather_than_not_applicable(self):
        ui, design, backend, schema = account_fixture()
        ui["pages"].pop("registration")
        with patch.object(pipeline_module, "AcceptanceChecks",
                side_effect=AssertionError("no browser work is expected")):
            report = run_acceptance(self.arc, None, schema, backend, ui, design, console=Mock())
        self.assertEqual(report["status"], "UNVERIFIED")
        self.assertTrue(report["problems"])
        self.assertTrue(report["error"])
        self.assertEqual(self.saved()["status"], "UNVERIFIED")

    def test_unsound_derivation_blocks_upstream_without_repairing_the_application(self):
        """A design-level derivation defect must not be repaired by rewriting working triggers."""
        ui, design, backend, schema = fixture()
        ui["elements"]["UI-books-new"]["prop"]["text"] = "{books.title}"
        with patch.object(pipeline_module, "AcceptanceChecks",
                side_effect=AssertionError("no browser work is expected")):
            report = run_acceptance(self.arc, None, schema, backend, ui, design, console=Mock())
        self.assertEqual(report["status"], "BLOCKED_UPSTREAM")
        self.assertTrue(any("template placeholder" in problem
            for problem in report["derivation_problems"]))
        self.assertEqual(report.get("tests"), None)
        self.assertEqual(self.saved()["status"], "BLOCKED_UPSTREAM")

    def test_lifecycle_journey_is_run_and_its_report_records_the_coverage(self):
        ui, design, backend, schema = account_fixture()
        checks = FakeChecks([GREEN])
        report = run_acceptance(self.arc, None, schema, backend, ui, design, checks=checks,
            console=Mock())
        self.assertEqual(report["status"], "GREEN")
        self.assertEqual(report["journeys"][0]["kind"], "account")
        self.assertEqual(checks.calls,
            [["frontend/tests/acceptance/account-lifecycle.journey.spec.ts"]])
        self.assertEqual(self.saved()["status"], "GREEN")
        self.assertIn("unverified", self.saved())


        checks = FakeChecks([GREEN])
        report = run_acceptance(self.arc, None, self.schema, self.backend, self.ui, self.design,
            checks=checks, console=Mock())
        self.assertEqual(report["status"], "GREEN")
        self.assertEqual(report["tests"]["passed"], 2)
        self.assertEqual(checks.calls, [["frontend/tests/acceptance/books.journey.spec.ts"]])
        self.assertEqual(self.saved()["status"], "GREEN")
        self.assertEqual(self.saved()["specs"], ["frontend/tests/acceptance/books.journey.spec.ts"])

    def test_failure_repairs_the_application_and_never_the_journey(self):
        checks = FakeChecks([RED, GREEN])
        seen = []

        def generate(instructions, payload):
            seen.append(payload)
            return {"edits": [{"path": "frontend/src/books.tsx",
                "content": "export function Page() { return null; } // repaired\n"}]}

        report = run_acceptance(self.arc, None, self.schema, self.backend, self.ui, self.design,
            checks=checks, console=Mock(), model=Mock(generate_json=generate), max_turns=2)
        self.assertEqual(report["status"], "GREEN")
        self.assertEqual(report["repair_turns"], 1)
        self.assertEqual(len(seen), 1)
        files = seen[0]["files"]
        self.assertFalse(files["frontend/tests/acceptance/books.journey.spec.ts"]["writable"])
        self.assertTrue(files["frontend/src/books.tsx"]["writable"])
        self.assertIn("Repair the application", seen[0]["feedback"][0]["error"])
        self.assertIn("repaired",
            (self.output / "frontend/src/books.tsx").read_text(encoding="utf-8"))
        self.assertIn("IDENTITY_HINT", seen[0]["task"]["specs"]["frontend/tests/acceptance/books.journey.spec.ts"])

    def test_failure_without_a_model_is_reported_not_hidden(self):
        checks = FakeChecks([RED])
        report = run_acceptance(self.arc, None, self.schema, self.backend, self.ui, self.design,
            checks=checks, console=Mock(), model=None, max_turns=2)
        self.assertEqual(report["status"], "FAILED")
        self.assertIn("Repair the application", report["error"])
        self.assertEqual(self.saved()["status"], "FAILED")
