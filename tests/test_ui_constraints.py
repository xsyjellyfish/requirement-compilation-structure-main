"""Requirement wording becomes verifiable UI controls: parsing, inheritance, design gate and browser checks.

The fixtures describe an abstract signed-out homepage plus a registration page, never a concrete
product: the same parser must derive the same obligations for tickets, books or notes.
"""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.project.initialize import initialize_project
from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.stages.frontend.ui import implementation as ui_implementation
from infrastructure.stages.frontend.ui.constraints import (ConstraintError, required_controls,
    required_controls_for_page, requirement_controls, unmet_controls)
from infrastructure.stages.frontend.ui.runtime import media_url, sync_media
from infrastructure.stages.frontend.ui.synthesis import bind_image_sources, synthesize_ui
from infrastructure.stages.frontend.ui.testing import existence_suite


ROOT_TEXT = ("The module presents a shared header on every page. The header shows a Search button "
    "and a Login link whose href is /login.")
REGISTER_TEXT = ("The registration page /register collects traveler details. The form ends with a "
    "button labeled \"Next step\" and a checkbox labeled \"I accept the terms\".")
LOGIN_TEXT = "The login page /login accepts a username and a password."


def node(kind, parent, data=(), mode=(), **props):
    return {"type": kind, "parent": parent, "data": list(data), "mode": list(mode), "prop": props}


def binding(initial=""):
    return {"dtype": "string", "constraints": [], "initial": initial, "source": "test"}


def requirement_tree() -> RequirementTree:
    """Build a parent requirement plus two leaf pages that repeat its header."""
    def leaf(rid, description, when):
        return {"id": rid, "name": rid, "description": description, "scenarios": [{"id": "SCN-" + rid,
            "name": "Scenario", "steps": [{"id": "STEP-" + rid, "Given": "A signed-out visitor",
                "When": when, "Then": "The page shows its controls"}]}]}
    return RequirementTree({"id": "ROOT", "name": "Accounts", "description": ROOT_TEXT,
        "scenarios": [], "children": [leaf("REQ-A", REGISTER_TEXT, "Open /register"),
            leaf("REQ-B", LOGIN_TEXT, "Open /login")]})


def ui_design() -> dict:
    """Design both pages; the shared header is repeated on the child page, as inheritance requires."""
    return {"pages": {
        "home": {"name": "Home", "route": "/", "root": "UI-HOME", "images": ["IMG-HOME"], "fixtures": {"default": {}}},
        "register": {"name": "Register", "route": "/register", "root": "UI-REGISTER", "images": [], "fixtures": {"default": {}}},
        "login": {"name": "Login", "route": "/login", "root": "UI-LOGIN", "images": [], "fixtures": {"default": {}}}},
        "elements": {
            "UI-HOME": node("page", None),
            "UI-HOME-NAV": node("container", "UI-HOME"),
            "UI-HOME-SEARCH": node("button", "UI-HOME-NAV", text="Search"),
            "UI-HOME-LOGIN": node("link", "UI-HOME-NAV", text="Login", href="/login"),
            "UI-HOME-ART": node("image", "UI-HOME", image="IMG-HOME", alt="Home layout"),
            "UI-REGISTER": node("page", None),
            "UI-REGISTER-NAV": node("container", "UI-REGISTER"),
            "UI-REGISTER-SEARCH": node("button", "UI-REGISTER-NAV", text="Search"),
            "UI-REGISTER-LOGIN": node("link", "UI-REGISTER-NAV", text="Login", href="/login"),
            "UI-REGISTER-FORM": node("form", "UI-REGISTER"),
            "UI-REGISTER-AGREE": node("checkbox", "UI-REGISTER-FORM", ["form.agree"], ["user_write"],
                label="I accept the terms"),
            "UI-REGISTER-NEXT": node("button", "UI-REGISTER-FORM", text="Next step"),
            "UI-LOGIN": node("page", None),
            "UI-LOGIN-NAV": node("container", "UI-LOGIN"),
            "UI-LOGIN-SEARCH": node("button", "UI-LOGIN-NAV", text="Search"),
            "UI-LOGIN-LOGIN": node("link", "UI-LOGIN-NAV", text="Login", href="/login"),
            "UI-LOGIN-FORM": node("form", "UI-LOGIN"),
            "UI-LOGIN-SUBMIT": node("button", "UI-LOGIN-FORM", text="LOGIN")},
        "bindings": {"form.agree": binding(initial=False)},
        "requirements": {"ROOT": [], "REQ-A": ["UI-REGISTER", "UI-REGISTER-NAV", "UI-REGISTER-SEARCH",
            "UI-REGISTER-LOGIN", "UI-REGISTER-FORM", "UI-REGISTER-AGREE", "UI-REGISTER-NEXT"],
            "REQ-B": ["UI-LOGIN", "UI-LOGIN-NAV", "UI-LOGIN-SEARCH", "UI-LOGIN-LOGIN", "UI-LOGIN-FORM",
                "UI-LOGIN-SUBMIT"]},
        "steps": {"STEP-REQ-A": ["UI-REGISTER-AGREE", "UI-REGISTER-NEXT"], "STEP-REQ-B": ["UI-LOGIN-SUBMIT"]}}


