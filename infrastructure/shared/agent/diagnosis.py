"""Classify one repair failure so the loop can target a cause and stop repeating it.

The classifier is deterministic and local: it reads the check report the stage already
produced and names the kind of defect behind it. Two consumers use the result:

* the repair loop compares consecutive signatures, so an error that repeats without any
  progress stops the loop instead of spending every remaining round on the same failure;
* a stage can report the category in ``.arc`` and mention the source files the failure
  actually names, so the next request supplies the smallest responsible file set.

Classification never decides acceptance: a category is evidence for repair, not a verdict,
and no category can turn a failed check into a passing one.
"""

import json
import re
from hashlib import sha256

from infrastructure.stages.database.acceptance import DATABASE_STAGE

CATEGORIES = ("typescript", "api_route", "api_contract", "database", "browser", "backend",
    "ui_locator", "assertion", "protocol", "unknown")

# Ordered from the most specific evidence to the most generic. The first matching rule
# wins, so a Playwright report that also mentions "Unknown API route" is a routing defect
# rather than a locator problem.
RULES = (
    ("protocol", (r"Return a JSON object", r"invalid edit", r"Choose an unused path",
        r"Return exactly one JSON object", r"not a valid JSON", r"Edit target")),
    ("typescript", (r"error TS\d+", r"\bTS\d{4}\b", r"TypeScript", r"tsc\b")),
    ("api_route", (r"Unknown API route", r"E2E_API_ROUTE_ERROR", r"no route", r"404 Not Found")),
    ("api_contract", (r"E2E_API_CONTRACT_ERROR", r"contract problem", r"is not a declared",
        r"contradicts the API contract", r"__ARC_API_VIOLATIONS__")),
    ("database", (r"SQLITE_", r"better-sqlite3", r"no such table", r"no such column",
        r"UNIQUE constraint", r"FOREIGN KEY constraint", r"database is locked")),
    ("browser", (r"browserType\.launch", r"Executable doesn't exist", r"playwright install",
        r"Target page, context or browser has been closed")),
    ("backend", (r"E2E_BACKEND_ERROR", r"E2E_NETWORK_ERROR", r"ECONNREFUSED", r"EADDRINUSE",
        r"status: 5\d\d", r"Internal Server Error")),
    ("ui_locator", (r"waiting for locator", r"strict mode violation", r"locator resolved to",
        r"element is not visible", r"getbyrole", r"getbylabel", r"getbytestid",
        r"no element", r"resolved to 0 elements")),
    ("assertion", (r"expect\(", r"tobe\t?[a-z]*\(", r"assertion", r"toBeVisible",
        r"toHaveText", r"toHaveValue", r"toHaveURL", r"toBeChecked")),
)
SOURCE = re.compile(r"\b(?:frontend|backend)/(?:src|tests|scripts)/[A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx|mjs|css|json|sql)\b")
#: Evidence that no edit in this pass can remove the failure: the cause is a published
#: artifact of an earlier pass, or the filesystem itself. A repair loop that retries such a
#: failure spends its whole round budget on changes that cannot take effect, so the first
#: match stops the loop and names the pass that owns the cause.
BLOCKERS = (
    ("database_structure", DATABASE_STAGE, (r"non-deterministic use of",
        r"\bin a CHECK constraint\b", r"\bin a generated column\b")),
    ("write_permission", None, (r"\bEACCES\b", r"\bEPERM\b", r"\bEROFS\b",
        r"read-only file system", r"attempt to write a readonly database",
        r"Permission denied", r"Access is denied", r"Operation not permitted")),
)
COMPILED_BLOCKERS = tuple((category, stage, tuple(re.compile(pattern, re.I) for pattern in patterns))
    for category, stage, patterns in BLOCKERS)
# Cached case-insensitive matchers: a report may capitalize an error type or a path.
COMPILED = tuple((name, tuple(re.compile(pattern, re.I) for pattern in patterns))
    for name, patterns in RULES)
VOLATILE = (
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\b", re.I), "<duration>"),
    (re.compile(r"\b\d+:\d+\b"), "<position>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<id>"),
    (re.compile(r"\b\d+\b"), "<n>"),
)


def text_of(value) -> str:
    """Flatten a feedback object or exception message into one searchable string."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def normalize(text: str) -> str:
    """Remove timings, positions and counts so the same defect keeps one signature."""
    for pattern, replacement in VOLATILE:
        text = pattern.sub(replacement, text)
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def signature(value) -> str:
    """Return a stable signature for one failure, ignoring incidental numbers."""
    lines = normalize(text_of(value)).splitlines()
    # The first lines carry the headline and the failing location; keep them stable.
    return sha256("\n".join(lines[:24]).encode("utf-8")).hexdigest()[:16]


def classify(value) -> dict:
    """Name the category, a stable signature and the files the failure names."""
    text = text_of(value)
    category = next((name for name, patterns in COMPILED
        if any(pattern.search(text) for pattern in patterns)), "unknown")
    reason = next((line.strip() for line in text.splitlines() if line.strip()), "")[:300]
    return {"category": category, "signature": signature(value),
        "files": sorted(set(SOURCE.findall(text.replace("\\", "/"))))[:8], "reason": reason}


def blocker(value) -> dict | None:
    """Name an upstream or filesystem cause that this pass cannot repair by editing files.

    ``classify`` says what kind of defect a failure is; this says whether the pass can fix
    it at all. Only evidence that the cause lies outside the writable files matches, so a
    loop that receives a blocker stops instead of repeating an edit that cannot help. The
    returned ``stage`` is the pass that owns the cause, or ``None`` when no pass does
    (a read-only filesystem fails again however often it is retried).
    """
    text = text_of(value)
    for category, stage, patterns in COMPILED_BLOCKERS:
        if any(pattern.search(text) for pattern in patterns):
            reason = next((line.strip() for line in text.splitlines() if line.strip()), "")[:300]
            return {"category": category, "stage": stage, "reason": reason,
                "evidence": text[:1500]}
    return None
