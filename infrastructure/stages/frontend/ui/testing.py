"""Deterministic UI existence suites and bounded browser/typecheck commands."""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile

from infrastructure.stages.frontend.ui.design import page_elements, visible
from infrastructure.stages.frontend.routing import sample_route
from infrastructure.utils.processes import capture_process, npm_command

KEY_TYPES = {"page", "form", "input", "checkbox", "select", "button", "heading", "link", "list", "image", "alert", "table", "text"}

#: Designed element types whose role a reviewer's locator depends on. A control that only
#: carries the test ID and the right text is not reachable by ``getByRole``, so the display
#: suite asserts the role here instead of letting the acceptance journey discover it later.
ROLE_TYPES = {"button": "button", "link": "link", "checkbox": "checkbox", "heading": "heading",
    "list": "list"}
#: Input types a reviewer fills by label, so the rendered control must expose the textbox role.
TEXT_INPUT_TYPES = {"text", "email", "search", "tel", "url"}


def presentation_run_status(result):
    """Repair existing page runtime failures; only assertion failures establish valid RED."""
    if result["returncode"] == 0:
        require_green(result)
        return "GREEN"
    headlines = [message.split("\n", 1)[0] for message in result.get("failures", [])]
    if not result.get("errors") and not result.get("skipped") and not result.get("flaky") and result.get("failed") and any("UI_RUNTIME_ERROR" in message or "UI_BUSINESS_CALL" in message for message in headlines):
        return "REPAIR_REQUIRED"
    require_red(result)
    return "RED_CONFIRMED"


def test_path(pid: str) -> str:
    """Return a stable, filesystem-safe Playwright spec path for a page ID."""
    return f"frontend/tests/ui/{pid}.spec.ts"


def control_assertions(design: dict, control: dict, values: dict) -> list[str]:
    """Render the browser checks that prove one required control is really reachable.

    An external reviewer finds a control through its role and accessible name, so the suite uses
    the same locator rather than the internal test ID: a link rendered as a span, a button whose
    name gained extra text or a link pointed at another href then fails here instead of passing as
    an invisible or renamed copy.
    """
    role, name = control.get("role"), control.get("name")
    uid = control.get("element")
    if role not in {"link", "button", "checkbox"} or not isinstance(name, str) or not name or not uid:
        return []
    locator = (f"page.getByRole({json.dumps(role)}, {{ name: {json.dumps(name)}, exact: true }})"
        ".first()")
    shown = visible(design, uid, values)
    lines = [f"  await expect({locator}){'' if shown else '.not'}.toBeVisible();"]
    if shown and control.get("href"):
        lines.append(f'  await expect({locator}).toHaveAttribute("href", {json.dumps(control["href"])});')
    if shown and role == "checkbox":
        lines.append(f'  await expect({locator}).toHaveAttribute("type", "checkbox");')
    return lines


def image_assertions(node: dict) -> list[str]:
    """Render the checks that prove a designed reference image is actually served.

    A broken source still renders an <img>, so the suite waits for the browser to decode it:
    a missing static asset fails here instead of surviving as an empty frame.
    """
    source = (node.get("prop") or {}).get("src")
    if not isinstance(source, str) or not source.startswith("/"):
        return []
    return [f'  await expect(page.locator("img[src={json.dumps(source)}]").first()).toBeVisible();',
        f'  await expect.poll(async () => page.locator("img[src={json.dumps(source)}]").first()'
        '.evaluate(node => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);']