def images() -> dict:
    return {"IMG-HOME": {"id": "IMG-HOME", "reference": "home.png", "url": "/media/IMG-HOME.png"}}


class UiControlConstraints(unittest.TestCase):
    def setUp(self):
        self.tree = requirement_tree()
        self.controls = requirement_controls(self.tree)
        self.design = ui_design()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.arc = self.root / ".arc"

    def test_parent_wording_becomes_role_name_and_href_records(self):
        indexed = {(control["role"], control["name"]): control for control in self.controls}
        self.assertEqual(indexed[("link", "Login")]["href"], "/login")
        self.assertEqual(indexed[("button", "Search")]["sources"], ["ROOT"])
        self.assertEqual(indexed[("button", "Next step")]["sources"], ["REQ-A"])
        self.assertEqual(indexed[("checkbox", "I accept the terms")]["sources"], ["REQ-A"])
        self.assertTrue(all(control["id"].startswith("UIC-") for control in self.controls))
        self.assertNotIn(("link", "every page"), indexed)

    def test_leaf_inherits_parent_controls_but_not_its_sibling_controls(self):
        inherited = required_controls(self.tree, self.controls, "REQ-B")
        by_name = {control["name"]: control for control in inherited}
        self.assertTrue(by_name["Search"]["inherited"])
        self.assertTrue(by_name["Login"]["inherited"])
        self.assertEqual(by_name["Login"]["required_by"], "ROOT")
        self.assertNotIn("Next step", by_name)

    def test_design_gate_accepts_the_repeated_header_and_rejects_substitutes(self):
        self.assertEqual(unmet_controls(self.design, self.tree, self.controls, "REQ-A"), [])
        broken = deepcopy(self.design)
        broken["elements"]["UI-REGISTER-SEARCH"] = node("text", "UI-REGISTER-NAV", text="Search")
        problems = unmet_controls(broken, self.tree, self.controls, "REQ-A")
        self.assertEqual([problem["name"] for problem in problems], ["Search"])
        self.assertTrue(problems[0]["inherited"])
        self.assertIn("Add a button element", problems[0]["fix"])
        with self.assertRaises(ConstraintError) as raised:
            raise ConstraintError(problems)
        self.assertIn("Search", json.dumps(raised.exception.feedback, ensure_ascii=False))

    def test_design_gate_requires_the_declared_href_not_only_the_name(self):
        broken = deepcopy(self.design)
        broken["elements"]["UI-REGISTER-LOGIN"]["prop"]["href"] = "/signin"
        problems = unmet_controls(broken, self.tree, self.controls, "REQ-A")
        self.assertEqual([problem["name"] for problem in problems], ["Login"])
        self.assertEqual(problems[0]["href"], "/login")

    def test_page_controls_carry_the_element_that_renders_them(self):
        selected = required_controls_for_page(self.design, self.tree, "register", self.controls)
        rendered = {(control["role"], control["name"]): control["element"] for control in selected}
        self.assertEqual(rendered[("button", "Search")], "UI-REGISTER-SEARCH")
        self.assertEqual(rendered[("link", "Login")], "UI-REGISTER-LOGIN")
        self.assertEqual(rendered[("button", "Next step")], "UI-REGISTER-NEXT")
        self.assertEqual(rendered[("checkbox", "I accept the terms")], "UI-REGISTER-AGREE")

    def test_presence_suite_asserts_role_name_href_and_served_image(self):
        self.assertEqual(bind_image_sources(self.design, images()), [])
        self.assertEqual(self.design["elements"]["UI-HOME-ART"]["prop"]["src"], "/media/IMG-HOME.png")
        selected = required_controls_for_page(self.design, self.tree, "register", self.controls)
        suite = existence_suite(self.design, "register", selected)
        self.assertIn('page.getByRole("link", { name: "Login", exact: true }).first()', suite)
        self.assertIn('toHaveAttribute("href", "/login")', suite)
        self.assertIn('page.getByRole("button", { name: "Next step", exact: true }).first()', suite)
        self.assertIn('toHaveAttribute("type", "checkbox")', suite)
        home = existence_suite(self.design, "home",
            required_controls_for_page(self.design, self.tree, "home", self.controls))
        self.assertIn("naturalWidth", home)

    def test_presence_suite_asserts_the_designed_role(self):
        """A control rendered as a styled copy has the test ID and text but no usable role."""
        suite = existence_suite(self.design, "register")
        self.assertIn('toHaveRole("link")', suite)
        self.assertIn('toHaveRole("button")', suite)
        self.assertIn('toHaveRole("checkbox")', suite)

    def test_presence_suite_asserts_a_text_field_is_a_textbox(self):
        design = deepcopy(self.design)
        design["elements"]["UI-REGISTER-EMAIL"] = node("input", "UI-REGISTER-FORM",
            ["form.email"], ["user_write"], label="Email", input_type="email")
        design["bindings"]["form.email"] = binding()
        design["requirements"]["REQ-A"].append("UI-REGISTER-EMAIL")
        suite = existence_suite(design, "register")
        self.assertIn('page.getByTestId("UI-REGISTER-EMAIL")).toHaveRole("textbox")', suite)

    def test_unknown_image_reference_is_reported_instead_of_served(self):
        broken = deepcopy(self.design)
        broken["elements"]["UI-HOME-ART"]["prop"]["image"] = "IMG-MISSING"
        problems = bind_image_sources(broken, images())
        self.assertEqual([problem["image"] for problem in problems], ["IMG-MISSING"])
        self.assertIn("IMG-HOME", problems[0]["fix"])

    def test_supplied_images_are_copied_into_the_served_public_directory(self):
        inputs = self.root / "input"
        inputs.mkdir()
        (inputs / "home.png").write_bytes(b"PNG-BYTES")
        output = self.root / "output"
        sync_media(output, {"IMG-HOME": {"id": "IMG-HOME", "reference": "home.png"}}, inputs)
        served = output / "frontend/public/media/IMG-HOME.png"
        self.assertEqual(served.read_bytes(), b"PNG-BYTES")
        self.assertEqual(media_url("IMG-HOME", "home.png"), "/media/IMG-HOME.png")
        stale = served.parent / "stale.png"
        stale.write_bytes(b"OLD")
        sync_media(output, {"IMG-HOME": {"id": "IMG-HOME", "reference": "home.png"}}, inputs)
        self.assertFalse(stale.is_file())
        with self.assertRaises(ValueError):
            sync_media(output, {"IMG-X": {"id": "IMG-X", "reference": "../escape.png"}}, inputs)

    def test_design_proposal_without_an_inherited_control_is_repaired(self):
        tree = RequirementTree({"id": "ROOT", "name": "Accounts", "description": ROOT_TEXT,
            "scenarios": [], "children": [{"id": "REQ-A", "name": "Register", "description": REGISTER_TEXT,
                "scenarios": [{"id": "SCN-REQ-A", "name": "Scenario", "steps": [{"id": "STEP-REQ-A",
                    "Given": "A visitor", "When": "Open /register", "Then": "Show the form"}]}]}]})
        contexts = {rid: {**tree.requirements[rid], "steps": [tree.steps[sid] for sid in tree.own_steps[rid]],
            "images": {}} for rid in tree.requirements}
        without_header = {"pages": {"register": {"name": "Register", "route": "/register",
                "root": "UI-REGISTER", "images": [], "fixtures": {"default": {}}}},
            "elements": {"UI-REGISTER": node("page", None), "UI-REGISTER-FORM": node("form", "UI-REGISTER"),
                "UI-REGISTER-AGREE": node("checkbox", "UI-REGISTER-FORM", ["form.agree"], ["user_write"],
                    label="I accept the terms"),
                "UI-REGISTER-NEXT": node("button", "UI-REGISTER-FORM", text="Next step")},
            "bindings": {"form.agree": binding(initial=False)},
            "requirement_ui": ["UI-REGISTER", "UI-REGISTER-FORM", "UI-REGISTER-AGREE", "UI-REGISTER-NEXT"],
            "step_ui": {"STEP-REQ-A": ["UI-REGISTER-AGREE", "UI-REGISTER-NEXT"]}}
        with_header = deepcopy(without_header)
        with_header["elements"].update({"UI-REGISTER-NAV": node("container", "UI-REGISTER"),
            "UI-REGISTER-SEARCH": node("button", "UI-REGISTER-NAV", text="Search"),
            "UI-REGISTER-LOGIN": node("link", "UI-REGISTER-NAV", text="Login", href="/login")})
        with_header["requirement_ui"] = [*with_header["requirement_ui"], "UI-REGISTER-NAV",
            "UI-REGISTER-SEARCH", "UI-REGISTER-LOGIN"]
        requests = []

        def generate(instructions, payload):
            requests.append(deepcopy(payload))
            return deepcopy(without_header if len(requests) == 1 else with_header)

        design = synthesize_ui(self.arc, tree, {}, contexts, Mock(generate_json=generate), Mock(),
            max_attempts=3)
        self.assertEqual(len(requests), 2)
        offered = {control["name"]: control for control in requests[0]["required_controls"]}
        self.assertTrue(offered["Search"]["inherited"])
        self.assertEqual(offered["Login"]["href"], "/login")
        self.assertIn("Search", json.dumps(requests[1]["correction"], ensure_ascii=False))
        self.assertEqual(design["elements"]["UI-REGISTER-SEARCH"]["prop"]["text"], "Search")
        self.assertTrue((self.arc / "ui_design.json").is_file())


