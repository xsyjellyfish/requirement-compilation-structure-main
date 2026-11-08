"""Dynamic page routes match, navigate and survive refresh in both frontend stages."""

import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import quote

from infrastructure.shared.parameters import PARAMETER_PATTERN
from infrastructure.shared.project.entrypoints import remember_path
from infrastructure.stages.frontend.routing import router_source, sample_route, sync_router
from infrastructure.stages.frontend.triggers.runtime import sync_triggers
from infrastructure.stages.frontend.ui.runtime import UI_RUNTIME, sync_pages
from infrastructure.stages.frontend.ui.testing import existence_suite
from infrastructure.utils.files import read_source, save_text


def element(kind="container", parent=None):
    """Build the minimal element record the emitters and presence suites read."""
    return {"type": kind, "parent": parent, "data": [], "mode": [], "prop": {}}


def ui_design() -> dict:
    """One static route plus dynamic routes with one and two parameters."""
    return {"pages": {
            "home": {"name": "Home", "route": "/", "root": "UI-home", "images": [], "fixtures": {"default": {}}},
            "book": {"name": "Book", "route": "/books/:bookName", "root": "UI-book", "images": [],
                "fixtures": {"default": {}}},
            "editor": {"name": "New book", "route": "/books/new", "root": "UI-editor", "images": [],
                "fixtures": {"default": {}}},
            "chapter": {"name": "Chapter", "route": "/books/{bookName}/pages/:pageNumber", "root": "UI-chapter",
                "images": [], "fixtures": {"default": {}}}},
        "elements": {"UI-home": element("page"), "UI-book": element("page"), "UI-editor": element("page"),
            "UI-chapter": element("page"), "UI-book-title": element("heading", "UI-book")},
        "bindings": {}}


ROUTES = [{"id": "home", "route": "/"}, {"id": "book", "route": "/books/:bookName"},
    {"id": "editor", "route": "/books/new"}, {"id": "chapter", "route": "/books/{bookName}/pages/:pageNumber"}]


ROUTER_HARNESS = '''
import { readFileSync } from "node:fs";

const listeners = new Set();
let stack = ["/"];
let index = 0;

globalThis.window = {
  location: { get pathname() { return stack[index]; } },
  history: {
    pushState(_state, _title, url) {
      stack = stack.slice(0, index + 1);
      stack.push(url);
      index = stack.length - 1;
    },
    back() { if (index > 0) { index -= 1; for (const listener of listeners) listener({}); } },
    forward() { if (index < stack.length - 1) { index += 1; for (const listener of listeners) listener({}); } },
  },
  addEventListener(type, listener) { if (type === "popstate") listeners.add(listener); },
  removeEventListener(type, listener) { if (type === "popstate") listeners.delete(listener); },
};

const router = await import(new URL("./ui-router.ts", import.meta.url).href);
const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));

// Mirrors App: initialLocation() on load, routeView/mountKey per render, pushed/popped on history.
function session(routes, path, steps) {
  stack = [path];
  index = 0;
  let location = router.initialLocation();
  let entered = "";
  const renders = [];
  const render = () => {
    const view = router.routeView(routes, location.path);
    const key = router.mountKey(location, view);
    renders.push({ pageId: view.pageId, params: view.params, path: view.path, key,
      mount: key !== entered && view.page !== null });
    entered = key;
  };
  const navigate = (id, params) => {
    const page = routes.find(item => item.id === id);
    if (!page) throw new Error(`Unknown page: ${id}`);
    const target = router.buildRoute(page.route, params ?? {});
    globalThis.window.history.pushState({}, "", target);
    location = router.pushed(location, target);
    render();
  };
  const pop = () => { location = router.popped(location); render(); };
  globalThis.window.addEventListener("popstate", pop);
  render();
  for (const step of steps) {
    if (step.navigate) navigate(step.navigate[0], step.navigate[1]);
    if (step.back) globalThis.window.history.back();
    if (step.forward) globalThis.window.history.forward();
    if (step.refresh) { location = router.initialLocation(); entered = ""; render(); }
  }
  globalThis.window.removeEventListener("popstate", pop);
  return { renders, path: location.path };
}

const results = [];
for (const item of cases) {
  try {
    if (item.op === "match") results.push({ ok: true, value: router.matchRoute(item.route, item.path) });
    else if (item.op === "build") results.push({ ok: true, value: router.buildRoute(item.route, item.params ?? {}) });
    else if (item.op === "parameters") results.push({ ok: true, value: router.routeParameters(item.route) });
    else if (item.op === "view") results.push({ ok: true, value: router.routeView(item.routes, item.path) });
    else if (item.op === "session") results.push({ ok: true, value: session(item.routes, item.start, item.steps ?? []) });
    else results.push({ ok: false, error: `unknown operation: ${item.op}` });
  } catch (error) {
    results.push({ ok: false, error: String(error && error.message ? error.message : error) });
  }
}
console.log(JSON.stringify({ results }));
'''