def existence_suite(design: dict, pid: str, required: list[dict] | None = None) -> str:
    """Render deterministic presence tests for key visible UI elements across the page's designed display fixtures.

    ``required`` carries the controls the requirement wording demands on this page, including the
    ones inherited from a parent requirement, so the browser verifies the same role, name and href
    an external reviewer would use.
    """
    page = design["pages"][pid]
    elements = page_elements(design, pid)
    defaults = {var: binding["initial"] for var, binding in design["bindings"].items()}
    containers = {node.get("parent") for node in elements.values() if node.get("parent")}
    lines = ['// Compiler-owned existence assertions, generated from the UI/data design.',
        'import { test, expect } from "@playwright/test";', '']
    for name, fixture in page["fixtures"].items():
        values = defaults | fixture
        lines.append(f'test({json.dumps(pid + " / " + name + " / UI element existence")}, async ({{ page }}) => {{')
        lines.extend([
            '  const runtimeErrors: string[] = [];',
            '  page.on("pageerror", error => runtimeErrors.push(error.message));',
            '  await page.addInitScript(fixture => { (window as unknown as { __ARC_UI_FIXTURE__: unknown }).__ARC_UI_FIXTURE__ = fixture; }, '
                + json.dumps({"page": pid, "data": fixture}, ensure_ascii=False) + ');',
            f'  await page.goto({json.dumps(sample_route(page["route"]))});',
            '  if (runtimeErrors.length) throw new Error("UI_RUNTIME_ERROR: " + runtimeErrors.join("\\n"));',
            '  try {',
        ])
        for uid, node in elements.items():
            kind = node.get("type", "page" if uid == page["root"] else "container")
            if kind not in KEY_TYPES:
                continue
            locator = f"page.getByTestId({json.dumps(uid)})"
            shown = visible(design, uid, values)
            lines.append(f"  await expect({locator}){'' if shown else '.not'}.toBeVisible();")
            if not shown:
                continue
            prop = node.get("prop", {})
            if kind in {"input", "checkbox", "select"}:
                if prop.get("label"):
                    lines.append(f'  await expect({locator}).toHaveAccessibleName({json.dumps(prop["label"])});')
                if "placeholder" in prop:
                    lines.append(f'  await expect({locator}).toHaveAttribute("placeholder", {json.dumps(prop["placeholder"])});')
                if kind in {"input", "checkbox"}:
                    input_type = "checkbox" if kind == "checkbox" else prop.get("input_type", "text")
                    lines.append(f'  await expect({locator}).toHaveAttribute("type", {json.dumps(input_type)});')
                if kind == "select":
                    for option in prop.get("options", []):
                        lines.append(f'  await expect({locator}.getByRole("option", {{ name: {json.dumps(option)}, exact: true }})).toHaveCount(1);')
                if node.get("data"):
                    initial = values[node["data"][0]]

                    if kind == "checkbox" and isinstance(initial, bool):
                        lines.append(
                            f"  await expect({locator})"
                            f"{'' if initial else '.not'}.toBeChecked();"
                        )

                    elif kind == "input" and prop.get("input_type") == "radio":
                        option_value = prop.get("value", prop.get("label"))
                        if option_value is not None:
                            modifier = "" if str(initial) == str(option_value) else ".not"
                            lines.append(
                                f"  await expect({locator}){modifier}.toBeChecked();"
                            )

                    elif kind in {"input", "select"} and isinstance(initial, (str, int, float)) and not isinstance(initial, bool):
                        lines.append(
                            f'  await expect({locator}).toHaveValue({json.dumps(str(initial))});'
                        )
            if prop.get("text") and kind in {"heading", "text", "button", "link", "alert"}:
                lines.append(f'  await expect({locator}).toContainText({json.dumps(prop["text"])});')
            # An external reviewer locates a control by role and accessible name, so the exact
            # name is asserted here: a button that concatenates extra text is a UI defect that
            # must be repaired now, not worked around in a later test.
            if not (kind in {"input", "checkbox", "select"} and prop.get("label")):
                name = None
                if isinstance(prop.get("name_from"), str):
                    declared = values.get(prop["name_from"])
                    name = declared if isinstance(declared, str) and declared else None
                elif kind in {"button", "link"} and uid not in containers:
                    name = prop.get("label") or prop.get("text")
                if isinstance(name, str) and name:
                    lines.append(f"  await expect({locator}).toHaveAccessibleName({json.dumps(name)});")
            role = ROLE_TYPES.get(kind)
            if not role and kind == "input" and prop.get("input_type", "text") in TEXT_INPUT_TYPES:
                # A journey types into a field by its label, so a text field rendered as a
                # styled div with the right test ID has a name but no addressable role.
                role = "textbox"
            if role:
                lines.append(f'  await expect({locator}).toHaveRole({json.dumps(role)});')
            if kind == "image":
                lines.extend(image_assertions(node))
            if kind == "alert":
                lines.append(f'  await expect({locator}).toHaveAttribute("role", "alert");')
            if kind in {"text", "heading", "alert"}:
                for var in node["data"]:
                    value = values[var]
                    if isinstance(value, str) and value:
                        lines.append(f'  await expect({locator}).toContainText({json.dumps(value)});')
        for control in required or []:
            lines.extend(control_assertions(design, control, values))
        lines.extend([
            '  } catch (error) {',
            '    if (runtimeErrors.length) throw new Error("UI_RUNTIME_ERROR: " + runtimeErrors.join("\\n"));',
            '    throw error;',
            '  }',
            '  if (runtimeErrors.length) throw new Error("UI_RUNTIME_ERROR: " + runtimeErrors.join("\\n"));',
            '});', '',
        ])
    return "\n".join(lines)


def require_green(result: dict) -> None:
    """Require passing actual browser assertions with no reported runtime errors."""
    if result["returncode"] or result.get("errors") or not result.get("passed") or result.get("failed"):
        raise ValueError(f"UI checks are not GREEN: {result}")


def require_red(result: dict) -> None:
    """Require missing-component assertion failures rather than browser, server, or application runtime failures."""
    errors = result.get("failures", [])
    excluded = ("UI_RUNTIME_ERROR", "UI_BUSINESS_CALL", "ERR_", "browserType.launch", "page.goto:", "SyntaxError", "ReferenceError", "Cannot find module")
    # Code frames can contain the literal runtime-guard text from adjacent lines;
    # classify the actual error headline rather than those source snippets.
    headlines = [re.sub(r"\x1b\[[0-9;]*m", "", message).split("\n", 1)[0] for message in errors]
    if not result["returncode"] or not result.get("failed") or result.get("errors") or not errors or any(not re.search(r"expect\(|AssertionError", message) or any(word in message for word in excluded) for message in headlines):
        raise ValueError(f"Expected missing-component assertion RED, not infrastructure/runtime failure: {result}")