class UiAccessibilityGate(unittest.TestCase):
    """Step 09 refuses a design whose controls no reviewer could locate by name."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.arc = Path(temporary.name) / ".arc"
        self.tree = RequirementTree({"id": "ROOT", "name": "Accounts", "description": ROOT_TEXT,
            "scenarios": [], "children": [{"id": "REQ-A", "name": "Register", "description": REGISTER_TEXT,
                "scenarios": [{"id": "SCN-REQ-A", "name": "Scenario", "steps": [{"id": "STEP-REQ-A",
                    "Given": "A visitor", "When": "Open /register", "Then": "Show the form"}]}]}]})
        self.contexts = {rid: {**self.tree.requirements[rid],
            "steps": [self.tree.steps[sid] for sid in self.tree.own_steps[rid]], "images": {}}
            for rid in self.tree.requirements}

    def proposal(self, help_text="Help"):
        elements = {"UI-REGISTER": node("page", None), "UI-REGISTER-FORM": node("form", "UI-REGISTER"),
            "UI-REGISTER-NAME": node("input", "UI-REGISTER-FORM", ["form.name"], ["user_write"],
                label="Name", input_type="text"),
            "UI-REGISTER-AGREE": node("checkbox", "UI-REGISTER-FORM", ["form.agree"], ["user_write"],
                label="I accept the terms"),
            "UI-REGISTER-NEXT": node("button", "UI-REGISTER-FORM", text="Next step"),
            "UI-REGISTER-HELP": node("button", "UI-REGISTER", text=help_text),
            "UI-REGISTER-SEARCH": node("button", "UI-REGISTER", text="Search"),
            "UI-REGISTER-LOGIN": node("link", "UI-REGISTER", text="Login", href="/login")}
        return {"pages": {"register": {"name": "Register", "route": "/register", "root": "UI-REGISTER",
                "images": [], "fixtures": {"default": {}}}},
            "elements": elements,
            "bindings": {"form.name": binding(), "form.agree": binding(initial=False)},
            "requirement_ui": list(elements),
            "step_ui": {"STEP-REQ-A": ["UI-REGISTER-NAME", "UI-REGISTER-NEXT"]}}

    def design(self, proposals):
        requests = []
        def generate(instructions, payload):
            requests.append(deepcopy(payload))
            return deepcopy(proposals[min(len(requests) - 1, len(proposals) - 1)])
        result = synthesize_ui(self.arc, self.tree, {}, self.contexts,
            Mock(generate_json=generate), Mock(), max_attempts=3)
        return result, requests

    def test_control_without_an_accessible_name_is_repaired_before_publishing(self):
        unnamed = self.proposal()
        unnamed["elements"]["UI-REGISTER-HELP"] = node("button", "UI-REGISTER")
        design, requests = self.design([unnamed, self.proposal()])
        self.assertIn("access_name", json.dumps(requests[1]["correction"], ensure_ascii=False))
        self.assertIn("prop.name_from", json.dumps(requests[1]["correction"], ensure_ascii=False))
        self.assertEqual(design["elements"]["UI-REGISTER-HELP"]["prop"]["text"], "Help")

    def test_template_placeholder_in_a_name_is_repaired_before_publishing(self):
        templated = self.proposal()
        templated["elements"]["UI-REGISTER-HELP"] = node("button", "UI-REGISTER",
            text="{form.title}")
        design, requests = self.design([templated, self.proposal()])
        self.assertIn("placeholder", json.dumps(requests[1]["correction"], ensure_ascii=False))
        self.assertEqual(design["elements"]["UI-REGISTER-HELP"]["prop"]["text"], "Help")


class UiImplementationWiring(unittest.TestCase):
    """Step 10 must hand the compiled suite and the model the same inherited controls."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        initialize_project(self.output)
        self.arc = self.output / ".arc"
        self.arc.mkdir()
        self.input_dir = self.output / "input"
        self.input_dir.mkdir()
        (self.input_dir / "home.png").write_bytes(b"PNG-BYTES")
        self.tree = requirement_tree()
        self.design = ui_design()
        self.assertIsNotNone(bind_image_sources(self.design, images()))
        self.report = {"status": "VALIDATING", "pages": {}}

    def test_generated_suites_report_and_model_context_share_the_inherited_controls(self):
        contexts, reds = [], set()

        class FakeChecks:
            def __init__(self, output, timeout: int = 600):
                self.output = output

            def install_browser(self):
                pass

            def typecheck(self, files, **options):
                return {"returncode": 0, "output": "types ok"}

            def tests(self, files):
                if tuple(files) in reds:
                    return {"returncode": 0, "passed": 4, "failed": 0, "errors": [], "output": "green"}
                reds.add(tuple(files))
                return {"returncode": 1, "passed": 0, "failed": 1, "errors": [],
                    "failures": ['expect(locator).toBeVisible() failed'], "output": "red"}

            def build(self):
                return {"returncode": 0, "output": "built"}

        def run_agent(model, instructions, task, workspace, gate, max_turns, console, label,
                feedback=None, feedback_filter=None, stall_limit=None):
            contexts.append(deepcopy(task))
            return {"turns": 1, "checks": {"typecheck": {"returncode": 0},
                "tests": {"returncode": 0, "passed": 4, "failed": 0, "errors": [], "output": "green"}}}

        with patch.object(ui_implementation, "install_dependencies", lambda output: None), \
                patch.object(ui_implementation, "FrontendChecks", FakeChecks), \
                patch.object(ui_implementation, "run_file_agent", run_agent), \
                patch.object(ui_implementation, "SourceFiles", lambda output, design: SimpleNamespace(
                    workspace=lambda **options: SimpleNamespace(files=set(), writable=set()),
                    feedback=lambda workspace, value: value)):
            report = ui_implementation.implement_ui(self.arc, self.design, Mock(), Mock(), self.report,
                max_turns=2, images=images(), tree=self.tree, input_dir=self.input_dir)

        self.assertEqual(report["status"], "GREEN")
        # The image is served from the public directory the dev server exposes.
        self.assertEqual((self.output / "frontend/public/media/IMG-HOME.png").read_bytes(), b"PNG-BYTES")
        register_suite = (self.output / "frontend/tests/ui/register.spec.ts").read_text(encoding="utf-8")
        self.assertIn('page.getByRole("link", { name: "Login", exact: true }).first()', register_suite)
        self.assertIn('toHaveAttribute("href", "/login")', register_suite)
        self.assertIn('page.getByRole("button", { name: "Search", exact: true }).first()', register_suite)
        self.assertIn("naturalWidth", (self.output / "frontend/tests/ui/home.spec.ts").read_text(encoding="utf-8"))
        # The report names the controls the page owes, inherited ones included.
        expected = {control["id"] for control in required_controls(self.tree, requirement_controls(self.tree), "REQ-A")}
        self.assertEqual(set(report["pages"]["register"]["required_controls"]), expected)
        self.assertEqual(len(contexts), 3)
        offered = {control["name"]: control for control in contexts[1]["required_controls"]}
        self.assertTrue(offered["Login"]["inherited"])
        self.assertEqual(offered["Login"]["element"], "UI-REGISTER-LOGIN")
        self.assertFalse(offered["Next step"]["inherited"])
        self.assertEqual(offered["Next step"]["required_by"], "REQ-A")