class RouterRuntimeTests(unittest.TestCase):
    """Execute the emitted router so matching and navigation are observed, not assumed."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def run_cases(self, cases: list) -> list:
        (self.directory / "ui-router.ts").write_text(router_source(), encoding="utf-8")
        (self.directory / "harness.mjs").write_text(ROUTER_HARNESS, encoding="utf-8")
        (self.directory / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to execute the emitted router")
        arguments = [str(self.directory / "harness.mjs"), str(self.directory / "cases.json")]
        result = subprocess.run([node, "--experimental-strip-types", *arguments], cwd=self.directory,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        if result.returncode and "bad option" in result.stderr:
            result = subprocess.run([node, *arguments], cwd=self.directory,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["results"]

    def one(self, cases: list) -> dict:
        results = self.run_cases(cases)
        self.assertEqual(len(results), 1, results)
        return results[0]


    def test_encoded_path_parameters_are_matched_and_decoded(self):
        result = self.one([{"op": "match", "route": "/books/:bookName", "path": "/books/My%20Book"}])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"], {"bookName": "My Book"})

    def test_chinese_and_special_characters_are_decoded(self):
        result = self.one([{"op": "match", "route": "/books/:bookName",
            "path": "/books/" + quote("深入 React/C++ & 50% #1", safe="")}])
        self.assertEqual(result["value"], {"bookName": "深入 React/C++ & 50% #1"})

    def test_several_parameters_are_matched_positionally(self):
        result = self.one([{"op": "match", "route": "/books/{bookName}/pages/:pageNumber",
            "path": "/books/My%20Book/pages/2"}])
        self.assertEqual(result["value"], {"bookName": "My Book", "pageNumber": "2"})

    def test_static_routes_and_trailing_slashes_are_unaffected(self):
        results = self.run_cases([
            {"op": "match", "route": "/", "path": "/"},
            {"op": "match", "route": "/summary", "path": "/summary/"},
            {"op": "match", "route": "/books/:bookName", "path": "/books"},
            {"op": "match", "route": "/books/:bookName", "path": "/books/a/pages"},
            {"op": "match", "route": "/books/:bookName", "path": "/books/%E0%A4%A"}])
        self.assertEqual(results[0]["value"], {})
        self.assertEqual(results[1]["value"], {})
        self.assertIsNone(results[2]["value"])
        self.assertIsNone(results[3]["value"])
        self.assertIsNone(results[4]["value"])

    def test_a_static_route_wins_over_a_dynamic_neighbour(self):
        result = self.one([{"op": "view", "routes": ROUTES, "path": "/books/new"}])
        self.assertEqual(result["value"]["pageId"], "editor")
        self.assertEqual(result["value"]["params"], {})

    def test_route_view_decodes_the_selected_page(self):
        result = self.one([{"op": "view", "routes": ROUTES, "path": "/books/My%20Book"}])
        self.assertEqual(result["value"]["pageId"], "book")
        self.assertEqual(result["value"]["route"], "/books/:bookName")
        self.assertEqual(result["value"]["params"], {"bookName": "My Book"})
        self.assertEqual(result["value"]["key"], "/books/My%20Book")

    def test_building_a_route_encodes_parameters_in_both_spellings(self):
        results = self.run_cases([
            {"op": "build", "route": "/books/:bookName", "params": {"bookName": "My Book"}},
            {"op": "build", "route": "/books/{bookName}/pages/:pageNumber",
                "params": {"bookName": "深入", "pageNumber": "1"}},
            {"op": "parameters", "route": "/books/{bookName}/pages/:pageNumber"}])
        self.assertEqual(results[0]["value"], "/books/My%20Book")
        self.assertEqual(results[1]["value"], "/books/" + quote("深入", safe="") + "/pages/1")
        self.assertEqual(results[2]["value"], ["bookName", "pageNumber"])

    def test_missing_parameters_are_reported_instead_of_written_literally(self):
        results = self.run_cases([
            {"op": "build", "route": "/books/:bookName", "params": {}},
            {"op": "build", "route": "/books/{bookName}/pages/:pageNumber", "params": {"bookName": "Known"}},
            {"op": "build", "route": "/books/:bookName", "params": {"bookName": ""}}])
        for expectation, result in zip(("bookName", "pageNumber", "bookName"), results):
            self.assertFalse(result["ok"], result)
            self.assertIn(f"Missing route parameter: {expectation}", result["error"])

    def test_navigation_refresh_and_history_restore_the_route(self):
        result = self.one([{"op": "session", "routes": ROUTES, "start": "/",
            "steps": [{"navigate": ["book", {"bookName": "My Book"}]}, {"back": True},
                {"forward": True}, {"refresh": True}]}])
        self.assertTrue(result["ok"], result)
        renders = result["value"]["renders"]
        self.assertEqual([render["pageId"] for render in renders], ["home", "book", "home", "book", "book"])
        self.assertEqual([render["path"] for render in renders],
            ["/", "/books/My%20Book", "/", "/books/My%20Book", "/books/My%20Book"])
        self.assertEqual(renders[-1]["params"], {"bookName": "My Book"})
        self.assertTrue(all(render["mount"] for render in renders))

    def test_switching_books_reloads_the_page_with_the_new_parameters(self):
        result = self.one([{"op": "session", "routes": ROUTES, "start": "/books/First",
            "steps": [{"navigate": ["book", {"bookName": "Second Book"}]}]}])
        self.assertTrue(result["ok"], result)
        renders = result["value"]["renders"]
        self.assertEqual([render["params"] for render in renders],
            [{"bookName": "First"}, {"bookName": "Second Book"}])
        self.assertEqual(len({render["key"] for render in renders}), 2)
        self.assertTrue(all(render["mount"] for render in renders))

    def test_unknown_urls_resolve_to_no_page(self):
        result = self.one([{"op": "session", "routes": ROUTES, "start": "/books/a/pages/1/extra", "steps": []}])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"]["renders"], [{"pageId": "", "params": {},
            "path": "/books/a/pages/1/extra", "key": "0:/books/a/pages/1/extra", "mount": False}])


class RouteSamplingTests(unittest.TestCase):
    """Presence tests must visit a concrete URL, never a route template."""

    def test_sample_routes_fill_dynamic_segments(self):
        self.assertEqual(sample_route("/books/:bookName"), "/books/example")
        self.assertEqual(sample_route("/books/{bookName}/pages/:pageNumber", {"bookName": "My Book"}),
            "/books/My%20Book/pages/example")
        self.assertEqual(sample_route("/books/:bookName", {"bookName": "深入"}),
            "/books/" + quote("深入", safe=""))
        self.assertEqual(sample_route("/"), "/")

    def test_presence_tests_visit_a_concrete_dynamic_url(self):
        suite = existence_suite(ui_design(), "book")
        self.assertIn('await page.goto("/books/example")', suite)
        self.assertNotIn(":bookName", suite)


class EmittedRoutingTests(unittest.TestCase):
    """Both frontend stages must delegate routing to the same compiler-owned module."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)

    def presentation_app(self) -> str:
        save_text(self.output / "frontend/src/pages/book.tsx", "export function Page() { return <main />; }")
        remember_path(self.output, "page", "book", "frontend/src/pages/book.tsx")
        sync_pages(self.output, ui_design())
        return read_source(self.output / "frontend/src/App.tsx")

    def interaction_app(self) -> str:
        sync_triggers(self.output, ui_design(), {"triggers": {}}, {"apis": {}}, [])
        return read_source(self.output / "frontend/src/App.tsx")

    def test_presentation_stage_routes_through_the_shared_router(self):
        source = self.presentation_app()
        self.assertIn('from "./ui-router"', source)
        self.assertIn("routeView(pages, location.path)", source)
        self.assertIn("useState(initialLocation)", source)
        self.assertIn('window.addEventListener("popstate"', source)
        self.assertIn("switch (view.pageId)", source)
        self.assertIn("params={view.params}", source)
        self.assertIn("Page not found", source)
        self.assertNotIn("window.location.pathname", source)
        self.assertNotIn("localStorage", source)

    def test_interaction_stage_navigates_with_parameters(self):
        source = self.interaction_app()
        self.assertIn('from "./ui-router"', source)
        self.assertIn("const view = routeView(pages, location.path);", source)
        self.assertIn("const target = buildRoute(page.route, params);", source)
        self.assertIn("setLocation(current => pushed(current, target));", source)
        self.assertIn("const pop = () => { epoch.current += 1; setLocation(popped); };", source)
        self.assertIn("const key = mountKey(location, view);", source)
        self.assertIn("<Component key={view.key}", source)
        self.assertIn("params={view.params}", source)
        self.assertNotIn("page.route === route", source)
        self.assertNotIn("localStorage", source)

    def test_interaction_stage_exposes_parameters_to_triggers(self):
        source = self.interaction_app()
        self.assertIn("get params() { return view.params; }", source)
        self.assertIn("get route() { return view.path; }", source)
        self.assertIn("navigate: (id, params) =>", source)

    def test_the_ui_runtime_types_the_parameter_aware_navigate(self):
        self.assertIn("navigate(pageId: string, params?: UiRouteParams): void;", UI_RUNTIME)
        self.assertIn("readonly params: Readonly<UiRouteParams>;", UI_RUNTIME)
        self.assertIn("export type UiRouteParams = Record<string, string>;", UI_RUNTIME)

    def test_both_stages_emit_the_same_router(self):
        sync_pages(self.output, ui_design())
        presentation = read_source(self.output / "frontend/src/ui-router.ts")
        self.interaction_app()
        self.assertEqual(presentation, read_source(self.output / "frontend/src/ui-router.ts"))
        self.assertIn(PARAMETER_PATTERN, presentation)
        self.assertNotIn("__UI_ROUTE_PARAMETER__", presentation)