def parse_playwright_result(result: dict, include_files: bool = False) -> dict:
    """Parse the Playwright JSON reporter into counts, failures, runtime errors, and optional per-file totals."""
    try:
        report = json.loads(result.pop("stdout"))
    except (ValueError, KeyError) as error:
        raise ValueError(f"Playwright did not return its JSON report: {result['output']}") from error
    failures = []
    files = {}
    #: Per-spec failures, so a final full run can replace an earlier per-requirement GREEN
    #: instead of leaving a requirement accepted that the last run saw fail.
    failed_files = {}

    def visit(suite):
        """Accumulate nested Playwright suite/spec results into the normalized browser-check report."""
        for spec in suite.get("specs", []):
            filename = spec.get("file", suite.get("file", ""))
            files[filename] = files.get(filename, 0) + len(spec.get("tests", []))
            for test in spec.get("tests", []):
                for run in test.get("results", []):
                    if run.get("status") not in {"passed", "skipped"}:
                        failed_files[filename] = failed_files.get(filename, 0) + 1
                        failures.append("\n".join(item.get("message", "") for item in run.get("errors", [])))
        for child in suite.get("suites", []):
            visit(child)

    for suite in report.get("suites", []):
        visit(suite)
    stats = report.get("stats", {})
    if isinstance(stats.get("duration"), (int, float)):
        result["test_seconds"] = stats["duration"] / 1000
    result.update({"passed": stats.get("expected", 0), "failed": stats.get("unexpected", 0),
        "skipped": stats.get("skipped", 0), "flaky": stats.get("flaky", 0),
        "errors": report.get("errors", []), "failures": failures})
    if include_files:
        result["files"] = files
        result["files_failed"] = failed_files
    result["output"] = json.dumps({key: value for key, value in result.items() if key != "output"}, ensure_ascii=False, indent=2)[-24000:]
    return result


class FrontendChecks:
    """Compiler-owned browser installation, TypeScript, Playwright, and workspace build commands."""
    def __init__(self, output: Path, timeout: int = 600) -> None:
        """Bind browser/build checks to the generated workspace and locate Node.js and npm."""
        self.output, self.timeout = output.resolve(), timeout
        self.node = shutil.which("node")
        if not self.node:
            raise ValueError("Node.js is required for UI checks")
        self.node = str(Path(self.node).resolve())

    def execute(self, args: list[str], timeout: int | None = None, env: dict | None = None, executable: str | None = None) -> dict:
        """Run a fixed check command, capture its output, and stop its process group on timeout or interruption."""
        try:
            return capture_process([executable or self.node, *args], cwd=self.output,
                timeout=timeout or self.timeout, env=env)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"Frontend check timed out after {timeout or self.timeout}s") from error

    def install_browser(self) -> None:
        """Install the Chromium browser used by the generated Playwright tests."""
        result = self.execute(["node_modules/@playwright/test/cli.js", "install", "chromium"], timeout=600)
        print(result["output"], flush=True)
        if result["returncode"]:
            raise ValueError("Playwright Chromium installation failed; install its OS libraries if reported above")

    def typecheck(self, files: list[str], *, test_roots=("frontend/tests/ui",), source_files=("src/**/*.ts", "src/**/*.tsx")) -> dict:
        """Compile selected frontend/test roots with a temporary TypeScript configuration, then remove that configuration."""
        includes = list(source_files)
        for relative in files:
            path = (self.output / relative).resolve()
            if not any(path.is_relative_to(self.output / root) for root in test_roots):
                raise ValueError("Typecheck only the supplied frontend test roots")
            includes.append(path.relative_to(self.output / "frontend").as_posix())
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", suffix=".json", prefix=".tsconfig-ui-", dir=self.output / "frontend", delete=False) as config:
            path = Path(config.name)
            json.dump({"extends": "./tsconfig.json", "compilerOptions": {"types": ["vite/client", "node"], "forceConsistentCasingInFileNames": True}, "include": includes}, config)
        try:
            result = self.execute(["node_modules/typescript/bin/tsc", "--noEmit", "-p", str(path)])
            result.pop("stdout", None)
            return result
        finally:
            path.unlink(missing_ok=True)

    def tests(self, files: list[str]) -> dict:
        """Run selected UI-existence specs with Playwright and normalize the reporter output."""
        if not files:
            raise ValueError("Do not run an empty UI suite")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {**os.environ, "ARC_UI_PORT": str(port)}
        result = self.execute(["node_modules/@playwright/test/cli.js", "test", "--config", "frontend/playwright.ui.config.ts", *files], env=env)
        return parse_playwright_result(result)

    def build(self) -> dict:
        """Run the generated workspace's frontend and backend build command and return the captured result."""
        command = npm_command()
        return self.execute([*command[1:], "run", "build"], executable=command[0])