class UiImageBinding(unittest.TestCase):
    """A supplied image reaches the page as a served URL, and a guessed ID is rejected."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.arc = Path(temporary.name) / ".arc"
        self.tree = RequirementTree({"id": "ROOT", "name": "Gallery",
            "description": "The module presents a shared header on every page.", "scenarios": [],
            "children": [{"id": "REQ-A", "name": "Gallery",
                "description": "The gallery page /gallery shows the supplied layout image.",
                "scenarios": [{"id": "SCN-REQ-A", "name": "Scenario", "steps": [{"id": "STEP-REQ-A",
                    "Given": "A visitor", "When": "Open /gallery", "Then": "The layout is shown"}]}]}]})
        self.images = {"IMG-HOME": {"id": "IMG-HOME", "reference": "home.png",
            "url": "/media/IMG-HOME.png", "page": "gallery"}}
        self.contexts = {"REQ-A": {**self.tree.requirements["REQ-A"],
            "steps": [self.tree.steps["STEP-REQ-A"]], "images": self.images}}

    def proposal(self, image="IMG-HOME"):
        return {"pages": {"gallery": {"name": "Gallery", "route": "/gallery", "root": "UI-GALLERY",
                "images": [image], "fixtures": {"default": {}}}},
            "elements": {"UI-GALLERY": node("page", None),
                "UI-GALLERY-ART": node("image", "UI-GALLERY", image=image, alt="Gallery layout")},
            "bindings": {}, "requirement_ui": ["UI-GALLERY", "UI-GALLERY-ART"],
            "step_ui": {"STEP-REQ-A": []}}

    def test_designed_image_receives_the_served_url_and_owns_its_page(self):
        requests = []

        def generate(instructions, payload):
            requests.append(deepcopy(payload))
            return self.proposal()

        design = synthesize_ui(self.arc, self.tree, self.images, self.contexts,
            Mock(generate_json=generate), Mock(), max_attempts=2)
        self.assertEqual(requests[0]["requirement"]["images"], self.images)
        self.assertEqual(design["elements"]["UI-GALLERY-ART"]["prop"]["src"], "/media/IMG-HOME.png")
        self.assertEqual(design["pages"]["gallery"]["images"], ["IMG-HOME"])
        self.assertEqual(design["requirements"]["REQ-A"], ["UI-GALLERY", "UI-GALLERY-ART"])

    def test_guessed_image_is_repaired_with_the_supplied_identifier(self):
        requests = []

        def generate(instructions, payload):
            requests.append(deepcopy(payload))
            return self.proposal("IMG-MISSING" if len(requests) == 1 else "IMG-HOME")

        design = synthesize_ui(self.arc, self.tree, self.images, self.contexts,
            Mock(generate_json=generate), Mock(), max_attempts=2)
        self.assertEqual(len(requests), 2)
        self.assertIn("IMG-HOME", json.dumps(requests[1]["correction"], ensure_ascii=False))
        self.assertEqual(design["elements"]["UI-GALLERY-ART"]["prop"]["src"], "/media/IMG-HOME.png")


if __name__ == "__main__":
    unittest.main()