class RouterTypecheckTests(unittest.TestCase):
    """Compile the emitted router with the project's strict frontend settings."""

    CALLER = '''
import { buildRoute, currentPath, initialLocation, matchRoute, mountKey, normalizePath, popped, pushed,
  routeParameters, routeView } from "./ui-router";
import type { UiRouteParams, UiTriggerContext } from "./ui-runtime";
const routes = [{ id: "home", route: "/" }, { id: "book", route: "/books/:bookName" }];
const view = routeView(routes, currentPath());
const params: UiRouteParams = view.params;
const target: string = buildRoute("/books/:bookName", { bookName: params.bookName });
const key: string = mountKey(initialLocation(), view);
const moved: string = pushed(popped(initialLocation()), target).path;
const names: string[] = routeParameters("/books/:bookName/pages/{pageNumber}");
const matched: UiRouteParams | null = matchRoute("/books/:bookName", normalizePath("/books/x/"));
export type Navigate = UiTriggerContext["navigate"];
export const observed = [target, key, moved, names, matched];
'''

    def test_router_and_runtime_types_compile(self):
        root = Path(__file__).resolve().parents[1]
        compiler = next((candidate for candidate in (root / "node_modules/typescript/bin/tsc",
            root / "output/demo/node_modules/typescript/bin/tsc") if candidate.is_file()), None)
        node = shutil.which("node")
        if compiler is None or node is None:
            self.skipTest("An installed workspace TypeScript compiler is required for this check")
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        frontend = Path(temporary.name) / "frontend"
        sync_router(frontend.parent)
        (frontend / "src/ui-runtime.ts").write_text(UI_RUNTIME, encoding="utf-8")
        (frontend / "src/caller.ts").write_text(self.CALLER, encoding="utf-8")
        (frontend / "tsconfig.json").write_text(json.dumps({"compilerOptions": {
            "target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler", "strict": True,
            "noEmit": True, "skipLibCheck": True, "lib": ["ES2022", "DOM", "DOM.Iterable"]},
            "include": ["src"]}), encoding="utf-8")
        result = subprocess.run([node, str(compiler), "-p", str(frontend)], cwd=frontend.parent,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
