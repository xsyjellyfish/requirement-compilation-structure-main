"""API route parameters keep Express registration and browser requests consistent."""

import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, quote, urlparse

from infrastructure.shared.api.paths import PARAMETER_PATTERN, express_path
from infrastructure.stages.backend.runtime import register_source
from infrastructure.stages.frontend.triggers.runtime import api_client_source


def api(path: str, method: str = "GET") -> dict:
    """Build the API IR member that path handling depends on."""
    return {"signature": {"name": "handle", "method": method, "path": path}}


def book_backend(path: str = "/api/books/{title}") -> dict:
    return {"apis": {"API-BOOK": api(path), "API-PAGE": api("/api/books/{bookTitle}/pages/{pageTitle}")}}


CLIENT_HARNESS = '''
import { readFileSync } from "node:fs";

globalThis.window = { location: { origin: "http://localhost:3000" } };
const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push({
    url: String(url), method: init.method, credentials: init.credentials,
    body: init.body === undefined ? null : init.body,
    headers: Object.fromEntries(new Headers(init.headers).entries()),
  });
  return { status: 200, json: async () => ({ ok: true }), headers: new Headers({ "content-type": "application/json" }) };
};
const client = await import(new URL("./ui-api.ts", import.meta.url).href);
const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const results = [];
for (const item of cases) {
  const start = calls.length;
  let entry;
  try {
    const value = item.call ? await client.callApi(item.call, item.request ?? {}) : client.apiPath(item.path, item.params ?? {});
    entry = { ok: true, value };
  } catch (error) {
    entry = { ok: false, error: String(error && error.message ? error.message : error) };
  }
  entry.requests = calls.slice(start);
  results.push(entry);
}
console.log(JSON.stringify({ results }));
'''


