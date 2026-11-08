"""Static rules that keep the acceptance suite a black-box test instead of a weaker replay.

A generated suite can look green and prove nothing: intercept the API and fulfill a canned
response, seed the "Given" rows with SQL, inject a display fixture, assert that an element
exists, or skip the case that would fail. This module reads every spec under
``frontend/tests/acceptance`` and rejects those shortcuts, then requires the capabilities a
real acceptance journey needs: user input, a click, a URL assertion, a reload, a database
side effect, an absence assertion and an independent browser context.

The rules are checked twice: before the first browser run (so a bad suite never wastes one)
and after every repair attempt (so a passing suite is still the right kind of suite).
"""

import re

from infrastructure.stages.frontend.acceptance.journey import SPEC_ROOT
from infrastructure.utils.files import read_source

FORBIDDEN = (
    (r"__ARC_UI_FIXTURE__", "fixture",
        "a display fixture replaces the real data load, so the journey proves nothing",
        "Load the record from the backend and assert the visible result instead."),
    (r"\b(?:page|context)\.(?:route|routeFromHAR|unroute|unrouteAll)\s*\(", "mock",
        "request interception fakes the backend response",
        "Drive the real backend; never fulfill or rewrite an API response."),
    (r"\baddInitScript\b", "injection",
        "an injected script can fabricate application state",
        "Create the state through the UI like a user does."),
    (r"\b(?:localStorage|sessionStorage|indexedDB)\b", "storage",
        "seeding browser storage bypasses the sign-in and data-loading flows",
        "Sign in and create data through the UI."),
    (r"\b(?:INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER)\s+(?:INTO|FROM|SET|TABLE)\b", "sql-write",
        "rows written with SQL are not user operations",
        "Create the record through the UI and read the database only for assertions."),
    (r"\b(?:createApp|registerApis)\s*\(", "handler",
        "calling the application directly replaces the user operation",
        "Reach the behavior through the browser."),
    (r"\bfetch\s*\(|\brequest\.(?:get|post|put|patch|delete)\s*\(", "network",
        "a raw request bypasses the UI the requirement describes",
        "Perform the operation through a control the user can see."),
    (r"\bpage\.evaluate\b|\bwindow\.__ARC", "evaluate",
        "evaluating page code can rewrite the state under test",
        "Act through the UI and assert what the user can see."),
    (r"\btest\.(?:skip|fixme|fail|only|slow)\b|\bexpect\.soft\b", "weakened",
        "a skipped, focused or soft assertion hides a real failure",
        "Keep every case running with hard assertions."),
    (r"\bwaitForTimeout\b", "sleep",
        "a fixed sleep hides a race instead of asserting observable state",
        "Wait for the visible result with a retrying assertion."),
    (r"\bbeforeAll\b|\bafterAll\b|\bdescribe\.serial\b", "order",
        "shared setup or serial execution makes the suite depend on execution order",
        "Create everything a case needs inside that case; never rely on another case."),
)

FILE_REQUIRED = (
    ("navigation", r"toHaveURL", "the journey must verify the URL it navigated to"),
    ("input", r"\.(?:fill|selectOption|check)\s*\(", "the journey must type real user input"),
    ("click", r"\.(?:click|press)\s*\(", "the journey must click real controls"),
    ("database", r"\bdatabase\.(?:stores|total|count|value|rows)\s*\(",
        "the journey must verify the stored side effect"),
)

SUITE_REQUIRED = (
    ("reload", r"\.reload\s*\(", "a reload must prove the record is stored, not held in memory"),
    ("absence", r"toHaveCount\(\s*0\s*\)", "the journey must prove the deleted record is gone"),
    ("no-write", r"\bdatabase\.total\s*\(", "a rejected submission must prove nothing was written"),
    ("independent-context", r"\bvisitor\b",
        "a second, independent browser context must observe the same server-side state"),
)

