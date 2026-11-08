"""Execute actual backend compilation and tests without source/title policy gates."""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from infrastructure.utils.processes import capture_process, npm_command


class BackendChecks:
    """Compiler-owned TypeScript and Node test commands; generated code cannot choose shell commands."""
    def __init__(self, output: Path, timeout: int = 600) -> None:
        """Bind fixed backend check commands to the generated workspace and require an available Node.js executable."""
        self.output = output.resolve()
        self.timeout = timeout
        self.node = shutil.which("node")
        if self.node:
            self.node = str(Path(self.node).resolve())
        if not self.node or not (self.output / "node_modules/typescript/bin/tsc").is_file() or not (self.output / "node_modules/tsx").is_dir():
            raise ValueError("Node.js and workspace dev dependencies (typescript/tsx) are required for backend checks")

    def execute(self, argv: list[str], executable: str | None = None) -> dict:
        """Run a fixed Node command in the workspace and return its exit code and captured output."""
        try:
            result = capture_process([executable or self.node, *argv], cwd=self.output, timeout=self.timeout)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"Backend check timed out after {self.timeout}s") from error
        return {"returncode": result["returncode"], "output": result["output"]}

    def build(self) -> dict:
        """Build the backend, including publication of its SQL runtime assets."""
        command = npm_command()
        return self.execute([*command[1:], "run", "build", "--workspace", "@arc/backend"], executable=command[0])

    def typecheck(self, files: list[str] | None = None) -> dict:
        """Compile selected backend tests with TypeScript, using and cleaning a temporary test configuration."""
        if files is None:
            return self.execute(["node_modules/typescript/bin/tsc", "--noEmit", "-p", "backend/tsconfig.test.json"])
        # Imported API modules and helpers are included by TypeScript. Future
        # requirements' tests and the global registry must not enter this gate.
        includes = []
        for relative in dict.fromkeys(files):
            path = (self.output / relative).resolve()
            if not path.is_relative_to(self.output / "backend/tests"):
                raise ValueError(f"Test path is outside backend/tests: {relative}")
            includes.append(path.relative_to(self.output / "backend").as_posix())
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", suffix=".json", prefix=".tsconfig-tdd-", dir=self.output / "backend", delete=False) as config:
            temporary = Path(config.name)
            json.dump({"extends": "./tsconfig.test.json", "compilerOptions": {"forceConsistentCasingInFileNames": True}, "files": [], "include": includes}, config)
        try:
            return self.execute(["node_modules/typescript/bin/tsc", "--noEmit", "-p", str(temporary)])
        finally:
            temporary.unlink(missing_ok=True)

    def tests(self, files: list[str]) -> dict:
        """Run selected API test files with the fixed Node test command and parse its TAP counts."""
        if not files:
            return {"returncode": 0, "output": "No tests for this empty API set.", "tests": 0, "pass": 0, "fail": 0}
        result = self.execute(["--import", "tsx", "--test", "--test-reporter=tap", *files])
        for kind in ("tests", "pass", "fail", "skipped", "todo"):
            found = re.search(rf"^# {kind} (\d+)\s*$", result["output"], re.MULTILINE)
            result[kind] = int(found.group(1)) if found else 0
        return result


def require_green(report: dict) -> None:
    """Reject a test report unless the selected suite passed."""
    if report["returncode"] != 0 or not report.get("tests") or report.get("fail"):
        raise ValueError(f"Tests are not GREEN: {report}")


class RedRejected(ValueError):
    """RED was not proven, so the contract tests must not be treated as frozen evidence.

    ``category`` names the reason, and ``failures`` holds the failing tests that carried it:
    ``infrastructure`` (the suite could not run), ``database`` (SQLite refused the published
    table structure), ``vacuous`` (nothing failed), or ``runtime`` (a thrown error instead of
    a failed assertion).
    """

    def __init__(self, message: str, category: str, failures=()):
        super().__init__(message)
        self.category = category
        self.failures = list(failures)


#: A TAP failure block starts at ``not ok`` and repeats one line per failing test.
FAILURE = re.compile(r"^not ok \d+ - (?P<name>.*)$", re.MULTILINE)
CODE = re.compile(r"^\s+code: '(?P<code>[^']+)'", re.MULTILINE)
#: Failures of the test harness itself: the suite proved nothing about behavior.
INFRASTRUCTURE = (r"\bSyntaxError\b", r"ERR_MODULE_NOT_FOUND", r"Cannot find module",
    r"ERR_UNKNOWN_FILE_EXTENSION", r"ERR_DLOPEN_FAILED", r"ERR_INVALID_ARG_TYPE")
#: Failures only the pass that owns the SQL can fix, as SQLite reports them.
DATABASE = (r"non-deterministic use of", r"\bin a CHECK constraint\b", r"\bin a generated column\b")


def failure_blocks(output: str) -> list[dict]:
    """Split a TAP report into one record per failing test, with its error code and kind.

    A report without TAP blocks is treated as one block, so a shortened or crashed report
    is judged by the evidence it does contain instead of being accepted silently.
    """
    blocks, current = [], None
    for line in str(output).splitlines():
        match = FAILURE.match(line)
        if match:
            current = {"name": match.group("name").strip(), "lines": []}
            blocks.append(current)
        elif current is not None:
            current["lines"].append(line)
    if not blocks:
        blocks = [{"name": "(suite)", "lines": str(output).splitlines()}]
    for block in blocks:
        block["text"] = "\n".join(block.pop("lines"))
        found = CODE.search(block["text"])
        block["code"] = found.group("code") if found else ""
        block["kind"] = ("infrastructure" if matches(block["text"], INFRASTRUCTURE)
            else "database" if matches(block["text"], DATABASE)
            else "assertion" if block["code"] == "ERR_ASSERTION" or "AssertionError" in block["text"]
            else "runtime")
    return blocks


def matches(text: str, patterns) -> bool:
    """Report whether the text contains one of the case-insensitive evidence patterns."""
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def require_red(report: dict) -> None:
    """Require failing contract assertions and reject every other reason the suite failed.

    RED means the frozen tests disagree with the stub. A suite that already passes, holds no
    test, cannot be imported, is refused by SQLite, or throws instead of asserting proves
    something else. Accepting such a report freezes a test that never described the contract,
    and the implementation agent is then asked to repair a cause it cannot reach.
    """
    blocks = failure_blocks(report.get("output") or "")
    for category, summary in (
            ("infrastructure", "the suite could not run"),
            ("database", "SQLite refused the published table structure")):
        found = [block for block in blocks if block["kind"] == category]
        if found:
            raise RedRejected(f"Expected assertion-based RED, but {summary}: {found}\n{report}",
                category, found)
    if not report["returncode"] or not report.get("tests") or not report.get("fail"):
        raise RedRejected(f"Expected assertion-based RED, but the suite did not fail: {report}",
            "vacuous", blocks)
    found = [block for block in blocks if block["kind"] == "assertion"]
    if not found:
        raise RedRejected(f"Expected failing assertions, but the suite threw runtime errors: "
            f"{blocks}\n{report}", "runtime", blocks)