class RoutePathTests(unittest.TestCase):
    def test_braced_parameter_becomes_an_express_parameter(self):
        self.assertEqual(express_path("/api/books/{title}"), "/api/books/:title")

    def test_every_braced_parameter_of_a_multi_parameter_path_is_converted(self):
        self.assertEqual(express_path("/api/books/{bookTitle}/pages/{pageTitle}"),
            "/api/books/:bookTitle/pages/:pageTitle")

    def test_express_and_mixed_spellings_are_supported(self):
        self.assertEqual(express_path("/api/books/:title"), "/api/books/:title")
        self.assertEqual(express_path("/api/books/{bookTitle}/pages/:pageTitle"),
            "/api/books/:bookTitle/pages/:pageTitle")
        self.assertEqual(express_path("/api/notes/count"), "/api/notes/count")

    def test_unusable_parameters_and_relative_paths_are_rejected(self):
        for path in ("/api/books/{1bad}", "/api/books/{}", "/api/books/{title", "api/books/{title}", "/api/books/:"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                express_path(path)


class RegisterSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def test_registration_converts_a_braced_parameter(self):
        source = register_source({"API-BOOK": api("/api/books/{title}", "GET")}, self.output)
        self.assertIn('app.get("/api/books/:title"', source)
        self.assertNotIn("{title}", source)

    def test_registration_converts_multiple_parameters(self):
        source = register_source({"API-PAGE": api("/api/books/{bookTitle}/pages/{pageTitle}", "PUT")}, self.output)
        self.assertIn('app.put("/api/books/:bookTitle/pages/:pageTitle"', source)
        self.assertNotIn("{bookTitle}", source)
        self.assertNotIn("{pageTitle}", source)

    def test_registration_keeps_express_parameters_and_static_routes(self):
        source = register_source({"API-BOOK": api("/api/books/:title"), "API-NOTES": api("/api/notes/count", "POST")}, self.output)
        self.assertIn('app.get("/api/books/:title"', source)
        self.assertIn('app.post("/api/notes/count"', source)

    def test_registration_rejects_an_unusable_parameter_instead_of_routing_a_literal(self):
        with self.assertRaisesRegex(ValueError, "API-BAD"):
            register_source({"API-BAD": api("/api/books/{1bad}")}, self.output)


class FrontendClientTests(unittest.TestCase):
    """Execute the emitted TypeScript client so URL building is observed, not assumed."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def run_client(self, backend: dict, cases: list) -> list:
        (self.directory / "ui-api.ts").write_text(api_client_source(backend), encoding="utf-8")
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
        return json.loads(result.stdout)["results"]

    def request_url(self, result: dict):
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["requests"]), 1, result)
        return urlparse(result["requests"][0]["url"])

    def test_book_title_with_a_space_is_encoded(self):
        result = self.run_client(book_backend(), [{"call": "API-BOOK", "request": {"params": {"title": "My Book"}}}])[0]
        url = self.request_url(result)
        self.assertEqual(url.path, "/api/books/My%20Book")
        self.assertEqual(url.query, "")
        self.assertEqual(result["value"]["status"], 200)

    def test_parameters_with_chinese_and_special_characters_are_encoded(self):
        title = "深入 React/C++ & 50%? #1"
        result = self.run_client(book_backend(), [{"call": "API-BOOK", "request": {"params": {"title": title}}}])[0]
        url = self.request_url(result)
        self.assertEqual(url.path, "/api/books/" + quote(title, safe=""))
        self.assertEqual(url.query, "")
        self.assertEqual(url.fragment, "")
        self.assertNotIn("{", url.path)

    def test_multiple_parameters_are_replaced_positionally(self):
        result = self.run_client(book_backend(), [{"call": "API-PAGE",
            "request": {"params": {"bookTitle": "My Book", "pageTitle": "Page 1"}}}])[0]
        self.assertEqual(self.request_url(result).path, "/api/books/My%20Book/pages/Page%201")

    def test_express_style_paths_are_replaced_the_same_way(self):
        backend = book_backend("/api/books/:title")
        cases = [{"path": "/api/books/{title}", "params": {"title": "A B"}},
            {"path": "/api/books/:title", "params": {"title": "A B"}},
            {"call": "API-BOOK", "request": {"params": {"title": "A B"}}}]
        results = self.run_client(backend, cases)
        self.assertEqual(results[0]["value"], "/api/books/A%20B")
        self.assertEqual(results[1]["value"], "/api/books/A%20B")
        self.assertEqual(self.request_url(results[2]).path, "/api/books/A%20B")

    def test_a_missing_parameter_fails_without_sending_a_request(self):
        results = self.run_client(book_backend(), [
            {"call": "API-BOOK", "request": {}},
            {"call": "API-PAGE", "request": {"params": {"bookTitle": "Known"}}}])
        self.assertFalse(results[0]["ok"])
        self.assertIn("Missing route parameter: title", results[0]["error"])
        self.assertEqual(results[0]["requests"], [])
        self.assertFalse(results[1]["ok"])
        self.assertIn("Missing route parameter: pageTitle", results[1]["error"])
        self.assertEqual(results[1]["requests"], [])

    def test_an_unknown_placeholder_fails_without_sending_a_request(self):
        result = self.run_client(book_backend(), [{"path": "/api/books/{1bad}", "params": {}}])[0]
        self.assertFalse(result["ok"])
        self.assertIn("Unresolved route parameter", result["error"])
        self.assertEqual(result["requests"], [])

    def test_apis_without_parameters_keep_method_query_body_and_headers(self):
        backend = {"apis": {"API-NOTES": api("/api/notes"), "API-SAVE": api("/api/notes", "POST")}}
        results = self.run_client(backend, [
            {"call": "API-NOTES", "request": {"query": {"tag": ["a", "b"], "skipped": None},
                "headers": {"x-trace": "t1", "cookie": "secret"}, "credentials": "include"}},
            {"call": "API-SAVE", "request": {"body": {"text": "hi"}, "headers": {"x-trace": "t2"}}}])
        read_url, write_url = urlparse(results[0]["requests"][0]["url"]), urlparse(results[1]["requests"][0]["url"])
        read, write = results[0]["requests"][0], results[1]["requests"][0]
        self.assertEqual(read_url.path, "/api/notes")
        self.assertEqual(parse_qs(read_url.query), {"tag": ["a", "b"]})
        self.assertEqual(read["method"], "GET")
        self.assertEqual(read["credentials"], "include")
        self.assertIsNone(read["body"])
        self.assertEqual(read["headers"].get("x-trace"), "t1")
        self.assertNotIn("cookie", read["headers"])
        self.assertEqual(write_url.path, "/api/notes")
        self.assertEqual(write["method"], "POST")
        self.assertEqual(json.loads(write["body"]), {"text": "hi"})
        self.assertEqual(write["headers"].get("content-type"), "application/json")
        self.assertEqual(write["credentials"], "same-origin")

    def test_parameters_and_query_are_combined(self):
        result = self.run_client(book_backend(), [{"call": "API-BOOK",
            "request": {"params": {"title": "My Book"}, "query": {"version": "2"}}}])[0]
        url = self.request_url(result)
        self.assertEqual(url.path, "/api/books/My%20Book")
        self.assertEqual(parse_qs(url.query), {"version": ["2"]})

    def test_the_emitted_client_accepts_the_compiler_parameter_syntax(self):
        self.assertIn(PARAMETER_PATTERN, api_client_source(book_backend()))


if __name__ == "__main__":
    unittest.main()