STEP_REQUIRED = {
    "find": (r"\brecord\s*\(", "the record must be found by its accessible name"),
    "open-detail": (r"record\([^)]*\)\.first\(\)\.click\s*\(",
        "the record must be opened by clicking its own name"),
    "independent": (r"expectIndependentlyVisible\s*\(",
        "a fresh browser context must observe the stored record"),
    "update": (r"toHaveValue\s*\(", "the modified value must be read back after a fresh load"),
    "gone": (r"toHaveCount\(\s*0\s*\)", "the deleted record must disappear from the list"),
    "delete": (r"\baction\s*\(", "the object must be deleted through a designed control"),
    # The account lifecycle steps. Each one is a capability, not a label: dropping the assertion
    # behind it would leave a case that navigates but proves nothing.
    "entry": (r"toHaveCount\(\s*0\s*\)",
        "the signed-out state must be proven by an absence assertion"),
    "register": (r"\.fill\s*\(", "the account must be created with real user input"),
    "identity": (r"getByText\s*\(", "the signed-in state must show the account identity"),
    "restore": (r"\.reload\s*\(", "a reload must prove the session is resolved by the server"),
    "isolated": (r"\bvisitor\b", "an independent browser context must stay signed out"),
}


def problem(path: str, rule: str, reason: str, fix: str = "") -> dict:
    """Describe one acceptance rule violation in a form the repair loop can act on."""
    return {"file": path, "rule": rule, "reason": reason, "fix": fix}



def audit_problems(path: str, source: str) -> list[dict]:
    """Reject the shortcuts that turn an acceptance test into a rehearsal."""
    problems = []
    for pattern, rule, reason, fix in FORBIDDEN:
        match = re.search(pattern, source)
        if match:
            problems.append(problem(path, rule,
                reason + " (found " + repr(match.group(0)) + ")", fix))
    for rule, pattern, reason in FILE_REQUIRED:
        if not re.search(pattern, source):
            problems.append(problem(path, rule, reason,
                "An acceptance spec types input, clicks controls, verifies the URL and the stored data."))
    return problems


def step_problems(path: str, source: str, steps) -> list[dict]:
    """Require the capabilities of every step the derived journey claims to cover."""
    problems = []
    for step in steps or ():
        required = STEP_REQUIRED.get(step)
        if required and not re.search(required[0], source):
            problems.append(problem(path, "step:" + step, required[1],
                "Keep the derived assertion for this step intact."))
    return problems


def suite_problems(source: str) -> list[dict]:
    """Require the capabilities the suite as a whole must demonstrate."""
    return [problem("", rule, reason, "Add the missing acceptance capability to the journey.")
        for rule, pattern, reason in SUITE_REQUIRED if not re.search(pattern, source)]


def acceptance_audit(output, plan: dict | None = None) -> dict:
    """Audit the rendered acceptance suite and the plan that produced it."""
    directory = output / SPEC_ROOT
    files = sorted(path for path in directory.rglob("*.spec.ts")
        if path.is_file() and not path.is_symlink()) if directory.is_dir() else []
    problems = []
    for path in files:
        problems.extend(audit_problems(path.relative_to(output).as_posix(), read_source(path)))
    for entry in (plan or {}).get("journeys") or ():
        relative = entry["spec"]
        if not (output / relative).is_file():
            problems.append(problem(relative, "missing", "the derived journey was not rendered",
                "Regenerate the acceptance suite."))
            continue
        problems.extend(step_problems(relative, read_source(output / relative), entry["steps"]))
    for message in (plan or {}).get("problems") or ():
        problems.append(problem("", "design", message,
            "Fix the UI design/implementation: the journey must be completable by a real user."))
    if files:
        problems.extend(suite_problems("\n".join(read_source(path) for path in files)))
    names = [path.relative_to(output).as_posix() for path in files]
    if not files and not problems:
        return {"returncode": 0, "problems": [], "files": names, "status": "NOT_APPLICABLE",
            "output": "No acceptance journey applies to this application, so nothing was audited."}
    if problems:
        lines = []
        for item in problems:
            where = (item["file"] + ": ") if item["file"] else ""
            lines.append("[" + item["rule"] + "] " + where + item["reason"]
                + ((" Fix: " + item["fix"]) if item.get("fix") else ""))
        return {"returncode": 1, "problems": problems, "files": names, "status": "REPAIR",
            "output": "Black-box acceptance audit found " + str(len(problems)) + " problem(s):\n"
                + "\n".join(lines)}
    return {"returncode": 0, "problems": [], "files": names, "status": "GREEN",
        "output": str(len(files)) + " black-box acceptance spec(s) satisfy every rule"}


def require_audit(result: dict) -> dict:
    """Accept the suite only when it is a black-box acceptance test rather than a rehearsal."""
    if result.get("problems"):
        raise ValueError(result.get("output") or "Black-box acceptance rules violated")
    return result

