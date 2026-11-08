"""Normalize backend, browser and audit check results into one honest count record.

The passes report their final checks in three different shapes:

    node --test TAP          {"tests": 15, "pass": 15, "fail": 0, "skipped": 0, "todo": 0}
    Playwright JSON          {"passed": 6, "failed": 3, "skipped": 0, "flaky": 0, "errors": []}
    acceptance / audit       {"passed": 4, "status": "GREEN", "problems": ["..."]}

Counting rule: only a GREEN check may report its passing tests as success. When a check
fails, its failures and its skipped tests are kept and its passing tests are recorded as
``unverified``: they are evidence that something ran, not evidence that a required
behavior is accepted. A failed check with no test counts (typecheck, build, contract
audit) still counts as one failed check.
"""

from re import compile as compile_pattern

from infrastructure.reporting.status import GREEN, UNKNOWN

#: Bounded length of a reason copied into the final report; full text stays in the
#: stage report and in compiler.log, which the same entry points at.
REASON_LIMIT = 1200
COUNTS = ("total", "passed", "failed", "skipped", "flaky", "unverified")
#: Terminal color codes are meaningful in compiler.log but noise in a Markdown report.
ANSI = compile_pattern(r"\x1b\[[0-9;]*[A-Za-z]")


def count(value) -> int:
    """Read a count defensively: booleans, ``None``, strings and negatives are not counts."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def clip(text, limit: int = REASON_LIMIT) -> str:
    """Bound a multi-megabyte failure body while pointing at the full copy."""
    text = ANSI.sub("", str(text)).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... [truncated; the full text is in the stage report and compiler.log]"


def test_counts(result) -> dict:
    """Normalize any check result shape into total/passed/failed/skipped/flaky/unverified."""
    result = result if isinstance(result, dict) else {}
    if any(key in result for key in ("tests", "pass", "fail")):
        passed, failed = count(result.get("pass")), count(result.get("fail"))
        skipped = count(result.get("skipped")) + count(result.get("todo"))
        flaky = 0
        total = count(result.get("tests")) or passed + failed + skipped
    else:
        passed, failed = count(result.get("passed")), count(result.get("failed"))
        skipped, flaky = count(result.get("skipped")), count(result.get("flaky"))
        total = passed + failed + skipped + flaky
    total = max(total, passed + failed + skipped + flaky)
    return {"total": total, "passed": passed, "failed": failed, "skipped": skipped,
        "flaky": flaky, "unverified": 0}


def accepted_counts(entry) -> dict:
    """Keep only what a GREEN check proves; a failed check keeps failures, not passes."""
    entry = entry if isinstance(entry, dict) else {}
    counts = test_counts(entry.get("result"))
    if entry.get("status") == GREEN:
        return counts
    return {**counts, "passed": 0, "unverified": counts["passed"]}


def reason(entry, limit: int = REASON_LIMIT) -> str:
    """Extract the most specific explanation from a check, item or stage entry.

    Recorded failure lines (Playwright locators, TAP assertions, acceptance problems) are
    more specific than the summary error the check was raised with, so they come first.
    """
    entry = entry if isinstance(entry, dict) else {}
    result = entry.get("result")
    if isinstance(result, dict):
        for key in ("failures", "problems"):
            items = result.get(key)
            if isinstance(items, list) and items:
                return clip("\n".join(str(item) for item in items), limit)
    for key in ("error", "reason", "message"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return clip(value, limit)
    if isinstance(result, dict):
        for key in ("output", "error"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return clip(value, limit)
    return ""


def check_record(name: str, entry) -> dict:
    """Describe one final check: status, honest counts, raw counts and a bounded reason."""
    entry = entry if isinstance(entry, dict) else {}
    status = entry.get("status", UNKNOWN)
    return {"name": name, "status": status, "accepted": accepted_counts(entry),
        "tests": test_counts(entry.get("result")), "reason": reason(entry)}


def check_records(verification) -> dict:
    """Describe every final check of one pass, keeping only what failed or was verified."""
    if not isinstance(verification, dict):
        return {}
    return {name: check_record(name, entry) for name, entry in verification.items()
        if isinstance(entry, dict)}


def totals(entries) -> dict:
    """Sum normalized count records, separating verified passes from unverified ones."""
    total = {key: 0 for key in COUNTS}
    total["checks"] = 0
    for entry in entries:
        total["checks"] += 1
        for key in COUNTS:
            total[key] += count(entry.get(key))
    return total
